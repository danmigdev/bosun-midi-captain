Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$deployScript = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../deploy-captain.ps1'))
$testId = [Guid]::NewGuid().ToString('N')
$testRoot = Join-Path ([IO.Path]::GetTempPath()) "bosun-captain-script-test-$testId"
$firmware = Join-Path $testRoot 'firmware'
$captainLib = Join-Path $firmware 'lib/captain'
$pathComparison = if ([IO.Path]::DirectorySeparatorChar -eq '\') {
    [StringComparison]::OrdinalIgnoreCase
} else {
    [StringComparison]::Ordinal
}

function Write-Utf8File {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Content)
    [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Path))
    [IO.File]::WriteAllText($Path, $Content, [Text.UTF8Encoding]::new($false))
}

function Write-TestMpy {
    param([Parameter(Mandatory)][string]$Path)
    [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Path))
    [IO.File]::WriteAllBytes($Path, [byte[]]@(0x43, 0x06, 0x00, 0x1f, 0x42))
}

function Assert-Fails {
    param([Parameter(Mandatory)][scriptblock]$Action, [Parameter(Mandatory)][string]$ExpectedText)
    try {
        & $Action
    } catch {
        if ($_.Exception.Message -notlike "*$ExpectedText*") {
            throw "Expected failure containing '$ExpectedText', got: $($_.Exception.Message)"
        }
        return
    }
    throw "Expected action to fail with: $ExpectedText"
}

function Assert-Ordered {
    param(
        [Parameter(Mandatory)][string]$Text,
        [Parameter(Mandatory)][string[]]$Tokens
    )
    $cursor = 0
    foreach ($token in $Tokens) {
        $next = $Text.IndexOf($token, $cursor, [StringComparison]::Ordinal)
        if ($next -lt 0) {
            throw "Missing or out-of-order deploy operation: $token"
        }
        $cursor = $next + $token.Length
    }
}

function Get-PythonPath {
    foreach ($name in @('python.exe', 'python3.exe', 'python', 'python3')) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -ne $command) {
            return $command.Source
        }
    }
    throw 'Python 3 is required for the deploy runtime-verifier regression tests'
}

function Assert-RuntimeVerifierCase {
    param(
        [Parameter(Mandatory)][string]$Python,
        [Parameter(Mandatory)][string]$ValidatorPath,
        [Parameter(Mandatory)][string]$ServerPath,
        [Parameter(Mandatory)][string]$Mode,
        [Parameter(Mandatory)][bool]$ShouldPass,
        [Parameter(Mandatory)][string]$ExpectedText,
        [Parameter(Mandatory)][string[]]$ExpectedRequests,
        [string]$RepeatedFinalRequest = '',
        [double]$MaximumSeconds = 4.0
    )

    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $Python
    $startInfo.Arguments = '"' + $ServerPath + '" ' + $Mode
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $server = [Diagnostics.Process]::Start($startInfo)
    if ($null -eq $server) {
        throw "Could not start fake runtime server for mode $Mode"
    }

    try {
        $portLine = $server.StandardOutput.ReadLine()
        if ($portLine -notmatch '^PORT=([0-9]+)$') {
            throw "Fake runtime server did not report a port: $portLine"
        }
        $port = [int]$Matches[1]
        $stopwatch = [Diagnostics.Stopwatch]::StartNew()
        $oldPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $output = @(& $Python $ValidatorPath "socket://127.0.0.1:$port" '1.5' 2>&1)
            $exitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $oldPreference
            $stopwatch.Stop()
        }
        $renderedOutput = ($output | ForEach-Object { $_.ToString() }) -join "`n"

        if (-not $server.WaitForExit(3000)) {
            $server.Kill()
            [void]$server.WaitForExit(3000)
            throw "Fake runtime server did not stop for mode $Mode"
        }
        $serverTail = $server.StandardOutput.ReadToEnd()
        $serverError = $server.StandardError.ReadToEnd()
        if ($server.ExitCode -ne 0) {
            throw "Fake runtime server failed for mode ${Mode}: $serverError"
        }

        if ($ShouldPass) {
            if ($exitCode -ne 0) {
                throw "Runtime verifier unexpectedly failed for mode ${Mode}: $renderedOutput"
            }
        } elseif ($exitCode -eq 0) {
            throw "Runtime verifier unexpectedly accepted failure mode $Mode"
        }
        if ($renderedOutput.IndexOf($ExpectedText, [StringComparison]::Ordinal) -lt 0) {
            throw "Runtime verifier mode $Mode did not report '$ExpectedText': $renderedOutput"
        }
        if ($stopwatch.Elapsed.TotalSeconds -gt $MaximumSeconds) {
            throw "Runtime verifier exceeded its bounded deadline in mode ${Mode}: $($stopwatch.Elapsed)"
        }

        $requestLine = @($serverTail -split "`r?`n" |
            Where-Object { $_ -like 'REQUESTS=*' } | Select-Object -First 1)
        if ($requestLine.Count -ne 1) {
            throw "Fake runtime server did not report requests for mode ${Mode}: $serverTail"
        }
        $actualRequests = $requestLine[0].Substring('REQUESTS='.Length).Split(',')
        if ($RepeatedFinalRequest) {
            if ($actualRequests.Count -le $ExpectedRequests.Count -or
                $actualRequests.Count -gt 17) {
                throw "Unbounded or missing busy retries for mode ${Mode}: $($actualRequests.Count)"
            }
            foreach ($request in $actualRequests[$ExpectedRequests.Count..($actualRequests.Count - 1)]) {
                if ($request -ne $RepeatedFinalRequest) {
                    throw "Busy retry sent another request in mode ${Mode}: $request"
                }
            }
            $actualRequests = $actualRequests[0..($ExpectedRequests.Count - 1)]
        }
        if (($actualRequests -join ',') -ne ($ExpectedRequests -join ',')) {
            throw "Unexpected runtime requests for mode ${Mode}: $($actualRequests -join ',')"
        }
    } finally {
        if (-not $server.HasExited) {
            $server.Kill()
            [void]$server.WaitForExit(3000)
        }
        $server.Dispose()
    }
}

try {
    Write-Utf8File -Path (Join-Path $firmware 'code.py') -Content "print('test')`n"
    Write-Utf8File -Path (Join-Path $captainLib 'protocol.py') -Content "VALUE = 1`n"
    Write-Utf8File -Path (Join-Path $captainLib 'manifest_dynamic.py') -Content "VALUE = 2`n"
    Write-TestMpy -Path (Join-Path $captainLib 'protocol.mpy')
    Write-TestMpy -Path (Join-Path $captainLib 'manifest_dynamic.mpy')
    Write-TestMpy -Path (Join-Path $captainLib 'app.mpy')
    Write-TestMpy -Path (Join-Path $captainLib 'store.mpy')

    # An unreachable host proves that DryRun performs no SSH lookup or call.
    $output = @(& $deployScript -DryRun -HostName 'unreachable.invalid' `
        -FirmwarePath $firmware `
        -Files 'lib/captain/protocol.mpy,lib/captain/manifest_dynamic.mpy' *>&1)
    $rendered = ($output | ForEach-Object { $_.ToString() }) -join "`n"
    if ($rendered -notmatch 'DRY-RUN: no SSH connection' -or
        $rendered -notmatch 'Files: lib/captain/protocol\.mpy, lib/captain/manifest_dynamic\.mpy') {
        throw 'Dry-run did not render the validated partial-deploy selection'
    }
    if ($rendered -notmatch 'Remote staging: /tmp/bosun-captain-[a-f0-9]{32}') {
        throw 'Dry-run staging directory is not uniquely and safely named'
    }

    # Reproduce the unsafe real invocation: app was listed before its new
    # store dependency. Both the preview and push arguments must move app
    # after dependencies and code.py after the whole library set.
    $orderOutput = @(& $deployScript -DryRun -HostName 'unreachable.invalid' `
        -FirmwarePath $firmware `
        -Files 'lib/captain/app.mpy,lib/captain/store.mpy,code.py' *>&1)
    $orderRendered = ($orderOutput | ForEach-Object { $_.ToString() }) -join "`n"
    if ($orderRendered -notmatch
            'Files: lib/captain/store\.mpy, lib/captain/app\.mpy, code\.py' -or
        $orderRendered -notmatch
            '--files ''lib/captain/store\.mpy'' ''lib/captain/app\.mpy'' ''code\.py''') {
        throw 'Partial deploy did not move app/code after their dependencies'
    }

    $transactionStart = $rendered.IndexOf('--- TRANSACTION ---', [StringComparison]::Ordinal)
    $recoveryStart = $rendered.IndexOf('--- LOCAL FINALLY RECOVERY ---', [StringComparison]::Ordinal)
    if ($transactionStart -lt 0 -or $recoveryStart -le $transactionStart) {
        throw 'Dry-run is missing its transaction/recovery boundaries'
    }
    $transaction = $rendered.Substring($transactionStart, $recoveryStart - $transactionStart)
    $recovery = $rendered.Substring($recoveryStart)
    if ($rendered -match '\$[0-9]+') {
        throw "Rendered remote shell contains an accidental positional variable: $($Matches[0])"
    }
    if ($transaction.IndexOf('PORT_WAIT_SECONDS=30', [StringComparison]::Ordinal) -lt 0 -or
        $transaction.IndexOf('$PORT_WAIT_SECONDS', [StringComparison]::Ordinal) -lt 0) {
        throw 'Port timeout was not rendered as a safe named shell variable'
    }

    Assert-Ordered -Text $transaction -Tokens @(
        'trap finish EXIT HUP INT TERM',
        'as_root systemctl stop "$SERVICE"',
        'if wait_for_captain_ping; then',
        'BOSUN_CAPTAIN_PREPARE=healthy_no_reset',
        'normal_console_reset',
        'BOSUN_CAPTAIN_PREPARE=normal_console_reset',
        'as_root python3 "$root/push_firmware.py"',
        'wait_for_captain_ports',
        'wait_for_captain_ping',
        'as_root systemctl start "$SERVICE"',
        'as_root systemctl is-active --quiet "$SERVICE"',
        'verify_captain_runtime'
    )
    foreach ($required in @(
        'EXPECTED_VENDOR=239a',
        'EXPECTED_PRODUCT=80f4',
        'expected exactly one Captain',
        'usb_parent_for_tty',
        'CONSOLE_TTY=/dev/ttyACM0',
        'DATA_TTY=/dev/ttyACM1',
        'microcontroller.on_next_reset(microcontroller.RunMode.NORMAL)',
        'BOSUN_CAPTAIN_SAFE_MODE=',
        'report_captain_diagnostics',
        'BOSUN_HUB_SERVICE=active_but_captain_missing',
        '--files ''lib/captain/protocol.mpy'' ''lib/captain/manifest_dynamic.mpy''',
        'BOSUN_CAPTAIN_PING=ACK',
        'RUNTIME_URL=socket://127.0.0.1:9876',
        'One global deadline bounds connection readiness and all five requests',
        '"id": "deploy-runtime-ping-%d" % ping_attempt',
        '"type": "GET_DEVICE_INFO"',
        '"type": "GET_CONTEXT"',
        '"type": "GET_PATCH"',
        '"type": "LIST_PATCHES"',
        '"bank": bank, "slot": slot',
        'BOSUN_CAPTAIN_RUNTIME=OK',
        'Captain runtime verification failed:',
        'systemctl restart "$SERVICE"',
        'systemctl is-active --quiet "$SERVICE"'
    )) {
        if ($transaction.IndexOf($required, [StringComparison]::Ordinal) -lt 0) {
            throw "Transaction is missing safety invariant: $required"
        }
    }
    if ($recovery -notmatch 'systemctl restart|systemctl is-active' -or
        $recovery -notmatch 'require_captain_ports' -or
        $recovery -notmatch 'verify_captain_runtime' -or
        $recovery -notmatch 'BOSUN_HUB_RECOVERY=OK captain=present') {
        throw 'Independent finally recovery does not verify the service and Captain runtime'
    }
    if ($transaction.IndexOf('if ! systemctl is-active --quiet "$SERVICE"; then',
            [StringComparison]::Ordinal) -lt 0) {
        throw 'Remote EXIT trap does not restore a stopped hub on verification failure'
    }
    if ($transaction.IndexOf('command -v usbreset', [StringComparison]::OrdinalIgnoreCase) -ge 0 -or
        $transaction -match '(?m)^\s*as_root\s+[^\r\n]*usbreset' -or
        $transaction.IndexOf('/dev/bus/usb', [StringComparison]::Ordinal) -ge 0) {
        throw 'Captain deploy must never issue a bus-level USB reset'
    }

    # Execute the exact healthy/fallback preparation branch with shell mocks.
    # A healthy data PING must not touch the console reset path; one failed PING
    # may use exactly one NORMAL reset and must revalidate ports and PING.
    $prepareMatch = [regex]::Match(
        $transaction,
        '(?ms)^if wait_for_captain_ping; then\r?\n(?<branch>.*?)^fi$'
    )
    if (-not $prepareMatch.Success) {
        throw 'Could not extract the Captain preparation branch'
    }
    $prepareBlock = "if wait_for_captain_ping; then`n" +
        $prepareMatch.Groups['branch'].Value + "fi`n"
    $bash = Get-Command bash -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $bash) {
        foreach ($mode in @('healthy', 'fallback')) {
            $probe = @'
set -eu
probe_mode=__MODE__
ping_calls=0
wait_for_captain_ping() {
    ping_calls=$((ping_calls + 1))
    if [ "$probe_mode" = healthy ]; then return 0; fi
    [ "$ping_calls" -gt 1 ]
}
normal_console_reset() { printf 'MOCK_NORMAL_RESET\n'; }
wait_for_captain_ports() { printf 'MOCK_PORTS_OK\n'; }
sleep() { :; }
__PREPARE__
printf 'MOCK_PING_CALLS=%s\n' "$ping_calls"
'@.Replace('__MODE__', $mode).Replace('__PREPARE__', $prepareBlock)
            $encoded = [Convert]::ToBase64String(
                [Text.UTF8Encoding]::new($false).GetBytes($probe)
            )
            $savedPreference = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            try {
                $probeOutput = @(& $bash.Source '-lc' "printf %s $encoded | base64 -d | bash" 2>&1)
                $probeExit = $LASTEXITCODE
            } finally {
                $ErrorActionPreference = $savedPreference
            }
            $probeRendered = ($probeOutput | ForEach-Object { $_.ToString() }) -join "`n"
            if ($probeExit -ne 0) {
                throw "Captain prepare mock '$mode' failed: $probeRendered"
            }
            if ($mode -eq 'healthy') {
                if ($probeRendered -notlike '*BOSUN_CAPTAIN_PREPARE=healthy_no_reset*' -or
                    $probeRendered -like '*MOCK_NORMAL_RESET*' -or
                    $probeRendered -notlike '*MOCK_PING_CALLS=1*') {
                    throw "Healthy Captain preparation performed a reset: $probeRendered"
                }
            } elseif ($probeRendered -notlike '*MOCK_NORMAL_RESET*' -or
                $probeRendered -notlike '*MOCK_PORTS_OK*' -or
                $probeRendered -notlike '*BOSUN_CAPTAIN_PREPARE=normal_console_reset*' -or
                $probeRendered -notlike '*MOCK_PING_CALLS=2*') {
                throw "Fallback Captain preparation was incomplete: $probeRendered"
            }
        }
    }

    Assert-Fails -ExpectedText 'normalized relative path' -Action {
        & $deployScript -DryRun -FirmwarePath $firmware -Files '../code.py' | Out-Null
    }
    Assert-Fails -ExpectedText 'Duplicate selected firmware file' -Action {
        & $deployScript -DryRun -FirmwarePath $firmware `
            -Files 'lib/captain/protocol.mpy,lib/captain/protocol.mpy' | Out-Null
    }

    $protocolSource = Get-Item -LiteralPath (Join-Path $captainLib 'protocol.py')
    $protocolMpy = Get-Item -LiteralPath (Join-Path $captainLib 'protocol.mpy')
    $protocolSource.LastWriteTimeUtc = [DateTime]::UtcNow
    $protocolMpy.LastWriteTimeUtc = [DateTime]::UtcNow.AddMinutes(-2)
    Assert-Fails -ExpectedText 'older than its source' -Action {
        & $deployScript -DryRun -FirmwarePath $firmware | Out-Null
    }
    $protocolMpy.LastWriteTimeUtc = [DateTime]::UtcNow.AddMinutes(1)

    [IO.File]::WriteAllBytes($protocolMpy.FullName, [byte[]]@(0x4d, 0x06, 0x00, 0x1f))
    Assert-Fails -ExpectedText 'Invalid CircuitPython mpy-v6 artifact' -Action {
        & $deployScript -DryRun -FirmwarePath $firmware | Out-Null
    }

    $tokens = $null
    $parseErrors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile(
        $deployScript, [ref]$tokens, [ref]$parseErrors
    )
    if ($parseErrors.Count -ne 0) {
        throw "Deploy helper has PowerShell parse errors: $($parseErrors -join '; ')"
    }
    $finallyBlocks = @($ast.FindAll(
        {
            param($node)
            $node -is [Management.Automation.Language.TryStatementAst] -and
                $null -ne $node.Finally
        },
        $true
    ))
    if ($finallyBlocks.Count -eq 0) {
        throw 'Deploy helper has no local PowerShell finally recovery boundary'
    }
    $deploySource = Get-Content -LiteralPath $deployScript -Raw
    if ($deploySource.IndexOf('throw $primaryError', [StringComparison]::Ordinal) -ge 0 -or
        $deploySource.IndexOf('throw "Captain deployment failed: $primaryDiagnostic"',
            [StringComparison]::Ordinal) -lt 0) {
        throw 'Deploy helper does not preserve the primary native failure diagnostic'
    }

    # Execute the real wrapper with a native child process. Windows
    # PowerShell 5.1 turns redirected stderr into ErrorRecords; a warning
    # must neither abort a successful tool nor hide a failed tool's final
    # diagnostic and actual exit code.
    $nativeWrapper = $ast.Find({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq 'Invoke-CheckedNative'
    }, $true)
    if ($null -eq $nativeWrapper) {
        throw 'Could not extract the native-command wrapper'
    }
    . ([scriptblock]::Create($nativeWrapper.Extent.Text))
    $nativeProbe = Join-Path $testRoot 'native_stderr_probe.py'
    Write-Utf8File -Path $nativeProbe -Content @'
import sys
import time

print("WARN: first native diagnostic", file=sys.stderr, flush=True)
time.sleep(0.05)
print("completed stdout after warning", flush=True)
print("FINAL: complete native diagnostic", file=sys.stderr, flush=True)
sys.exit(int(sys.argv[1]))
'@
    $nativePython = Get-PythonPath
    & {
        $ErrorActionPreference = 'Stop'
        # Dot invocation makes preference restoration observable in this
        # scope, instead of merely relying on a function's scope disappearing.
        $captured = @(. Invoke-CheckedNative -Executable $nativePython `
            -Arguments @($nativeProbe, '0') -Quiet -Capture)
        $capturedText = $captured -join "`n"
        foreach ($expected in @('WARN: first native diagnostic',
                'completed stdout after warning', 'FINAL: complete native diagnostic')) {
            if ($capturedText.IndexOf($expected, [StringComparison]::Ordinal) -lt 0) {
                throw "Successful native tool lost diagnostic '$expected': $capturedText"
            }
        }
        if ($ErrorActionPreference -ne 'Stop' -or $LASTEXITCODE -ne 0) {
            throw 'Successful native wrapper did not restore Stop or preserve exit code 0'
        }

        $diagnostic = $null
        try {
            . Invoke-CheckedNative -Executable $nativePython `
                -Arguments @($nativeProbe, '7') -Quiet -Capture | Out-Null
        } catch {
            $diagnostic = $_.Exception.Message
        }
        if ([string]::IsNullOrWhiteSpace($diagnostic)) {
            throw 'Native wrapper accepted exit code 7'
        }
        foreach ($expected in @('exit code 7', 'WARN: first native diagnostic',
                'completed stdout after warning', 'FINAL: complete native diagnostic')) {
            if ($diagnostic.IndexOf($expected, [StringComparison]::Ordinal) -lt 0) {
                throw "Failed native tool lost diagnostic '$expected': $diagnostic"
            }
        }
        if ($ErrorActionPreference -ne 'Stop' -or $LASTEXITCODE -ne 7) {
            throw 'Failed native wrapper did not restore Stop or preserve exit code 7'
        }
    }

    # Execute the exact Python verifier embedded in the remote transaction
    # against a deterministic TCP bridge. This catches the former PING-only
    # false positive and proves protocol ERROR/invalid-data paths fail closed.
    $runtimeMatch = [Text.RegularExpressions.Regex]::Match(
        $transaction,
        '(?ms)^verify_captain_runtime\(\) \{.*?<<''PY''\r?\n(?<code>.*?)\r?\nPY\r?\n\}',
        [Text.RegularExpressions.RegexOptions]::Multiline -bor
            [Text.RegularExpressions.RegexOptions]::Singleline
    )
    if (-not $runtimeMatch.Success) {
        throw 'Could not extract the embedded Captain runtime verifier'
    }
    $runtimeValidator = Join-Path $testRoot 'runtime_validator.py'
    Write-Utf8File -Path $runtimeValidator -Content $runtimeMatch.Groups['code'].Value

    $fakeServer = Join-Path $testRoot 'fake_runtime_server.py'
    Write-Utf8File -Path $fakeServer -Content @'
import json
import socket
import sys

mode = sys.argv[1]
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", 0))
server.listen(1)
print("PORT=%d" % server.getsockname()[1], flush=True)
requests = []
request_ids = set()
first_ids = {}
connection, _ = server.accept()
connection.settimeout(3.0)
buffer = bytearray()

def send(message):
    wire = (json.dumps(message, separators=(",", ":")) + "\n").encode()
    # Force split framing. The first reply also carries noise and an
    # unsolicited message so correlation is exercised, not merely ordering.
    if len(requests) == 1:
        connection.sendall(b"not-json\n")
    connection.sendall(wire[:3])
    connection.sendall(wire[3:])

try:
    while True:
        try:
            chunk = connection.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        buffer.extend(chunk)
        while b"\n" in buffer:
            raw, _, tail = bytes(buffer).partition(b"\n")
            buffer[:] = tail
            if not raw.strip():
                continue
            message = json.loads(raw)
            kind = message["type"]
            requests.append(kind)
            ident = message["id"]
            if ident in request_ids:
                raise AssertionError("runtime retry reused a request id: " + ident)
            request_ids.add(ident)
            if kind != "PING" and mode in ("busy_reads", "busy_forever", "busy_then_error"):
                first_ids.setdefault(kind, ident)
                if requests.count(kind) == 1 or mode == "busy_forever":
                    send({"type": "ERROR", "id": ident,
                          "error": "background_busy", "of": kind})
                    continue
                if mode == "busy_then_error":
                    send({"type": "ERROR", "id": ident,
                          "error": "exception", "detail": "MemoryError"})
                    continue
                # A late malformed reply for the refused attempt must not be
                # accepted as the successful retry, especially for PATCH's
                # coordinates and the DEVICE_INFO navigation metadata.
                send({"type": "ERROR", "id": first_ids[kind],
                      "error": "exception", "detail": "stale response"})
            if kind == "PING":
                if mode == "transient_link" and requests.count("PING") == 1:
                    send({"type": "ERROR", "id": ident,
                          "error": "link_down", "of": "PING"})
                    continue
                connection.sendall(b'{"type":"CONTEXT","context":{"push":true}}\n')
                send({"type": "ACK", "id": ident})
            elif mode == "ping_only":
                continue
            elif kind == "GET_DEVICE_INFO":
                send({"type": "DEVICE_INFO", "id": ident,
                      "current": {"bank": 2, "slot": 3},
                      "preset_navigation": {}})
            elif kind == "GET_CONTEXT":
                send({"type": "CONTEXT", "id": ident,
                      "context": {"kemper_block_X": "on"}})
            elif kind == "GET_PATCH":
                if mode == "patch_error":
                    send({"type": "ERROR", "id": ident,
                          "error": "exception", "detail": "MemoryError"})
                    break
                send({"type": "PATCH", "id": ident, "bank": 2, "slot": 3,
                      "patch": {"name": "Clean", "bindings": []}})
            elif kind == "LIST_PATCHES":
                patches = ([{"bank": 1, "slot": 1, "name": "Wrong"}]
                           if mode == "missing_current" else
                           [{"bank": 2, "slot": 3, "name": "Clean"}])
                send({"type": "PATCH_LIST", "id": ident, "patches": patches})
                break
finally:
    connection.close()
    server.close()
    print("REQUESTS=" + ",".join(requests), flush=True)
'@

    $python = Get-PythonPath

    # Execute the exact console diagnostic against a mocked serial port. A
    # missing ACM1 must surface supervisor.runtime.safe_mode_reason instead of
    # being reduced to a generic port timeout.
    $diagnosticMatch = [regex]::Match(
        $transaction,
        '(?ms)^report_captain_diagnostics\(\).*?as_root python3 - "\$CONSOLE_TTY" <<''PY'' >&2 \|\| true\r?\n(?<code>.*?)\r?\nPY'
    )
    if (-not $diagnosticMatch.Success) {
        throw 'Could not extract the Captain safe-mode diagnostic'
    }
    $diagnosticScript = Join-Path $testRoot 'safe_mode_diagnostic.py'
    $fakeSerialRoot = Join-Path $testRoot 'fake-serial'
    Write-Utf8File -Path $diagnosticScript -Content $diagnosticMatch.Groups['code'].Value
    Write-Utf8File -Path (Join-Path $fakeSerialRoot 'serial.py') -Content @'
class Serial:
    def __init__(self, *_args, **_kwargs):
        self._reads = []

    def reset_input_buffer(self):
        self._reads = []

    def write(self, payload):
        if b"SAFE_MODE" in payload:
            self._reads = [
                b'echoed command\r\n',
                b'BOSUN_CAPTAIN_SAFE_MODE=HARD_FAULT\r\n>>> ',
            ]
        else:
            self._reads = [b'>>> ', b'']
        return len(payload)

    def flush(self):
        pass

    def read(self, _size):
        return self._reads.pop(0) if self._reads else b''

    def close(self):
        pass
'@
    $savedPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = $fakeSerialRoot
    $savedPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $diagnosticOutput = @(& $python $diagnosticScript 'mock-console' 2>&1)
        $diagnosticExit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $savedPreference
        $env:PYTHONPATH = $savedPythonPath
    }
    $diagnosticRendered = ($diagnosticOutput | ForEach-Object { $_.ToString() }) -join "`n"
    if ($diagnosticExit -ne 0 -or
        $diagnosticRendered -notlike '*BOSUN_CAPTAIN_SAFE_MODE=HARD_FAULT*') {
        throw "Safe-mode diagnostic did not surface HARD_FAULT: $diagnosticRendered"
    }

    Assert-RuntimeVerifierCase -Python $python -ValidatorPath $runtimeValidator `
        -ServerPath $fakeServer -Mode 'success' -ShouldPass $true `
        -ExpectedText 'BOSUN_CAPTAIN_RUNTIME=OK bank=2 slot=3 patches=1' `
        -ExpectedRequests @('PING', 'GET_DEVICE_INFO', 'GET_CONTEXT', 'GET_PATCH', 'LIST_PATCHES')
    Assert-RuntimeVerifierCase -Python $python -ValidatorPath $runtimeValidator `
        -ServerPath $fakeServer -Mode 'transient_link' -ShouldPass $true `
        -ExpectedText 'BOSUN_CAPTAIN_RUNTIME=OK bank=2 slot=3 patches=1' `
        -ExpectedRequests @('PING', 'PING', 'GET_DEVICE_INFO', 'GET_CONTEXT', 'GET_PATCH', 'LIST_PATCHES')
    Assert-RuntimeVerifierCase -Python $python -ValidatorPath $runtimeValidator `
        -ServerPath $fakeServer -Mode 'busy_reads' -ShouldPass $true `
        -ExpectedText 'BOSUN_CAPTAIN_RUNTIME=OK bank=2 slot=3 patches=1' `
        -ExpectedRequests @('PING', 'GET_DEVICE_INFO', 'GET_DEVICE_INFO', `
            'GET_CONTEXT', 'GET_CONTEXT', 'GET_PATCH', 'GET_PATCH', 'LIST_PATCHES', 'LIST_PATCHES')
    Assert-RuntimeVerifierCase -Python $python -ValidatorPath $runtimeValidator `
        -ServerPath $fakeServer -Mode 'busy_forever' -ShouldPass $false `
        -ExpectedText 'background_busy persisted for GET_DEVICE_INFO' `
        -ExpectedRequests @('PING', 'GET_DEVICE_INFO') `
        -RepeatedFinalRequest 'GET_DEVICE_INFO' -MaximumSeconds 2.5
    Assert-RuntimeVerifierCase -Python $python -ValidatorPath $runtimeValidator `
        -ServerPath $fakeServer -Mode 'busy_then_error' -ShouldPass $false `
        -ExpectedText 'MemoryError' `
        -ExpectedRequests @('PING', 'GET_DEVICE_INFO', 'GET_DEVICE_INFO')
    Assert-RuntimeVerifierCase -Python $python -ValidatorPath $runtimeValidator `
        -ServerPath $fakeServer -Mode 'ping_only' -ShouldPass $false `
        -ExpectedText 'no response to GET_DEVICE_INFO#deploy-runtime-device' `
        -ExpectedRequests @('PING', 'GET_DEVICE_INFO')
    Assert-RuntimeVerifierCase -Python $python -ValidatorPath $runtimeValidator `
        -ServerPath $fakeServer -Mode 'patch_error' -ShouldPass $false `
        -ExpectedText 'GET_PATCH returned ERROR' `
        -ExpectedRequests @('PING', 'GET_DEVICE_INFO', 'GET_CONTEXT', 'GET_PATCH')
    Assert-RuntimeVerifierCase -Python $python -ValidatorPath $runtimeValidator `
        -ServerPath $fakeServer -Mode 'missing_current' -ShouldPass $false `
        -ExpectedText 'LIST_PATCHES omits the current patch' `
        -ExpectedRequests @('PING', 'GET_DEVICE_INFO', 'GET_CONTEXT', 'GET_PATCH', 'LIST_PATCHES')

    Write-Host 'PASS: deploy-captain.ps1 runtime bootstrap, bounded failures and recovery guards'
} finally {
    $tempParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([char[]]@('\', '/')) + [IO.Path]::DirectorySeparatorChar
    $candidate = [IO.Path]::GetFullPath($testRoot)
    if ($candidate.StartsWith($tempParent, $pathComparison) -and
        [IO.Path]::GetFileName($candidate) -eq "bosun-captain-script-test-$testId") {
        Remove-Item -LiteralPath $candidate -Recurse -Force -ErrorAction SilentlyContinue
    }
}
# Expected failures above leave a native exit code behind. Report success only
# after every assertion and cleanup completed; uncaught failures still terminate.
exit 0
