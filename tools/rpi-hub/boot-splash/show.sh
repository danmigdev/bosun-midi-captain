#!/bin/sh
# Wait briefly for the existing KMS framebuffer; never load a driver or change a mode.
SRC="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
phase="${1:-20}"
case "$phase" in
    20) picture=splash.bin; attempts=20 ;;
    45|65|80) picture="splash-$phase.bin"; attempts=1 ;;
    *) echo 'Unknown boot milestone' >&2; exit 1 ;;
esac
attempt=0
while [ "$attempt" -lt "$attempts" ]; do
    if [ -c /dev/fb0 ] && [ "$(cat /sys/class/graphics/fb0/name 2>/dev/null)" = vc4drmfb ]; then
        # Retain a read-only snapshot for diagnosing a successful draw with no
        # visible picture. Debugfs may be unavailable during early boot.
        if [ "$phase" = 20 ]; then
            for state in /sys/kernel/debug/dri/[0-9]/state; do
                [ ! -r "$state" ] || cat "$state" > /run/bosun-splash.drm-state
            done
            echo "Framebuffer blank state: $(cat /sys/class/graphics/fb0/blank 2>/dev/null)"
        fi
        "$SRC/bosun-boot-picture" "$SRC/$picture" /dev/fb0 > /run/bosun-splash.result 2>&1
        result=$?
        cat /run/bosun-splash.result
        if [ "$result" -eq 0 ]; then
            echo "$phase" > /run/bosun-splash.progress
            echo "Bosun boot progress: $phase%"
        fi
        exit "$result"
    fi
    attempt=$((attempt + 1))
    sleep 0.1
done
echo 'KMS framebuffer not ready; continuing normal boot' > /run/bosun-splash.result
exit 0
