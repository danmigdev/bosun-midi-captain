#!/usr/bin/env bash
# Optional boot branding for an already installed Bosun Pi appliance.
# Does not restart the appliance services, reboot, or access the Captain.
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo" >&2
    exit 1
fi
if [[ ! -r /proc/device-tree/model ]] || ! grep -q 'Raspberry Pi' /proc/device-tree/model; then
    echo "This installer requires a Raspberry Pi with /boot/firmware." >&2
    exit 1
fi
if [[ ! -f /etc/systemd/system/bosun-kiosk.service ]]; then
    echo "Install the Bosun Pi appliance first." >&2
    exit 1
fi
# Preserve the working Cage/Chromium display path; no extra display server.
python3 "$SRC/boot-splash/configure.py" --check
python3 "$SRC/boot-splash/configure.py"
# Root runs the early boot files at startup. Older installers handed
# /opt/bosun-hub and these trees to the network-facing bosun account.
chown root:root /opt/bosun-hub
chmod go-w /opt/bosun-hub
for tree in /opt/bosun-hub/boot-splash /opt/bosun-hub/boot-splash-trials; do
    if [[ -L "$tree" ]]; then
        echo "Refusing a symlinked $tree; inspect it first." >&2
        exit 1
    fi
    if [[ -d "$tree" ]]; then
        chown -R root:root "$tree"
        chmod -R go-w "$tree"
    fi
done
# The hub (bosun) stores a custom startup logo; this root applier pastes it
# into the early pictures without decoding any image.
install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 "$SRC/boot-splash/apply_logo.py" /usr/local/libexec/bosun-boot-logo
install -o root -g root -m 0644 "$SRC/systemd/bosun-boot-logo.service" /etc/systemd/system/bosun-boot-logo.service
install -o root -g root -m 0644 "$SRC/systemd/bosun-boot-logo.path" /etc/systemd/system/bosun-boot-logo.path
systemctl daemon-reload
if [[ -f /etc/systemd/system/bosun-framebuffer-splash.service ]]; then
    # No logo run may write a picture while the installer replaces it.
    systemctl stop bosun-boot-logo.path bosun-boot-logo.service
    # Refresh only the picture for an already validated framebuffer installation.
    # New renderer code must go through prepare-framebuffer-splash.sh and tryboot.
    picture_dir="$(mktemp -d /opt/bosun-hub/boot-splash/.pictures.XXXXXX)"
    trap 'rm -f -- "$picture_dir"/*.bin; rmdir -- "$picture_dir"' EXIT
    if [[ -f /etc/systemd/system/bosun-framebuffer-splash-80.service ]]; then
        python3 "$SRC/boot-splash/render.py" "$picture_dir/splash.bin" --milestones
        echo "Early framebuffer pictures rendered."
    else
        # Preserve a validated static-logo installation until a progress trial.
        echo 'Keeping the validated early logo; trial the progress renderer to update it.'
    fi
    for picture in "$picture_dir"/*.bin; do
        [[ -f "$picture" ]] || continue
        chmod 0644 "$picture"
        mv -- "$picture" "/opt/bosun-hub/boot-splash/$(basename "$picture")"
    done
fi
systemctl enable --now bosun-boot-logo.path
# Keep a custom logo across updates: refreshed pictures become the new Bosun
# defaults and get the logo pasted again.
if ! systemctl restart bosun-boot-logo.service; then
    echo "warning: the custom startup logo was not applied; see journalctl -u bosun-boot-logo.service" >&2
fi
echo "Quiet boot and Stage loading logo installed. They take effect on the next reboot."
if [[ ! -f /etc/systemd/system/bosun-framebuffer-splash.service ]]; then
    echo "Without the optional framebuffer splash, the display stays black until Chromium starts."
fi
echo "Original boot files: /boot/firmware/{config,cmdline}.txt.bosun-before-splash"
