#!/usr/bin/env bash
#
# Install / update the bosun-hub appliance on Raspberry Pi OS (Trixie).
# Idempotent: safe to re-run to pick up new code.
#
#   sudo bash install.sh                 # from a checkout of this dir
#
# A built editor/dist-stage bundle is installed when present. The service,
# kiosk launcher and systemd units are always installed reproducibly.

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
    python3-serial python3-websockets alsa-utils rsync \
    cage chromium wlr-randr wayvnc

if ! id "$HUB_USER" >/dev/null 2>&1; then
    useradd --system --create-home "$HUB_USER"
fi
# Keep device access correct on upgrades too, not only when the account is
# first created. Some distributions expose seatd through `seat`, while Debian
# configures its socket for `video`; add every relevant group that exists.
for group in audio video input render plugdev seat; do
    if getent group "$group" >/dev/null 2>&1; then
        usermod --append --groups "$group" "$HUB_USER"
    fi
done

echo "== code -> $DEST =="
mkdir -p "$DEST"
rsync -a --delete \
    --exclude __pycache__ --exclude '*.pyc' --exclude '.git' \
    "$SRC"/bosun_hub "$SRC"/requirements.txt "$SRC"/README.md "$DEST"/
rsync -a --delete "$SRC"/kiosk/ "$DEST"/kiosk/
# Deploys are commonly staged from Windows, where Git may materialize shell
# scripts with CRLF. A CR in the shebang makes systemd fail with 203/EXEC.
sed -i 's/\r$//' "$DEST"/kiosk/*.sh
chmod 755 "$DEST"/kiosk/*.sh
# keep an already-built stage bundle across updates
mkdir -p "$DEST/stage"
STAGE_BUILD="$(cd "$SRC/../../editor" 2>/dev/null && pwd)/dist-stage"
if [[ -f "$STAGE_BUILD/stage-kiosk.html" ]]; then
    rsync -a --delete "$STAGE_BUILD"/ "$DEST"/stage/
    # The appliance serves /index.html; Vite keeps the explicit source entry
    # name in multi-page builds, so install it under the public kiosk name.
    cp "$STAGE_BUILD/stage-kiosk.html" "$DEST"/stage/index.html
elif [[ -f "$STAGE_BUILD/index.html" ]]; then
    rsync -a --delete "$STAGE_BUILD"/ "$DEST"/stage/
else
    echo "warning: no editor/dist-stage Stage bundle; preserving installed bundle" >&2
fi
chown -R "$HUB_USER:$HUB_USER" "$DEST"

echo "== systemd units =="
install -m 644 "$SRC"/systemd/bosun-hub.service   /etc/systemd/system/
install -m 644 "$SRC"/systemd/bosun-midi.service  /etc/systemd/system/
install -m 644 "$SRC"/systemd/bosun-midi.timer    /etc/systemd/system/
install -m 644 "$SRC"/systemd/bosun-kiosk.service /etc/systemd/system/
install -m 644 "$SRC"/systemd/bosun-wayvnc.service /etc/systemd/system/
install -m 644 "$SRC"/udev/33-bosun-midi.rules    /etc/udev/rules.d/

systemctl daemon-reload
udevadm control --reload
systemctl enable bosun-hub.service bosun-midi.timer bosun-kiosk.service
systemctl restart bosun-hub.service
systemctl restart bosun-midi.timer
systemctl restart bosun-kiosk.service

# `restart` returning successfully only means systemd accepted and completed
# the start job.  Type=simple can still leave the active state immediately
# afterwards (for example on an import or bind failure), so never let the
# diagnostic `status || true` below turn a broken hub install into exit 0.
if ! systemctl is-active --quiet bosun-hub.service; then
    echo "bosun-hub.service is not active after restart" >&2
    systemctl --no-pager --lines=30 status bosun-hub.service >&2 || true
    exit 1
fi

# A successful process spawn is not sufficient here: Chromium silently falls
# back to ~/.config when its requested profile is unavailable. Verify the
# persistent systemd-managed directory as part of every install/update.
systemctl is-active --quiet bosun-kiosk.service
test -d /var/lib/bosun-hub/chromium
test ! -L /var/lib/bosun-hub/chromium
profile_owner="$(stat -c %U /var/lib/bosun-hub/chromium)"
if [[ "$profile_owner" != "$HUB_USER" ]]; then
    echo "unexpected Chromium profile owner: $profile_owner" >&2
    exit 1
fi
if ! runuser -u "$HUB_USER" -- test -w /var/lib/bosun-hub/chromium; then
    echo "Chromium profile is not writable by $HUB_USER" >&2
    exit 1
fi

echo
echo "== status =="
systemctl --no-pager --lines=0 status bosun-hub.service || true
echo
echo "done. protocol on tcp://<pi>:9876, ws://<pi>:8081, http://<pi>:8080"
