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
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$editorDir  = Join-Path $projectRoot "editor"
$tauriDir   = Join-Path $editorDir "src-tauri"
$genDir     = Join-Path $tauriDir "gen\android"
$soSource   = Join-Path $tauriDir "target\aarch64-linux-android\release\libbosun_editor_lib.so"
$soDestDir  = Join-Path $genDir "app\src\main\jniLibs\arm64-v8a"
$soDest     = Join-Path $soDestDir "libbosun_editor_lib.so"

$apkUnsigned = Join-Path $genDir "app\build\outputs\apk\arm64\release\app-arm64-release-unsigned.apk"
$apkOut      = Join-Path $projectRoot "bosun-debug.apk"

$cargoExe    = Join-Path $env:USERPROFILE ".cargo\bin\cargo.exe"
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

# ---------- 1. Frontend ----------
if (-not $SkipFrontend) {
    Write-Host "[1/4] Building frontend (Vite) ..." -ForegroundColor Yellow
    Push-Location $editorDir
    try {
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "Vite build failed" }
    } finally { Pop-Location }
} else {
    Write-Host "[1/4] Skipping frontend build." -ForegroundColor DarkGray
}

# Sync the fresh dist/ into the Android assets.  CRITICAL: wipe first -
# stale hashed bundles survive otherwise and index.html keeps pointing
# at them, so the WebView loads an old cached JS bundle (2026-08-13).
$distDir = Join-Path $editorDir "dist"
$androidAssets = Join-Path $genDir "app\src\main\assets\public"
if (Test-Path $distDir) {
    Write-Host "[assets] Syncing dist -> android assets ..." -ForegroundColor Yellow
    if (Test-Path $androidAssets) {
        Remove-Item -Recurse -Force $androidAssets
    }
    New-Item -ItemType Directory -Force -Path $androidAssets | Out-Null
    Copy-Item -Recurse -Force (Join-Path $distDir "*") $androidAssets
} else {
    Write-Host "[assets] No dist/ found - skipping sync." -ForegroundColor DarkYellow
}

# ---------- 2. Rust ----------
if (-not $SkipRust) {
    Write-Host "[2/4] Compiling Rust (aarch64-linux-android) ..." -ForegroundColor Yellow
    Push-Location $tauriDir
    try {
        # Touch dist to force tauri-build to re-run asset embedding
        $distIndex = Join-Path $editorDir "dist\index.html"
        if (Test-Path $distIndex) { (Get-Item $distIndex).LastWriteTime = Get-Date }

        # Touch build.rs too: Cargo only re-runs tauri_build's resource
        # embedding (resources/firmware, resources/lib, circuitpython.uf2)
        # when it thinks the build script's tracked inputs changed. Editing
        # file CONTENTS under resources/ without touching anything Cargo
        # fingerprints does not qualify, so a version bump could sit
        # unbaked in the .so indefinitely (found 2026-08-15: app reported
        # bundled firmware 0.5.15 after multiple bumps up to 0.5.20 had
        # already synced resources/firmware on disk). build.rs itself is
        # always an implicit dependency of its own build script, so
        # touching it unconditionally forces a fresh embed every build.
        (Get-Item (Join-Path $tauriDir "build.rs")).LastWriteTime = Get-Date

        & $cargoExe build --release --target aarch64-linux-android
        if ($LASTEXITCODE -ne 0) { throw "Cargo build failed" }
    } finally { Pop-Location }
} else {
    Write-Host "[2/4] Skipping Rust build." -ForegroundColor DarkGray
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
    & .\gradlew @gradleArgs
    if ($LASTEXITCODE -ne 0) { throw "Gradle build failed" }
} finally { Pop-Location }

# ---------- 5. Sign ----------
Write-Host "Signing APK ..." -ForegroundColor Yellow
$apkSigner = Join-Path $env:ANDROID_HOME "build-tools\35.0.0\apksigner.bat"
$keystore  = Join-Path $env:USERPROFILE ".android\debug.keystore"
& $apkSigner sign --ks $keystore --ks-pass pass:android --ks-key-alias androiddebugkey --key-pass pass:android $apkUnsigned
if ($LASTEXITCODE -ne 0) { throw "Signing failed" }

Copy-Item -Force $apkUnsigned $apkOut

Write-Host "`nBuild complete: $apkOut" -ForegroundColor Green

# ---------- Deploy ----------
if ($Deploy) {
    Write-Host "Deploying to device ..." -ForegroundColor Yellow
    & $adbExe install -r $apkOut
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Installed successfully." -ForegroundColor Green
    }
}
