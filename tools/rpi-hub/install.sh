#!/usr/bin/env bash
#
# Install / update the bosun-hub appliance on Raspberry Pi OS (Trixie).
# Idempotent: safe to re-run to pick up new code.
#
#   sudo bash install.sh                 # from a checkout of this dir
#
# Build editor/dist-stage first. Re-running the installer preserves an existing
# Stage bundle when no replacement is present, and never flashes the Captain.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/opt/bosun-hub
HUB_USER=bosun
REPO_ROOT="$(cd "$SRC/../.." && pwd)"
STAGE_BUILD="$REPO_ROOT/editor/dist-stage"

if [[ $EUID -ne 0 ]]; then
    echo "run with sudo" >&2
    exit 1
fi

# Fail before changing the appliance when a fresh checkout has no Stage build.
if [[ ! -f "$STAGE_BUILD/stage-kiosk.html" && ! -f "$STAGE_BUILD/index.html" &&
      ! -f "$DEST/stage/index.html" ]]; then
    echo "Build Stage first: cd editor && npm install --no-audit --no-fund && npm run build:stage" >&2
    exit 1
fi
# Validate native updater inputs and the kiosk files before package/code changes.
# A partially copied checkout must fail here, not after services were changed.
for required in \
    tools/rpi-hub/kiosk/bosun-hdmi-recovery.py tools/rpi-hub/kiosk/bosun-stage-watchdog.py \
    tools/rpi-hub/udev/99-bosun-kiosk-input.rules \
    tools/rpi-hub/install-native-updater.sh tools/rpi-hub/udev/60-bosun-update.rules \
    tools/rpi-hub/pi-update/install_package.py \
    tools/rpi-hub/systemd/bosun-pi-update.path tools/rpi-hub/systemd/bosun-pi-update.service; do
    if [[ ! -f "$REPO_ROOT/$required" ]]; then
        echo "Run the installer from a complete Bosun checkout; missing $required." >&2
        exit 1
    fi
done

echo "== packages =="
# openssh-client provides ssh-keygen, which checks Desktop self-update packages.
packages=(python3-serial python3-websockets alsa-utils rsync
    cage chromium seatd wlr-randr wayvnc kanshi novnc websockify
    picotool openssh-client)
missing=()
for package in "${packages[@]}"; do
    if [[ "$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true)" != installed ]]; then
        missing+=("$package")
    fi
done
# A Pi without Internet access can still update when nothing is missing.
if ((${#missing[@]})); then
    echo "installing: ${missing[*]}"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${packages[@]}"
else
    echo "all required packages are installed; skipping apt-get"
fi

# On a Cortex-A53 (Raspberry Pi 3, Zero 2 W) Chromium must be linked around CPU
# erratum 843419, or Stage's page crashes at random ("Aw, Snap!"). Raspberry
# Pi's builds are from this version on; any later one stays as it is.
CHROMIUM_A53_FIXED=1:156.0.8078.108
chromium_crashes_here() {
    local installed
    grep -q '^CPU part.*0xd03' /proc/cpuinfo || return 1
    installed="$(dpkg-query -W -f='${Version}' chromium 2>/dev/null)" || return 1
    dpkg --compare-versions "$installed" lt "$CHROMIUM_A53_FIXED"
}
if chromium_crashes_here; then
    echo "== Chromium update =="
    echo "the installed Chromium can crash Stage on this Pi; this takes several minutes"
    # Never a reason to fail: the Pi may be offline, and the watchdog restarts a
    # crashed Stage. Only the network steps are bounded, dpkg must not be killed.
    chromium=(chromium chromium-common chromium-l10n chromium-sandbox)
    export DEBIAN_FRONTEND=noninteractive
    timeout 120 apt-get update -qq &&
        timeout 900 apt-get install -y -qq --only-upgrade --download-only "${chromium[@]}" &&
        apt-get install -y -qq --only-upgrade --no-download "${chromium[@]}" || true
    if chromium_crashes_here; then
        echo "warning: Chromium was not updated; Stage may restart at times. Rerun this installer when the Pi is online." >&2
    fi
fi

if ! id "$HUB_USER" >/dev/null 2>&1; then
    useradd --system --create-home "$HUB_USER"
fi
# Keep device access correct on upgrades too, not only when the account is
# first created. Some distributions expose seatd through `seat`, while Debian
# configures its socket for `video`; add every relevant group that exists.
for group in audio video input render plugdev seat dialout; do
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
# Only Stage belongs to the hub account (deploy-stage.ps1 keeps that owner).
# Root runs boot-splash files below $DEST at startup, so the directory, the
# code and every other tree stay root-owned; no service writes here. Skip
# links an older bosun-owned $DEST may hold: chmod follows them.
chown root:root "$DEST"
chmod 755 "$DEST"
find "$DEST" -mindepth 1 -maxdepth 1 ! -name stage ! -type l -exec chown -R root:root {} +
find "$DEST" -mindepth 1 -maxdepth 1 ! -name stage ! -type l -exec chmod -R go-w {} +
chown -R "$HUB_USER:$HUB_USER" "$DEST/stage"

echo "== systemd units =="
install -m 644 "$SRC"/systemd/bosun-hub.service   /etc/systemd/system/
install -m 644 "$SRC"/systemd/bosun-midi.service  /etc/systemd/system/
install -m 644 "$SRC"/systemd/bosun-midi.timer    /etc/systemd/system/
install -m 644 "$SRC"/systemd/bosun-kiosk.service /etc/systemd/system/
install -m 644 "$SRC"/systemd/bosun-wayvnc.service /etc/systemd/system/
install -m 644 "$SRC"/systemd/bosun-outputs.service /etc/systemd/system/
install -m 644 "$SRC"/systemd/bosun-stage-watchdog.service /etc/systemd/system/
install -m 644 "$SRC"/systemd/bosun-stage-vnc-web.service /etc/systemd/system/
install -m 644 "$SRC"/udev/33-bosun-midi.rules    /etc/udev/rules.d/
install -m 644 "$SRC"/udev/99-bosun-kiosk-input.rules /etc/udev/rules.d/

echo "== Pi self-update =="
# Desktop sends signed packages through the hub; root installs only those that
# verify against this key. A package may replace it (key rotation) only because
# it was itself signed with the current one.
if [[ -f "$SRC/allowed_signers" ]]; then
    install -d -o root -g root -m 0755 /etc/bosun
    install -o root -g root -m 0644 "$SRC/allowed_signers" /etc/bosun/allowed_signers
else
    echo "warning: no allowed_signers in this checkout; Desktop cannot update this Pi" >&2
fi
install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 "$SRC/pi-update/install_package.py" /usr/local/libexec/bosun-pi-update
install -o root -g root -m 0644 "$SRC/systemd/bosun-pi-update.service" /etc/systemd/system/bosun-pi-update.service
install -o root -g root -m 0644 "$SRC/systemd/bosun-pi-update.path" /etc/systemd/system/bosun-pi-update.path

bash "$SRC/install-native-updater.sh"
systemctl daemon-reload
udevadm control --reload
# Apply the CEC exclusion to devices already present before Cage reopens them.
udevadm trigger --action=change --subsystem-match=input
udevadm settle --timeout=10
systemctl enable --now seatd.service
systemctl enable bosun-hub.service bosun-midi.timer bosun-kiosk.service
systemctl enable bosun-outputs.service bosun-wayvnc.service bosun-stage-vnc-web.service
systemctl enable bosun-stage-watchdog.service
# bosun-pi-update.service may be the caller of this script: only enable it (it
# checks for an interrupted update at startup), never stop, start or restart it.
systemctl enable bosun-pi-update.service
systemctl enable --now bosun-pi-update.path
systemctl restart bosun-hub.service
systemctl restart bosun-midi.timer
systemctl restart bosun-kiosk.service
systemctl restart bosun-stage-watchdog.service
systemctl start bosun-stage-vnc-web.service

# `restart` returning successfully only means systemd accepted and completed
# the start job.  Type=simple can still leave the active state immediately
# afterwards (for example on an import or bind failure), so never let the
# diagnostic `status || true` below turn a broken hub install into exit 0.
# The reason comes last: the Pi self-update reports the final output line.
if ! systemctl is-active --quiet bosun-hub.service; then
    systemctl --no-pager --lines=30 status bosun-hub.service >&2 || true
    echo "bosun-hub.service is not active after restart" >&2
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

# Keep an opted-in boot screen on the release just installed. A fresh appliance
# enables this separately with install-boot-splash.sh.
if [[ -f /etc/systemd/system/getty@tty1.service.d/50-bosun-splash.conf ]]; then
    bash "$SRC/install-boot-splash.sh"
fi

echo
echo "== status =="
systemctl --no-pager --lines=0 status bosun-hub.service || true
echo
echo "done. protocol on tcp://<pi>:9876, ws://<pi>:8081, http://<pi>:8080"
