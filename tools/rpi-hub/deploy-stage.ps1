[CmdletBinding()]
param(
    [ValidateNotNullOrEmpty()]
    [string]$HostName = "bosun-hub",

    [string]$BundlePath,

    [switch]$Build,
    [switch]$SkipBuild,
    [switch]$ValidateOnly
)

# Deploy only the already-configured Stage static site, then restart the kiosk
# so Chromium loads the committed bundle. The hub and MIDI services remain
# deliberately outside this script's scope.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../.."))
$editorRoot = Join-Path $repoRoot "editor"
$defaultBundle = [IO.Path]::GetFullPath((Join-Path $editorRoot "dist-stage"))
$remoteTarget = "/opt/bosun-hub/stage"
$pathComparison = if ([IO.Path]::DirectorySeparatorChar -eq '\') {
    [StringComparison]::OrdinalIgnoreCase
} else {
    [StringComparison]::Ordinal
}
if ([string]::IsNullOrWhiteSpace($BundlePath)) {
    $BundlePath = $defaultBundle
}

function ConvertTo-ShellLiteral {
    param([Parameter(Mandatory)][string]$Value)

    if ($Value.IndexOf([char]0) -ge 0) {
        throw "A remote path contains a NUL character."
    }
    $singleQuoteEscape = "'" + '"' + "'" + '"' + "'"
    return "'" + $Value.Replace("'", $singleQuoteEscape) + "'"
}

function Get-ApplicationPath {
    param([Parameter(Mandatory)][string[]]$Names)

    foreach ($name in $Names) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -ne $command) {
            return $command.Source
        }
    }
    throw "Required command not found: $($Names -join ' or ')."
}

function Invoke-CheckedNative {
    param(
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string[]]$Arguments,
        [switch]$Quiet,
        [switch]$Capture
    )

    $nativeOutput = @(& $Executable @Arguments 2>&1)
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        $details = ($nativeOutput | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
        if ([string]::IsNullOrWhiteSpace($details)) {
            $details = "no diagnostic output"
        }
        throw "Command '$([IO.Path]::GetFileName($Executable))' failed with exit code ${exitCode}: $details"
    }

    if (-not $Quiet) {
        foreach ($line in $nativeOutput) {
            Write-Host $line
        }
    }
    if ($Capture) {
        return @($nativeOutput | ForEach-Object { $_.ToString() })
    }
}

function ConvertTo-EncodedRemoteCommand {
    param([Parameter(Mandatory)][string]$Script)

    # Windows PowerShell 5 rewrites embedded quotes while marshalling native
    # argv. Passing a multiline shell program directly to ssh.exe can turn
    # `[ -n "$value" ]` into `[ -n ]`. The latter is true in POSIX sh and
    # caused a false failure after an otherwise successful Stage commit.
    $normalized = $Script.Replace("`r`n", "`n")
    $encoded = [Convert]::ToBase64String(
        [Text.UTF8Encoding]::new($false).GetBytes($normalized)
    )
    # This envelope has no quotes or shell-sensitive Base64 characters. Even
    # if ssh receives it as several argv entries, ssh joins them back with
    # spaces and the decoded script remains byte-for-byte intact.
    return "printf %s $encoded | base64 -d | sh"
}

function Invoke-CheckedRemote {
    param(
        [Parameter(Mandatory)][string]$Ssh,
        [Parameter(Mandatory)][string]$RemoteHost,
        [Parameter(Mandatory)][string]$Script,
        [switch]$Quiet,
        [switch]$Capture
    )

    $remoteCommand = ConvertTo-EncodedRemoteCommand -Script $Script
    $arguments = @(
        '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', $RemoteHost,
        $remoteCommand
    )
    if ($Capture) {
        return @(Invoke-CheckedNative -Executable $Ssh -Arguments $arguments -Quiet:$Quiet -Capture)
    }
    Invoke-CheckedNative -Executable $Ssh -Arguments $arguments -Quiet:$Quiet
}

function Test-StageBundle {
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "Stage bundle directory not found: $Path"
    }

    $rootItem = Get-Item -LiteralPath $Path -Force
    $root = [IO.Path]::GetFullPath($rootItem.FullName).TrimEnd([char[]]@('\', '/'))
    $rootPrefix = $root + [IO.Path]::DirectorySeparatorChar
    $indexPath = Join-Path $root "index.html"

    if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "The bundle root must not be a symlink or reparse point: $root"
    }
    if (-not (Test-Path -LiteralPath $indexPath -PathType Leaf)) {
        throw "Stage bundle is missing index.html: $root"
    }

    $allItems = @(Get-ChildItem -LiteralPath $root -Force -Recurse)
    foreach ($item in $allItems) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "The Stage bundle must not contain symlinks or reparse points: $($item.FullName)"
        }
    }

    $files = @($allItems | Where-Object { -not $_.PSIsContainer })
    if ($files.Count -lt 2) {
        throw "Stage bundle has no assets: $root"
    }

    $seenNames = @{}
    $fileRecords = foreach ($file in $files) {
        $fullName = [IO.Path]::GetFullPath($file.FullName)
        if (-not $fullName.StartsWith($rootPrefix, $pathComparison)) {
            throw "Bundle file escapes its root: $fullName"
        }
        $relative = $fullName.Substring($rootPrefix.Length).Replace('\', '/')
        $segments = @($relative.Split('/'))
        if ($relative -notmatch '^[A-Za-z0-9._/-]+$' -or
            $segments.Count -eq 0 -or
            @($segments | Where-Object { $_ -in @('', '.', '..') -or $_.StartsWith('-') }).Count -ne 0) {
            throw "Bundle path is not safe and portable for deployment: $relative"
        }
        $caseKey = $relative.ToLowerInvariant()
        if ($seenNames.ContainsKey($caseKey)) {
            throw "Bundle contains paths which differ only by case: $relative"
        }
        $seenNames[$caseKey] = $true
        [pscustomobject]@{
            FullPath = $fullName
            RelativePath = $relative
            Hash = (Get-FileHash -LiteralPath $fullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
    $fileRecords = @($fileRecords | Sort-Object RelativePath)
    $exactRelativePaths = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($fileRecord in $fileRecords) {
        [void]$exactRelativePaths.Add($fileRecord.RelativePath)
    }

    $html = [IO.File]::ReadAllText($indexPath)
    if ([string]::IsNullOrWhiteSpace($html)) {
        throw "Stage index.html is empty: $indexPath"
    }

    $referencePattern = @'
(?is)\b(?:src|href)\s*=\s*(?:"(?<double>[^"]+)"|'(?<single>[^']+)')
'@
    $localReferences = 0
    foreach ($match in [regex]::Matches($html, $referencePattern)) {
        $url = if ($match.Groups['double'].Success) {
            $match.Groups['double'].Value
        } else {
            $match.Groups['single'].Value
        }
        if ([string]::IsNullOrWhiteSpace($url) -or
            $url.StartsWith('#') -or
            $url.StartsWith('//') -or
            $url -match '^[A-Za-z][A-Za-z0-9+.-]*:') {
            continue
        }

        $pathPart = ($url -split '[?#]', 2)[0]
        try {
            $pathPart = [Uri]::UnescapeDataString($pathPart)
        } catch {
            throw "index.html contains an invalid asset URL: $url"
        }
        if ($pathPart.Contains('\')) {
            throw "index.html contains a non-portable asset URL: $url"
        }
        $relativeReference = $pathPart.TrimStart('/')
        if ([string]::IsNullOrWhiteSpace($relativeReference)) {
            throw "index.html contains an invalid local asset URL: $url"
        }

        $candidate = [IO.Path]::GetFullPath((Join-Path $root $relativeReference.Replace('/', [IO.Path]::DirectorySeparatorChar)))
        if (-not $candidate.StartsWith($rootPrefix, $pathComparison)) {
            throw "index.html asset reference escapes the bundle: $url"
        }
        $candidateRelative = $candidate.Substring($rootPrefix.Length).Replace('\', '/')
        if (-not $exactRelativePaths.Contains($candidateRelative)) {
            if ($seenNames.ContainsKey($candidateRelative.ToLowerInvariant())) {
                throw "index.html asset path case does not match the deployed file: $url"
            }
            throw "index.html references a missing asset: $url"
        }
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            throw "index.html references a missing asset: $url"
        }
        $localReferences++
    }
    if ($localReferences -eq 0) {
        throw "index.html does not reference any local Stage asset."
    }

    return [pscustomobject]@{
        Root = $root
        Files = $fileRecords
        ReferenceCount = $localReferences
    }
}

if ($Build -and $SkipBuild) {
    throw "Use either -Build or -SkipBuild, not both."
}

# Normal deploys build first.  Offline validation intentionally uses the
# existing bundle unless -Build is explicitly supplied.
$shouldBuild = $Build -or (-not $SkipBuild -and -not $ValidateOnly)
$requestedBundle = [IO.Path]::GetFullPath($BundlePath)
if ($shouldBuild -and
    -not $requestedBundle.Equals($defaultBundle, $pathComparison)) {
    throw "-Build writes editor/dist-stage; use the default -BundlePath or pass -SkipBuild."
}

$validation = $null
if (-not $shouldBuild) {
    $validation = Test-StageBundle -Path $requestedBundle
}

$ssh = $null
$scp = $null
if (-not $ValidateOnly) {
    if ($HostName.StartsWith('-') -or $HostName -notmatch '^[A-Za-z0-9_.@-]+$') {
        throw "Unsafe or unsupported SSH host name: $HostName"
    }
    $ssh = Get-ApplicationPath -Names @('ssh.exe', 'ssh')
    $scp = Get-ApplicationPath -Names @('scp.exe', 'scp')

    $targetLiteral = ConvertTo-ShellLiteral $remoteTarget
    $preflight = @'
set -eu
target=__TARGET__
test -d "$target"
test ! -L "$target"
command -v sha256sum >/dev/null
command -v rsync >/dev/null
command -v base64 >/dev/null
command -v grep >/dev/null
command -v systemctl >/dev/null
command -v systemd-cgls >/dev/null
id bosun >/dev/null 2>&1
if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null
    sudo -n true
fi
as_root() {
    if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo -n "$@"; fi
}
as_root systemctl cat bosun-kiosk.service >/dev/null
'@.Replace('__TARGET__', $targetLiteral)

    Write-Host "Checking Stage destination on $HostName ..."
    Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName -Script $preflight -Quiet
}

if ($shouldBuild) {
    $npm = Get-ApplicationPath -Names @('npm.cmd', 'npm.exe', 'npm')
    if (-not (Test-Path -LiteralPath (Join-Path $editorRoot 'package.json') -PathType Leaf)) {
        throw "Editor package.json not found: $editorRoot"
    }
    Write-Host "Building Stage bundle ..."
    Push-Location $editorRoot
    try {
        Invoke-CheckedNative -Executable $npm -Arguments @('run', 'build:stage')
    } finally {
        Pop-Location
    }
    $validation = Test-StageBundle -Path $defaultBundle
}

if ($null -eq $validation) {
    $validation = Test-StageBundle -Path $requestedBundle
}

Write-Host "Valid Stage bundle: $($validation.Files.Count) files, $($validation.ReferenceCount) local references."
if ($ValidateOnly) {
    Write-Host "Validation only: no SSH connection or remote change was made."
    return
}

$deployId = [Guid]::NewGuid().ToString('N')
$remoteStageRoot = "/tmp/bosun-stage-$deployId"
if ($remoteStageRoot -notmatch '^/tmp/bosun-stage-[a-f0-9]{32}$') {
    throw "Internal error: unsafe staging path."
}
$remoteBundle = "$remoteStageRoot/bundle"
$localTempRoot = Join-Path ([IO.Path]::GetTempPath()) "bosun-stage-$deployId"
$remoteCreated = $false
$localCreated = $false

try {
    [void][IO.Directory]::CreateDirectory($localTempRoot)
    $localCreated = $true
    $manifestPath = Join-Path $localTempRoot 'manifest.sha256'
    $manifestLines = @($validation.Files | ForEach-Object { "$($_.Hash)  $($_.RelativePath)" })
    [IO.File]::WriteAllText(
        $manifestPath,
        (($manifestLines -join "`n") + "`n"),
        [Text.UTF8Encoding]::new($false)
    )

    $remoteDirectories = @($remoteBundle)
    foreach ($file in $validation.Files) {
        $slash = $file.RelativePath.LastIndexOf('/')
        if ($slash -gt 0) {
            $remoteDirectories += "$remoteBundle/$($file.RelativePath.Substring(0, $slash))"
        }
    }
    $directoryArguments = @($remoteDirectories | Sort-Object -Unique | ForEach-Object {
        ConvertTo-ShellLiteral $_
    }) -join ' '
    $stageLiteral = ConvertTo-ShellLiteral $remoteStageRoot
    $createStage = @'
set -eu
root=__ROOT__
mkdir -m 700 -- "$root"
if ! mkdir -p -- __DIRECTORIES__; then
    rm -rf -- "$root"
    exit 1
fi
'@.Replace('__ROOT__', $stageLiteral).Replace('__DIRECTORIES__', $directoryArguments)

    Write-Host "Creating unique remote staging area ..."
    Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName -Script $createStage -Quiet
    $remoteCreated = $true

    foreach ($file in $validation.Files) {
        $remoteFile = "$remoteBundle/$($file.RelativePath)"
        Invoke-CheckedNative -Executable $scp -Arguments @(
            '-q', '-p', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
            $file.FullPath, "${HostName}:$remoteFile"
        ) -Quiet
    }
    Invoke-CheckedNative -Executable $scp -Arguments @(
        '-q', '-p', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
        $manifestPath, "${HostName}:$remoteStageRoot/manifest.sha256"
    ) -Quiet

    $bundleLiteral = ConvertTo-ShellLiteral $remoteBundle
    $manifestLiteral = ConvertTo-ShellLiteral "$remoteStageRoot/manifest.sha256"
    $verifyStage = "set -eu; cd $bundleLiteral; sha256sum -c $manifestLiteral >/dev/null"
    Write-Host "Verifying uploaded hashes ..."
    Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName -Script $verifyStage -Quiet

    $targetLiteral = ConvertTo-ShellLiteral $remoteTarget
    $diffCommand = @'
set -eu
src=__SOURCE__
dst=__TARGET__
as_root() {
    if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo -n "$@"; fi
}
as_root rsync -rcni --delete --checksum --out-format=%i:%n%L "$src/" "$dst/"
'@.Replace('__SOURCE__', $bundleLiteral).Replace('__TARGET__', $targetLiteral)
    $remoteDiff = @(Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName `
        -Script $diffCommand -Quiet -Capture)

    if (($remoteDiff -join '').Trim().Length -eq 0) {
        Write-Host "Remote Stage bundle is already identical."
    } else {
        Write-Host "Verified remote diff:"
        foreach ($line in $remoteDiff) {
            Write-Host "  $line"
        }

        $indexTempLiteral = ConvertTo-ShellLiteral "$remoteTarget/.index.html.bosun-$deployId"
        $deployCommand = @'
set -eu
src=__SOURCE__
dst=__TARGET__
manifest=__MANIFEST__
index_tmp=__INDEX_TEMP__
as_root() {
    if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo -n "$@"; fi
}
cleanup_index() {
    as_root rm -f -- "$index_tmp" || true
}
trap cleanup_index EXIT HUP INT TERM

# Recheck immutable staging immediately before changing the served tree.
cd "$src"
sha256sum -c "$manifest" >/dev/null

# New fingerprinted assets coexist with the old release first.  index.html is
# committed last with a same-filesystem rename, then obsolete files are pruned.
as_root rsync -a --checksum --chown=bosun:bosun --chmod=D755,F644 --exclude=/index.html "$src/" "$dst/"
as_root cp -p -- "$src/index.html" "$index_tmp"
as_root chmod 0644 "$index_tmp"
as_root chown bosun:bosun "$index_tmp"
as_root mv -f -- "$index_tmp" "$dst/index.html"
as_root rsync -a --checksum --chown=bosun:bosun --chmod=D755,F644 --delete-delay "$src/" "$dst/"

cd "$dst"
sha256sum -c "$manifest" >/dev/null
remaining="$(as_root rsync -rcni --delete --checksum "$src/" "$dst/")"
if [ -n "$remaining" ]; then
    printf '%s\n' "$remaining" >&2
    exit 1
fi
wrong_owner="$(as_root find "$dst" \( ! -user bosun -o ! -group bosun \) -print -quit)"
if [ -n "$wrong_owner" ]; then
    printf 'unexpected_Stage_owner:%s\n' "$wrong_owner" >&2
    exit 1
fi
trap - EXIT HUP INT TERM
'@.Replace('__SOURCE__', $bundleLiteral).
        Replace('__TARGET__', $targetLiteral).
        Replace('__MANIFEST__', $manifestLiteral).
        Replace('__INDEX_TEMP__', $indexTempLiteral)

        Write-Host "Deploying assets, then atomically switching index.html ..."
        Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName -Script $deployCommand -Quiet
        Write-Host "Stage bundle deployed and verified."
    }

    # systemctl restart can succeed before the Chromium child is usable. Check
    # both the unit state and its own cgroup, rather than accepting an unrelated
    # Chromium process or the cage parent by itself. A failed readiness check
    # is fatal; the finally block still removes both staging areas.
    $restartKioskCommand = @'
set -eu
kiosk_service=bosun-kiosk.service
as_root() {
    if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo -n "$@"; fi
}
diagnose_kiosk() {
    as_root systemctl --no-pager --lines=30 status "$kiosk_service" >&2 || true
}

if ! as_root systemctl restart "$kiosk_service"; then
    echo "failed to restart $kiosk_service" >&2
    diagnose_kiosk
    exit 1
fi

attempt=0
while [ "$attempt" -lt 20 ]; do
    if as_root systemctl is-active --quiet "$kiosk_service"; then
        control_group="$(as_root systemctl show --property=ControlGroup --value "$kiosk_service" 2>/dev/null || true)"
        case "$control_group" in
            /*)
                if systemd-cgls --no-pager "$control_group" 2>/dev/null |
                    grep -E -q '^[^0-9]*[0-9]+[[:space:]]+([^[:space:]]*/)?chromium(-browser)?([[:space:]]|$)'; then
                    exit 0
                fi
                ;;
        esac
    fi
    attempt=$((attempt + 1))
    sleep 0.5
done

echo "$kiosk_service did not become active with Chromium in its cgroup" >&2
diagnose_kiosk
exit 1
'@
    Write-Host "Restarting the Stage kiosk and waiting for Chromium ..."
    Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName -Script $restartKioskCommand -Quiet
    Write-Host "Stage kiosk restarted and Chromium verified; hub and MIDI services were untouched."
} finally {
    if ($remoteCreated) {
        try {
            $cleanupLiteral = ConvertTo-ShellLiteral $remoteStageRoot
            Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName `
                -Script "rm -rf -- $cleanupLiteral" -Quiet
        } catch {
            Write-Warning "Could not remove the exact remote staging directory ${remoteStageRoot}: $($_.Exception.Message)"
        }
    }
    if ($localCreated) {
        $tempParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([char[]]@('\', '/')) + [IO.Path]::DirectorySeparatorChar
        $tempCandidate = [IO.Path]::GetFullPath($localTempRoot)
        if ($tempCandidate.StartsWith($tempParent, $pathComparison) -and
            [IO.Path]::GetFileName($tempCandidate) -eq "bosun-stage-$deployId") {
            Remove-Item -LiteralPath $tempCandidate -Recurse -Force
        } else {
            Write-Warning "Refusing to clean unexpected local staging path: $tempCandidate"
        }
    }
}
