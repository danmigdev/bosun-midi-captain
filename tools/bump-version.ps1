<#
.SYNOPSIS
  Bump the project version everywhere it needs to match, in one call.

.DESCRIPTION
  Bosun keeps native C firmware and editor on the same semver. CircuitPython
  firmware is retired: its source version and bundled recovery copies remain
  frozen independently of the new release. Existing packaging still needs the
  legacy resource trees, so the verified resource sync is retained.

  This script:
    1. Aligns the editor and native firmware version fields,
       including the npm/Cargo lockfiles and RP2040 program metadata.
    2. Runs the same verified firmware-resource sync used by every package
       build (exact firmware mirror plus additive vendored-library tree).
    3. Prints a diff-style summary so you can eyeball what moved.

  Does NOT build or push anything - run package:portable / build-android.ps1
  / the firmware install flow yourself afterward, whichever platform(s)
  actually need a fresh build this round.

.PARAMETER Version
  The new version, e.g. "0.5.14". Must be plain X.Y.Z (no leading "v", no
  pre-release suffix - matches what compareVersions()/cmpVer() in the
  editor expect).

.EXAMPLE
  pwsh -File tools\bump-version.ps1 0.5.14
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string] $Version
)

$ErrorActionPreference = "Stop"

function Invoke-NativeTool {
    param([scriptblock]$Command)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Command } finally { $ErrorActionPreference = $previous }
}

if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Version must be plain X.Y.Z (got '$Version'). No leading 'v', no -rc/-scaffold suffix."
}

$repoRoot   = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$native     = Join-Path $repoRoot "firmware-native"
$editor     = Join-Path $repoRoot "editor"
$tauriDir   = Join-Path $editor "src-tauri"
$resources  = Join-Path $tauriDir "resources"
$syncScript = Join-Path $PSScriptRoot "sync_firmware_resources.py"
$pythonExe  = (Get-Command python -ErrorAction Stop).Source

# ---------- 1. Align canonical firmware/editor versions and lockfiles ----------

function Set-VersionLine {
    param([string] $Path, [string] $Pattern, [string] $Replacement, [int] $ExpectedMatches = 0)
    if (-not (Test-Path $Path)) { throw "Missing: $Path" }
    $content = Get-Content -Path $Path -Raw
    # Check the pattern actually matched BEFORE replacing - comparing
    # before/after content would false-positive as "not found" whenever the
    # new version happens to equal the old one (e.g. re-running the same
    # version to verify the sync, as this script's own smoke test does).
    if ($content -notmatch $Pattern) {
        throw "Version pattern not found in $Path - refusing to write (would silently no-op)."
    }
    if ($ExpectedMatches -gt 0 -and [regex]::Matches($content, $Pattern).Count -ne $ExpectedMatches) {
        throw "Expected $ExpectedMatches version fields in $Path - refusing a partial version update."
    }
    $updated = $content -replace $Pattern, $Replacement
    Set-Content -Path $Path -Value $updated -NoNewline
    Write-Host "[ok  ] $Path" -ForegroundColor Green
}

Write-Host "[1/3] Writing version $Version into maintained native/editor version fields" -ForegroundColor Yellow

Set-VersionLine `
    -Path (Join-Path $editor "package.json") `
    -Pattern '"version":\s*"\d+\.\d+\.\d+"' `
    -Replacement "`"version`": `"$Version`""

Set-VersionLine `
    -Path (Join-Path $tauriDir "tauri.conf.json") `
    -Pattern '"version":\s*"\d+\.\d+\.\d+"' `
    -Replacement "`"version`": `"$Version`""

# Anchored to line-start: the [package] version line always starts with
# "version", while a dependency's inline pin never does (it's
# `crate-name = { version = "...", ... }`). Without the anchor, a future
# dependency pinned to a full X.Y.Z (unlike today's "2"/"1"/"0.32" short
# specs) would get silently rewritten too.
Set-VersionLine `
    -Path (Join-Path $tauriDir "Cargo.toml") `
    -Pattern '(?m)^version = "\d+\.\d+\.\d+"' `
    -Replacement "version = `"$Version`""

Set-VersionLine `
    -Path (Join-Path $tauriDir "Cargo.lock") `
    -Pattern '(name = "bosun-editor"\r?\nversion = )"\d+\.\d+\.\d+"' `
    -Replacement "`${1}`"$Version`""

# npm doesn't rewrite package-lock.json's own version stamp just from
# editing package.json - only `npm install`/`npm ci` do, and this pipeline
# never runs those on a bump (package:portable and build-android.ps1 both
# reuse the existing node_modules). Left alone, the lockfile's root
# "version" field silently drifts from package.json's (2026-08-15: found
# stuck at 0.5.4 while package.json was already at 0.5.13). Both
# occurrences (the top-level field and packages[""].version) sit
# immediately after a `"name": "bosun-editor"` line, so one anchored,
# all-occurrences replace catches both safely.
Set-VersionLine `
    -Path (Join-Path $editor "package-lock.json") `
    -Pattern '("name": "bosun-editor",\r?\n\s*"version": )"\d+\.\d+\.\d+"' `
    -Replacement "`${1}`"$Version`""

# Native firmware shares the Bosun release number. Keep both CMake platform
# branches, the runtime protocol version and picotool's program metadata aligned.
# Require both project() declarations so a missing branch cannot silently ship
# a different version from the host build or from the desktop update manifest.
Set-VersionLine `
    -Path (Join-Path $native "CMakeLists.txt") `
    -Pattern '(?m)^(\s*project\(BosunNative VERSION )\d+\.\d+\.\d+(?= LANGUAGES\b)' `
    -Replacement "`${1}$Version" `
    -ExpectedMatches 2

Set-VersionLine `
    -Path (Join-Path $native "include\bosun\protocol.h") `
    -Pattern '(?m)^(#define BOSUN_NATIVE_VERSION )"\d+\.\d+\.\d+-native(?:-experimental)?"' `
    -Replacement "`${1}`"${Version}-native`"" `
    -ExpectedMatches 1

Set-VersionLine `
    -Path (Join-Path $native "platform\rp2040\CMakeLists.txt") `
    -Pattern '(?m)^(\s*pico_set_program_version\(\$\{target\} )"\d+\.\d+\.\d+-native(?:-experimental)?"(\))' `
    -Replacement "`${1}`"${Version}-native`"`${2}" `
    -ExpectedMatches 1

# Android's versionName is READ AT GRADLE BUILD TIME from gen/android's own
# tauri.properties - it is NOT derived from tauri.conf.json unless something
# runs `tauri android build`/`cargo tauri android init` to regenerate it.
# build-android.ps1 skips straight to `gradlew assembleRelease` on the
# already-generated project, so this file only ever updates if something
# writes it directly (2026-08-15: found stuck at 0.5.4 for the entire
# session - every APK this session self-reported the wrong version despite
# shipping fresh code, which is what made the write-only-stall fix look
# like it wasn't deployed when it actually was). versionCode has no
# semantic tie to semver - Android just requires it to strictly increase
# on every release - so it's auto-incremented here rather than derived.
#
# tauri.properties itself is gitignored (editor/src-tauri/gen/android/app/
# .gitignore), so the counter it holds is purely LOCAL to whichever
# machine last ran this script - CI starts every build from a fresh
# checkout with no tauri.properties, so `tauri android init` seeds its
# own default (observed: 2), completely disconnected from what local
# builds had reached (29) - found 2026-08-16 when a CI-built release APK
# failed to install over a locally-built one with
# INSTALL_FAILED_VERSION_DOWNGRADE. android-version-code.txt is the fix:
# a small tracked file that travels with the commit, so CI and every
# dev machine increment the SAME counter. release.yml's Android job reads
# it and overwrites tauri.properties with it after `tauri android init`,
# right before the real build.
$versionCodeTrackedPath = Join-Path $tauriDir "android-version-code.txt"
$tauriPropsPath = Join-Path $tauriDir "gen\android\app\tauri.properties"
$currentCode = 0
if (Test-Path $versionCodeTrackedPath) {
    $currentCode = [int](Get-Content -Path $versionCodeTrackedPath -Raw).Trim()
} elseif (Test-Path $tauriPropsPath) {
    # One-time migration: seed from whatever a local tauri.properties
    # already had, so this doesn't regress below a value already shipped.
    $propsContent = Get-Content -Path $tauriPropsPath -Raw
    if ($propsContent -match 'tauri\.android\.versionCode=(\d+)') {
        $currentCode = [int]$Matches[1]
    }
}
$nextCode = $currentCode + 1
Set-Content -Path $versionCodeTrackedPath -Value "$nextCode" -NoNewline
Write-Host "[ok  ] versionName/versionCode (Android)" -ForegroundColor Green
Write-Host "      -> versionName=$Version versionCode=$nextCode ($versionCodeTrackedPath, tracked)" -ForegroundColor Green
if (Test-Path $tauriPropsPath) {
    $newProps = "// THIS IS AN AUTOGENERATED FILE. DO NOT EDIT THIS FILE DIRECTLY.`ntauri.android.versionName=$Version`ntauri.android.versionCode=$nextCode`n"
    Set-Content -Path $tauriPropsPath -Value $newProps -NoNewline
    Write-Host "      -> also wrote local $tauriPropsPath (untracked, for local builds)" -ForegroundColor DarkGray
} else {
    Write-Host "      -> local $tauriPropsPath not found yet (Android project not generated) - fine, CI/build-android.ps1 will pick up the tracked file" -ForegroundColor DarkYellow
}

# ---------- 2. Verified resource sync ----------

Write-Host "`n[2/3] Verifying frozen legacy firmware resources without changing their version" -ForegroundColor Yellow
Invoke-NativeTool { & $pythonExe $syncScript --repo-root $repoRoot }
if ($LASTEXITCODE -ne 0) {
    throw "Firmware resource sync failed"
}

# ---------- 3. Completion ----------

Write-Host "`n[3/3] Resource hashes verified by the shared sync helper" -ForegroundColor Yellow

Write-Host "`nVersion bump complete: $Version" -ForegroundColor Green
Write-Host "Next: run the build(s) that actually need it -" -ForegroundColor Cyan
Write-Host "  tools\native-build.ps1 -Platform rp2040   (then regenerate the native update package for $Version)" -ForegroundColor Cyan
Write-Host "  npm run package:portable   (from editor/, desktop dist)" -ForegroundColor Cyan
Write-Host "  tools\build-android.ps1 -Deploy   (Android APK)" -ForegroundColor Cyan
