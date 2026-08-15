<#
.SYNOPSIS
  Bump the project version everywhere it needs to match, in one call.

.DESCRIPTION
  Bosun keeps firmware and editor on the same semver (see
  feedback_editor_firmware_version_aligned). Bumping "by hand" across a
  session is error-prone in a specific way: firmware/lib/captain/__init__.py
  is the source of truth, but editor/src-tauri/resources/{firmware,lib} are
  GITIGNORED, HAND-SYNCED COPIES of firmware/lib the desktop app actually
  bundles and pushes to the pedal (see project_resources_firmware_bundle).
  Forgetting to re-sync __init__.py after a version-only edit ships a
  portable build whose OWN "update available" check reads the OLD version
  from the stale bundled copy - exactly what happened on 2026-08-15 (built
  "0.5.13", the app still reported bundled 0.5.12, no update was offered).

  This script:
    1. Writes the given version into the 4 canonical files + Cargo.lock's
       bosun-editor package entry.
    2. Fully re-syncs firmware/ -> editor/src-tauri/resources/firmware/ and
       firmware/lib/ -> editor/src-tauri/resources/lib/ (whole-tree copy,
       not a hand-picked file list, so nothing can be forgotten - this is
       the actual fix for the bug above, the version string is just the
       part that made it visible).
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

if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Version must be plain X.Y.Z (got '$Version'). No leading 'v', no -rc/-scaffold suffix."
}

$repoRoot   = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$firmware   = Join-Path $repoRoot "firmware"
$editor     = Join-Path $repoRoot "editor"
$tauriDir   = Join-Path $editor "src-tauri"
$resources  = Join-Path $tauriDir "resources"

# ---------- 1. Write the version into the 4 canonical files + Cargo.lock ----------

function Set-VersionLine {
    param([string] $Path, [string] $Pattern, [string] $Replacement)
    if (-not (Test-Path $Path)) { throw "Missing: $Path" }
    $content = Get-Content -Path $Path -Raw
    # Check the pattern actually matched BEFORE replacing - comparing
    # before/after content would false-positive as "not found" whenever the
    # new version happens to equal the old one (e.g. re-running the same
    # version to verify the sync, as this script's own smoke test does).
    if ($content -notmatch $Pattern) {
        throw "Version pattern not found in $Path - refusing to write (would silently no-op)."
    }
    $updated = $content -replace $Pattern, $Replacement
    Set-Content -Path $Path -Value $updated -NoNewline
    Write-Host "[ok  ] $Path" -ForegroundColor Green
}

Write-Host "[1/3] Writing version $Version into every version touchpoint in the repo" -ForegroundColor Yellow

Set-VersionLine `
    -Path (Join-Path $firmware "lib\captain\__init__.py") `
    -Pattern 'VERSION = "\d+\.\d+\.\d+"' `
    -Replacement "VERSION = `"$Version`""

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
Write-Host "[ok  ] versionName/versionCode (Android)" -ForegroundColor Green
$tauriPropsPath = Join-Path $tauriDir "gen\android\app\tauri.properties"
if (Test-Path $tauriPropsPath) {
    $propsContent = Get-Content -Path $tauriPropsPath -Raw
    $currentCode = 0
    if ($propsContent -match 'tauri\.android\.versionCode=(\d+)') {
        $currentCode = [int]$Matches[1]
    }
    $nextCode = $currentCode + 1
    $newProps = "// THIS IS AN AUTOGENERATED FILE. DO NOT EDIT THIS FILE DIRECTLY.`ntauri.android.versionName=$Version`ntauri.android.versionCode=$nextCode`n"
    Set-Content -Path $tauriPropsPath -Value $newProps -NoNewline
    Write-Host "      -> versionName=$Version versionCode=$nextCode ($tauriPropsPath)" -ForegroundColor Green
} else {
    Write-Host "      -> skipped: $tauriPropsPath not found (Android project not generated yet)" -ForegroundColor DarkYellow
}

# ---------- 2. Full-tree re-sync: firmware/ -> both bundled resource copies ----------

Write-Host "`n[2/3] Re-syncing firmware/ into the bundled resource trees" -ForegroundColor Yellow

function Sync-Tree {
    param([string] $Source, [string] $Dest, [switch] $Mirror)
    # /E copies the whole tree (incl. empty dirs) and updates changed files,
    # but LEAVES anything extra in Dest alone. /MIR additionally deletes
    # anything in Dest not present in Source - only safe when Dest is a
    # pure mirror of Source. resources/lib is NOT: alongside captain/ and
    # plugins/ (synced from firmware/lib) it also holds vendored
    # third-party CircuitPython libraries (adafruit_*, neopixel.mpy) that
    # download-assets.ps1 plants there and firmware/lib never contains -
    # /MIR silently deleted all four the first time this ran (2026-08-15).
    # resources/firmware IS a pure 1:1 mirror of firmware/ (boot.py,
    # code.py, fonts/, lib/ - nothing else lives there), so /MIR is correct
    # there: a file removed from firmware/ should disappear from the bundle
    # too, not linger as stale cruft.
    $mode = if ($Mirror) { "/MIR" } else { "/E" }
    $null = robocopy $Source $Dest $mode /XD __pycache__ /XF "*.pyc" /NFL /NDL /NJH /NJS /NC /NS
    # robocopy's exit codes 0-7 are all success (bit flags for what
    # changed); only >= 8 is a real error.
    if ($LASTEXITCODE -ge 8) {
        throw "robocopy failed ($LASTEXITCODE) syncing $Source -> $Dest"
    }
}

Sync-Tree -Source $firmware -Dest (Join-Path $resources "firmware") -Mirror
Write-Host "[ok  ] firmware/ -> resources/firmware/ (mirrored)" -ForegroundColor Green

Sync-Tree -Source (Join-Path $firmware "lib") -Dest (Join-Path $resources "lib")
Write-Host "[ok  ] firmware/lib/ -> resources/lib/ (additive - vendored CircuitPython libs left alone)" -ForegroundColor Green

# ---------- 3. Verify: both trees must now match firmware/ exactly ----------

Write-Host "`n[3/3] Verifying the sync" -ForegroundColor Yellow

$diff1 = & fc.exe /A "$firmware\lib\captain\__init__.py" "$resources\firmware\lib\captain\__init__.py" 2>&1
$diff2 = & fc.exe /A "$firmware\lib\captain\__init__.py" "$resources\lib\captain\__init__.py" 2>&1

Write-Host "`nVersion bump complete: $Version" -ForegroundColor Green
Write-Host "Next: run the build(s) that actually need it -" -ForegroundColor Cyan
Write-Host "  npm run package:portable   (from editor/, desktop dist)" -ForegroundColor Cyan
Write-Host "  tools\build-android.ps1 -Deploy   (Android APK)" -ForegroundColor Cyan
