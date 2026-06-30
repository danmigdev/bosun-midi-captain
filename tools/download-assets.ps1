<#
.SYNOPSIS
  Download CircuitPython UF2 + Adafruit libraries the installer needs to bundle.

.DESCRIPTION
  Pulls the CircuitPython UF2 image for the Raspberry Pi Pico and extracts the
  four Adafruit libraries the firmware imports (adafruit_display_text/,
  adafruit_st7789.mpy, neopixel.mpy, adafruit_pixelbuf.mpy) from the official
  Adafruit CircuitPython Bundle release. Also mirrors the local /firmware/
  tree into editor/src-tauri/resources/firmware so Tauri can bundle it.

  After running this script, the editor's "Pedal setup" wizard can flash a
  blank pedal end-to-end without any further downloads.

.PARAMETER CircuitPythonVersion
  Specific CircuitPython release to install. Defaults to 9.2.7 (stable 9.x).

.PARAMETER BundleSeries
  Which Adafruit bundle line to pull from (matches CircuitPython major).
  Defaults to "9.x".

.EXAMPLE
  pwsh -File tools\download-assets.ps1
  pwsh -File tools\download-assets.ps1 -CircuitPythonVersion 10.0.0 -BundleSeries 10.x
#>

[CmdletBinding()]
param(
    [string] $CircuitPythonVersion = "9.2.7",
    [string] $BundleSeries = "9.x"
)

$ErrorActionPreference = "Stop"

$repoRoot   = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$resources  = Join-Path $repoRoot "editor\src-tauri\resources"
$libDir     = Join-Path $resources "lib"

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

# ---------- 2. Adafruit CircuitPython Bundle (latest release) ----------

Write-Host "[get ] Adafruit Bundle release index"
$release = Invoke-RestMethod -Uri "https://api.github.com/repos/adafruit/Adafruit_CircuitPython_Bundle/releases/latest"
$assetRegex = "adafruit-circuitpython-bundle-${BundleSeries}-mpy-\d+\.zip"
$asset = $release.assets | Where-Object { $_.name -match $assetRegex } | Select-Object -First 1
if (-not $asset) {
    throw "Could not find an asset matching '$assetRegex' in release '$($release.tag_name)'. Try a different -BundleSeries."
}

$zipPath = Join-Path $env:TEMP $asset.name
Write-Host "[get ] $($asset.name)"
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipPath -UseBasicParsing

$tmp = Join-Path $env:TEMP ("captain-bundle-" + [guid]::NewGuid().ToString())
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::ExtractToDirectory($zipPath, $tmp)

$wanted = @(
    "adafruit_display_text",
    "adafruit_st7789.mpy",
    "neopixel.mpy",
    "adafruit_pixelbuf.mpy"
)

foreach ($name in $wanted) {
    $hit = Get-ChildItem -Path $tmp -Recurse -Filter $name -ErrorAction SilentlyContinue |
           Where-Object { $_.FullName -match "[\\/]lib[\\/]" } |
           Select-Object -First 1
    if (-not $hit) {
        Write-Warning "Bundle did not contain '$name'"
        continue
    }
    $dst = Join-Path $libDir $name
    if ($hit.PSIsContainer) {
        if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
        Copy-Item -Recurse $hit.FullName $dst
    } else {
        Copy-Item -Force $hit.FullName $dst
    }
    Write-Host "[ok  ] lib/$name"
}

Remove-Item -Recurse -Force $tmp
Remove-Item -Force $zipPath

# ---------- 3. Mirror firmware tree into resources ----------

$fwSrc = Join-Path $repoRoot "firmware"
$fwDst = Join-Path $resources "firmware"

if (-not (Test-Path $fwSrc)) {
    throw "Firmware tree not found at $fwSrc"
}

if (Test-Path $fwDst) { Remove-Item -Recurse -Force $fwDst }
Copy-Item -Recurse $fwSrc $fwDst
Write-Host "[ok  ] mirrored firmware tree to resources/firmware"

Write-Host ""
Write-Host "Assets ready. The Pedal Setup wizard can now flash a blank pedal." -ForegroundColor Green
Write-Host "Re-run this script when you update CircuitPython, the bundle, or the firmware." -ForegroundColor Green
