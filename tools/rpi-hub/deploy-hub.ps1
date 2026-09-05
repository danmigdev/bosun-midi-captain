[CmdletBinding()]
param(
    [ValidateNotNullOrEmpty()]
    [string]$HostName = 'bosun-hub',

    [string]$SourcePath,

    [ValidateRange(5, 120)]
    [int]$HealthWaitSeconds = 30,

    [switch]$SkipTests,
    [switch]$DryRun
)

# Deploy only /opt/bosun-hub/bosun_hub.  Appliance packages, systemd units,
# Stage assets, kiosk state, MIDI routing and Captain firmware are deliberately
# outside this helper's scope.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$hubRoot = [IO.Path]::GetFullPath($PSScriptRoot)
if ([string]::IsNullOrWhiteSpace($SourcePath)) {
    $SourcePath = Join-Path $hubRoot 'bosun_hub'
}
$remoteParent = '/opt/bosun-hub'
$remoteTarget = '/opt/bosun-hub/bosun_hub'
$serviceName = 'bosun-hub.service'
$pathComparison = if ([IO.Path]::DirectorySeparatorChar -eq '\') {
    [StringComparison]::OrdinalIgnoreCase
} else {
    [StringComparison]::Ordinal
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
        [string]$WorkingDirectory,
        [switch]$Quiet,
        [switch]$Capture
    )

    if ([string]::IsNullOrWhiteSpace($WorkingDirectory)) {
        $nativeOutput = @(& $Executable @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } else {
        Push-Location $WorkingDirectory
        try {
            $nativeOutput = @(& $Executable @Arguments 2>&1)
            $exitCode = $LASTEXITCODE
        } finally {
            Pop-Location
        }
    }
    if ($exitCode -ne 0) {
        $details = ($nativeOutput | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
        if ([string]::IsNullOrWhiteSpace($details)) {
            $details = 'no diagnostic output'
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

    # Keep the complete POSIX program in one quote-free ssh argument.  This
    # avoids Windows PowerShell's native argv quote rewriting.
    $normalized = $Script.Replace("`r`n", "`n")
    $encoded = [Convert]::ToBase64String(
        [Text.UTF8Encoding]::new($false).GetBytes($normalized)
    )
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

    $arguments = @(
        '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
        '-o', 'ConnectionAttempts=1',
        '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=3',
        $RemoteHost,
        (ConvertTo-EncodedRemoteCommand -Script $Script)
    )
    if ($Capture) {
        return @(Invoke-CheckedNative -Executable $Ssh -Arguments $arguments -Quiet:$Quiet -Capture)
    }
    Invoke-CheckedNative -Executable $Ssh -Arguments $arguments -Quiet:$Quiet
}

function ConvertTo-ShellLiteral {
    param([Parameter(Mandatory)][string]$Value)

    if ($Value.IndexOf([char]0) -ge 0) {
        throw 'A remote value contains a NUL character.'
    }
    $singleQuoteEscape = "'" + '"' + "'" + '"' + "'"
    return "'" + $Value.Replace("'", $singleQuoteEscape) + "'"
}

function Get-TextSha256 {
    param([Parameter(Mandatory)][string]$Text)

    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($Text)
        return ([BitConverter]::ToString($algorithm.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
    }
}

function Test-HubPackage {
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "Hub package directory not found: $Path"
    }
    $rootItem = Get-Item -LiteralPath $Path -Force
    if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Hub package root must not be a symlink or reparse point: $($rootItem.FullName)"
    }
    $root = [IO.Path]::GetFullPath($rootItem.FullName)
    $files = @(Get-ChildItem -LiteralPath $root -Force -File |
        Where-Object { $_.Extension -ceq '.py' })
    if ($files.Count -eq 0) {
        throw "Hub package contains no Python modules: $root"
    }

    $required = @('__init__.py', '__main__.py', 'hub.py', 'link.py', 'midi_connect.py', 'server.py')
    $seen = @{}
    $records = foreach ($file in $files) {
        if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Hub modules must not be symlinks or reparse points: $($file.FullName)"
        }
        $name = $file.Name
        if ($name -notmatch '^[A-Za-z0-9_]+\.py$' -or $name.StartsWith('-')) {
            throw "Hub module has an unsafe deployment name: $name"
        }
        $key = $name.ToLowerInvariant()
        if ($seen.ContainsKey($key)) {
            throw "Hub package contains module names which differ only by case: $name"
        }
        $seen[$key] = $true
        [pscustomobject]@{
            FullPath = [IO.Path]::GetFullPath($file.FullName)
            Name = $name
            Hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
    foreach ($name in $required) {
        if (-not $seen.ContainsKey($name.ToLowerInvariant())) {
            throw "Hub package is missing required module: $name"
        }
    }
    $records = @($records | Sort-Object Name)
    $manifest = (($records | ForEach-Object { "$($_.Hash)  $($_.Name)" }) -join "`n") + "`n"
    return [pscustomobject]@{
        Root = $root
        Files = $records
        Manifest = $manifest
        ManifestHash = Get-TextSha256 -Text $manifest
    }
}

if ($HostName.StartsWith('-') -or $HostName -notmatch '^[A-Za-z0-9_.@-]+$') {
    throw "Unsafe or unsupported SSH host name: $HostName"
}
$package = Test-HubPackage -Path $SourcePath

if (-not $SkipTests) {
    $python = Get-ApplicationPath -Names @('python.exe', 'python3.exe', 'python', 'python3')
    Write-Host 'Running the complete local hub test suite ...'
    Invoke-CheckedNative -Executable $python -Arguments @('-m', 'pytest', '-q') -WorkingDirectory $hubRoot
    Write-Host 'Local hub test suite passed.'
} else {
    Write-Host 'WARNING: local hub tests explicitly skipped.'
}

$deployId = [Guid]::NewGuid().ToString('N')
$remoteRoot = "/tmp/bosun-hub-deploy-$deployId"
$remoteSource = "$remoteRoot/bosun_hub"
$remoteManifest = "$remoteRoot/manifest.sha256"
$remoteNew = "$remoteParent/.bosun_hub.new-$deployId"
$remoteBackup = "$remoteParent/.bosun_hub.backup-$deployId"
$remoteJournalParent = '/var/lib/bosun-hub-deploy'
$remoteLock = "$remoteJournalParent/lock"
if ($remoteRoot -notmatch '^/tmp/bosun-hub-deploy-[a-f0-9]{32}$' -or
    $remoteNew -notmatch '^/opt/bosun-hub/\.bosun_hub\.new-[a-f0-9]{32}$' -or
    $remoteBackup -notmatch '^/opt/bosun-hub/\.bosun_hub\.backup-[a-f0-9]{32}$') {
    throw 'Internal error: unsafe deployment transaction path.'
}

Write-Host "Validated hub package: $($package.Files.Count) modules."
Write-Host "Manifest SHA-256: $($package.ManifestHash)"
if ($DryRun) {
    Write-Host 'DRY-RUN: no SSH connection or remote change was made.'
    Write-Host "Remote staging: $remoteRoot"
    Write-Host "Atomic directory exchange: verified $remoteNew <-> $remoteTarget; old package retained at $remoteBackup"
    Write-Host "Health gate: $serviceName active, stable NRestarts/MainPID, protocol ACK on tcp://127.0.0.1:9876"
    Write-Host 'Preserved siblings: /opt/bosun-hub/stage and /opt/bosun-hub/config'
    return
}

$ssh = Get-ApplicationPath -Names @('ssh.exe', 'ssh')
$scp = Get-ApplicationPath -Names @('scp.exe', 'scp')
$rootLiteral = ConvertTo-ShellLiteral $remoteRoot
$sourceLiteral = ConvertTo-ShellLiteral $remoteSource
$manifestLiteral = ConvertTo-ShellLiteral $remoteManifest
$targetLiteral = ConvertTo-ShellLiteral $remoteTarget
$parentLiteral = ConvertTo-ShellLiteral $remoteParent
$newLiteral = ConvertTo-ShellLiteral $remoteNew
$backupLiteral = ConvertTo-ShellLiteral $remoteBackup
$journalParentLiteral = ConvertTo-ShellLiteral $remoteJournalParent
$lockLiteral = ConvertTo-ShellLiteral $remoteLock
$serviceLiteral = ConvertTo-ShellLiteral $serviceName

$remoteLibrary = @'
DEPLOY_ID=__DEPLOY_ID__
ROOT=__ROOT__
SOURCE=__SOURCE__
MANIFEST=__MANIFEST__
PARENT=__PARENT__
TARGET=__TARGET__
NEW=__NEW__
BACKUP=__BACKUP__
JOURNAL_PARENT=__JOURNAL_PARENT__
LOCK=__LOCK__
SERVICE=__SERVICE__
HEALTH_WAIT_SECONDS=__HEALTH_WAIT_SECONDS__
EXPECTED_FILE_COUNT=__EXPECTED_FILE_COUNT__
EXPECTED_MANIFEST_SHA256=__EXPECTED_MANIFEST_SHA256__

as_root() {
    if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo -n "$@"; fi
}

validate_lock_path() {
    [ "$JOURNAL_PARENT" = /var/lib/bosun-hub-deploy ] &&
        [ "$LOCK" = /var/lib/bosun-hub-deploy/lock ]
}

ensure_journal_parent() {
    validate_lock_path || return 1
    if ! as_root test -e "$JOURNAL_PARENT" && ! as_root test -L "$JOURNAL_PARENT"; then
        as_root mkdir -m 0700 -- "$JOURNAL_PARENT" 2>/dev/null || true
    fi
    as_root python3 - "$JOURNAL_PARENT" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
item = os.lstat(path)
if (not stat.S_ISDIR(item.st_mode) or item.st_uid != 0
        or stat.S_IMODE(item.st_mode) != 0o700):
    raise SystemExit("unsafe hub deployment journal parent")
PY
}

validate_lock_directory() {
    validate_lock_path || return 1
    as_root python3 - "$LOCK" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
item = os.lstat(path)
if (not stat.S_ISDIR(item.st_mode) or item.st_uid != 0
        or stat.S_IMODE(item.st_mode) != 0o700):
    raise SystemExit("unsafe hub deployment lock directory")
PY
}

read_lock_value() {
    rlv_name=$1
    case "$rlv_name" in
        owner|root|backup|target|identity|phase|health) ;;
        *) printf 'invalid deployment journal field: %s\n' "$rlv_name" >&2; return 1 ;;
    esac
    validate_lock_directory || return 1
    as_root python3 - "$LOCK/$rlv_name" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
flags = os.O_RDONLY
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
descriptor = os.open(path, flags)
try:
    item = os.fstat(descriptor)
    if (not stat.S_ISREG(item.st_mode) or item.st_uid != 0
            or stat.S_IMODE(item.st_mode) != 0o600 or item.st_nlink != 1):
        raise SystemExit("unsafe hub deployment journal field")
    payload = os.read(descriptor, 16385)
    if len(payload) > 16384:
        raise SystemExit("oversized hub deployment journal field")
finally:
    os.close(descriptor)
sys.stdout.write(payload.decode("utf-8").removesuffix("\n"))
PY
}

require_deploy_lock() {
    validate_lock_path || return 1
    validate_lock_directory || {
        printf 'hub deployment lock is absent or unsafe\n' >&2
        return 1
    }
    rdl_owner=$(read_lock_value owner 2>/dev/null || true)
    [ "$rdl_owner" = "$DEPLOY_ID" ] || {
        printf 'hub deployment lock belongs to %s, not %s\n' \
            "${rdl_owner:-unknown}" "$DEPLOY_ID" >&2
        return 1
    }
}

write_lock_value() {
    wlv_name=$1
    wlv_value=$2
    require_deploy_lock || return 1
    case "$wlv_name" in
        root|backup|target|identity|phase|health) ;;
        *) printf 'invalid deployment journal field: %s\n' "$wlv_name" >&2; return 1 ;;
    esac
    as_root python3 - "$LOCK/$wlv_name" "$wlv_value" <<'PY'
import os
import stat
import sys

path, value = sys.argv[1], sys.argv[2]
flags = os.O_WRONLY
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
try:
    descriptor = os.open(path, flags)
except FileNotFoundError:
    descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
try:
    item = os.fstat(descriptor)
    if (not stat.S_ISREG(item.st_mode) or item.st_uid != 0
            or stat.S_IMODE(item.st_mode) != 0o600 or item.st_nlink != 1):
        raise SystemExit("unsafe hub deployment journal field")
    os.ftruncate(descriptor, 0)
    payload = (value + "\n").encode("utf-8")
    while payload:
        written = os.write(descriptor, payload)
        if written <= 0:
            raise OSError("journal write made no progress")
        payload = payload[written:]
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
}

release_deploy_lock() {
    require_deploy_lock || return 1
    # Remove only the fixed journal fields, owner last. Unknown content makes
    # rmdir fail closed instead of broadening cleanup.
    as_root python3 - "$LOCK" <<'PY'
import os
import sys

allowed = {"owner", "root", "backup", "target", "identity", "phase", "health"}
actual = set(os.listdir(sys.argv[1]))
unexpected = actual - allowed
if unexpected:
    raise SystemExit("unexpected deployment lock entries: %r" %
                     sorted(unexpected))
PY
    as_root rm -f -- "$LOCK/root" "$LOCK/backup" "$LOCK/target" \
        "$LOCK/identity" "$LOCK/phase" "$LOCK/health"
    as_root rm -f -- "$LOCK/owner"
    as_root rmdir -- "$LOCK"
}

validate_transaction_path() {
    vtp_path=$1
    vtp_kind=$2
    case "$vtp_kind" in
        root) vtp_prefix=/tmp/bosun-hub-deploy- ;;
        new) vtp_prefix=/opt/bosun-hub/.bosun_hub.new- ;;
        backup) vtp_prefix=/opt/bosun-hub/.bosun_hub.backup- ;;
        *) return 1 ;;
    esac
    case "$vtp_path" in
        "$vtp_prefix"*) vtp_suffix=${vtp_path#"$vtp_prefix"} ;;
        *) return 1 ;;
    esac
    [ "${#vtp_suffix}" -eq 32 ] || return 1
    case "$vtp_suffix" in *[!0-9a-f]*|'') return 1 ;; esac
    [ "$vtp_suffix" = "$DEPLOY_ID" ]
}

safe_remove_tree() {
    srt_path=$1
    srt_kind=$2
    validate_transaction_path "$srt_path" "$srt_kind" || {
        printf 'refusing unsafe recursive cleanup: %s\n' "$srt_path" >&2
        return 1
    }
    if [ -L "$srt_path" ]; then
        printf 'refusing to recursively clean symlink: %s\n' "$srt_path" >&2
        return 1
    fi
    if [ -e "$srt_path" ]; then
        [ -d "$srt_path" ] || {
            printf 'refusing to recursively clean non-directory: %s\n' "$srt_path" >&2
            return 1
        }
        as_root rm -rf -- "$srt_path"
    fi
}

exchange_directories() {
    ed_left=$1
    ed_right=$2
    if ! { [ -d "$ed_left" ] && [ ! -L "$ed_left" ] &&
           [ -d "$ed_right" ] && [ ! -L "$ed_right" ] &&
           [ "$(stat -c %d -- "$ed_left")" = "$(stat -c %d -- "$ed_right")" ]; }; then
        printf 'refusing to exchange unsafe or cross-device directories\n' >&2
        return 1
    fi
    # Linux renameat2(RENAME_EXCHANGE) changes both directory names in one
    # atomic filesystem operation: TARGET is never absent to concurrent
    # readers or at a process/SSH failure boundary.
    as_root python3 - "$ed_left" "$ed_right" <<'PY'
import ctypes
import os
import sys

left, right = map(os.fsencode, sys.argv[1:3])
libc = ctypes.CDLL(None, use_errno=True)
try:
    renameat2 = libc.renameat2
except AttributeError:
    raise SystemExit("libc does not expose renameat2")
renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p,
                      ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
renameat2.restype = ctypes.c_int
if renameat2(-100, left, -100, right, 2) != 0:  # AT_FDCWD, RENAME_EXCHANGE
    error = ctypes.get_errno()
    raise OSError(error, os.strerror(error))
PY
}

restore_previous_package() {
    [ -d "$BACKUP" ] && [ ! -L "$BACKUP" ] || return 0
    require_deploy_lock || return 1
    rpp_recorded_backup=$(read_lock_value backup 2>/dev/null || true)
    [ "$rpp_recorded_backup" = "$BACKUP" ] || {
        printf 'rollback backup does not match its persistent journal\n' >&2
        return 1
    }
    rpp_identity=$(read_lock_value identity 2>/dev/null || true)
    rpp_old=$(printf '%s\n' "$rpp_identity" | sed -n 's/^old=//p')
    rpp_candidate=$(printf '%s\n' "$rpp_identity" | sed -n 's/^candidate=//p')
    [ -n "$rpp_old" ] && [ -n "$rpp_candidate" ] || {
        printf 'rollback identity journal is incomplete\n' >&2
        return 1
    }
    rpp_target_now=$(stat -c '%d:%i' -- "$TARGET") || return 1
    rpp_backup_now=$(stat -c '%d:%i' -- "$BACKUP") || return 1
    if [ "$rpp_target_now" = "$rpp_old" ] && [ "$rpp_backup_now" = "$rpp_candidate" ]; then
        : # Candidate was prepared but never exchanged.
    elif [ "$rpp_target_now" = "$rpp_candidate" ] && [ "$rpp_backup_now" = "$rpp_old" ]; then
        exchange_directories "$TARGET" "$BACKUP" || return 1
        [ "$(stat -c '%d:%i' -- "$TARGET")" = "$rpp_old" ] || return 1
        [ "$(stat -c '%d:%i' -- "$BACKUP")" = "$rpp_candidate" ] || return 1
    else
        printf 'rollback directory identities are ambiguous; preserving both trees\n' >&2
        return 1
    fi
    safe_remove_tree "$BACKUP" backup || return 1
    write_lock_value phase rolled_back || return 1
}

snapshot_path() {
    sp_path=$1
    if [ -e "$sp_path" ] || [ -L "$sp_path" ]; then
        stat -c '%d:%i:%f:%s:%Y' -- "$sp_path"
    else
        printf 'absent\n'
    fi
}

verify_package() {
    vp_root=$1
    [ -d "$vp_root" ] && [ ! -L "$vp_root" ] || {
        printf 'invalid hub package directory: %s\n' "$vp_root" >&2
        return 1
    }
    vp_manifest_hash=$(sha256sum "$MANIFEST" | awk '{print $1}')
    [ "$vp_manifest_hash" = "$EXPECTED_MANIFEST_SHA256" ] || {
        printf 'manifest hash mismatch: %s\n' "$vp_manifest_hash" >&2
        return 1
    }
    (cd "$vp_root" && sha256sum -c "$MANIFEST" >/dev/null)
    python3 - "$vp_root" "$MANIFEST" "$EXPECTED_FILE_COUNT" <<'PY'
import hashlib
import os
import re
import sys

root, manifest_path, expected_count = sys.argv[1], sys.argv[2], int(sys.argv[3])
expected = {}
with open(manifest_path, "r", encoding="ascii", newline="") as stream:
    for raw in stream.read().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_]+\.py)", raw)
        if match is None:
            raise SystemExit("invalid manifest record: " + repr(raw))
        digest, name = match.groups()
        key = name.casefold()
        if key in expected:
            raise SystemExit("duplicate manifest module: " + name)
        expected[key] = (name, digest)
if len(expected) != expected_count:
    raise SystemExit("manifest module count mismatch")

actual = {}
with os.scandir(root) as entries:
    for entry in entries:
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            raise SystemExit("unexpected package entry: " + entry.name)
        key = entry.name.casefold()
        if key in actual:
            raise SystemExit("case-colliding package entry: " + entry.name)
        actual[key] = entry.name
if set(actual) != set(expected):
    raise SystemExit("package inventory mismatch: expected=%r actual=%r" %
                     (sorted(name for name, _ in expected.values()),
                      sorted(actual.values())))
for key, (name, wanted) in expected.items():
    if actual[key] != name:
        raise SystemExit("package filename case mismatch: " + actual[key])
    digest = hashlib.sha256()
    with open(os.path.join(root, name), "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != wanted:
        raise SystemExit("package SHA-256 mismatch: " + name)
PY
}

verify_installed_permissions() {
    vip_root=$1
    as_root python3 - "$vip_root" "$MANIFEST" <<'PY'
import os
import stat
import sys

root, manifest_path = sys.argv[1:3]
root_item = os.lstat(root)
if (not stat.S_ISDIR(root_item.st_mode) or root_item.st_uid != 0
        or stat.S_IMODE(root_item.st_mode) != 0o755):
    raise SystemExit("installed package directory is not root:0755")
with open(manifest_path, "r", encoding="ascii") as stream:
    names = [line[66:].rstrip("\n") for line in stream]
for name in names:
    item = os.stat(os.path.join(root, name), follow_symlinks=False)
    if (not stat.S_ISREG(item.st_mode) or item.st_uid != 0
            or stat.S_IMODE(item.st_mode) != 0o644 or item.st_nlink != 1):
        raise SystemExit("unsafe installed module permissions: " + name)
PY
}

read_service_restarts() {
    rsr_value=$(as_root systemctl show --property=NRestarts --value "$SERVICE")
    case "$rsr_value" in ''|*[!0-9]*)
        printf 'invalid NRestarts for %s: %s\n' "$SERVICE" "$rsr_value" >&2
        return 1
    esac
    printf '%s\n' "$rsr_value"
}

wait_for_service() {
    wfs_attempt=0
    wfs_limit=$((HEALTH_WAIT_SECONDS * 4))
    while [ "$wfs_attempt" -lt "$wfs_limit" ]; do
        if as_root systemctl is-active --quiet "$SERVICE"; then
            SERVICE_PID=$(as_root systemctl show --property=MainPID --value "$SERVICE")
            case "$SERVICE_PID" in ''|0|*[!0-9]*) ;; *) return 0 ;; esac
        fi
        wfs_attempt=$((wfs_attempt + 1))
        sleep 0.25
    done
    printf '%s did not become active with a live MainPID\n' "$SERVICE" >&2
    as_root systemctl --no-pager --lines=40 status "$SERVICE" >&2 || true
    return 1
}

wait_for_protocol_ping() {
    python3 - 9876 "$HEALTH_WAIT_SECONDS" "$DEPLOY_ID" <<'PY'
import json
import socket
import sys
import time

port = int(sys.argv[1])
timeout = float(sys.argv[2])
deploy_id = sys.argv[3]
deadline = time.monotonic() + timeout
last_error = "TCP endpoint did not accept a connection"
attempt = 0

while time.monotonic() < deadline:
    attempt += 1
    connection = None
    try:
        remaining = max(0.05, deadline - time.monotonic())
        connection = socket.create_connection(
            ("127.0.0.1", port), timeout=min(1.0, remaining))
        request_id = "deploy-hub-%s-%d" % (deploy_id, attempt)
        payload = (json.dumps({"type": "PING", "id": request_id},
                              separators=(",", ":")) + "\n").encode()
        connection.settimeout(min(1.0, remaining))
        connection.sendall(b"\n" + payload)
        receive = bytearray()
        attempt_deadline = min(deadline, time.monotonic() + 1.5)
        retry_link = False
        while time.monotonic() < attempt_deadline:
            connection.settimeout(max(0.05, min(0.25,
                                  attempt_deadline - time.monotonic())))
            try:
                chunk = connection.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                last_error = "TCP endpoint closed the connection"
                break
            receive.extend(chunk)
            if len(receive) > 65536:
                raise RuntimeError("unterminated PING response exceeds 64 KiB")
            while b"\n" in receive:
                raw, _, tail = bytes(receive).partition(b"\n")
                receive[:] = tail
                try:
                    reply = json.loads(raw.strip())
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(reply, dict) or reply.get("id") != request_id:
                    continue
                if reply.get("type") == "ACK":
                    print("BOSUN_HUB_PING=ACK")
                    raise SystemExit(0)
                if (reply.get("type") == "ERROR"
                        and reply.get("error") == "link_down"):
                    last_error = "Captain link is not ready"
                    retry_link = True
                    break
                raise RuntimeError("PING returned unexpected reply: %r" % reply)
            if retry_link:
                break
        else:
            last_error = "PING timed out"
    except SystemExit:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        last_error = str(error)
    finally:
        if connection is not None:
            connection.close()
    time.sleep(0.1)

raise SystemExit("hub protocol PING failed: " + last_error)
PY
}
'@.Replace('__DEPLOY_ID__', $deployId).
    Replace('__ROOT__', $rootLiteral).
    Replace('__SOURCE__', $sourceLiteral).
    Replace('__MANIFEST__', $manifestLiteral).
    Replace('__PARENT__', $parentLiteral).
    Replace('__TARGET__', $targetLiteral).
    Replace('__NEW__', $newLiteral).
    Replace('__BACKUP__', $backupLiteral).
    Replace('__JOURNAL_PARENT__', $journalParentLiteral).
    Replace('__LOCK__', $lockLiteral).
    Replace('__SERVICE__', $serviceLiteral).
    Replace('__HEALTH_WAIT_SECONDS__', $HealthWaitSeconds.ToString([Globalization.CultureInfo]::InvariantCulture)).
    Replace('__EXPECTED_FILE_COUNT__', $package.Files.Count.ToString([Globalization.CultureInfo]::InvariantCulture)).
    Replace('__EXPECTED_MANIFEST_SHA256__', $package.ManifestHash)

$remotePreflight = $remoteLibrary + @'

set -eu
command -v base64 >/dev/null
command -v sha256sum >/dev/null
command -v awk >/dev/null
command -v install >/dev/null
command -v python3 >/dev/null
command -v readlink >/dev/null
command -v sed >/dev/null
command -v stat >/dev/null
command -v systemctl >/dev/null
if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null
    sudo -n true
fi
id bosun >/dev/null 2>&1
[ "$PARENT" = /opt/bosun-hub ]
[ "$TARGET" = /opt/bosun-hub/bosun_hub ]
[ -d "$PARENT" ] && [ ! -L "$PARENT" ]
[ -d "$TARGET" ] && [ ! -L "$TARGET" ]
[ "$(readlink -f "$PARENT")" = "$PARENT" ]
[ "$(readlink -f "$TARGET")" = "$TARGET" ]
validate_transaction_path "$ROOT" root
validate_transaction_path "$NEW" new
validate_transaction_path "$BACKUP" backup
[ ! -e "$ROOT" ] && [ ! -L "$ROOT" ]
[ ! -e "$NEW" ] && [ ! -L "$NEW" ]
[ ! -e "$BACKUP" ] && [ ! -L "$BACKUP" ]
python3 - <<'PY'
import ctypes
if not hasattr(ctypes.CDLL(None), "renameat2"):
    raise SystemExit("libc does not expose renameat2")
PY
ensure_journal_parent
if as_root test -e "$LOCK" || as_root test -L "$LOCK"; then
    validate_lock_directory || {
        printf 'unsafe persistent hub deployment lock: %s\n' "$LOCK" >&2
        exit 1
    }
    stale_owner=$(read_lock_value owner 2>/dev/null || true)
    stale_phase=$(read_lock_value phase 2>/dev/null || true)
    stale_backup=$(read_lock_value backup 2>/dev/null || true)
    printf 'unfinished/concurrent hub deployment: owner=%s phase=%s backup=%s lock=%s\n' \
        "${stale_owner:-unknown}" "${stale_phase:-unknown}" \
        "${stale_backup:-unknown}" "$LOCK" >&2
    exit 1
fi
as_root systemctl cat "$SERVICE" >/dev/null
as_root systemctl is-active --quiet "$SERVICE"
preflight_restarts=$(read_service_restarts)
wait_for_protocol_ping
printf 'BOSUN_HUB_NRESTARTS_BEFORE=%s\n' "$preflight_restarts"
printf 'BOSUN_HUB_STAGE_IDENTITY=%s\n' "$(snapshot_path "$PARENT/stage")"
printf 'BOSUN_HUB_CONFIG_IDENTITY=%s\n' "$(snapshot_path "$PARENT/config")"
'@

$remoteVerify = $remoteLibrary + @'

set -eu
validate_transaction_path "$ROOT" root
[ -d "$ROOT" ] && [ ! -L "$ROOT" ]
verify_package "$SOURCE"
PYTHONPYCACHEPREFIX="$ROOT/pycache" python3 -m py_compile "$SOURCE"/*.py
verify_package "$SOURCE"
printf 'BOSUN_HUB_UPLOAD=VERIFIED files=%s manifest=%s\n' \
    "$EXPECTED_FILE_COUNT" "$EXPECTED_MANIFEST_SHA256"
'@

$remoteAcquireLock = $remoteLibrary + @'

set -eu
validate_lock_path
ensure_journal_parent
if as_root test -e "$LOCK" || as_root test -L "$LOCK"; then
    if validate_lock_directory; then
        lock_owner=$(read_lock_value owner 2>/dev/null || true)
    else
        lock_owner=unsafe
    fi
    printf 'hub deployment lock is already held by %s at %s\n' \
        "${lock_owner:-unknown}" "$LOCK" >&2
    exit 1
fi
if ! as_root mkdir -m 0700 -- "$LOCK"; then
    printf 'could not acquire hub deployment lock: %s\n' "$LOCK" >&2
    exit 1
fi
acquire_ok=0
cleanup_incomplete_lock() {
    acquire_status=$?
    trap - EXIT HUP INT TERM
    if [ "$acquire_ok" -ne 1 ]; then
        as_root rm -f -- "$LOCK/owner" "$LOCK/root" "$LOCK/backup" \
            "$LOCK/target" "$LOCK/identity" "$LOCK/phase" "$LOCK/health" || true
        as_root rmdir -- "$LOCK" || true
    fi
    exit "$acquire_status"
}
trap cleanup_incomplete_lock EXIT HUP INT TERM
as_root python3 - "$LOCK/owner" "$DEPLOY_ID" <<'PY'
import os
import sys

path, owner = sys.argv[1], sys.argv[2]
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
descriptor = os.open(path, flags, 0o600)
try:
    payload = (owner + "\n").encode("ascii")
    while payload:
        written = os.write(descriptor, payload)
        if written <= 0:
            raise OSError("lock owner write made no progress")
        payload = payload[written:]
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
require_deploy_lock
write_lock_value root "$ROOT"
write_lock_value backup "$BACKUP"
write_lock_value target "$TARGET"
write_lock_value phase acquired
acquire_ok=1
trap - EXIT HUP INT TERM
printf 'BOSUN_HUB_LOCK=ACQUIRED owner=%s\n' "$DEPLOY_ID"
'@

$remoteTransaction = $remoteLibrary + @'

set -eu
require_deploy_lock
validate_transaction_path "$ROOT" root
validate_transaction_path "$NEW" new
validate_transaction_path "$BACKUP" backup
[ -d "$ROOT" ] && [ ! -L "$ROOT" ]
[ ! -e "$NEW" ] && [ ! -L "$NEW" ]
[ ! -e "$BACKUP" ] && [ ! -L "$BACKUP" ]
verify_package "$SOURCE"
[ -d "$TARGET" ] && [ ! -L "$TARGET" ]
[ "$(readlink -f "$TARGET")" = "$TARGET" ]

stage_before=$(snapshot_path "$PARENT/stage")
config_before=$(snapshot_path "$PARENT/config")
initial_restarts=$(read_service_restarts)

rollback_deploy() {
    rb_original=$?
    trap - EXIT HUP INT TERM
    set +e
    rb_failed=0
    if [ -d "$BACKUP" ] && [ ! -L "$BACKUP" ]; then
        as_root systemctl stop "$SERVICE" || rb_failed=1
        restore_previous_package || rb_failed=1
        as_root systemctl restart "$SERVICE" || rb_failed=1
        wait_for_service || rb_failed=1
        wait_for_protocol_ping || rb_failed=1
    fi
    safe_remove_tree "$NEW" new || rb_failed=1
    if [ "$rb_failed" -eq 0 ]; then
        printf 'BOSUN_HUB_ROLLBACK=OK\n' >&2
        exit "$rb_original"
    fi
    printf 'BOSUN_HUB_ROLLBACK=FAILED\n' >&2
    exit 97
}
trap rollback_deploy EXIT HUP INT TERM

as_root mkdir -m 0755 -- "$NEW"
while IFS= read -r install_record; do
    install_name=${install_record#*  }
    as_root install -o root -g root -m 0644 -- \
        "$SOURCE/$install_name" "$NEW/$install_name"
done < "$MANIFEST"
verify_package "$NEW"
verify_installed_permissions "$NEW"

old_identity=$(stat -c '%d:%i' -- "$TARGET")
candidate_identity=$(stat -c '%d:%i' -- "$NEW")
write_lock_value identity "old=$old_identity
candidate=$candidate_identity"
write_lock_value phase prepared

# Put the candidate under the eventual backup name, then exchange it with the
# live package atomically. After the exchange BACKUP contains the complete old
# package until a second, independent health check commits the transaction.
as_root mv -- "$NEW" "$BACKUP"
write_lock_value phase backup_ready
as_root systemctl stop "$SERVICE"
write_lock_value phase service_stopped
exchange_directories "$TARGET" "$BACKUP"
if [ "$(stat -c '%d:%i' -- "$TARGET")" != "$candidate_identity" ] ||
   [ "$(stat -c '%d:%i' -- "$BACKUP")" != "$old_identity" ]; then
    printf 'atomic exchange produced unexpected directory identities\n' >&2
    exit 1
fi
write_lock_value phase exchanged
as_root systemctl restart "$SERVICE"
wait_for_service
started_pid=$SERVICE_PID
wait_for_protocol_ping
sleep 1
wait_for_service
[ "$SERVICE_PID" = "$started_pid" ] || {
    printf 'MainPID changed during the hub health window: %s -> %s\n' \
        "$started_pid" "$SERVICE_PID" >&2
    exit 1
}
final_restarts=$(read_service_restarts)
[ "$final_restarts" = "$initial_restarts" ] || {
    printf 'NRestarts changed during deploy: %s -> %s\n' \
        "$initial_restarts" "$final_restarts" >&2
    exit 1
}
verify_package "$TARGET"
verify_installed_permissions "$TARGET"
[ "$(snapshot_path "$PARENT/stage")" = "$stage_before" ] || {
    printf 'Stage identity changed during hub-only deployment\n' >&2
    exit 1
}
[ "$(snapshot_path "$PARENT/config")" = "$config_before" ] || {
    printf 'config identity changed during hub-only deployment\n' >&2
    exit 1
}
write_lock_value health "pid=$SERVICE_PID
restarts=$final_restarts
stage=$stage_before
config=$config_before"
write_lock_value phase first_health_passed
trap - EXIT HUP INT TERM
printf 'BOSUN_HUB_TRANSACTION=READY pid=%s nrestarts=%s manifest=%s\n' \
    "$SERVICE_PID" "$final_restarts" "$EXPECTED_MANIFEST_SHA256"
'@

$remoteCommit = $remoteLibrary + @'

set -eu
require_deploy_lock
health_state=$(read_lock_value health)
expected_pid=$(printf '%s\n' "$health_state" | sed -n 's/^pid=//p')
expected_restarts=$(printf '%s\n' "$health_state" | sed -n 's/^restarts=//p')
expected_stage=$(printf '%s\n' "$health_state" | sed -n 's/^stage=//p')
expected_config=$(printf '%s\n' "$health_state" | sed -n 's/^config=//p')
case "$expected_pid" in ''|0|*[!0-9]*) exit 1 ;; esac
case "$expected_restarts" in ''|*[!0-9]*) exit 1 ;; esac
[ -d "$BACKUP" ] && [ ! -L "$BACKUP" ]
verify_package "$TARGET"
verify_installed_permissions "$TARGET"
wait_for_service
[ "$SERVICE_PID" = "$expected_pid" ]
[ "$(read_service_restarts)" = "$expected_restarts" ]
wait_for_protocol_ping
sleep 1
wait_for_service
[ "$SERVICE_PID" = "$expected_pid" ]
[ "$(read_service_restarts)" = "$expected_restarts" ]
[ "$(snapshot_path "$PARENT/stage")" = "$expected_stage" ]
[ "$(snapshot_path "$PARENT/config")" = "$expected_config" ]
verify_package "$TARGET"
verify_installed_permissions "$TARGET"
write_lock_value phase committed
printf 'BOSUN_HUB_COMMIT=OK pid=%s nrestarts=%s manifest=%s\n' \
    "$SERVICE_PID" "$expected_restarts" "$EXPECTED_MANIFEST_SHA256"
'@

$remoteRecovery = $remoteLibrary + @'

set -eu
require_deploy_lock
validate_transaction_path "$NEW" new
validate_transaction_path "$BACKUP" backup
if [ -e "$BACKUP" ] || [ -L "$BACKUP" ]; then
    [ -d "$BACKUP" ] && [ ! -L "$BACKUP" ]
    as_root systemctl stop "$SERVICE" || true
    restore_previous_package
    as_root systemctl restart "$SERVICE"
fi
safe_remove_tree "$NEW" new
if ! as_root systemctl is-active --quiet "$SERVICE"; then
    as_root systemctl restart "$SERVICE"
fi
wait_for_service
wait_for_protocol_ping
printf 'BOSUN_HUB_RECOVERY=OK pid=%s nrestarts=%s\n' \
    "$SERVICE_PID" "$(read_service_restarts)"
'@

$remoteCleanupBackup = $remoteLibrary + @'

set -eu
require_deploy_lock
safe_remove_tree "$BACKUP" backup
'@

$remoteReleaseLockIfOwned = $remoteLibrary + @'

set -eu
validate_lock_path
if ! as_root test -e "$LOCK" && ! as_root test -L "$LOCK"; then
    exit 0
fi
if validate_lock_directory; then
    lock_owner=$(read_lock_value owner 2>/dev/null || true)
else
    printf 'refusing to touch unsafe deployment lock: %s\n' "$LOCK" >&2
    exit 1
fi
if [ "$lock_owner" != "$DEPLOY_ID" ]; then
    printf 'leaving deployment lock owned by %s untouched\n' \
        "${lock_owner:-unknown}" >&2
    exit 0
fi
release_deploy_lock
printf 'BOSUN_HUB_LOCK=RELEASED owner=%s\n' "$DEPLOY_ID"
'@

$remoteCleanupStage = $remoteLibrary + @'

set -eu
safe_remove_tree "$ROOT" root
'@

Write-Host "Checking the hub-only destination on $HostName ..."
$preflightOutput = @(Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName `
    -Script $remotePreflight -Quiet -Capture)
foreach ($line in $preflightOutput) {
    if ($line -like 'BOSUN_*') { Write-Host $line }
}

$localTempRoot = Join-Path ([IO.Path]::GetTempPath()) "bosun-hub-deploy-$deployId"
$localCreated = $false
$remoteCreated = $false
$lockAttempted = $false
$lockSafeToRelease = $false
$transactionAttempted = $false
$committed = $false
$primaryDiagnostic = $null
$recoveryError = $null
$lockReleaseError = $null
$preserveRemoteState = $false

try {
    [void][IO.Directory]::CreateDirectory($localTempRoot)
    $localCreated = $true
    $localManifest = Join-Path $localTempRoot 'manifest.sha256'
    [IO.File]::WriteAllText(
        $localManifest, $package.Manifest, [Text.UTF8Encoding]::new($false)
    )

    $createRemote = @'
set -eu
root=__ROOT__
source=__SOURCE__
case "$root" in /tmp/bosun-hub-deploy-[0-9a-f]*) ;; *) exit 1 ;; esac
suffix=${root#/tmp/bosun-hub-deploy-}
[ "${#suffix}" -eq 32 ]
case "$suffix" in *[!0-9a-f]*|'') exit 1 ;; esac
umask 077
[ ! -e "$root" ] && [ ! -L "$root" ]
mkdir -- "$root"
mkdir -- "$source"
'@.Replace('__ROOT__', $rootLiteral).Replace('__SOURCE__', $sourceLiteral)
    Write-Host 'Creating a unique remote staging directory ...'
    Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName -Script $createRemote -Quiet
    $remoteCreated = $true

    foreach ($file in $package.Files) {
        Invoke-CheckedNative -Executable $scp -Arguments @(
            '-q', '-p', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
            '-o', 'ConnectionAttempts=1',
            '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=3',
            $file.FullPath, "${HostName}:$remoteSource/$($file.Name)"
        ) -Quiet
    }
    Invoke-CheckedNative -Executable $scp -Arguments @(
        '-q', '-p', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
        '-o', 'ConnectionAttempts=1',
        '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=3',
        $localManifest, "${HostName}:$remoteManifest"
    ) -Quiet

    Write-Host 'Verifying remote inventory, SHA-256 and Python syntax ...'
    Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName -Script $remoteVerify

    Write-Host 'Acquiring the exclusive persistent hub deployment lock ...'
    $lockAttempted = $true
    $lockSafeToRelease = $true
    Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName -Script $remoteAcquireLock

    Write-Host 'Installing the verified package with a rollback-protected directory swap ...'
    $transactionAttempted = $true
    $lockSafeToRelease = $false
    Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName -Script $remoteTransaction

    Write-Host 'Running the independent service/hash/protocol commit gate ...'
    Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName -Script $remoteCommit
    $committed = $true
    $lockSafeToRelease = $true

    try {
        Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName `
            -Script $remoteCleanupBackup -Quiet
    } catch {
        Write-Warning "Hub deployment committed, but its exact temporary backup could not be removed: $($_.Exception.Message)"
    }
} catch {
    $primaryDiagnostic = $_.Exception.Message
    if ([string]::IsNullOrWhiteSpace($primaryDiagnostic)) {
        $primaryDiagnostic = $_.ToString()
    }
} finally {
    if ($transactionAttempted -and -not $committed) {
        try {
            # This independent connection covers a transaction SSH failure in
            # addition to the remote EXIT/HUP trap.
            Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName `
                -Script $remoteRecovery -Quiet
            $lockSafeToRelease = $true
        } catch {
            $recoveryError = $_.Exception
            $preserveRemoteState = $true
        }
    }
    if ($lockAttempted -and $lockSafeToRelease) {
        try {
            Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName `
                -Script $remoteReleaseLockIfOwned -Quiet
        } catch {
            $lockReleaseError = $_.Exception
            $preserveRemoteState = $true
        }
    }
    if ($remoteCreated -and -not $preserveRemoteState) {
        try {
            Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName `
                -Script $remoteCleanupStage -Quiet
        } catch {
            Write-Warning "Could not remove exact remote staging directory ${remoteRoot}: $($_.Exception.Message)"
        }
    } elseif ($remoteCreated) {
        Write-Warning "Preserving recovery evidence at $remoteRoot and $remoteLock because rollback or lock release was not verified."
    }
    if ($localCreated) {
        $tempParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([char[]]@('\', '/')) + [IO.Path]::DirectorySeparatorChar
        $candidate = [IO.Path]::GetFullPath($localTempRoot)
        if ($candidate.StartsWith($tempParent, $pathComparison) -and
            [IO.Path]::GetFileName($candidate) -eq "bosun-hub-deploy-$deployId") {
            Remove-Item -LiteralPath $candidate -Recurse -Force
        } else {
            Write-Warning "Refusing to clean unexpected local staging path: $candidate"
        }
    }
}

if ($null -ne $primaryDiagnostic) {
    if ($null -ne $recoveryError) {
        throw "Hub deployment failed: $primaryDiagnostic Automatic rollback/recovery also failed: $($recoveryError.Message)"
    }
    if ($null -ne $lockReleaseError) {
        throw "Hub deployment failed and recovery completed, but its deployment lock could not be released: $primaryDiagnostic Lock error: $($lockReleaseError.Message)"
    }
    if (-not $transactionAttempted) {
        throw "Hub deployment failed before the package swap; installed code was untouched: $primaryDiagnostic"
    }
    throw "Hub deployment failed and the previous package was restored: $primaryDiagnostic"
}
if ($null -ne $recoveryError) {
    throw "Hub deployment recovery failed: $($recoveryError.Message)"
}
if ($null -ne $lockReleaseError) {
    throw "Hub code and health checks passed, but the deployment lock could not be released: $($lockReleaseError.Message)"
}

Write-Host 'Hub code deployed: service active and stable, installed hashes verified, protocol PING returned ACK.'
