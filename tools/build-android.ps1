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
    param([ValidateSet("public")][string]$Name)

    # Recursive removal is permitted only for this exact child of the
    # generated Android assets directory.  Resolve lexically even before the
    # destination exists, then reject junction/reparse-point escapes.
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
# The Android app uses none of the desktop resources (update, installer, pi),
# so the frontend is the only generated asset tree.
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

# ---------- 2. Rust ----------
if (-not $SkipRust) {
    Write-Host "[2/4] Compiling Rust (aarch64-linux-android) ..." -ForegroundColor Yellow
    Push-Location $tauriDir
    try {
        # Touch dist to force tauri-build to re-run asset embedding
        $distIndex = Join-Path $editorDir "dist\index.html"
        if (Test-Path $distIndex) { (Get-Item $distIndex).LastWriteTime = Get-Date }

        Invoke-NativeTool { & $cargoExe build --release --target aarch64-linux-android }
        if ($LASTEXITCODE -ne 0) { throw "Cargo build failed" }
    } finally { Pop-Location }
} else {
    Write-Host "[2/4] Skipping Rust build." -ForegroundColor DarkGray
    if (-not (Test-Path -LiteralPath $soSource -PathType Leaf)) {
        throw "-SkipRust requires a previous Android Rust build. Run once without -SkipRust."
    }
}

# ---------- 3. Copy .so ----------
Write-Host "[3/4] Copying .so to jniLibs ..." -ForegroundColor Yellow
if (-not (Test-Path $soSource)) { throw "Missing: $soSource" }
New-Item -ItemType Directory -Force -Path $soDestDir | Out-Null
Copy-Item -Force $soSource $soDest

# Direct Cargo + Gradle builds do not regenerate Tauri's Android version
# properties. Read the same tracked identities as CI so a fresh APK cannot
# silently retain an older versionName or a machine-local versionCode.
$tauriConfig = Get-Content -LiteralPath (Join-Path $tauriDir "tauri.conf.json") -Raw | ConvertFrom-Json
$androidVersionName = [string]$tauriConfig.version
$androidVersionCode = (Get-Content -LiteralPath (Join-Path $tauriDir "android-version-code.txt") -Raw).Trim()
if ($androidVersionName -notmatch '^\d+\.\d+\.\d+$' -or
    $androidVersionCode -notmatch '^[1-9]\d*$') {
    throw "Invalid canonical Android version: $androidVersionName ($androidVersionCode)"
}
$androidProperties = "// THIS IS AN AUTOGENERATED FILE. DO NOT EDIT THIS FILE DIRECTLY.`ntauri.android.versionName=$androidVersionName`ntauri.android.versionCode=$androidVersionCode`n"
[IO.File]::WriteAllText((Join-Path $genDir "app\tauri.properties"), $androidProperties, [Text.UTF8Encoding]::new($false))
Write-Host "[version] Android $androidVersionName (code $androidVersionCode)" -ForegroundColor Yellow

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

# Check the packaged manifest, not only the generated input properties.
$aaptExe = Join-Path $env:ANDROID_HOME "build-tools\35.0.0\aapt.exe"
$apkBadging = Invoke-NativeTool { & $aaptExe dump badging $apkUnsigned }
if ($LASTEXITCODE -ne 0) { throw "APK manifest inspection failed" }
$expectedPackage = [regex]::Escape([string]$tauriConfig.identifier)
$expectedVersion = [regex]::Escape($androidVersionName)
if (($apkBadging -join "`n") -notmatch "(?m)^package: name='$expectedPackage' versionCode='$androidVersionCode' versionName='$expectedVersion'(?: |$)") {
    throw "Packaged Android identity does not match $($tauriConfig.identifier) $androidVersionName (code $androidVersionCode)"
}
Write-Host "[version] Packaged Android identity verified." -ForegroundColor Green

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
