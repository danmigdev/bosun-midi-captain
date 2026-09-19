#!/usr/bin/env bash
# Optional boot branding for an already installed Bosun Pi appliance.
# Does not restart services, reboot, or access the Captain.
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
if [[ -f /etc/systemd/system/bosun-framebuffer-splash.service ]]; then
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
systemctl daemon-reload
echo "Quiet boot and Stage loading logo installed. They take effect on the next reboot."
if [[ ! -f /etc/systemd/system/bosun-framebuffer-splash.service ]]; then
    echo "Without the optional framebuffer splash, the display stays black until Chromium starts."
fi
echo "Original boot files: /boot/firmware/{config,cmdline}.txt.bosun-before-splash"
