#!/usr/bin/env bash
# Install RP2040 ROM access for hub-owned firmware updates.
# Run from a Bosun checkout on the Pi. This never opens or flashes a Captain.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ $EUID -ne 0 ]]; then
    printf 'Run with sudo; this installs a USB access rule.\n' >&2
    exit 1
fi
command -v picotool >/dev/null || { printf 'Install picotool first.\n' >&2; exit 1; }
id bosun >/dev/null
getent group plugdev >/dev/null
usermod --append --groups plugdev bosun
install -m 644 "$root/tools/rpi-hub/udev/60-bosun-update.rules" /etc/udev/rules.d/60-bosun-update.rules
udevadm control --reload-rules
# Let a verified flash or rollback finish on a normal service restart.
install -d -m 755 /etc/systemd/system/bosun-hub.service.d
printf '[Service]\nTimeoutStopSec=3600\n' > /etc/systemd/system/bosun-hub.service.d/update-timeout.conf
systemctl daemon-reload
printf 'Native update support installed. No Captain firmware was changed.\n'
