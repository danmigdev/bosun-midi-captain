#!/usr/bin/env bash
# Prepare only. Normal firmware, kernel, config, cmdline and initramfs stay untouched.
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/boot-splash"
[[ $EUID -eq 0 ]] || { echo 'Run with sudo' >&2; exit 1; }
[[ -f /boot/firmware/initramfs8 ]] || { echo 'Requires the tested 64-bit Pi boot layout' >&2; exit 1; }
python3 -c 'from PIL import Image, ImageFont'
BUILD="$(mktemp -d /var/tmp/bosun-splash-trial.XXXXXX)"
echo "Preparing splash in $BUILD"
cc -std=gnu11 -O2 -Wall -Wextra -Werror "$SRC/framebuffer.c" -o "$BUILD/bosun-boot-picture"
python3 "$SRC/render.py" "$BUILD/splash.bin" --milestones --preview "$BUILD/preview.png"
install -m 0755 "$SRC/show.sh" "$BUILD/show.sh"
python3 "$SRC/trial.py" stage "$BUILD"
systemctl daemon-reload
UNITS=(bosun-framebuffer-splash-trial.service bosun-framebuffer-splash-trial-{45,65,80}.service)
systemd-analyze verify "${UNITS[@]}"
systemctl enable "${UNITS[@]}"
sync
echo "Trial prepared. Run sudo reboot '0 tryboot' to test it once."
echo "A normal restart returns to the previous boot configuration until commit."
