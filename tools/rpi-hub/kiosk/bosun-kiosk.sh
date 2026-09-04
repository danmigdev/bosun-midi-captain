#!/usr/bin/env bash
# Launch the Stage kiosk: cage (a single-app wlroots compositor) running
# Chromium fullscreen on the hub's local page.
#
# Runs headless when no HDMI panel is attached (so wayvnc can mirror it
# for a remote look); when the Wisecoco panel is plugged in, drop
# WLR_BACKENDS and it drives the panel directly.
set -u

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# Headless output at the panel's native geometry so the layout is tested
# at the real size. Override BOSUN_KIOSK_MODE / BOSUN_KIOSK_HEADLESS from
# the unit's environment.
: "${BOSUN_KIOSK_HEADLESS:=1}"
: "${BOSUN_KIOSK_MODE:=1920x440}"
: "${BOSUN_KIOSK_URL:=http://localhost:8080/}"

if [[ "$BOSUN_KIOSK_HEADLESS" == "1" ]]; then
    export WLR_BACKENDS=headless
    export WLR_LIBINPUT_NO_DEVICES=1
fi

# Set the headless output mode once cage is up.
if [[ "$BOSUN_KIOSK_HEADLESS" == "1" ]]; then
    (
        for _ in $(seq 1 30); do
            sock=$(find "$XDG_RUNTIME_DIR" -maxdepth 1 -name 'wayland-*' ! -name '*.lock' -printf '%f\n' 2>/dev/null | head -1)
            [[ -n "$sock" ]] && break
            sleep 0.3
        done
        [[ -z "${sock:-}" ]] && exit 0
        export WAYLAND_DISPLAY="$sock"
        out=$(wlr-randr --json 2>/dev/null | grep -oE '"name": *"[^"]+"' | head -1 | grep -oE '[^"]+$')
        [[ -z "$out" ]] && out=HEADLESS-1
        wlr-randr --output "$out" --custom-mode "${BOSUN_KIOSK_MODE}" 2>/dev/null \
            || wlr-randr --output "$out" --mode "${BOSUN_KIOSK_MODE}" 2>/dev/null || true
    ) &
fi

CHROMIUM="$(command -v chromium || command -v chromium-browser)"

exec cage -- "$CHROMIUM" \
    --kiosk \
    --ozone-platform=wayland \
    --user-data-dir=/var/lib/bosun-hub/chromium \
    --no-first-run --fast --fast-start \
    --noerrdialogs --disable-infobars \
    --disable-session-crash-bubble --disable-features=Translate \
    --overscroll-history-navigation=0 --disable-pinch \
    --password-store=basic \
    --check-for-update-interval=31536000 \
    --autoplay-policy=no-user-gesture-required \
    "$BOSUN_KIOSK_URL"
