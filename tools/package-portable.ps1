<#
.SYNOPSIS
  Build the Bosun editor as a portable (no-install) ZIP.

.DESCRIPTION
  Produces a self-contained folder - Bosun.exe plus the installer assets the
  Pedal Setup wizard needs (circuitpython.uf2, firmware\, lib\) - and zips it.
  The recipient extracts the ZIP anywhere and double-clicks Bosun.exe. No
  installer, no admin rights, no registry writes.

  Tauri resolves BaseDirectory::Resource to the directory holding the
  executable, so the three asset entries must sit next to Bosun.exe. That is
  the same layout cargo already lays out under target\<config>\, which is how
  the wizard works during development.

  WebView2 ships with Windows 11. On older Windows the free Microsoft WebView2
  runtime must be installed (the window stays blank otherwise).

.PARAMETER SkipBuild
  Reuse the existing target\<config> build instead of running tauri build.

.PARAMETER Configuration
  release (default) or debug - which target directory to package from.

.EXAMPLE
  pwsh -File tools\package-portable.ps1
  pwsh -File tools\package-portable.ps1 -SkipBuild
#>

[CmdletBinding()]
param(
    [switch] $SkipBuild,
    [ValidateSet("release", "debug")]
    [string] $Configuration = "release"
)

$ErrorActionPreference = "Stop"

$repoRoot  = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$editor    = Join-Path $repoRoot "editor"
$tauriDir  = Join-Path $editor "src-tauri"
$resources = Join-Path $tauriDir "resources"

# Version + product name come from tauri.conf.json (single source of truth).
$conf    = Get-Content (Join-Path $tauriDir "tauri.conf.json") -Raw | ConvertFrom-Json
$version = $conf.version
$product = $conf.productName          # "Bosun"
$exeName = "bosun-editor.exe"         # cargo binary name

# ---------- 1. Build (unless reusing an existing one) ----------

if (-not $SkipBuild) {
    Write-Host "[build] tauri build --no-bundle ($Configuration)"
    Push-Location $editor
    try {
        # Call the tauri CLI through npx, NOT `npm run tauri -- ...`: the
        # nvm4w npm shim drops args after `--`, so `--no-bundle` was being
        # lost and the build produced an NSIS installer we don't ship.
        if ($Configuration -eq "debug") {
            npx tauri build --no-bundle --debug
        } else {
            npx tauri build --no-bundle
        }
        if ($LASTEXITCODE -ne 0) { throw "tauri build failed (exit $LASTEXITCODE)" }
    } finally {
        Pop-Location
    }
}

$exeSrc = Join-Path $tauriDir "target\$Configuration\$exeName"
if (-not (Test-Path $exeSrc)) {
    throw "Executable not found at $exeSrc. Run without -SkipBuild first."
}

# ---------- 2. Stage the portable layout ----------

$stageName = "$product-$version-portable-x64"
$distDir   = Join-Path $repoRoot "dist"
$stageDir  = Join-Path $distDir $stageName
if (Test-Path $stageDir) { Remove-Item -Recurse -Force $stageDir }
New-Item -ItemType Directory -Force -Path $stageDir | Out-Null

# Executable, renamed to the product name for a tidy portable folder.
Copy-Item $exeSrc (Join-Path $stageDir "$product.exe")
Write-Host "[ok  ] $product.exe"

# Installer assets, side by side with the exe.
$uf2 = Join-Path $resources "circuitpython.uf2"
if (-not (Test-Path $uf2)) {
    throw "Missing circuitpython.uf2 at $uf2. Run tools\download-assets.ps1 first."
}
Copy-Item $uf2 $stageDir
Write-Host "[ok  ] circuitpython.uf2"

foreach ($tree in @("firmware", "lib")) {
    $src = Join-Path $resources $tree
    if (-not (Test-Path $src)) {
        throw "Missing resource '$tree' at $src. Run tools\download-assets.ps1 first."
    }
    Copy-Item -Recurse $src (Join-Path $stageDir $tree)
    Write-Host "[ok  ] $tree\"
}

# Drop python caches the device installer skips anyway - keeps the ZIP clean.
Get-ChildItem -Path $stageDir -Recurse -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force
Get-ChildItem -Path $stageDir -Recurse -File -Filter "*.pyc" |
    Remove-Item -Force

# A short note so the recipient knows it is extract-and-run.
$readme = @"
$product $version - portable build

1. Extract this folder anywhere (Desktop, a USB stick, wherever).
2. Double-click $product.exe. No installation, no admin rights.

Windows 11 already includes the WebView2 runtime. On older Windows, install
the free Microsoft WebView2 runtime if the window stays blank.

Keep $product.exe together with circuitpython.uf2, firmware\ and lib\: the
firmware installer reads them from beside the executable.
"@
Set-Content -Path (Join-Path $stageDir "README.txt") -Value $readme -Encoding utf8

# ---------- 3. Zip it ----------

$zipPath = Join-Path $distDir "$stageName.zip"
if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
Compress-Archive -Path $stageDir -DestinationPath $zipPath

Write-Host ""
Write-Host "Portable build ready:" -ForegroundColor Green
Write-Host "  $zipPath" -ForegroundColor Green
Write-Host "  (staged folder: $stageDir)" -ForegroundColor Green
