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

.PARAMETER OutputSuffix
  Optional suffix for the portable folder and ZIP, to keep builds side by side.

.EXAMPLE
  pwsh -File tools\package-portable.ps1
  pwsh -File tools\package-portable.ps1 -SkipBuild
#>

[CmdletBinding()]
param(
    [switch] $SkipBuild,
    [ValidateSet("release", "debug")]
    [string] $Configuration = "release",
    [ValidatePattern('^[a-zA-Z0-9-]*$')]
    [string] $OutputSuffix = ""
)

$ErrorActionPreference = "Stop"

# PowerShell 5.1 promotes EACH LINE a native tool writes to stderr into a
# terminating error under $ErrorActionPreference = "Stop", even when the
# tool's own exit code is 0 - a routine lint warning (Vite/svelte-check,
# ...) can abort the whole script before the $LASTEXITCODE check below
# ever runs (found 2026-08-16 chasing the same issue in build-android.ps1).
# Run native tools through this wrapper - it drops to "Continue" only for
# the duration of that one call, so stderr text is just printed instead of
# thrown, and $LASTEXITCODE remains the actual source of truth.
function Invoke-NativeTool {
    param([scriptblock]$Command)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Command } finally { $ErrorActionPreference = $prev }
}

$repoRoot  = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$editor    = Join-Path $repoRoot "editor"
$tauriDir  = Join-Path $editor "src-tauri"
$resources = Join-Path $tauriDir "resources"
$resourceSyncScript = Join-Path $repoRoot "tools\sync_firmware_resources.py"
$resourceVerifyScript = Join-Path $repoRoot "tools\verify_firmware_package.py"
$vendorVerifyScript = Join-Path $repoRoot "tools\provision_adafruit_bundle.py"
$resourceDigestDir = Join-Path $tauriDir "target\bosun-resource-sync"
$resourceDigestBefore = Join-Path $resourceDigestDir "portable-before.sha256"
$resourceDigestAfter  = Join-Path $resourceDigestDir "portable-after.sha256"
$resourceDigestFinal  = Join-Path $resourceDigestDir "portable-final.sha256"
$cargoExe  = Join-Path $env:USERPROFILE ".cargo\bin\cargo.exe"
$pythonExe = (Get-Command python -ErrorAction Stop).Source
if (-not (Test-Path $cargoExe)) {
    throw "Cargo not found at $cargoExe. Install the Rust toolchain first."
}
# npx/tauri launches `cargo` by name, so make the per-user rustup bin visible
# even in shells where the installer has not updated PATH yet.
$env:PATH = "$(Split-Path $cargoExe);$env:PATH"

function Sync-FirmwareResources {
    param([string]$DigestFile, [switch]$Check)
    $syncArgs = @($resourceSyncScript, "--repo-root", $repoRoot, "--digest-file", $DigestFile)
    if ($Check) { $syncArgs += "--check" }
    Invoke-NativeTool { & $pythonExe @syncArgs }
    if ($LASTEXITCODE -ne 0) { throw "Firmware resource sync failed" }
}

function Invoke-FirmwarePackageVerification {
    param([string]$Directory, [string]$Archive, [string]$Prefix)
    $verifyArgs = @($resourceVerifyScript, "--resources", $resources)
    if ($Directory) {
        $verifyArgs += @("--directory", $Directory)
    } elseif ($Archive) {
        $verifyArgs += @("--archive", $Archive, "--prefix", $Prefix)
    } else {
        throw "Firmware package verification requires a directory or archive"
    }
    Invoke-NativeTool { & $pythonExe @verifyArgs }
    if ($LASTEXITCODE -ne 0) { throw "Packaged firmware verification failed" }
}

# Always derive distributable resources from the canonical firmware tree,
# including -SkipBuild packages. The verified digest detects an edit racing
# the build so a mixed/stale archive can never be emitted.
New-Item -ItemType Directory -Force -Path $resourceDigestDir | Out-Null
Write-Host "[resources] Syncing canonical firmware resources ..."
Sync-FirmwareResources -DigestFile $resourceDigestBefore
Invoke-NativeTool { & $pythonExe $vendorVerifyScript --destination (Join-Path $resources "lib") --check }
if ($LASTEXITCODE -ne 0) { throw "Pinned Adafruit vendor verification failed" }
$resourceDigest = (Get-Content -LiteralPath $resourceDigestBefore -Raw).Trim()
$nativePackage = Join-Path $resources 'update/bosun-update.zip'
$nativeDigest = $null
if (Test-Path -LiteralPath $nativePackage) {
    Invoke-NativeTool { & $pythonExe (Join-Path $repoRoot 'tools/package-native-update.py') --verify $nativePackage }
    if ($LASTEXITCODE -ne 0) { throw 'Native update package validation failed' }
    $nativeDigest = (Get-FileHash -Algorithm SHA256 -LiteralPath $nativePackage).Hash
}

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
        # Resource contents are not always part of Cargo's input fingerprint.
        # build.rs is an implicit build-script dependency, so touching it
        # forces Tauri to regenerate the resource embedding/copy metadata.
        (Get-Item -LiteralPath (Join-Path $tauriDir "build.rs")).LastWriteTime = Get-Date
        # Call the tauri CLI through npx, NOT `npm run tauri -- ...`: the
        # nvm4w npm shim drops args after `--`, so `--no-bundle` was being
        # lost and the build produced an NSIS installer we don't ship.
        if ($Configuration -eq "debug") {
            Invoke-NativeTool { npx tauri build --no-bundle --debug }
        } else {
            Invoke-NativeTool { npx tauri build --no-bundle }
        }
        if ($LASTEXITCODE -ne 0) { throw "tauri build failed (exit $LASTEXITCODE)" }
    } finally {
        Pop-Location
    }
}

Sync-FirmwareResources -DigestFile $resourceDigestAfter -Check
$resourceDigestAfterBuild = (Get-Content -LiteralPath $resourceDigestAfter -Raw).Trim()
if ($resourceDigestAfterBuild -ne $resourceDigest) {
    throw "Firmware resources changed during packaging; refusing to emit a stale/mixed portable archive. Re-run the build."
}

$exeSrc = Join-Path $tauriDir "target\$Configuration\$exeName"
if (-not (Test-Path $exeSrc)) {
    throw "Executable not found at $exeSrc. Run without -SkipBuild first."
}

# ---------- 2. Stage the portable layout ----------

$stageName = "$product-$version-portable-x64"
if ($OutputSuffix) { $stageName += "-$OutputSuffix" }
$distDir   = Join-Path $repoRoot "dist"
if ($stageName -ne [IO.Path]::GetFileName($stageName) -or
    $stageName.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0) {
    throw "Unsafe portable stage name derived from product/version: $stageName"
}
New-Item -ItemType Directory -Force -Path $distDir | Out-Null
$distItem = Get-Item -Force -LiteralPath $distDir
if (($distItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "Portable dist directory must not be a link or junction: $distDir"
}
$distFull = [IO.Path]::GetFullPath($distDir)
$distPrefix = $distFull.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar

function Get-SafeDistChildPath {
    param([string]$Name)
    if ([string]::IsNullOrWhiteSpace($Name) -or
        $Name -ne [IO.Path]::GetFileName($Name) -or
        $Name.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0) {
        throw "Unsafe portable output name: $Name"
    }
    $destination = [IO.Path]::GetFullPath((Join-Path $distFull $Name))
    if (-not $destination.StartsWith($distPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Portable output escaped dist: $destination"
    }
    if (Test-Path -LiteralPath $destination) {
        $item = Get-Item -Force -LiteralPath $destination
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Portable output must not be a link or junction: $destination"
        }
    }
    return $destination
}

$stageDir = Get-SafeDistChildPath -Name $stageName
if (Test-Path -LiteralPath $stageDir) {
    Remove-Item -Recurse -Force -LiteralPath $stageDir
}
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

foreach ($tree in @("firmware", "lib", "update")) {
    $src = Join-Path $resources $tree
    if (-not (Test-Path $src)) {
        throw "Missing resource '$tree' at $src. Run tools\download-assets.ps1 first."
    }
    Copy-Item -Recurse $src (Join-Path $stageDir $tree)
    Write-Host "[ok  ] $tree\"
}

# License for the small RP2040 flash-identification helper embedded in Bosun.
$licenseDir = Join-Path $stageDir "licenses"
New-Item -ItemType Directory -Force -Path $licenseDir | Out-Null
Copy-Item -LiteralPath (Join-Path $tauriDir "vendor/picotool/LICENSE.TXT") -Destination (Join-Path $licenseDir "picotool.txt")

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

Keep $product.exe together with circuitpython.uf2, firmware\, lib\ and update\: the
firmware installer reads them from beside the executable.

Native firmware can be updated via Maintenance > Install firmware (USB),
or through a configured Raspberry Pi. Direct USB requires the Captain's
PICOBOOT interface to use WinUSB. Bosun keeps its recovery backup in the
application data directory and shows its location in the update window.
"@
Set-Content -Path (Join-Path $stageDir "README.txt") -Value $readme -Encoding utf8

# Verify every staged firmware/lib/UF2 byte and reject stale compiled siblings
# before spending time compressing. Bosun.exe and README.txt are intentionally
# outside this resource-only inventory.
Invoke-FirmwarePackageVerification -Directory $stageDir
if ($nativeDigest -and (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $stageDir 'update/bosun-update.zip')).Hash -ne $nativeDigest) {
    throw 'Native update package changed while building the portable app'
}

# ---------- 3. Zip it ----------

$zipPath = Get-SafeDistChildPath -Name "$stageName.zip"
$zipTemp = Get-SafeDistChildPath -Name (".{0}-{1}.tmp.zip" -f $stageName, [guid]::NewGuid().ToString("N"))
try {
    Compress-Archive -Path $stageDir -DestinationPath $zipTemp

    # Inspect the archive itself, not only its source directory: this catches
    # missing, duplicated or stale entries introduced during compression.
    Invoke-FirmwarePackageVerification -Archive $zipTemp -Prefix $stageName
    if ($nativeDigest) {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [IO.Compression.ZipFile]::OpenRead($zipTemp)
        try {
            $entries = @($archive.Entries | Where-Object { $_.FullName.Replace('\', '/') -ceq "$stageName/update/bosun-update.zip" })
            if ($entries.Count -ne 1) { throw 'Portable archive is missing the unique native update package' }
            $stream = $entries[0].Open()
            $sha = [Security.Cryptography.SHA256]::Create()
            try {
                $actualNativeDigest = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '')
            } finally { $sha.Dispose(); $stream.Dispose() }
            if ($actualNativeDigest -ne $nativeDigest) { throw 'Portable archive native update package checksum mismatch' }
        } finally { $archive.Dispose() }
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $nativePackage).Hash -ne $nativeDigest) {
            throw 'Native update package changed during compression'
        }
    }

    # Do not replace a previous known-good archive if the firmware changed
    # while this package was being staged/compressed.
    Sync-FirmwareResources -DigestFile $resourceDigestFinal -Check
    $resourceDigestAtPublish = (Get-Content -LiteralPath $resourceDigestFinal -Raw).Trim()
    if ($resourceDigestAtPublish -ne $resourceDigest) {
        throw "Firmware resources changed while compressing; refusing to publish a stale/mixed portable archive. Re-run the build."
    }
    Move-Item -Force -LiteralPath $zipTemp -Destination $zipPath
} finally {
    if (Test-Path -LiteralPath $zipTemp) {
        Remove-Item -Force -LiteralPath $zipTemp
    }
}

Write-Host ""
Write-Host "Portable build ready:" -ForegroundColor Green
Write-Host "  $zipPath" -ForegroundColor Green
Write-Host "  (staged folder: $stageDir)" -ForegroundColor Green
