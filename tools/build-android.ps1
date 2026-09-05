<#
.SYNOPSIS
  Build the Bosun Android APK (arm64 release, debug-signed).

.DESCRIPTION
  One-command build for the Bosun editor Android APK.  The script:
    1. Builds the Svelte frontend (Vite)
    2. Compiles the Rust native library for aarch64-linux-android
    3. Copies the .so into jniLibs (workaround for Windows symlink
       requirement -- Developer Mode is NOT needed)
    4. Assembles the release APK via Gradle
    5. Signs the APK with the Android debug keystore

  Output: bosun-debug.apk in the project root.

.PARAMETER Deploy
  If set, also runs "adb install -r" on the first attached device.

.PARAMETER SkipFrontend
  Skip the Vite build step (use when only Rust or Kotlin changed).

.PARAMETER SkipRust
  Skip the Cargo build step (use when dist or Kotlin changed, .so unchanged).

.EXAMPLE
  .\tools\build-android.ps1
  .\tools\build-android.ps1 -Deploy
  .\tools\build-android.ps1 -SkipFrontend -Deploy
#>

param(
    [switch]$Deploy,
    [switch]$SkipFrontend,
    [switch]$SkipRust
)

$ErrorActionPreference = "Stop"

# PowerShell 5.1 promotes EACH LINE a native tool writes to stderr into a
# terminating error under $ErrorActionPreference = "Stop", even when the
# tool's own exit code is 0 - a routine lint warning (Vite/svelte-check,
# Gradle deprecation notices, ...) aborts the whole script before the
# explicit $LASTEXITCODE checks below ever run (found 2026-08-16: a single
# a11y warning in App.svelte silently killed the Android build with no
# real failure). Run native tools through this wrapper - it drops to
# "Continue" only for the duration of that one call, so stderr text is
# just printed instead of thrown, and the $LASTEXITCODE check right after
# each call remains the actual source of truth for success/failure.
function Invoke-NativeTool {
    param([scriptblock]$Command)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Command } finally { $ErrorActionPreference = $prev }
}

$projectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$editorDir  = Join-Path $projectRoot "editor"
$tauriDir   = Join-Path $editorDir "src-tauri"
$genDir     = Join-Path $tauriDir "gen\android"
$resources  = Join-Path $tauriDir "resources"
$soSource   = Join-Path $tauriDir "target\aarch64-linux-android\release\libbosun_editor_lib.so"
$soDestDir  = Join-Path $genDir "app\src\main\jniLibs\arm64-v8a"
$soDest     = Join-Path $soDestDir "libbosun_editor_lib.so"
$resourceDigestDir = Join-Path $tauriDir "target\bosun-resource-sync"
$resourceDigestBefore = Join-Path $resourceDigestDir "android-before.sha256"
$resourceDigestAfter  = Join-Path $resourceDigestDir "android-after.sha256"
$resourceDigestFinal  = Join-Path $resourceDigestDir "android-final.sha256"
$resourceBuildStamp   = "$soSource.resources.sha256"
$resourceSyncScript   = Join-Path $projectRoot "tools\sync_firmware_resources.py"
$resourceVerifyScript = Join-Path $projectRoot "tools\verify_firmware_package.py"
$vendorVerifyScript   = Join-Path $projectRoot "tools\provision_adafruit_bundle.py"

$apkUnsigned = Join-Path $genDir "app\build\outputs\apk\arm64\release\app-arm64-release-unsigned.apk"
$apkOut      = Join-Path $projectRoot "bosun-debug.apk"

$cargoExe    = Join-Path $env:USERPROFILE ".cargo\bin\cargo.exe"
$pythonExe   = (Get-Command python -ErrorAction Stop).Source
$adbExe      = Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools\adb.exe"
if (-not (Test-Path $adbExe)) {
    $adbExe = "C:\development\Android\Sdk\platform-tools\adb.exe"
}

if (-not $env:ANDROID_HOME) { $env:ANDROID_HOME = "C:\development\Android\Sdk" }
if (-not $env:JAVA_HOME)    { $env:JAVA_HOME    = "C:\Program Files\Android\Android Studio\jbr" }

# ---------- NDK linker for Rust ----------
$ndkBin = Join-Path $env:ANDROID_HOME "ndk\30.0.15729638\toolchains\llvm\prebuilt\windows-x86_64\bin"
$env:CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER = Join-Path $ndkBin "aarch64-linux-android21-clang.cmd"

# Ensure cargo is on PATH
$env:PATH = "$(Split-Path $cargoExe);$env:PATH"

Write-Host "=== Bosun Android Build ===" -ForegroundColor Cyan

function Sync-FirmwareResources {
    param([string]$DigestFile, [switch]$Check)
    $syncArgs = @($resourceSyncScript, "--repo-root", $projectRoot, "--digest-file", $DigestFile)
    if ($Check) { $syncArgs += "--check" }
    Invoke-NativeTool { & $pythonExe @syncArgs }
    if ($LASTEXITCODE -ne 0) { throw "Firmware resource sync failed" }
}

function Invoke-FirmwarePackageVerification {
    param(
        [string]$Directory,
        [string]$Archive
    )
    $verifyArgs = @($resourceVerifyScript, "--resources", $resources)
    if ($Directory) {
        $verifyArgs += @("--directory", $Directory)
    } elseif ($Archive) {
        $verifyArgs += @("--archive", $Archive, "--prefix", "assets")
    } else {
        throw "Firmware package verification requires a directory or archive"
    }
    Invoke-NativeTool { & $pythonExe @verifyArgs }
    if ($LASTEXITCODE -ne 0) { throw "Packaged firmware verification failed" }
}

function Assert-NoReparsePathComponents {
    param([string]$Root, [string]$Target)

    # A lexical containment check alone is insufficient on Windows: an
    # existing junction in the middle of the path can redirect a later
    # recursive removal outside the generated Android tree.
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $targetFull = [IO.Path]::GetFullPath($Target)
    $rootPrefix = $rootFull + [IO.Path]::DirectorySeparatorChar
    if ($targetFull -ne $rootFull -and
        -not $targetFull.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path is outside its trusted root: $targetFull"
    }
    $current = $rootFull
    $relative = $targetFull.Substring($rootFull.Length).TrimStart(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $parts = $relative.Split(
        @([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar),
        [StringSplitOptions]::RemoveEmptyEntries
    )
    foreach ($part in @(".") + $parts) {
        if ($part -ne ".") { $current = Join-Path $current $part }
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -Force -LiteralPath $current
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Android asset path must not cross a link or junction: $current"
            }
        }
    }
}

function Get-SafeAndroidAssetDestination {
    param([ValidateSet("public", "firmware", "lib", "circuitpython.uf2")][string]$Name)

    # Recursive removal is permitted only for these three exact children of
    # the generated Android assets directory.  Resolve lexically even before
    # the destination exists, then reject junction/reparse-point escapes.
    $assetsFull = [IO.Path]::GetFullPath($androidAssets)
    $genFull = [IO.Path]::GetFullPath($genDir)
    $genPrefix = $genFull.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $assetsFull.StartsWith($genPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe Android assets root outside generated project: $assetsFull"
    }
    Assert-NoReparsePathComponents -Root $tauriDir -Target $assetsFull
    if (Test-Path -LiteralPath $assetsFull) {
        $assetsItem = Get-Item -Force -LiteralPath $assetsFull
        if (($assetsItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Android assets root must not be a link or junction: $assetsFull"
        }
    }
    $destination = [IO.Path]::GetFullPath((Join-Path $assetsFull $Name))
    $assetsPrefix = $assetsFull.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $destination.StartsWith($assetsPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($destination) -ne $Name) {
        throw "Unsafe Android asset destination: $destination"
    }
    Assert-NoReparsePathComponents -Root $tauriDir -Target $destination
    if (Test-Path -LiteralPath $destination) {
        $destinationItem = Get-Item -Force -LiteralPath $destination
        if (($destinationItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Android asset destination must not be a link or junction: $destination"
        }
    }
    return $destination
}

# Refresh and hash the canonical Tauri resources before any build/reuse
# decision. Android packages them as generated APK assets (staged explicitly
# below), not inside the Rust .so. The helper mirrors Bosun firmware, preserves
# vendored CircuitPython libs, rejects links and verifies every copied byte.
New-Item -ItemType Directory -Force -Path $resourceDigestDir | Out-Null
Write-Host "[resources] Syncing canonical firmware resources ..." -ForegroundColor Yellow
Sync-FirmwareResources -DigestFile $resourceDigestBefore
Invoke-NativeTool { & $pythonExe $vendorVerifyScript --destination (Join-Path $resources "lib") --check }
if ($LASTEXITCODE -ne 0) { throw "Pinned Adafruit vendor verification failed" }
$resourceDigest = (Get-Content -LiteralPath $resourceDigestBefore -Raw).Trim()

# ---------- 1. Frontend ----------
if (-not $SkipFrontend) {
    Write-Host "[1/4] Building frontend (Vite) ..." -ForegroundColor Yellow
    Push-Location $editorDir
    try {
        Invoke-NativeTool { npm run build }
        if ($LASTEXITCODE -ne 0) { throw "Vite build failed" }
    } finally { Pop-Location }
} else {
    Write-Host "[1/4] Skipping frontend build." -ForegroundColor DarkGray
}

# Sync the fresh dist/ into the Android assets.  CRITICAL: wipe first -
# stale hashed bundles survive otherwise and index.html keeps pointing
# at them, so the WebView loads an old cached JS bundle (2026-08-13).
$distDir = Join-Path $editorDir "dist"
$androidAssets = Join-Path $genDir "app\src\main\assets"
Assert-NoReparsePathComponents -Root $tauriDir -Target $androidAssets
New-Item -ItemType Directory -Force -Path $androidAssets | Out-Null
$androidPublicAssets = Get-SafeAndroidAssetDestination -Name "public"
if (Test-Path (Join-Path $distDir "index.html") -PathType Leaf) {
    Write-Host "[assets] Syncing dist -> android assets ..." -ForegroundColor Yellow
    if (Test-Path -LiteralPath $androidPublicAssets) {
        Remove-Item -Recurse -Force -LiteralPath $androidPublicAssets
    }
    New-Item -ItemType Directory -Force -Path $androidPublicAssets | Out-Null
    Copy-Item -Recurse -Force (Join-Path $distDir "*") $androidPublicAssets
} else {
    throw "Missing frontend dist/index.html; refusing to package stale Android assets. Run without -SkipFrontend or build editor/dist first."
}

# Tauri resolves BaseDirectory::Resource from Android's APK assets. A direct
# Cargo + Gradle build does not refresh these generated copies (unlike the
# full `tauri android build` flow), so stale files can otherwise survive for
# months even though editor/src-tauri/resources was correctly synchronized.
Write-Host "[assets] Staging verified firmware resources ..." -ForegroundColor Yellow
foreach ($tree in @("firmware", "lib")) {
    $source = Join-Path $resources $tree
    if (-not (Test-Path -LiteralPath $source -PathType Container)) {
        throw "Missing firmware resource tree: $source"
    }
    $destination = Get-SafeAndroidAssetDestination -Name $tree
    if (Test-Path -LiteralPath $destination) {
        Remove-Item -Recurse -Force -LiteralPath $destination
    }
    Copy-Item -Recurse -Force -LiteralPath $source -Destination $destination
}
$uf2Source = Join-Path $resources "circuitpython.uf2"
if (-not (Test-Path -LiteralPath $uf2Source -PathType Leaf)) {
    throw "Missing CircuitPython resource: $uf2Source"
}
$uf2Destination = Get-SafeAndroidAssetDestination -Name "circuitpython.uf2"
Copy-Item -Force -LiteralPath $uf2Source -Destination $uf2Destination
Invoke-FirmwarePackageVerification -Directory $androidAssets

# ---------- 2. Rust ----------
if (-not $SkipRust) {
    Write-Host "[2/4] Compiling Rust (aarch64-linux-android) ..." -ForegroundColor Yellow
    Push-Location $tauriDir
    try {
        # Touch dist to force tauri-build to re-run asset embedding
        $distIndex = Join-Path $editorDir "dist\index.html"
        if (Test-Path $distIndex) { (Get-Item $distIndex).LastWriteTime = Get-Date }

        # Keep the Rust build provenance tied to the resource digest too.
        # Android reads these files from APK assets (staged and independently
        # verified above), but a full build must not silently reuse outputs
        # produced against a different synchronized resource generation.
        (Get-Item (Join-Path $tauriDir "build.rs")).LastWriteTime = Get-Date

        Invoke-NativeTool { & $cargoExe build --release --target aarch64-linux-android }
        if ($LASTEXITCODE -ne 0) { throw "Cargo build failed" }
    } finally { Pop-Location }

    # Record which synchronized resource generation accompanied this Rust
    # build. This is only a conservative -SkipRust reuse gate; packaged-byte
    # proof comes from the staging/APK verifier, never from the .so stamp.
    Sync-FirmwareResources -DigestFile $resourceDigestAfter -Check
    $resourceDigestAfterBuild = (Get-Content -LiteralPath $resourceDigestAfter -Raw).Trim()
    if ($resourceDigestAfterBuild -ne $resourceDigest) {
        throw "Firmware resources changed during the Rust build; refusing mixed build provenance. Re-run the build."
    }
    Copy-Item -Force -LiteralPath $resourceDigestAfter -Destination $resourceBuildStamp
} else {
    Write-Host "[2/4] Skipping Rust build." -ForegroundColor DarkGray
    if (-not (Test-Path -LiteralPath $soSource -PathType Leaf) -or
        -not (Test-Path -LiteralPath $resourceBuildStamp -PathType Leaf)) {
        throw "-SkipRust requires a previously verified Android Rust build. Run once without -SkipRust."
    }
    Sync-FirmwareResources -DigestFile $resourceDigestAfter -Check
    $resourceDigestAfterBuild = (Get-Content -LiteralPath $resourceDigestAfter -Raw).Trim()
    $stampedDigest = (Get-Content -LiteralPath $resourceBuildStamp -Raw).Trim()
    if ($resourceDigestAfterBuild -ne $resourceDigest -or $stampedDigest -ne $resourceDigest) {
        throw "-SkipRust would reuse Rust output from a different firmware resource generation. Run without -SkipRust."
    }
}

# ---------- 3. Copy .so ----------
Write-Host "[3/4] Copying .so to jniLibs ..." -ForegroundColor Yellow
if (-not (Test-Path $soSource)) { throw "Missing: $soSource" }
New-Item -ItemType Directory -Force -Path $soDestDir | Out-Null
Copy-Item -Force $soSource $soDest

# ---------- 4. Gradle ----------
Write-Host "[4/4] Assembling APK (Gradle) ..." -ForegroundColor Yellow
Push-Location $genDir
try {
    # Skip Rust build tasks -- we already compiled above.
    # The full list covers all 4 architectures.
    $gradleArgs = @(
        "assembleRelease",
        "-x", "rustBuildArm64Release",
        "-x", "rustBuildArmRelease",
        "-x", "rustBuildX86Release",
        "-x", "rustBuildX86_64Release"
    )
    Invoke-NativeTool { & .\gradlew @gradleArgs }
    if ($LASTEXITCODE -ne 0) { throw "Gradle build failed" }
} finally { Pop-Location }

# ---------- 5. Sign ----------
Write-Host "Signing APK ..." -ForegroundColor Yellow
$apkSigner = Join-Path $env:ANDROID_HOME "build-tools\35.0.0\apksigner.bat"
$keystore  = Join-Path $env:USERPROFILE ".android\debug.keystore"
Invoke-NativeTool { & $apkSigner sign --ks $keystore --ks-pass pass:android --ks-key-alias androiddebugkey --key-pass pass:android $apkUnsigned }
if ($LASTEXITCODE -ne 0) { throw "Signing failed" }
Invoke-NativeTool { & $apkSigner verify --verbose --print-certs $apkUnsigned }
if ($LASTEXITCODE -ne 0) { throw "APK signature verification failed" }

# Publish only if the canonical source still matches the exact resources
# recorded for the Rust library. This closes the longer Gradle/signing window.
Sync-FirmwareResources -DigestFile $resourceDigestFinal -Check
$resourceDigestAtPublish = (Get-Content -LiteralPath $resourceDigestFinal -Raw).Trim()
$stampedDigest = (Get-Content -LiteralPath $resourceBuildStamp -Raw).Trim()
if ($resourceDigestAtPublish -ne $resourceDigest -or $stampedDigest -ne $resourceDigest) {
    throw "Firmware resources changed during APK assembly; refusing to publish a stale APK. Re-run the build."
}

# Inspect the bytes Gradle actually placed in the signed APK. Source/resource
# digests cannot prove the generated assets were refreshed or that Gradle did
# not reuse stale inputs from its own cache.
Invoke-FirmwarePackageVerification -Archive $apkUnsigned

Copy-Item -Force $apkUnsigned $apkOut

Write-Host "`nBuild complete: $apkOut" -ForegroundColor Green

# ---------- Deploy ----------
if ($Deploy) {
    Write-Host "Deploying to device ..." -ForegroundColor Yellow
    Invoke-NativeTool { & $adbExe install -r $apkOut }
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Installed successfully." -ForegroundColor Green
    } else {
        throw "ADB install failed ($LASTEXITCODE)"
    }
}
