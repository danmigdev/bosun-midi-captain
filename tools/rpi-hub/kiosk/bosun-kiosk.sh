#!/usr/bin/env bash
# Launch the Stage kiosk: cage (a single-app wlroots compositor) running
# Chromium fullscreen on the hub's local page.
#
# Drives HDMI through DRM, including a panel connected after boot. The same
# compositor also provides a virtual output for VNC while no panel is attached.
set -u

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# StateDirectory= in bosun-kiosk.service is the authority for Chromium's
# persistent profile. Refuse to launch when that systemd contract is absent or
# unusable: Chromium can otherwise ignore --user-data-dir and silently create
# a different profile under ~/.config.
EXPECTED_CHROMIUM_PROFILE=/var/lib/bosun-hub/chromium
CHROMIUM_PROFILE="${STATE_DIRECTORY:-}"
if [[ "$CHROMIUM_PROFILE" != "$EXPECTED_CHROMIUM_PROFILE" ||
      ! -d "$CHROMIUM_PROFILE" ||
      -L "$CHROMIUM_PROFILE" ||
      ! -O "$CHROMIUM_PROFILE" ||
      ! -w "$CHROMIUM_PROFILE" ]]; then
    echo "systemd Chromium state directory is missing, unsafe, or not writable: ${CHROMIUM_PROFILE:-<unset>}" >&2
    exit 1
fi
umask 077

# HDMI uses the panel's advertised preferred mode. kanshi owns output selection
# and the virtual 1920x440 mode; do not force unverified HDMI timings here.
: "${BOSUN_KIOSK_HEADLESS:=0}"
: "${BOSUN_KIOSK_VIRTUAL_FALLBACK:=1}"
: "${BOSUN_KIOSK_URL:=http://localhost:8080/}"

if [[ "$BOSUN_KIOSK_HEADLESS" == "1" ]]; then
    export WLR_BACKENDS=headless
else
    if [[ "$BOSUN_KIOSK_VIRTUAL_FALLBACK" == "1" ]]; then
        export WLR_BACKENDS=headless,drm,libinput
    else
        export WLR_BACKENDS=drm,libinput
    fi
    export LIBSEAT_BACKEND=seatd
fi
export WLR_HEADLESS_OUTPUTS=1
# The HDMI bar panel need not have a keyboard, mouse or touch controller.
export WLR_LIBINPUT_NO_DEVICES=1

# Raspberry Pi OS injects --force-renderer-accessibility and extension UI
# through /etc/chromium.d when /usr/bin/chromium is used.  Those desktop
# defaults create an otherwise unused WebUI renderer and consume a sizeable
# share of an RPi3 core.  The kiosk needs none of them, so prefer the packaged
# browser binary and supply the small, explicit set of appliance flags below.
if [[ -x /usr/lib/chromium/chromium ]]; then
    CHROMIUM=/usr/lib/chromium/chromium
else
    CHROMIUM="$(command -v chromium || command -v chromium-browser)"
fi
if [[ -z "$CHROMIUM" ]] || ! command -v cage >/dev/null 2>&1; then
    echo "cage/chromium missing; kiosk cannot start" >&2
    exit 1
fi

exec cage -- "$CHROMIUM" \
    --kiosk \
    --ozone-platform=wayland \
    --force-prefers-reduced-motion \
    --enable-gpu-rasterization --use-angle=gles \
    --disable-dev-shm-usage --disable-background-networking \
    --disable-extensions \
    --user-data-dir="$CHROMIUM_PROFILE" \
    --no-first-run --fast --fast-start \
    --noerrdialogs --disable-infobars \
    --disable-session-crash-bubble --disable-features=Translate \
    --overscroll-history-navigation=0 --disable-pinch \
    --password-store=basic \
    --check-for-update-interval=31536000 \
    --autoplay-policy=no-user-gesture-required \
    "$BOSUN_KIOSK_URL"
