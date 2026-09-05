[CmdletBinding()]
param(
    [ValidateNotNullOrEmpty()]
    [string]$HostName = "bosun-hub",

    [string]$FirmwarePath,

    [string[]]$Files,

    [ValidateRange(5, 120)]
    [int]$PortWaitSeconds = 30,

    [switch]$DryRun
)

# Deploy only the Captain firmware through the Raspberry Pi's two USB CDC
# interfaces. The Stage bundle and hub installation are outside this script.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$pushTool = [IO.Path]::GetFullPath((Join-Path $repoRoot 'tools/push_firmware.py'))
if ([string]::IsNullOrWhiteSpace($FirmwarePath)) {
    $FirmwarePath = Join-Path $repoRoot 'firmware'
}

$expectedVendor = '239a'
$expectedProduct = '80f4'
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
        [switch]$Quiet,
        [switch]$Capture
    )

    # Windows PowerShell 5.1 promotes redirected native stderr to errors.
    # Collect the whole process result before judging its exit code, so an
    # SSH/Python warning cannot abort the transaction or hide its final error.
    $savedErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $nativeOutput = @(& $Executable @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $savedErrorActionPreference
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
        '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', $RemoteHost,
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
        throw 'A remote path contains a NUL character.'
    }
    $singleQuoteEscape = "'" + '"' + "'" + '"' + "'"
    return "'" + $Value.Replace("'", $singleQuoteEscape) + "'"
}

function Test-FirmwareTree {
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "Firmware directory not found: $Path"
    }
    if (-not (Test-Path -LiteralPath $pushTool -PathType Leaf)) {
        throw "Firmware push helper not found: $pushTool"
    }
    $rootItem = Get-Item -LiteralPath $Path -Force
    if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Firmware root must not be a symlink or reparse point: $($rootItem.FullName)"
    }
    $root = [IO.Path]::GetFullPath($rootItem.FullName)
    if (-not (Test-Path -LiteralPath (Join-Path $root 'code.py') -PathType Leaf)) {
        throw "Firmware tree is missing code.py: $root"
    }

    $items = @(Get-ChildItem -LiteralPath $root -Force -Recurse)
    foreach ($item in $items) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Firmware tree must not contain symlinks or reparse points: $($item.FullName)"
        }
    }

    # push_firmware.py prefers .mpy over its source sibling. Fail closed when
    # a checked-in artifact is visibly older or has the wrong bytecode header.
    foreach ($artifact in @($items | Where-Object { -not $_.PSIsContainer -and $_.Extension -eq '.mpy' })) {
        $bytes = [IO.File]::ReadAllBytes($artifact.FullName)
        if ($bytes.Length -lt 2 -or $bytes[0] -ne 0x43 -or $bytes[1] -ne 0x06) {
            throw "Invalid CircuitPython mpy-v6 artifact: $($artifact.FullName)"
        }
        $source = [IO.Path]::ChangeExtension($artifact.FullName, '.py')
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            $sourceItem = Get-Item -LiteralPath $source
            if ($artifact.LastWriteTimeUtc -lt $sourceItem.LastWriteTimeUtc) {
                throw "MPY artifact is older than its source; rebuild before deploy: $($artifact.FullName)"
            }
        }
    }
    return $root
}

if ($HostName.StartsWith('-') -or $HostName -notmatch '^[A-Za-z0-9_.@-]+$') {
    throw "Unsafe or unsupported SSH host name: $HostName"
}
$firmwareRoot = Test-FirmwareTree -Path $FirmwarePath
$selectedFiles = @()
if ($null -ne $Files -and $Files.Count -gt 0) {
    $rootPrefix = $firmwareRoot.TrimEnd([char[]]@('\', '/')) + [IO.Path]::DirectorySeparatorChar
    $seenFiles = @{}
    # -File in Windows PowerShell cannot marshal an array reliably, so also
    # accept a comma-separated value (firmware paths cannot contain commas).
    foreach ($rawFileArgument in $Files) {
        foreach ($rawFile in $rawFileArgument.Split(',')) {
            $relative = $rawFile.Trim().Replace('\', '/')
            $segments = @($relative.Split('/'))
            if ([string]::IsNullOrWhiteSpace($relative) -or
                $relative.StartsWith('/') -or
                $relative -match '^[A-Za-z]:' -or
                $relative -notmatch '^[A-Za-z0-9._/-]+$' -or
                @($segments | Where-Object { $_ -in @('', '.', '..') -or $_.StartsWith('-') }).Count -ne 0) {
                throw "Firmware file must be a normalized relative path: $rawFile"
            }
            $candidate = [IO.Path]::GetFullPath((Join-Path $firmwareRoot $relative.Replace('/', [IO.Path]::DirectorySeparatorChar)))
            if (-not $candidate.StartsWith($rootPrefix, $pathComparison) -or
                -not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
                throw "Selected firmware file is missing or outside the firmware root: $rawFile"
            }
            $key = $relative.ToLowerInvariant()
            if ($seenFiles.ContainsKey($key)) {
                throw "Duplicate selected firmware file: $rawFile"
            }
            $seenFiles[$key] = $true
            $selectedFiles += $relative
        }
    }

    # PUT_FILE_END commits each module independently. Keep dependencies in
    # the caller's order, but never expose a new app root before its core
    # modules, and never expose code.py before the complete library set.
    $dependencyFiles = @($selectedFiles | Where-Object {
        $_ -notin @('lib/captain/app.py', 'lib/captain/app.mpy', 'code.py', 'code.mpy')
    })
    $applicationFiles = @($selectedFiles | Where-Object {
        $_ -in @('lib/captain/app.py', 'lib/captain/app.mpy')
    })
    $entryPointFiles = @($selectedFiles | Where-Object {
        $_ -in @('code.py', 'code.mpy')
    })
    $selectedFiles = @($dependencyFiles + $applicationFiles + $entryPointFiles)
}
$remoteFileArguments = ''
if ($selectedFiles.Count -gt 0) {
    $remoteFileArguments = ' --files ' + (($selectedFiles | ForEach-Object {
        ConvertTo-ShellLiteral $_
    }) -join ' ')
}

$deployId = [Guid]::NewGuid().ToString('N')
$remoteRoot = "/tmp/bosun-captain-$deployId"
if ($remoteRoot -notmatch '^/tmp/bosun-captain-[a-f0-9]{32}$') {
    throw 'Internal error: unsafe remote staging path.'
}
$remoteRootLiteral = ConvertTo-ShellLiteral $remoteRoot

$remoteLibrary = @'
EXPECTED_VENDOR=239a
EXPECTED_PRODUCT=80f4
SERVICE=bosun-hub.service
CONSOLE_TTY=/dev/ttyACM0
DATA_TTY=/dev/ttyACM1
RUNTIME_URL=socket://127.0.0.1:9876
PORT_WAIT_SECONDS=__PORT_WAIT_SECONDS__

as_root() {
    if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo -n "$@"; fi
}

captain_sysfs() {
    cs_match=
    cs_count=0
    for cs_vendor_file in /sys/bus/usb/devices/*/idVendor; do
        [ -e "$cs_vendor_file" ] || continue
        cs_root=${cs_vendor_file%/idVendor}
        cs_vendor=$(tr 'A-F' 'a-f' < "$cs_vendor_file")
        cs_product=$(tr 'A-F' 'a-f' < "$cs_root/idProduct")
        if [ "$cs_vendor" = "$EXPECTED_VENDOR" ] && [ "$cs_product" = "$EXPECTED_PRODUCT" ]; then
            cs_count=$((cs_count + 1))
            cs_match=$(readlink -f "$cs_root")
        fi
    done
    if [ "$cs_count" -ne 1 ]; then
        printf 'expected exactly one Captain %s:%s, found %s\n' \
            "$EXPECTED_VENDOR" "$EXPECTED_PRODUCT" "$cs_count" >&2
        return 1
    fi
    printf '%s\n' "$cs_match"
}

usb_parent_for_tty() {
    up_name=${1##*/}
    up_path=$(readlink -f "/sys/class/tty/$up_name/device") || return 1
    while [ -n "$up_path" ] && [ "$up_path" != / ]; do
        if [ -r "$up_path/idVendor" ] && [ -r "$up_path/idProduct" ]; then
            printf '%s\n' "$up_path"
            return 0
        fi
        up_path=${up_path%/*}
        [ -n "$up_path" ] || up_path=/
    done
    return 1
}

validate_captain_ports() {
    vp_captain=$(captain_sysfs) || return 1
    for vp_tty in "$CONSOLE_TTY" "$DATA_TTY"; do
        if [ ! -c "$vp_tty" ]; then
            printf 'missing Captain CDC port: %s\n' "$vp_tty" >&2
            return 1
        fi
        vp_parent=$(usb_parent_for_tty "$vp_tty") || {
            printf 'cannot resolve USB parent for %s\n' "$vp_tty" >&2
            return 1
        }
        if [ "$vp_parent" != "$vp_captain" ]; then
            printf '%s does not belong to the sole %s:%s Captain\n' \
                "$vp_tty" "$EXPECTED_VENDOR" "$EXPECTED_PRODUCT" >&2
            return 1
        fi
    done
    printf '%s\n' "$vp_captain"
}

validate_captain_console() {
    vcc_captain=$(captain_sysfs) || return 1
    if [ ! -c "$CONSOLE_TTY" ]; then
        printf 'missing Captain console CDC port: %s\n' "$CONSOLE_TTY" >&2
        return 1
    fi
    vcc_parent=$(usb_parent_for_tty "$CONSOLE_TTY") || return 1
    [ "$vcc_parent" = "$vcc_captain" ] || {
        printf '%s does not belong to the sole %s:%s Captain\n' \
            "$CONSOLE_TTY" "$EXPECTED_VENDOR" "$EXPECTED_PRODUCT" >&2
        return 1
    }
}

report_captain_diagnostics() {
    if [ -c "$CONSOLE_TTY" ]; then
        printf 'BOSUN_CAPTAIN_CONSOLE=present\n' >&2
    else
        printf 'BOSUN_CAPTAIN_CONSOLE=missing\n' >&2
    fi
    if [ -c "$DATA_TTY" ]; then
        printf 'BOSUN_CAPTAIN_DATA=present\n' >&2
    else
        printf 'BOSUN_CAPTAIN_DATA=missing\n' >&2
    fi
    if ! validate_captain_console >/dev/null 2>&1; then
        printf 'BOSUN_CAPTAIN_SAFE_MODE=unavailable_no_valid_console\n' >&2
        return 0
    fi
    as_root python3 - "$CONSOLE_TTY" <<'PY' >&2 || true
import re
import serial
import sys
import time

device = sys.argv[1]
try:
    port = serial.Serial(device, 115200, timeout=0.15, write_timeout=1.0)
    try:
        port.reset_input_buffer()
        port.write(b"\x03\x03")
        port.flush()
        time.sleep(0.25)
        while port.read(4096):
            pass
        command = (
            b'import supervisor; print("BOSUN_CAPTAIN_"+"SAFE_MODE="+'
            b'str(supervisor.runtime.safe_mode_reason))\r\n'
        )
        port.write(command)
        port.flush()
        deadline = time.monotonic() + 1.5
        response = bytearray()
        while time.monotonic() < deadline:
            chunk = port.read(4096)
            if chunk:
                response.extend(chunk)
                if len(response) > 16384:
                    del response[:-16384]
            else:
                time.sleep(0.02)
        matches = re.findall(rb"BOSUN_CAPTAIN_SAFE_MODE=([^\r\n]+)", response)
        if matches:
            print("BOSUN_CAPTAIN_SAFE_MODE=" +
                  matches[-1].decode("utf-8", "replace").strip())
        else:
            print("BOSUN_CAPTAIN_SAFE_MODE=unavailable_no_reply")
        # Leave a normally-running Captain running after this diagnostic. In
        # safe mode Ctrl-D simply retries the boot after the reason is saved.
        try:
            port.write(b"\x04")
            port.flush()
        except (OSError, serial.SerialException):
            pass
    finally:
        port.close()
except Exception as error:
    print("BOSUN_CAPTAIN_SAFE_MODE=unavailable_%s" %
          type(error).__name__)
PY
}

require_captain_ports() {
    if rcp_captain=$(validate_captain_ports 2>/dev/null); then
        printf '%s\n' "$rcp_captain"
        return 0
    fi
    printf 'Captain CDC validation failed\n' >&2
    report_captain_diagnostics
    return 1
}

wait_for_captain_ports() {
    wf_attempts=$((PORT_WAIT_SECONDS * 4))
    wf_stable=0
    wf_index=0
    udevadm settle --timeout="$PORT_WAIT_SECONDS" || true
    while [ "$wf_index" -lt "$wf_attempts" ]; do
        if validate_captain_ports >/dev/null 2>&1; then
            wf_stable=$((wf_stable + 1))
            if [ "$wf_stable" -ge 4 ]; then
                return 0
            fi
        else
            wf_stable=0
        fi
        wf_index=$((wf_index + 1))
        sleep 0.25
    done
    printf 'Captain ports did not stabilize within %s seconds\n' "$PORT_WAIT_SECONDS" >&2
    report_captain_diagnostics
    return 1
}

normal_console_reset() {
    validate_captain_console
    as_root python3 - "$CONSOLE_TTY" <<'PY'
import serial
import sys
import time

port = serial.Serial(sys.argv[1], 115200, timeout=0.15, write_timeout=1.0)
try:
    def write_all(payload):
        view = memoryview(payload)
        while view:
            written = port.write(view)
            if not isinstance(written, int) or written <= 0:
                raise OSError("serial write made no progress")
            view = view[written:]

    port.reset_input_buffer()
    write_all(b"\x03\x03")
    port.flush()
    deadline = time.monotonic() + 1.5
    prompt = bytearray()
    while time.monotonic() < deadline and b">>>" not in prompt:
        chunk = port.read(4096)
        if chunk:
            prompt.extend(chunk)
        else:
            time.sleep(0.02)
    if b">>>" not in prompt:
        raise RuntimeError("CircuitPython console prompt not confirmed")

    command = (
        b"import microcontroller; "
        b"microcontroller.on_next_reset(microcontroller.RunMode.NORMAL); "
        b'print("BOSUN_CAPTAIN_"+"NORMAL_RESET=armed"); '
        b"microcontroller.reset()\r\n"
    )
    try:
        write_all(command)
        port.flush()
        deadline = time.monotonic() + 2.0
        response = bytearray()
        while time.monotonic() < deadline:
            chunk = port.read(4096)
            if chunk:
                response.extend(chunk)
                if b"BOSUN_CAPTAIN_NORMAL_RESET=armed" in response:
                    print("BOSUN_CAPTAIN_NORMAL_RESET=armed")
                    raise SystemExit(0)
            else:
                time.sleep(0.02)
    except (OSError, serial.SerialException):
        # A reset normally tears down ACM0 before its final text is readable.
        # The following port-stability and PING gates prove the actual result.
        print("BOSUN_CAPTAIN_NORMAL_RESET=requested_disconnect")
        raise SystemExit(0)
    raise RuntimeError("normal reset acknowledgement not received")
finally:
    port.close()
PY
}

wait_for_captain_ping() {
    as_root python3 - "$DATA_TTY" "$PORT_WAIT_SECONDS" <<'PY'
import json
import serial
import sys
import time

device = sys.argv[1]
deadline = time.monotonic() + float(sys.argv[2])
request = {"type": "PING", "id": "deploy-readiness"}
payload = (json.dumps(request, separators=(",", ":")) + "\n").encode()
last_error = "no response"

while time.monotonic() < deadline:
    try:
        port = serial.Serial(
            device, 115200, timeout=0.2, write_timeout=2.0,
        )
        try:
            port.reset_input_buffer()
            port.write(b"\n")
            view = memoryview(payload)
            while view:
                written = port.write(view)
                if not isinstance(written, int) or written <= 0:
                    raise OSError("serial write made no progress")
                view = view[written:]
            port.flush()
            receive = bytearray()
            attempt_deadline = min(deadline, time.monotonic() + 2.0)
            while time.monotonic() < attempt_deadline:
                chunk = port.read(4096)
                if chunk:
                    receive.extend(chunk)
                    while b"\n" in receive:
                        raw, _, tail = bytes(receive).partition(b"\n")
                        receive[:] = tail
                        try:
                            reply = json.loads(raw.strip())
                        except (ValueError, UnicodeDecodeError):
                            continue
                        if (isinstance(reply, dict)
                                and reply.get("id") == request["id"]
                                and reply.get("type") == "ACK"):
                            print("BOSUN_CAPTAIN_PING=ACK")
                            raise SystemExit(0)
                else:
                    time.sleep(0.02)
            last_error = "PING timed out"
        finally:
            port.close()
    except SystemExit:
        raise
    except (OSError, serial.SerialException) as error:
        last_error = str(error)
    time.sleep(0.25)

raise SystemExit("Captain protocol did not become ready: " + last_error)
PY
}

verify_captain_runtime() {
    # Validate the same TCP path used by Stage, not just the CDC endpoint.
    # One global deadline bounds connection readiness and all five requests.
    as_root python3 - "$RUNTIME_URL" "$PORT_WAIT_SECONDS" <<'PY'
import json
import socket
import sys
import time
from urllib.parse import urlsplit

url = sys.argv[1]
timeout = float(sys.argv[2])
parsed = urlsplit(url)
if (parsed.scheme != "socket" or not parsed.hostname or parsed.port is None
        or parsed.path not in ("", "/")):
    raise SystemExit("invalid Captain runtime URL: " + url)

deadline = time.monotonic() + timeout
last_error = "TCP endpoint did not accept a connection"
connection = None
while connection is None and time.monotonic() < deadline:
    try:
        remaining = max(0.05, deadline - time.monotonic())
        connection = socket.create_connection(
            (parsed.hostname, parsed.port), timeout=min(1.0, remaining))
    except OSError as error:
        last_error = str(error)
        time.sleep(0.1)
if connection is None:
    raise SystemExit("Captain runtime verification failed: " + last_error)

receive = bytearray()

class ProtocolError(RuntimeError):
    def __init__(self, request_type, reply):
        self.reply = reply
        super().__init__("%s returned ERROR: %r" % (request_type, reply))

def call(message, expected_type):
    payload = (json.dumps(message, separators=(",", ":")) + "\n").encode()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("global deadline expired before " + message["type"])
    connection.settimeout(min(2.0, remaining))
    connection.sendall(payload)
    while time.monotonic() < deadline:
        while b"\n" in receive:
            raw, _, tail = bytes(receive).partition(b"\n")
            receive[:] = tail
            try:
                reply = json.loads(raw.strip())
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(reply, dict) or reply.get("id") != message["id"]:
                continue
            if reply.get("type") == "ERROR":
                raise ProtocolError(message["type"], reply)
            if reply.get("type") != expected_type:
                raise RuntimeError("%s returned %r, expected %s" %
                                   (message["type"], reply, expected_type))
            return reply
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        connection.settimeout(min(0.25, remaining))
        try:
            chunk = connection.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            raise ConnectionError("TCP connection closed during " +
                                  message["type"])
        receive.extend(chunk)
        if len(receive) > 1024 * 1024:
            raise RuntimeError("unterminated runtime response exceeds 1 MiB")
    raise TimeoutError("no response to %s#%s" %
                       (message["type"], message["id"]))

try:
    # Fence any partial input inherited by the bridge, then test every read
    # used by Stage's cold bootstrap. GET_PATCH deliberately targets the
    # coordinates reported by the running firmware instead of assuming B1/R1.
    connection.sendall(b"\n")
    ping_attempt = 0
    while True:
        ping_attempt += 1
        try:
            call({"type": "PING",
                  "id": "deploy-runtime-ping-%d" % ping_attempt}, "ACK")
            break
        except ProtocolError as error:
            # systemd may report active a fraction before the hub's serial
            # worker opens ACM1. Only this explicit readiness state is
            # retryable; firmware ERRORs and all later link loss fail closed.
            if error.reply.get("error") != "link_down":
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)
    device = call({"type": "GET_DEVICE_INFO",
                   "id": "deploy-runtime-device"}, "DEVICE_INFO")
    current = device.get("current")
    if not isinstance(current, dict):
        raise RuntimeError("DEVICE_INFO has no current object")
    bank, slot = current.get("bank"), current.get("slot")
    if type(bank) is not int or bank < 1 or type(slot) is not int or slot < 1:
        raise RuntimeError("DEVICE_INFO has invalid current coordinates: %r" %
                           (current,))
    if "preset_navigation" not in device:
        raise RuntimeError("DEVICE_INFO is from stale firmware")

    context = call({"type": "GET_CONTEXT",
                    "id": "deploy-runtime-context"}, "CONTEXT")
    if not isinstance(context.get("context"), dict):
        raise RuntimeError("GET_CONTEXT has no context object")

    patch = call({"type": "GET_PATCH", "id": "deploy-runtime-patch",
                  "bank": bank, "slot": slot}, "PATCH")
    if (patch.get("bank"), patch.get("slot")) != (bank, slot):
        raise RuntimeError("GET_PATCH returned the wrong coordinates")
    if not isinstance(patch.get("patch"), dict):
        raise RuntimeError("GET_PATCH has no patch object")

    patch_list = call({"type": "LIST_PATCHES",
                       "id": "deploy-runtime-patches"}, "PATCH_LIST")
    patches = patch_list.get("patches")
    if not isinstance(patches, list):
        raise RuntimeError("LIST_PATCHES has no patches array")
    if not any(isinstance(item, dict) and item.get("bank") == bank
               and item.get("slot") == slot for item in patches):
        raise RuntimeError("LIST_PATCHES omits the current patch")
except Exception as error:
    raise SystemExit("Captain runtime verification failed: " + str(error))
finally:
    connection.close()

print("BOSUN_CAPTAIN_RUNTIME=OK bank=%d slot=%d patches=%d" %
      (bank, slot, len(patches)))
PY
}
'@.Replace('__PORT_WAIT_SECONDS__', $PortWaitSeconds.ToString([Globalization.CultureInfo]::InvariantCulture))

$remotePreflight = $remoteLibrary + @'

set -eu
command -v base64 >/dev/null
command -v readlink >/dev/null
command -v tr >/dev/null
command -v udevadm >/dev/null
command -v python3 >/dev/null
if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null
    sudo -n true
fi
as_root python3 -c 'import serial,sys; assert sys.version_info >= (3,10)'
systemctl cat "$SERVICE" >/dev/null
if ! systemctl is-active --quiet "$SERVICE"; then
    printf '%s must be active before deployment\n' "$SERVICE" >&2
    exit 1
fi
captain=$(require_captain_ports)
if command -v vcgencmd >/dev/null 2>&1; then
    throttled=$(vcgencmd get_throttled 2>&1 || true)
else
    throttled=unavailable
fi
printf 'BOSUN_CAPTAIN_SYSFS=%s\n' "$captain"
printf 'BOSUN_THROTTLED=%s\n' "$throttled"
'@

$remoteTransaction = $remoteLibrary + @'

set -eu
root=__REMOTE_ROOT__
hub_restart_required=0

finish() {
    finish_status=$?
    trap - EXIT HUP INT TERM
    if [ "$hub_restart_required" -eq 1 ]; then
        if ! systemctl is-active --quiet "$SERVICE"; then
            if ! as_root systemctl restart "$SERVICE"; then
                printf 'failed to restart %s\n' "$SERVICE" >&2
                finish_status=97
            fi
        fi
        if ! as_root systemctl is-active --quiet "$SERVICE"; then
            printf '%s is not active after restart\n' "$SERVICE" >&2
            finish_status=98
        elif ! require_captain_ports >/dev/null; then
            printf 'BOSUN_HUB_SERVICE=active_but_captain_missing\n' >&2
            [ "$finish_status" -ne 0 ] || finish_status=96
        else
            printf 'BOSUN_HUB_SERVICE=active captain=present\n'
        fi
    fi
    if ! rm -rf -- "$root"; then
        printf 'failed to remove exact staging directory %s\n' "$root" >&2
        [ "$finish_status" -ne 0 ] || finish_status=99
    fi
    exit "$finish_status"
}
trap finish EXIT HUP INT TERM

test -f "$root/push_firmware.py"
test -f "$root/firmware/code.py"
captain=$(require_captain_ports)

# From this point every exit path must restore the hub service.
hub_restart_required=1
as_root systemctl stop "$SERVICE"
if systemctl is-active --quiet "$SERVICE"; then
    printf 'failed to stop %s before taking ACM1\n' "$SERVICE" >&2
    exit 1
fi

# A healthy data protocol needs no pre-deploy USB or CircuitPython reset. A
# bus-level usbreset can itself put RP2040/CircuitPython into HARD_FAULT safe
# mode and remove ACM1. Only if the direct PING is unhealthy do we arm NORMAL
# mode explicitly and perform a console reset.
if wait_for_captain_ping; then
    printf 'BOSUN_CAPTAIN_PREPARE=healthy_no_reset\n'
else
    printf 'Captain data PING failed; attempting one NORMAL console reset\n' >&2
    normal_console_reset
    sleep 5
    wait_for_captain_ports
    wait_for_captain_ping
    printf 'BOSUN_CAPTAIN_PREPARE=normal_console_reset\n'
fi
as_root python3 "$root/push_firmware.py" \
    --port "$DATA_TTY" --firmware "$root/firmware"__FILE_ARGUMENTS__ --reboot

# push_firmware requests a hard reboot. Do not give ACM1 back to systemd
# until both interfaces have re-enumerated and stayed stable for one second.
wait_for_captain_ports
wait_for_captain_ping
as_root systemctl start "$SERVICE"
as_root systemctl is-active --quiet "$SERVICE"
verify_captain_runtime
'@.Replace('__REMOTE_ROOT__', $remoteRootLiteral).
    Replace('__FILE_ARGUMENTS__', $remoteFileArguments)

$remoteRecovery = $remoteLibrary + @'

set -eu
if ! systemctl is-active --quiet "$SERVICE"; then
    as_root systemctl restart "$SERVICE"
fi
as_root systemctl is-active --quiet "$SERVICE"
require_captain_ports >/dev/null
verify_captain_runtime
printf 'BOSUN_HUB_RECOVERY=OK captain=present\n'
'@

if ($DryRun) {
    Write-Host "DRY-RUN: no SSH connection or remote change will be made."
    Write-Host "Host: $HostName"
    Write-Host "Firmware: $firmwareRoot"
    Write-Host "Files: $(if ($selectedFiles.Count) { $selectedFiles -join ', ' } else { '<all except config>' })"
    Write-Host "Remote staging: $remoteRoot"
    Write-Host '--- PREFLIGHT ---'
    Write-Output $remotePreflight
    Write-Host '--- TRANSACTION ---'
    Write-Output $remoteTransaction
    Write-Host '--- LOCAL FINALLY RECOVERY ---'
    Write-Output $remoteRecovery
    return
}

$ssh = Get-ApplicationPath -Names @('ssh.exe', 'ssh')
$scp = Get-ApplicationPath -Names @('scp.exe', 'scp')

Write-Host "Checking SSH, Captain identity, CDC ownership and Pi health on $HostName ..."
$preflightOutput = @(Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName `
    -Script $remotePreflight -Quiet -Capture)
foreach ($line in $preflightOutput) {
    if ($line -like 'BOSUN_THROTTLED=*') {
        $value = $line.Substring('BOSUN_THROTTLED='.Length).Trim()
        if ($value -ne 'throttled=0x0') {
            Write-Warning "RPi power/thermal status is not clean: $value"
        } else {
            Write-Host "RPi power/thermal status: $value"
        }
    } elseif ($line -like 'BOSUN_CAPTAIN_SYSFS=*') {
        Write-Host "Validated $expectedVendor`:$expectedProduct at $($line.Substring('BOSUN_CAPTAIN_SYSFS='.Length))"
    }
}

$remoteCreated = $false
$transactionAttempted = $false
$primaryDiagnostic = $null
$recoveryError = $null
try {
    $createRemote = "set -eu; umask 077; test ! -e $remoteRootLiteral; mkdir -- $remoteRootLiteral"
    Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName -Script $createRemote -Quiet
    $remoteCreated = $true

    Write-Host 'Uploading the deploy helper and firmware to the unique remote staging directory ...'
    Invoke-CheckedNative -Executable $scp -Arguments @(
        '-q', '-p', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
        $pushTool, "${HostName}:$remoteRoot/push_firmware.py"
    ) -Quiet
    Invoke-CheckedNative -Executable $scp -Arguments @(
        '-q', '-p', '-r', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
        $firmwareRoot, "${HostName}:$remoteRoot/firmware"
    ) -Quiet

    Write-Host 'Stopping the hub, verifying the Captain data protocol, and deploying on ACM1 ...'
    $transactionAttempted = $true
    Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName -Script $remoteTransaction
} catch {
    # Preserve the native/SSH diagnostic now. Rethrowing an Exception object
    # after the finally boundary can render only the later `throw` site in
    # Windows PowerShell, hiding the command output that explains the failure.
    $primaryDiagnostic = $_.Exception.Message
    if ([string]::IsNullOrWhiteSpace($primaryDiagnostic)) {
        $primaryDiagnostic = $_.ToString()
    }
} finally {
    if ($transactionAttempted) {
        try {
            # The remote EXIT/HUP trap is the first line of defence. This
            # independent SSH call covers a broken transaction connection.
            Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName `
                -Script $remoteRecovery -Quiet
        } catch {
            $recoveryError = $_.Exception
        }
    }
    if ($remoteCreated) {
        try {
            Invoke-CheckedRemote -Ssh $ssh -RemoteHost $HostName `
                -Script "rm -rf -- $remoteRootLiteral" -Quiet
        } catch {
            Write-Warning "Could not remove exact remote staging directory ${remoteRoot}: $($_.Exception.Message)"
        }
    }
}

if ($null -ne $primaryDiagnostic) {
    if ($null -ne $recoveryError) {
        throw "Captain deployment failed: $primaryDiagnostic Hub recovery also failed: $($recoveryError.Message)"
    }
    throw "Captain deployment failed: $primaryDiagnostic"
}
if ($null -ne $recoveryError) {
    throw "Captain deployed, but hub active-state verification failed: $($recoveryError.Message)"
}

Write-Host 'Captain firmware deployed; full runtime bootstrap verified and bosun-hub.service is active.'
