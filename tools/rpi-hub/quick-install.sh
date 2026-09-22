#!/usr/bin/env bash
# Entry point for the Desktop-exported Pi package. No Captain flash operations.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ $EUID -ne 0 ]]; then echo 'Run this installer with sudo.' >&2; exit 1; fi
source /etc/os-release
if [[ ${VERSION_CODENAME:-} != trixie ]]; then
    echo 'Use Raspberry Pi OS based on Debian Trixie. No services have been changed.' >&2; exit 1
fi
if [[ ! -r /proc/device-tree/model ]] || ! grep -aq 'Raspberry Pi' /proc/device-tree/model; then
    echo 'Run this package on the Raspberry Pi, not on your desktop computer.' >&2; exit 1
fi
if [[ ! -s "$root/editor/dist-stage/index.html" ]]; then echo 'Incomplete package: Stage is missing.' >&2; exit 1; fi
echo 'Installing Bosun hub, MIDI bridge and Stage. Keep the Pi powered and online.'
bash "$root/tools/rpi-hub/install.sh"
for service in bosun-hub.service bosun-midi.timer bosun-kiosk.service; do
    systemctl is-active --quiet "$service"
done
printf '\nBosun ready. Connect Captain to the Pi; connect Kemper by supported USB or two-way MIDI DIN to Captain.\n'
printf 'In Bosun choose Raspberry Pi (network), then Find Raspberry Pi.\n'
printf 'Stage in your browser: http://%s.local:8080/\n' "$(hostname -s)"
