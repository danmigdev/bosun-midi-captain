<#
.SYNOPSIS
  Re-sync the F-Droid recipe's embedded tauri.conf.json after a new release.

.DESCRIPTION
  F-Droid's buildserver never runs the real `tauri android build` CLI (see
  metadata/com.bosun.app.yml in the fdroiddata fork) - which is the only
  thing that actually generates assets/tauri.conf.json (a fully-resolved,
  expanded Config, serialized). The recipe works around this by embedding a
  LITERAL byte-for-byte copy of that file, captured from a real release
  build. Every new version changes at least the "version" string inside it
  (and can reorder bundle.resources' keys too - a separate, still-unpatched
  HashMap-ordering bug in the `tauri` crate itself), so this literal needs
  re-extracting from a freshly published release APK EVERY TIME a new
  version is cut, or the reproducibility CI job (`fdroid build`) fails.

  This script automates that: downloads the release APK, extracts its
  embedded tauri.conf.json, splices it into the recipe file in place of the
  old literal, then runs fdroidserver's own rewritemeta/lint so the
  formatting matches what GitLab CI expects.

  IMPORTANT: GitLab CI's `fdroid rewritemeta` job installs ruamel.yaml
  0.18.10 (+ ruamel.yaml.clib) from Debian trixie's apt repos - a DIFFERENT
  version than whatever pip installs by default, with different plain-
  scalar line-wrap width. Running rewritemeta with the wrong ruamel.yaml
  version produces output that LOOKS canonical locally but still fails CI
  (this bit an earlier session hard - see project_fdroid_mr_pipeline_resolved
  in the memory system). This script pins and isolates the exact matching
  version automatically, so it can't drift from whatever's on this machine.

  This script does NOT commit or push - review the diff it leaves in your
  fdroiddata working copy, then commit/push by hand.

.PARAMETER Version
  Release version without the "v" prefix, e.g. "0.7.0". Must match an
  already-published GitHub release (release.yml must have finished
  uploading bosun.apk before running this).

.PARAMETER FdroidDataPath
  Path to a local clone of the danilo.migliarino/fdroiddata fork (the
  actual F-Droid submission working copy that gets pushed to GitLab -
  NOT this repo's docs/fdroid-com.bosun.app.yml, which is only a
  reference/documentation mirror).

.PARAMETER Repo
  GitHub "owner/repo" the release APK is published under. Defaults to
  danmigdev/bosun-midi-captain.

.PARAMETER AppId
  F-Droid application ID / metadata file basename. Defaults to
  com.bosun.app.

.EXAMPLE
  .\tools\update-fdroid-recipe.ps1 -Version 0.7.0 -FdroidDataPath C:\dev\fdroiddata

.EXAMPLE
  # Re-sync after rebuilding the SAME version's release asset (e.g. after
  # force-moving a tag to pick up a fix, same versionName/versionCode):
  .\tools\update-fdroid-recipe.ps1 -Version 0.6.0 -FdroidDataPath C:\dev\fdroiddata
#>

param(
    [Parameter(Mandatory=$true)]
    [string]$Version,

    [Parameter(Mandatory=$true)]
    [string]$FdroidDataPath,

    [string]$Repo = "danmigdev/bosun-midi-captain",
    [string]$AppId = "com.bosun.app"
)

$ErrorActionPreference = "Stop"

# See build-android.ps1's own comment on this exact pitfall: PowerShell 5.1
# promotes EVERY line a native tool writes to stderr into a terminating
# error under $ErrorActionPreference = "Stop", even on a routine warning
# with exit code 0 (pip's own upgrade notices, fdroidserver's logging
# module, which writes WARNING/INFO to stderr by design). Run native tools
# through this wrapper - it drops to "Continue" only for that one call.
function Invoke-NativeTool {
    param([scriptblock]$Command)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Command } finally { $ErrorActionPreference = $prev }
}

# ---------- 0. Resolve python ----------
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = "C:\Python312\python.exe" }
if (-not (Test-Path $python)) {
    throw "Could not find python.exe (checked PATH and C:\Python312\python.exe)."
}

Invoke-NativeTool { & $python -c "import fdroidserver" }
if ($LASTEXITCODE -ne 0) {
    throw "fdroidserver is not importable from $python. Install it first: $python -m pip install fdroidserver"
}

# ---------- 1. Validate the fdroiddata checkout ----------
$recipePath = Join-Path $FdroidDataPath "metadata\$AppId.yml"
if (-not (Test-Path $recipePath)) {
    throw "No recipe at $recipePath - is -FdroidDataPath a real fdroiddata clone with this app already submitted?"
}

# ---------- 2. Download the release APK ----------
Write-Host "[1/5] Downloading bosun.apk for v$Version ..." -ForegroundColor Cyan
$apkUrl = "https://github.com/$Repo/releases/download/v$Version/bosun.apk"
$tempApk = Join-Path $env:TEMP "bosun-fdroid-recipe-update.apk"
Invoke-WebRequest -Uri $apkUrl -OutFile $tempApk

# ---------- 3. Extract assets/tauri.conf.json ----------
Write-Host "[2/5] Extracting assets/tauri.conf.json ..." -ForegroundColor Cyan
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::OpenRead($tempApk)
try {
    $entry = $zip.GetEntry("assets/tauri.conf.json")
    if (-not $entry) { throw "assets/tauri.conf.json not found inside $tempApk - is this really a Bosun release APK?" }
    $reader = New-Object System.IO.StreamReader($entry.Open(), [System.Text.Encoding]::UTF8)
    try { $freshJson = $reader.ReadToEnd() } finally { $reader.Dispose() }
} finally {
    $zip.Dispose()
}
Remove-Item $tempApk -ErrorAction SilentlyContinue

if ($freshJson -notmatch '"versionCode":\d+') {
    throw "Extracted tauri.conf.json doesn't look right (no versionCode field found) - aborting before touching the recipe."
}

# ---------- 4. Splice it into the recipe ----------
Write-Host "[3/5] Updating $recipePath ..." -ForegroundColor Cyan
$content = [System.IO.File]::ReadAllText($recipePath)
$pattern = "printf '%s'\s*'(.*?)'\r?\n\s*> src-tauri/gen/android/app/src/main/assets/tauri\.conf\.json"
$regexOptions = [System.Text.RegularExpressions.RegexOptions]::Singleline
$match = [System.Text.RegularExpressions.Regex]::Match($content, $pattern, $regexOptions)
if (-not $match.Success) {
    throw "Could not find the tauri.conf.json printf literal in $recipePath - has the recipe's structure changed?"
}
$literalGroup = $match.Groups[1]
# The captured group is still YAML-folded (embedded newlines + indentation
# from the last rewritemeta pass) - fold it back to a single line the same
# way YAML's own parser would before comparing, or this always looks
# "different" even when nothing actually changed.
$foldedOld = [System.Text.RegularExpressions.Regex]::Replace($literalGroup.Value, "[ \t]*\r?\n\s*", " ")
if ($foldedOld -eq $freshJson) {
    Write-Host "  Already up to date - no change needed." -ForegroundColor DarkGray
} else {
    $newContent = $content.Substring(0, $literalGroup.Index) + $freshJson + $content.Substring($literalGroup.Index + $literalGroup.Length)
    # No BOM: Set-Content -Encoding utf8 in PowerShell 5.1 adds one, which
    # would corrupt this YAML file for fdroidserver's own parser.
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($recipePath, $newContent, $utf8NoBom)
    Write-Host "  Literal replaced." -ForegroundColor Green
}

# ---------- 5. Match GitLab CI's exact ruamel.yaml, then canonicalize ----------
Write-Host "[4/5] Ensuring the matching ruamel.yaml (0.18.10 + clib) is available ..." -ForegroundColor Cyan
$pyDepsDir = Join-Path $env:TEMP "bosun-fdroid-pydeps"
$ruamelMarker = Join-Path $pyDepsDir "ruamel"
if (-not (Test-Path $ruamelMarker)) {
    New-Item -ItemType Directory -Force -Path $pyDepsDir | Out-Null
    Invoke-NativeTool { & $python -m pip install --target $pyDepsDir --no-deps "ruamel.yaml==0.18.10" "ruamel.yaml.clib==0.2.12" }
    if ($LASTEXITCODE -ne 0) { throw "pip install of the pinned ruamel.yaml failed." }
}

Write-Host "[5/5] Running fdroidserver rewritemeta + lint ..." -ForegroundColor Cyan
$env:PYTHONPATH = $pyDepsDir
Push-Location $FdroidDataPath
try {
    Invoke-NativeTool { & $python -m fdroidserver.rewritemeta $AppId }
    if ($LASTEXITCODE -ne 0) { throw "fdroidserver.rewritemeta failed." }
    Invoke-NativeTool { & $python -m fdroidserver.lint $AppId }
    if ($LASTEXITCODE -ne 0) { throw "fdroidserver.lint reported an error (not just warnings) - check output above." }
} finally {
    Pop-Location
    Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
}

Write-Host "`nDone. Review the diff, then commit and push from $FdroidDataPath yourself:" -ForegroundColor Green
Write-Host "  git -C `"$FdroidDataPath`" diff -- metadata/$AppId.yml" -ForegroundColor Yellow
