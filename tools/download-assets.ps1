<#
.SYNOPSIS
  Download CircuitPython UF2 + Adafruit libraries the installer needs to bundle.

.DESCRIPTION
  Pulls the CircuitPython UF2 image for the Raspberry Pi Pico and installs the
  nine locked files belonging to the four Adafruit libraries the firmware
  imports. The official bundle release, archive SHA-256 and individual file
  hashes come from tools/adafruit-bundle-lock.json; no moving "latest" release
  is used. It then invokes the shared resource sync.

  After running this script, the editor's "Pedal setup" wizard can flash a
  blank pedal end-to-end without any further downloads.

.PARAMETER CircuitPythonVersion
  Specific CircuitPython release to install. Defaults to 9.2.7 (stable 9.x).

.EXAMPLE
  pwsh -File tools\download-assets.ps1
#>

[CmdletBinding()]
param(
    [ValidateSet("9.2.7")]
    [string] $CircuitPythonVersion = "9.2.7"
)

$ErrorActionPreference = "Stop"

function Invoke-NativeTool {
    param([scriptblock]$Command)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Command } finally { $ErrorActionPreference = $previous }
}

$repoRoot   = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$resources  = Join-Path $repoRoot "editor\src-tauri\resources"
$libDir     = Join-Path $resources "lib"
$syncScript = Join-Path $PSScriptRoot "sync_firmware_resources.py"
$vendorScript = Join-Path $PSScriptRoot "provision_adafruit_bundle.py"
$pythonExe  = (Get-Command python -ErrorAction Stop).Source

New-Item -ItemType Directory -Force -Path $resources, $libDir | Out-Null

# ---------- 1. CircuitPython UF2 ----------

$cpUrl = "https://downloads.circuitpython.org/bin/raspberry_pi_pico/en_US/adafruit-circuitpython-raspberry_pi_pico-en_US-${CircuitPythonVersion}.uf2"
$cpOut = Join-Path $resources "circuitpython.uf2"

if (Test-Path $cpOut) {
    Write-Host "[skip] CircuitPython UF2 already present at $cpOut"
} else {
    Write-Host "[get ] CircuitPython $CircuitPythonVersion from downloads.circuitpython.org"
    Invoke-WebRequest -Uri $cpUrl -OutFile $cpOut -UseBasicParsing
    Write-Host "[ok  ] $cpOut"
}

# ---------- 2. SHA-pinned Adafruit CircuitPython 9.x bundle ----------

Invoke-NativeTool { & $pythonExe $vendorScript --destination $libDir }
if ($LASTEXITCODE -ne 0) {
    throw "Pinned Adafruit bundle provisioning failed"
}

# ---------- 3. Synchronize canonical firmware resources ----------

Invoke-NativeTool { & $pythonExe $syncScript --repo-root $repoRoot }
if ($LASTEXITCODE -ne 0) {
    throw "Firmware resource sync failed"
}

Write-Host ""
Write-Host "Assets ready. The Pedal Setup wizard can now flash a blank pedal." -ForegroundColor Green
Write-Host "Firmware-only changes are synced automatically by every package build." -ForegroundColor Green
