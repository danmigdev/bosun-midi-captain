Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$deployScript = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../deploy-hub.ps1'))
$testId = [Guid]::NewGuid().ToString('N')
$testRoot = Join-Path ([IO.Path]::GetTempPath()) "bosun-hub-script-test-$testId"
$validSource = Join-Path $testRoot 'valid/bosun_hub'
$missingSource = Join-Path $testRoot 'missing/bosun_hub'
$unsafeSource = Join-Path $testRoot 'unsafe/bosun_hub'
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

function New-MinimalHubPackage {
    param([Parameter(Mandatory)][string]$Path)

    foreach ($name in @('__init__.py', '__main__.py', 'hub.py', 'link.py', 'midi_connect.py', 'server.py')) {
        Write-Utf8File -Path (Join-Path $Path $name) -Content "VALUE = '$name'`n"
    }
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
    throw 'Python 3 is required for the hub deploy protocol-mock tests'
}

function Assert-PingVerifierCase {
    param(
        [Parameter(Mandatory)][string]$Python,
        [Parameter(Mandatory)][string]$ValidatorPath,
        [Parameter(Mandatory)][string]$ServerPath,
        [Parameter(Mandatory)][string]$Mode,
        [Parameter(Mandatory)][bool]$ShouldPass,
        [Parameter(Mandatory)][string]$ExpectedText
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
        throw "Could not start fake hub server for mode $Mode"
    }

    try {
        $portLine = $server.StandardOutput.ReadLine()
        if ($portLine -notmatch '^PORT=([0-9]+)$') {
            throw "Fake hub server did not report a port: $portLine"
        }
        $port = [int]$Matches[1]
        $stopwatch = [Diagnostics.Stopwatch]::StartNew()
        $savedPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $output = @(& $Python $ValidatorPath $port '1.3' ('a' * 32) 2>&1)
            $exitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $savedPreference
            $stopwatch.Stop()
        }
        $rendered = ($output | ForEach-Object { $_.ToString() }) -join "`n"

        if (-not $server.WaitForExit(4000)) {
            $server.Kill()
            [void]$server.WaitForExit(3000)
            throw "Fake hub server did not stop for mode $Mode"
        }
        $serverTail = $server.StandardOutput.ReadToEnd()
        $serverError = $server.StandardError.ReadToEnd()
        if ($server.ExitCode -ne 0) {
            throw "Fake hub server failed for mode ${Mode}: $serverError"
        }
        if ($ShouldPass -and $exitCode -ne 0) {
            throw "PING verifier unexpectedly failed for mode ${Mode}: $rendered"
        }
        if (-not $ShouldPass -and $exitCode -eq 0) {
            throw "PING verifier unexpectedly accepted mode $Mode"
        }
        if ($rendered.IndexOf($ExpectedText, [StringComparison]::Ordinal) -lt 0) {
            throw "PING verifier mode $Mode did not report '$ExpectedText': $rendered"
        }
        if ($stopwatch.Elapsed.TotalSeconds -gt 3.5) {
            throw "PING verifier exceeded its global deadline in mode ${Mode}: $($stopwatch.Elapsed)"
        }
        if ($serverTail -notmatch 'REQUESTS=([1-9][0-9]*)') {
            throw "Fake hub server did not observe a PING in mode ${Mode}: $serverTail"
        }
        $requestCount = [int]$Matches[1]
        if ($Mode -eq 'transient_link' -and $requestCount -lt 2) {
            throw 'PING verifier did not retry the explicit transient link_down state'
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
    New-MinimalHubPackage -Path $validSource

    # An unreachable hostname proves DryRun performs neither SSH discovery nor
    # a remote call. Tests may only be omitted through the explicit switch.
    $dryOutput = @(& $deployScript -DryRun -SkipTests `
        -HostName 'unreachable.invalid' -SourcePath $validSource *>&1)
    $dryRendered = ($dryOutput | ForEach-Object { $_.ToString() }) -join "`n"
    foreach ($expected in @(
        'WARNING: local hub tests explicitly skipped.',
        'DRY-RUN: no SSH connection or remote change was made.',
        'Health gate: bosun-hub.service active, stable NRestarts/MainPID',
        'Preserved siblings: /opt/bosun-hub/stage and /opt/bosun-hub/config'
    )) {
        if ($dryRendered.IndexOf($expected, [StringComparison]::Ordinal) -lt 0) {
            throw "Dry-run output is missing: $expected"
        }
    }
    if ($dryRendered -notmatch 'Remote staging: /tmp/bosun-hub-deploy-[a-f0-9]{32}' -or
        $dryRendered -notmatch 'Manifest SHA-256: [a-f0-9]{64}') {
        throw 'Dry-run did not report a unique staging path and manifest digest'
    }

    Assert-Fails -ExpectedText 'Unsafe or unsupported SSH host name' -Action {
        & $deployScript -DryRun -SkipTests -HostName '-oProxyCommand=bad' `
            -SourcePath $validSource | Out-Null
    }

    New-MinimalHubPackage -Path $missingSource
    Remove-Item -LiteralPath (Join-Path $missingSource 'server.py') -Force
    Assert-Fails -ExpectedText 'missing required module: server.py' -Action {
        & $deployScript -DryRun -SkipTests -SourcePath $missingSource | Out-Null
    }

    New-MinimalHubPackage -Path $unsafeSource
    Write-Utf8File -Path (Join-Path $unsafeSource 'bad-name.py') -Content "VALUE = 1`n"
    Assert-Fails -ExpectedText 'unsafe deployment name: bad-name.py' -Action {
        & $deployScript -DryRun -SkipTests -SourcePath $unsafeSource | Out-Null
    }

    $tokens = $null
    $parseErrors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile(
        $deployScript, [ref]$tokens, [ref]$parseErrors
    )
    if ($parseErrors.Count -ne 0) {
        throw "Deploy helper has PowerShell parse errors: $($parseErrors -join '; ')"
    }
    $source = [IO.File]::ReadAllText($deployScript)

    foreach ($forbidden in @(
        'apt-get',
        'install.sh',
        'deploy-stage.ps1',
        'deploy-captain.ps1',
        'bosun-kiosk.service',
        'bosun-midi.service',
        'bosun-midi.timer'
    )) {
        if ($source.IndexOf($forbidden, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            throw "Hub deploy helper contains an out-of-scope operation: $forbidden"
        }
    }
    foreach ($required in @(
        "Invoke-CheckedNative -Executable `$python -Arguments @('-m', 'pytest', '-q') -WorkingDirectory `$hubRoot",
        'verify_package "$SOURCE"',
        'python3 -m py_compile "$SOURCE"/*.py',
        'sha256sum -c "$MANIFEST"',
        'as_root install -o root -g root -m 0644 --',
        'verify_installed_permissions "$TARGET"',
        'trap rollback_deploy EXIT HUP INT TERM',
        'BOSUN_HUB_ROLLBACK=OK',
        'BOSUN_HUB_RECOVERY=OK',
        'renameat2(-100, left, -100, right, 2)',
        'restore_previous_package',
        'LOCK=' + '__LOCK__',
        'JOURNAL_PARENT=' + '__JOURNAL_PARENT__',
        '[ "$JOURNAL_PARENT" = /var/lib/bosun-hub-deploy ]',
        'as_root mkdir -m 0700 -- "$LOCK"',
        'require_deploy_lock',
        'os.O_NOFOLLOW',
        'item.st_uid != 0',
        'write_lock_value identity "old=$old_identity',
        'rollback directory identities are ambiguous; preserving both trees',
        'systemctl show --property=NRestarts --value "$SERVICE"',
        'systemctl show --property=MainPID --value "$SERVICE"',
        'python3 - 9876 "$HEALTH_WAIT_SECONDS" "$DEPLOY_ID"',
        'BOSUN_HUB_PING=ACK',
        'snapshot_path "$PARENT/stage"',
        'snapshot_path "$PARENT/config"'
    )) {
        if ($source.IndexOf($required, [StringComparison]::Ordinal) -lt 0) {
            throw "Hub deploy helper is missing its safety contract: $required"
        }
    }

    $transactionMatch = [regex]::Match(
        $source,
        '(?s)\$remoteTransaction\s*=\s*\$remoteLibrary\s*\+\s*@''\r?\n(?<script>.*?)\r?\n''@'
    )
    if (-not $transactionMatch.Success) {
        throw 'Could not extract the rollback-protected remote transaction'
    }
    $transaction = $transactionMatch.Groups['script'].Value
    Assert-Ordered -Text $transaction -Tokens @(
        'verify_package "$SOURCE"',
        'as_root mkdir -m 0755 -- "$NEW"',
        'verify_package "$NEW"',
        'old_identity=$(stat -c',
        'write_lock_value identity',
        'write_lock_value phase prepared',
        'as_root mv -- "$NEW" "$BACKUP"',
        'as_root systemctl stop "$SERVICE"',
        'exchange_directories "$TARGET" "$BACKUP"',
        'as_root systemctl restart "$SERVICE"',
        'wait_for_service',
        'wait_for_protocol_ping',
        'verify_package "$TARGET"',
        'trap - EXIT HUP INT TERM',
        'BOSUN_HUB_TRANSACTION=READY'
    )
    if ($transaction.IndexOf('safe_remove_tree "$BACKUP"', [StringComparison]::Ordinal) -ge 0) {
        throw 'The first health gate must retain its rollback backup'
    }

    Assert-Ordered -Text $source -Tokens @(
        'Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName -Script $remoteAcquireLock',
        'Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName -Script $remoteTransaction',
        'Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName -Script $remoteCommit',
        '$committed = $true',
        '-Script $remoteCleanupBackup'
    )
    foreach ($protectedScript in @('remoteTransaction', 'remoteCommit', 'remoteRecovery', 'remoteCleanupBackup')) {
        $protectedMatch = [regex]::Match(
            $source,
            "(?s)\`$$protectedScript\s*=\s*\`$remoteLibrary\s*\+\s*@'\r?\n(?<script>.*?)\r?\n'@"
        )
        if (-not $protectedMatch.Success -or
            $protectedMatch.Groups['script'].Value.IndexOf('require_deploy_lock', [StringComparison]::Ordinal) -lt 0) {
            throw "$protectedScript can mutate deployment state without owning the persistent lock"
        }
    }
    if ([regex]::Matches($source, [regex]::Escape("'ServerAliveInterval=5'")).Count -lt 3 -or
        [regex]::Matches($source, [regex]::Escape("'ServerAliveCountMax=3'")).Count -lt 3 -or
        [regex]::Matches($source, [regex]::Escape("'ConnectionAttempts=1'")).Count -lt 3) {
        throw 'Not every SSH/SCP path has bounded post-connect liveness options'
    }
    if ($source.IndexOf('rm -rf -- "$LOCK"', [StringComparison]::Ordinal) -ge 0 -or
        $source.IndexOf('as_root rmdir -- "$LOCK"', [StringComparison]::Ordinal) -lt 0) {
        throw 'Persistent lock cleanup must be exact and non-recursive'
    }
    if ($source.IndexOf('as_root cat "$LOCK/', [StringComparison]::Ordinal) -ge 0) {
        throw 'Privileged lock reads must use O_NOFOLLOW, never follow journal symlinks through cat'
    }
    if ($source.IndexOf('verify_package "$TARGET" runtime', [StringComparison]::Ordinal) -ge 0 -or
        $source.IndexOf('chown bosun:bosun "$NEW"', [StringComparison]::Ordinal) -ge 0) {
        throw 'Installed executable code must remain root-owned with an exact inventory'
    }

    # Parse every complete remote program (shared library plus action) with a
    # real shell. This catches heredoc, trap and multiline-quote defects that
    # token-order assertions cannot see.
    $libraryMatch = [regex]::Match(
        $source,
        '(?s)\$remoteLibrary\s*=\s*@''\r?\n(?<script>.*?)\r?\n''@\.Replace'
    )
    if (-not $libraryMatch.Success) {
        throw 'Could not extract the shared remote shell library'
    }
    $bash = Get-Command bash -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $bash) {
        foreach ($remoteName in @(
            'remotePreflight', 'remoteVerify', 'remoteAcquireLock',
            'remoteTransaction', 'remoteCommit', 'remoteRecovery',
            'remoteCleanupBackup', 'remoteReleaseLockIfOwned', 'remoteCleanupStage'
        )) {
            $actionMatch = [regex]::Match(
                $source,
                "(?s)\`$$remoteName\s*=\s*\`$remoteLibrary\s*\+\s*@'\r?\n(?<script>.*?)\r?\n'@"
            )
            if (-not $actionMatch.Success) {
                throw "Could not extract $remoteName for shell syntax validation"
            }
            $shellProgram = $libraryMatch.Groups['script'].Value + "`n" +
                $actionMatch.Groups['script'].Value
            $shellProgram = [regex]::Replace($shellProgram, '__[A-Z0-9_]+__', 'x')
            $startInfo = [Diagnostics.ProcessStartInfo]::new()
            $startInfo.FileName = $bash.Source
            $startInfo.Arguments = '-n'
            $startInfo.UseShellExecute = $false
            $startInfo.CreateNoWindow = $true
            $startInfo.RedirectStandardInput = $true
            $startInfo.RedirectStandardOutput = $true
            $startInfo.RedirectStandardError = $true
            $shell = [Diagnostics.Process]::Start($startInfo)
            try {
                $shell.StandardInput.Write($shellProgram)
                $shell.StandardInput.Close()
                if (-not $shell.WaitForExit(5000)) {
                    $shell.Kill()
                    [void]$shell.WaitForExit(3000)
                    throw "$remoteName shell syntax check timed out"
                }
                $shellError = $shell.StandardError.ReadToEnd()
                if ($shell.ExitCode -ne 0) {
                    throw "$remoteName has invalid shell syntax: $shellError"
                }
            } finally {
                if (-not $shell.HasExited) {
                    $shell.Kill()
                    [void]$shell.WaitForExit(3000)
                }
                $shell.Dispose()
            }
        }
    }
    $finallyBlocks = @($ast.FindAll(
        {
            param($node)
            $node -is [Management.Automation.Language.TryStatementAst] -and
                $null -ne $node.Finally
        },
        $true
    ))
    if ($finallyBlocks.Count -eq 0 -or
        $source.IndexOf('if ($transactionAttempted -and -not $committed)', [StringComparison]::Ordinal) -lt 0 -or
        $source.IndexOf('-Script $remoteRecovery', [StringComparison]::Ordinal) -lt 0) {
        throw 'Hub deploy helper lacks an independent PowerShell rollback/recovery boundary'
    }

    # Execute the exact inventory/hash verifier in both modes. Starting the
    # real service would create __pycache__ in a writable package directory.
    # The deployed tree is root-owned and non-writable, so every check remains
    # strict and must reject both bytecode caches and unrelated entries.
    $packageVerifierMatch = [regex]::Match(
        $source,
        '(?ms)^\s*python3 - "\$vp_root" "\$MANIFEST" "\$EXPECTED_FILE_COUNT" <<''PY''\r?\n(?<code>.*?)\r?\nPY$',
        [Text.RegularExpressions.RegexOptions]::Multiline -bor
            [Text.RegularExpressions.RegexOptions]::Singleline
    )
    if (-not $packageVerifierMatch.Success) {
        throw 'Could not extract the embedded package inventory verifier'
    }
    $packageValidator = Join-Path $testRoot 'package_validator.py'
    Write-Utf8File -Path $packageValidator -Content $packageVerifierMatch.Groups['code'].Value
    $packageManifest = Join-Path $testRoot 'package-manifest.sha256'
    $packageFiles = @(Get-ChildItem -LiteralPath $validSource -File -Filter '*.py' |
        Sort-Object Name)
    $manifestText = (($packageFiles | ForEach-Object {
        "$((Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant())  $($_.Name)"
    }) -join "`n") + "`n"
    Write-Utf8File -Path $packageManifest -Content $manifestText
    $python = Get-PythonPath

    function Invoke-PackageVerifier {
        $savedPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $output = @(& $python $packageValidator $validSource $packageManifest `
                $packageFiles.Count 2>&1)
            return [pscustomobject]@{
                ExitCode = $LASTEXITCODE
                Output = (($output | ForEach-Object { $_.ToString() }) -join "`n")
            }
        } finally {
            $ErrorActionPreference = $savedPreference
        }
    }

    $strictClean = Invoke-PackageVerifier
    if ($strictClean.ExitCode -ne 0) {
        throw "Strict package verifier rejected clean staging: $($strictClean.Output)"
    }
    $savedPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $compileOutput = @(& $python -m py_compile @($packageFiles.FullName) 2>&1)
        $compileExit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $savedPreference
    }
    if ($compileExit -ne 0) {
        throw "Could not create the runtime bytecode cache: $($compileOutput -join '; ')"
    }
    $strictCached = Invoke-PackageVerifier
    if ($strictCached.ExitCode -eq 0 -or $strictCached.Output -notlike '*unexpected package entry: __pycache__*') {
        throw 'Strict package verification accepted a runtime bytecode cache'
    }
    Remove-Item -LiteralPath (Join-Path $validSource '__pycache__') -Recurse -Force
    $strictAgain = Invoke-PackageVerifier
    if ($strictAgain.ExitCode -ne 0) {
        throw "Strict package verifier did not recover after exact cache cleanup: $($strictAgain.Output)"
    }
    $roguePath = Join-Path $validSource 'rogue.txt'
    Write-Utf8File -Path $roguePath -Content "not part of the signed inventory`n"
    $runtimeRogue = Invoke-PackageVerifier
    if ($runtimeRogue.ExitCode -eq 0 -or $runtimeRogue.Output -notlike '*package inventory mismatch*') {
        throw 'Strict package verification accepted an unrelated file'
    }
    Remove-Item -LiteralPath $roguePath -Force

    # On Unix, prove the exact privileged journal reader refuses a symlink and
    # does not disclose its target. The production lock lives in a root-owned
    # parent; this specifically guards accidental future replacement of
    # O_NOFOLLOW with a privileged `cat`.
    if ([IO.Path]::DirectorySeparatorChar -eq '/') {
        $lockReaderMatch = [regex]::Match(
            $source,
            '(?ms)^\s*as_root python3 - "\$LOCK/\$rlv_name" <<''PY''\r?\n(?<code>.*?)\r?\nPY$',
            [Text.RegularExpressions.RegexOptions]::Multiline -bor
                [Text.RegularExpressions.RegexOptions]::Singleline
        )
        if (-not $lockReaderMatch.Success) {
            throw 'Could not extract the privileged journal reader'
        }
        $lockReader = Join-Path $testRoot 'lock_reader.py'
        Write-Utf8File -Path $lockReader -Content $lockReaderMatch.Groups['code'].Value
        $secret = Join-Path $testRoot 'journal-secret.txt'
        $link = Join-Path $testRoot 'journal-owner-link'
        Write-Utf8File -Path $secret -Content "must-not-be-disclosed`n"
        New-Item -ItemType SymbolicLink -Path $link -Target $secret | Out-Null
        $savedPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $readerOutput = @(& $python $lockReader $link 2>&1)
            $readerExit = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $savedPreference
        }
        $readerRendered = ($readerOutput | ForEach-Object { $_.ToString() }) -join "`n"
        if ($readerExit -eq 0 -or $readerRendered -like '*must-not-be-disclosed*') {
            throw 'Privileged journal reader followed or disclosed a symlink target'
        }
    }

    # Load only the encoder function and prove the Windows-to-ssh boundary is
    # byte preserving and contains no shell-sensitive quotes.
    $encoderAst = $ast.Find(
        {
            param($node)
            $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq 'ConvertTo-EncodedRemoteCommand'
        },
        $true
    )
    if ($null -eq $encoderAst) {
        throw 'Hub deploy helper is missing its encoded remote-command boundary'
    }
    Invoke-Expression $encoderAst.Extent.Text
    $quoteProbe = "set -eu`nvalue=''`n[ -z `"`$value`" ]`n"
    $envelope = ConvertTo-EncodedRemoteCommand -Script $quoteProbe
    $envelopeMatch = [regex]::Match(
        $envelope,
        '^printf %s (?<payload>[A-Za-z0-9+/]+={0,2}) \| base64 -d \| sh$'
    )
    if (-not $envelopeMatch.Success -or $envelope.Contains("'") -or $envelope.Contains('"')) {
        throw "Remote transport envelope is not quote-free: $envelope"
    }
    $decoded = [Text.UTF8Encoding]::new($false).GetString(
        [Convert]::FromBase64String($envelopeMatch.Groups['payload'].Value)
    )
    if ($decoded -cne $quoteProbe) {
        throw 'Encoded remote command did not preserve the shell program byte-for-byte'
    }

    # Execute the exact embedded protocol verifier against local mock servers.
    # This proves correlation, split frames, transient link readiness, failure
    # propagation and the global timeout without touching the Pi.
    $pingMatch = [regex]::Match(
        $source,
        '(?ms)^wait_for_protocol_ping\(\) \{.*?<<''PY''\r?\n(?<code>.*?)\r?\nPY\r?\n\}',
        [Text.RegularExpressions.RegexOptions]::Multiline -bor
            [Text.RegularExpressions.RegexOptions]::Singleline
    )
    if (-not $pingMatch.Success) {
        throw 'Could not extract the embedded hub protocol PING verifier'
    }
    $validatorPath = Join-Path $testRoot 'ping_validator.py'
    Write-Utf8File -Path $validatorPath -Content $pingMatch.Groups['code'].Value
    $serverPath = Join-Path $testRoot 'fake_hub_server.py'
    Write-Utf8File -Path $serverPath -Content @'
import json
import socket
import sys
import time

mode = sys.argv[1]
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", 0))
server.listen(8)
server.settimeout(0.4)
print("PORT=%d" % server.getsockname()[1], flush=True)
deadline = time.monotonic() + 2.2
requests = 0

try:
    while time.monotonic() < deadline:
        try:
            connection, _ = server.accept()
        except socket.timeout:
            continue
        connection.settimeout(0.4)
        receive = bytearray()
        try:
            while b"\n" not in receive or not receive.strip():
                chunk = connection.recv(4096)
                if not chunk:
                    break
                receive.extend(chunk)
                lines = [line for line in receive.split(b"\n") if line.strip()]
                if lines:
                    request = json.loads(lines[-1])
                    break
            else:
                continue
            if not receive.strip():
                continue
            requests += 1
            ident = request["id"]
            if request.get("type") != "PING" or not ident.startswith("deploy-hub-"):
                raise RuntimeError("unexpected request: %r" % request)
            if mode == "transient_link" and requests == 1:
                reply = {"type": "ERROR", "id": ident,
                         "error": "link_down", "of": "PING"}
            elif mode == "unexpected":
                reply = {"type": "ERROR", "id": ident,
                         "error": "exception", "of": "PING"}
            elif mode == "timeout":
                time.sleep(0.35)
                continue
            else:
                # Noise and an unrelated correlated-looking response must not
                # produce a false pass. Split the actual ACK across writes.
                connection.sendall(b"not-json\n")
                connection.sendall(b'{"type":"ACK","id":"other"}\n')
                reply = {"type": "ACK", "id": ident}
            wire = (json.dumps(reply, separators=(",", ":")) + "\n").encode()
            connection.sendall(wire[:3])
            connection.sendall(wire[3:])
            if mode == "success" or (mode == "transient_link" and requests >= 2):
                break
        finally:
            connection.close()
finally:
    server.close()
    print("REQUESTS=%d" % requests, flush=True)
'@

    Assert-PingVerifierCase -Python $python -ValidatorPath $validatorPath `
        -ServerPath $serverPath -Mode success -ShouldPass $true `
        -ExpectedText 'BOSUN_HUB_PING=ACK'
    Assert-PingVerifierCase -Python $python -ValidatorPath $validatorPath `
        -ServerPath $serverPath -Mode transient_link -ShouldPass $true `
        -ExpectedText 'BOSUN_HUB_PING=ACK'
    Assert-PingVerifierCase -Python $python -ValidatorPath $validatorPath `
        -ServerPath $serverPath -Mode unexpected -ShouldPass $false `
        -ExpectedText 'PING returned unexpected reply'
    Assert-PingVerifierCase -Python $python -ValidatorPath $validatorPath `
        -ServerPath $serverPath -Mode timeout -ShouldPass $false `
        -ExpectedText 'hub protocol PING failed'

    Write-Host 'PASS: deploy-hub.ps1 dry-run, rollback guards and protocol health mocks'
} finally {
    $tempParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([char[]]@('\', '/')) + [IO.Path]::DirectorySeparatorChar
    $candidate = [IO.Path]::GetFullPath($testRoot)
    if ($candidate.StartsWith($tempParent, $pathComparison) -and
        [IO.Path]::GetFileName($candidate) -eq "bosun-hub-script-test-$testId") {
        Remove-Item -LiteralPath $candidate -Recurse -Force -ErrorAction SilentlyContinue
    }
}
