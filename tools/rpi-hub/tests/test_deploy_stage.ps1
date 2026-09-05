Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$deployScript = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../deploy-stage.ps1'))
$testId = [Guid]::NewGuid().ToString('N')
$testRoot = Join-Path ([IO.Path]::GetTempPath()) "bosun-stage-script-test-$testId"
$validBundle = Join-Path $testRoot 'valid'
$missingBundle = Join-Path $testRoot 'missing'
$escapingBundle = Join-Path $testRoot 'escaping'
$caseBundle = Join-Path $testRoot 'case-mismatch'
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

try {
    Write-Utf8File -Path (Join-Path $validBundle 'index.html') -Content @'
<!doctype html><script type="module" src="/assets/app-12345678.js?v=1"></script><link rel="stylesheet" href="assets/app-12345678.css">
'@
    Write-Utf8File -Path (Join-Path $validBundle 'assets/app-12345678.js') -Content 'console.log("stage");'
    Write-Utf8File -Path (Join-Path $validBundle 'assets/app-12345678.css') -Content 'body { color: white; }'

    # An unreachable host proves that ValidateOnly never tries SSH.
    & $deployScript -ValidateOnly -SkipBuild -HostName 'unreachable.invalid' -BundlePath $validBundle | Out-Null

    Write-Utf8File -Path (Join-Path $missingBundle 'index.html') -Content '<script src="/assets/missing.js"></script>'
    Write-Utf8File -Path (Join-Path $missingBundle 'placeholder.txt') -Content 'keeps bundle non-empty'
    Assert-Fails -ExpectedText 'missing asset' -Action {
        & $deployScript -ValidateOnly -SkipBuild -BundlePath $missingBundle | Out-Null
    }

    Write-Utf8File -Path (Join-Path $escapingBundle 'index.html') -Content '<script src="../outside.js"></script>'
    Write-Utf8File -Path (Join-Path $escapingBundle 'placeholder.txt') -Content 'keeps bundle non-empty'
    Write-Utf8File -Path (Join-Path $testRoot 'outside.js') -Content 'must stay outside'
    Assert-Fails -ExpectedText 'escapes the bundle' -Action {
        & $deployScript -ValidateOnly -SkipBuild -BundlePath $escapingBundle | Out-Null
    }

    Write-Utf8File -Path (Join-Path $caseBundle 'index.html') -Content '<script src="/assets/App.js"></script>'
    Write-Utf8File -Path (Join-Path $caseBundle 'assets/app.js') -Content 'console.log("case-sensitive");'
    Assert-Fails -ExpectedText 'path case does not match' -Action {
        & $deployScript -ValidateOnly -SkipBuild -BundlePath $caseBundle | Out-Null
    }

    $source = [IO.File]::ReadAllText($deployScript)
    foreach ($forbidden in @(
        'apt-get',
        'npm install',
        'bosun-hub.service',
        'bosun-midi.service',
        'bosun-midi.timer'
    )) {
        if ($source.IndexOf($forbidden, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            throw "Deploy helper contains forbidden operation: $forbidden"
        }
    }

    # A changed Stage bundle is not usable until the already-running kiosk
    # reloads it.  Merely restarting systemd is insufficient: cage can remain
    # active even if its Chromium child never appears.  Keep this as a literal
    # remote script so the exact service and bounded readiness contract can be
    # audited without contacting the Pi.
    $kioskScriptMatch = [regex]::Match(
        $source,
        '(?s)\$restartKioskCommand\s*=\s*@''\r?\n(?<script>.*?)\r?\n''@'
    )
    if (-not $kioskScriptMatch.Success) {
        throw 'Deploy helper is missing its remote kiosk restart/readiness script'
    }
    $kioskScript = $kioskScriptMatch.Groups['script'].Value
    foreach ($required in @(
        'kiosk_service=bosun-kiosk.service',
        'systemctl restart "$kiosk_service"',
        'systemctl is-active --quiet "$kiosk_service"',
        'systemctl show --property=ControlGroup --value "$kiosk_service"',
        'systemd-cgls --no-pager "$control_group"',
        'chromium(-browser)?',
        'while [ "$attempt" -lt 20 ]',
        'systemctl --no-pager --lines=30 status "$kiosk_service"'
    )) {
        if ($kioskScript.IndexOf($required, [StringComparison]::Ordinal) -lt 0) {
            throw "Kiosk restart script is missing: $required"
        }
    }
    if ($kioskScript -match 'bosun-(?:hub|midi)\.(?:service|timer)') {
        throw 'Kiosk restart script must not alter hub or MIDI service state'
    }

    $deployInvocation = $source.IndexOf(
        'Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName -Script $deployCommand',
        [StringComparison]::Ordinal
    )
    $restartInvocation = $source.IndexOf(
        'Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName -Script $restartKioskCommand',
        [StringComparison]::Ordinal
    )
    if ($deployInvocation -lt 0 -or $restartInvocation -le $deployInvocation) {
        throw 'Kiosk must be restarted and verified only after the bundle commit succeeds'
    }

    # Execute the exact embedded POSIX script with fake systemd/cgroup tools.
    # This covers failure propagation without touching SSH, systemd or the Pi.
    $bashCommand = Get-Command bash -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $bashCommand) {
        function Invoke-KioskScriptProbe {
            param(
                [Parameter(Mandatory)][ValidateSet('ready', 'restart-fails', 'inactive', 'no-browser')]
                [string]$Mode,
                [Parameter(Mandatory)][int]$ExpectedExitCode,
                [string]$ExpectedOutput
            )

            $probe = @'
set -eu
probe_mode=__MODE__
id() {
    if [ "${1:-}" = -u ]; then printf '0\n'; else return 97; fi
}
systemctl() {
    case "$*" in
        'restart bosun-kiosk.service')
            [ "$probe_mode" != restart-fails ]
            ;;
        'is-active --quiet bosun-kiosk.service')
            [ "$probe_mode" != inactive ]
            ;;
        'show --property=ControlGroup --value bosun-kiosk.service')
            printf '/system.slice/bosun-kiosk.service\n'
            ;;
        '--no-pager --lines=30 status bosun-kiosk.service')
            printf 'mock kiosk status\n' >&2
            ;;
        *)
            printf 'unexpected systemctl call: %s\n' "$*" >&2
            return 96
            ;;
    esac
}
systemd-cgls() {
    if [ "$*" != '--no-pager /system.slice/bosun-kiosk.service' ]; then
        printf 'unexpected systemd-cgls call: %s\n' "$*" >&2
        return 95
    fi
    if [ "$probe_mode" = ready ]; then
        printf '%s\n' '42 /usr/lib/chromium/chromium --kiosk'
    else
        printf '%s\n' '41 /usr/bin/cage -- chromium'
    fi
}
sleep() { :; }
__KIOSK_SCRIPT__
'@.Replace('__MODE__', $Mode).Replace('__KIOSK_SCRIPT__', $kioskScript)
            $encodedProbe = [Convert]::ToBase64String(
                [Text.UTF8Encoding]::new($false).GetBytes($probe)
            )
            $command = "printf %s $encodedProbe | base64 -d | bash"
            $savedErrorPreference = $ErrorActionPreference
            try {
                # Windows PowerShell materialises redirected native stderr as
                # non-terminating ErrorRecord objects. Expected failure modes
                # still need their real process exit code asserted below.
                $ErrorActionPreference = 'Continue'
                $probeOutput = @(& $bashCommand.Source '-lc' $command 2>&1)
                $probeExitCode = $LASTEXITCODE
            } finally {
                $ErrorActionPreference = $savedErrorPreference
            }
            if ($probeExitCode -ne $ExpectedExitCode) {
                throw "Kiosk '$Mode' probe exited $probeExitCode instead of ${ExpectedExitCode}: $($probeOutput -join '; ')"
            }
            $joinedOutput = ($probeOutput | ForEach-Object { $_.ToString() }) -join "`n"
            if (-not [string]::IsNullOrEmpty($ExpectedOutput) -and
                $joinedOutput.IndexOf($ExpectedOutput, [StringComparison]::Ordinal) -lt 0) {
                throw "Kiosk '$Mode' probe did not report '$ExpectedOutput': $joinedOutput"
            }
        }

        Invoke-KioskScriptProbe -Mode ready -ExpectedExitCode 0
        Invoke-KioskScriptProbe -Mode restart-fails -ExpectedExitCode 1 `
            -ExpectedOutput 'failed to restart bosun-kiosk.service'
        Invoke-KioskScriptProbe -Mode inactive -ExpectedExitCode 1 `
            -ExpectedOutput 'did not become active with Chromium in its cgroup'
        Invoke-KioskScriptProbe -Mode no-browser -ExpectedExitCode 1 `
            -ExpectedOutput 'did not become active with Chromium in its cgroup'
    }

    # Windows PowerShell -> ssh.exe can lose shell quoting around an option
    # value containing spaces. That previously split rsync's out-format and
    # made the trailing %n%L look like a source path on the Pi.
    if ($source -notmatch [regex]::Escape('--out-format=%i:%n%L')) {
        throw 'Remote rsync diff format is not the expected whitespace-free token'
    }
    $remoteOptionWithWhitespace = [regex]::Match(
        $source,
        '--[A-Za-z0-9-]+=(?:''[^'']*\s[^'']*''|"[^"\r\n]*\s[^"\r\n]*")'
    )
    if ($remoteOptionWithWhitespace.Success) {
        throw "Remote option relies on whitespace quoting unsafe through ssh.exe: $($remoteOptionWithWhitespace.Value)"
    }

    # Load only the encoder function from the deploy script's AST, without
    # executing its main body or touching SSH, and prove embedded quotes and
    # newlines survive its quote-free transport envelope exactly.
    $tokens = $null
    $parseErrors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile(
        $deployScript, [ref]$tokens, [ref]$parseErrors
    )
    if ($parseErrors.Count -ne 0) {
        throw "Deploy helper has PowerShell parse errors: $($parseErrors -join '; ')"
    }
    $encoderAst = $ast.Find(
        {
            param($node)
            $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq 'ConvertTo-EncodedRemoteCommand'
        },
        $true
    )
    if ($null -eq $encoderAst) {
        throw 'Deploy helper is missing its encoded remote-command boundary'
    }
    Invoke-Expression $encoderAst.Extent.Text
    $quoteProbe = "set -eu`nvalue=''`nif [ -n `"`$value`" ]; then exit 91; fi`n"
    $envelope = ConvertTo-EncodedRemoteCommand -Script $quoteProbe
    $envelopeMatch = [regex]::Match(
        $envelope,
        '^printf %s (?<payload>[A-Za-z0-9+/]+={0,2}) \| base64 -d \| sh$'
    )
    if (-not $envelopeMatch.Success -or $envelope.Contains("'") -or $envelope.Contains('"')) {
        throw "Remote transport envelope is not quote-free: $envelope"
    }
    $decodedProbe = [Text.UTF8Encoding]::new($false).GetString(
        [Convert]::FromBase64String($envelopeMatch.Groups['payload'].Value)
    )
    if ($decodedProbe -cne $quoteProbe) {
        throw 'Encoded remote command did not preserve the shell script byte-for-byte'
    }

    $directSshCalls = [regex]::Matches(
        $source,
        'Invoke-CheckedNative\s+-Executable\s+\$ssh\b',
        [Text.RegularExpressions.RegexOptions]::IgnoreCase
    )
    if ($directSshCalls.Count -ne 2) {
        throw 'An SSH call bypasses the single encoded remote-command boundary'
    }

    Write-Host 'PASS: deploy-stage.ps1 offline validation and safety checks'
} finally {
    $tempParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([char[]]@('\', '/')) + [IO.Path]::DirectorySeparatorChar
    $candidate = [IO.Path]::GetFullPath($testRoot)
    if ($candidate.StartsWith($tempParent, $pathComparison) -and
        [IO.Path]::GetFileName($candidate) -eq "bosun-stage-script-test-$testId") {
        Remove-Item -LiteralPath $candidate -Recurse -Force -ErrorAction SilentlyContinue
    }
}
