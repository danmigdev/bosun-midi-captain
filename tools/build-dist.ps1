<#
.SYNOPSIS
  Build and install the local test build of the current version.

.DESCRIPTION
  The first step after tools\bump-version.ps1, before any push or release:

    1. Firmware: rebuilds the update package and installer assets for the
       version in editor\package.json in WSL (tools\build-factory-installer.sh).
    2. Desktop: tools\package-portable.ps1, which refuses a bundled firmware
       from any other release -> dist\Bosun-<version>-portable-x64 (+ .zip).
    3. Raspberry Pi: when the SSH host answers, copies the Pi package built in
       step 2 and runs its quick-install.sh (hub, MIDI bridge, Stage). Refused
       while a Captain update is running on the Pi.
    4. Android: when adb lists an attached device, tools\build-android.ps1
       -Deploy builds the APK and installs it.

  Nothing is pushed, tagged or published. The Captain firmware is not
  touched: update it from the new Desktop build as part of the manual tests.

.PARAMETER SkipFirmware
  Reuse the firmware package already in editor\src-tauri\resources (it must
  still match the current version; the packager checks).

.PARAMETER SkipPi
  Do not install on the Raspberry Pi.

.PARAMETER SkipAndroid
  Do not build or install the Android APK.

.PARAMETER PiHost
  SSH host or alias of the Pi, with key-based login and sudo (default: bosun-hub).

.PARAMETER Distro
  WSL distribution with the firmware toolchain (default: Ubuntu).

.PARAMETER OutputSuffix
  Passed to package-portable.ps1 to keep a build beside another one.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\build-dist.ps1
#>

[CmdletBinding()]
param(
    [switch] $SkipFirmware,
    [switch] $SkipPi,
    [switch] $SkipAndroid,
    [string] $PiHost = "bosun-hub",
    [string] $Distro = "Ubuntu",
    [ValidatePattern('^[a-zA-Z0-9-]*$')]
    [string] $OutputSuffix = ""
)

$ErrorActionPreference = "Stop"

# PowerShell 5.1 turns each stderr line of a native tool into a terminating
# error under "Stop"; $LASTEXITCODE stays the source of truth.
function Invoke-NativeTool {
    param([scriptblock]$Command)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Command } finally { $ErrorActionPreference = $previous }
}

function Write-LfFile([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, ($Text -replace "`r`n", "`n"), (New-Object Text.UTF8Encoding($false)))
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$version = (Get-Content (Join-Path $repoRoot "editor\package.json") -Raw | ConvertFrom-Json).version
$summary = New-Object System.Collections.Generic.List[string]
Write-Host "[dist] Bosun $version" -ForegroundColor Cyan

# ---------- 1. Firmware ----------
if ($SkipFirmware) {
    $summary.Add("Firmware: reused the existing package")
} else {
    Write-Host "[firmware] Building the $version firmware package in WSL ($Distro) ..."
    # A Windows checkout gives the shell helpers CRLF endings, which bash
    # rejects (set: pipefail: invalid option name). Run an LF copy that sits
    # in tools\ so the script still resolves the repository root from itself.
    $lfCopy = Join-Path $repoRoot "tools\.build-factory-installer-lf.sh"
    Write-LfFile $lfCopy ([IO.File]::ReadAllText((Join-Path $repoRoot "tools\build-factory-installer.sh")))
    try {
        $wslRoot = (Invoke-NativeTool { & wsl.exe -d $Distro --exec wslpath -a $repoRoot } | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $wslRoot) { throw "WSL distribution '$Distro' is not available" }
        # --exec avoids the intermediate shell that re-parses the command line.
        Invoke-NativeTool { & wsl.exe -d $Distro --exec bash -c "cd '$wslRoot' && bash tools/.build-factory-installer-lf.sh" }
        if ($LASTEXITCODE -ne 0) { throw "Firmware build failed (exit $LASTEXITCODE)" }
    } finally {
        Remove-Item -LiteralPath $lfCopy -Force -ErrorAction SilentlyContinue
    }
    $summary.Add("Firmware: $version update package rebuilt")
}

# ---------- 2. Desktop ----------
Write-Host "[desktop] Packaging the portable build ..."
$packager = Join-Path $repoRoot "tools\package-portable.ps1"
if ($OutputSuffix) {
    Invoke-NativeTool { & powershell -NoProfile -ExecutionPolicy Bypass -File $packager -OutputSuffix $OutputSuffix }
} else {
    Invoke-NativeTool { & powershell -NoProfile -ExecutionPolicy Bypass -File $packager }
}
if ($LASTEXITCODE -ne 0) { throw "Portable packaging failed (exit $LASTEXITCODE)" }
$name = "Bosun-$version-portable-x64" + $(if ($OutputSuffix) { "-$OutputSuffix" } else { "" })
$summary.Add("Desktop: $(Join-Path $repoRoot "dist\$name")")

# ---------- 3. Raspberry Pi ----------
if ($SkipPi) {
    $summary.Add("Raspberry Pi: skipped")
} else {
    Invoke-NativeTool { & ssh.exe -o BatchMode=yes -o ConnectTimeout=6 $PiHost true } | Out-Null
    if ($LASTEXITCODE -ne 0) {
        $summary.Add("Raspberry Pi: $PiHost not reachable over SSH; skipped")
    } else {
        Write-Host "[pi] Installing $version on $PiHost ..."
        $package = Join-Path $repoRoot "editor\src-tauri\resources\pi\bosun-pi-setup.tar.gz"
        $installer = Join-Path $env:TEMP "bosun-dist-install.sh"
        Write-LfFile $installer @'
#!/usr/bin/env bash
# Installs a test build of the Pi package. Never flashes the Captain.
set -euo pipefail
version="${1:?version}"
package="$HOME/bosun-pi-$version.tar.gz"
# quick-install.sh restarts the hub: never under a running Captain update.
busy=$(sudo python3 -c '
import glob, json
terminal = {"done", "error", "rolled-back", "recovery-required", "aborted"}
for path in glob.glob("/home/*/.local/state/bosun/updates/*/status.json"):
    try:
        phase = json.load(open(path)).get("phase")
    except (OSError, ValueError):
        continue
    if phase not in terminal:
        print(path, phase)
')
if [ -n "$busy" ]; then
    echo "A Captain update is still in progress on the Pi; nothing was installed:" >&2
    echo "$busy" >&2
    exit 3
fi
target="$HOME/bosun-$version"
rm -rf "$target"
mkdir "$target"
tar -xzf "$package" -C "$target"
if ! sudo bash "$target/tools/rpi-hub/quick-install.sh" > "$HOME/install-$version.log" 2>&1; then
    tail -20 "$HOME/install-$version.log" >&2
    exit 1
fi
systemctl is-active bosun-hub.service bosun-midi.timer bosun-kiosk.service
echo "Stage $(grep -o 'v[0-9][0-9.]*' /opt/bosun-hub/stage/index.html | head -1)"
'@
        try {
            Invoke-NativeTool { & scp.exe -q $package "${PiHost}:bosun-pi-$version.tar.gz" }
            if ($LASTEXITCODE -ne 0) { throw "Copying the Pi package failed (exit $LASTEXITCODE)" }
            Invoke-NativeTool { & scp.exe -q $installer "${PiHost}:bosun-dist-install.sh" }
            if ($LASTEXITCODE -ne 0) { throw "Copying the Pi installer failed (exit $LASTEXITCODE)" }
            Invoke-NativeTool { & ssh.exe -o BatchMode=yes $PiHost "bash ~/bosun-dist-install.sh $version; status=`$?; rm -f ~/bosun-dist-install.sh; exit `$status" }
            if ($LASTEXITCODE -ne 0) { throw "Pi installation failed (exit $LASTEXITCODE); see ~/install-$version.log on the Pi" }
        } finally {
            Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
        }
        $summary.Add("Raspberry Pi: $version installed on $PiHost")
    }
}

# ---------- 4. Android ----------
if ($SkipAndroid) {
    $summary.Add("Android: skipped")
} else {
    $adb = Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools\adb.exe"
    if (-not (Test-Path $adb)) { $adb = "C:\development\Android\Sdk\platform-tools\adb.exe" }
    $attached = @()
    if (Test-Path $adb) {
        # The first adb call forks a background server that inherits the caller's
        # output handles, so a caller capturing this script's output waits for it
        # forever. Start the server detached first; later adb calls reuse it.
        $server = Start-Process -FilePath $adb -ArgumentList "start-server" -WindowStyle Hidden -PassThru
        if (-not $server.WaitForExit(30000)) { $server.Kill(); throw "adb start-server did not finish in 30 s" }
        $attached = @(Invoke-NativeTool { & $adb devices } | Where-Object { $_ -match "`tdevice$" })
    }
    if ($attached.Count -eq 0) {
        $summary.Add("Android: no device attached (adb devices); skipped")
    } else {
        Write-Host "[android] Building and installing the APK on $($attached[0].Split("`t")[0]) ..."
        Invoke-NativeTool { & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $repoRoot "tools\build-android.ps1") -Deploy }
        if ($LASTEXITCODE -ne 0) { throw "Android build or install failed (exit $LASTEXITCODE)" }
        $summary.Add("Android: $version installed on $($attached[0].Split("`t")[0])")
    }
}

Write-Host "`nReady for manual tests:" -ForegroundColor Green
foreach ($line in $summary) { Write-Host "  $line" -ForegroundColor Green }
Write-Host "Nothing was pushed or published. Push, tag and release only after the tests." -ForegroundColor Cyan
