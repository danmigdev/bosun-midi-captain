#!/usr/bin/env bash
#
# Install / update the bosun-hub appliance on Raspberry Pi OS (Trixie).
# Idempotent: safe to re-run to pick up new code.
#
#   sudo bash install.sh                 # from a checkout of this dir
#
# What it does NOT do (yet): read-only overlay root, watchdog, the HDMI
# mode for the Wisecoco panel, the cage/chromium kiosk. Those land once
# the panel is on hand.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/opt/bosun-hub
HUB_USER=bosun

if [[ $EUID -ne 0 ]]; then
    echo "run with sudo" >&2
    exit 1
fi

echo "== packages =="
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3-serial python3-websockets alsa-utils

echo "== code -> $DEST =="
mkdir -p "$DEST"
rsync -a --delete \
    --exclude __pycache__ --exclude '*.pyc' --exclude '.git' \
    "$SRC"/bosun_hub "$SRC"/requirements.txt "$SRC"/README.md "$DEST"/
# keep an already-built stage bundle across updates
mkdir -p "$DEST/stage"
chown -R "$HUB_USER:$HUB_USER" "$DEST"

echo "== systemd units =="
install -m 644 "$SRC"/systemd/bosun-hub.service   /etc/systemd/system/
install -m 644 "$SRC"/systemd/bosun-midi.service  /etc/systemd/system/
install -m 644 "$SRC"/systemd/bosun-midi.timer    /etc/systemd/system/
install -m 644 "$SRC"/udev/33-bosun-midi.rules    /etc/udev/rules.d/

systemctl daemon-reload
udevadm control --reload
systemctl enable --now bosun-hub.service
systemctl enable --now bosun-midi.timer

echo
echo "== status =="
systemctl --no-pager --lines=0 status bosun-hub.service || true
echo
echo "done. protocol on tcp://<pi>:9876, ws://<pi>:8081, http://<pi>:8080"
