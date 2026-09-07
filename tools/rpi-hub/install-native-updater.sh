#!/usr/bin/env bash
# Install the trusted converter and RP2040 ROM access for hub-owned updates.
# Run from a Bosun checkout on the Pi. This never opens or flashes a Captain.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ $EUID -ne 0 ]]; then
    printf 'Run with sudo; this installs a converter and a USB access rule.\n' >&2
    exit 1
fi
command -v cc >/dev/null || { printf 'Install build-essential first.\n' >&2; exit 1; }
command -v picotool >/dev/null || { printf 'Install picotool first.\n' >&2; exit 1; }
id bosun >/dev/null
getent group plugdev >/dev/null
native="$root/firmware-native"
build="$(mktemp -d)"
trap 'rm -f -- "$build/bosun_storage_image"; rmdir -- "$build"' EXIT
cc -std=c11 -O2 -DNDEBUG -DBOSUN_PLATFORM_RP2040=1 \
    -DLFS_NO_MALLOC -DLFS_NO_DEBUG -DLFS_NO_WARN -DLFS_NO_ERROR \
    -DLFS_NAME_MAX=63 -DLFS_FILE_MAX=262144 \
    -I"$native/include" -I"$native/third_party/littlefs" \
    "$native/platform/host/storage_image.c" "$native/platform/rp2040/storage.c" \
    "$native/src/storage_path.c" "$native/src/config.c" "$native/src/json.c" \
    "$native/third_party/littlefs/lfs.c" "$native/third_party/littlefs/lfs_util.c" \
    -lm -o "$build/bosun_storage_image"
install -d -m 755 /opt/bosun-hub/bin
install -m 755 "$build/bosun_storage_image" /opt/bosun-hub/bin/bosun_storage_image
usermod --append --groups plugdev bosun
install -m 644 "$root/tools/rpi-hub/udev/60-bosun-update.rules" /etc/udev/rules.d/60-bosun-update.rules
udevadm control --reload-rules
# Let a verified flash or rollback finish on a normal service restart.
install -d -m 755 /etc/systemd/system/bosun-hub.service.d
printf '[Service]\nTimeoutStopSec=3600\n' > /etc/systemd/system/bosun-hub.service.d/update-timeout.conf
systemctl daemon-reload
printf 'Native update support installed. No Captain firmware was changed.\n'
