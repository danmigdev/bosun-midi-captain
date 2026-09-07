#!/usr/bin/env bash
# Mirror Cage's actual output. SSH authenticates access to this loopback server.
set -eu
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/bosun-kiosk}"
shopt -s nullglob

# Wait only at startup for Cage and kanshi; capture never changes display modes.
deadline=$((SECONDS + 12))
while ((SECONDS < deadline)); do
    for socket_path in "$XDG_RUNTIME_DIR"/wayland-*; do
        ((SECONDS < deadline)) || break
        [[ -S "$socket_path" ]] || continue
        export WAYLAND_DISPLAY="${socket_path##*/}"
        output="$(timeout 1 wlr-randr --json 2>/dev/null | python3 -c '
import json, sys
try:
    outputs = [o for o in json.load(sys.stdin) if o.get("enabled")]
    physical = [o for o in outputs if o["name"].startswith("HDMI")]
    fallback = [o for o in outputs if o["name"].startswith("HEADLESS")
                and any(m.get("current") and m.get("width") == 1920
                        and m.get("height") == 440 for m in o.get("modes", []))]
    selected = physical or fallback
    if selected:
        print(selected[0]["name"])
except (ValueError, KeyError, TypeError):
    pass
' || true)"
        if [[ -n "$output" ]]; then
            exec wayvnc --config=/dev/null --disable-input --disable-resizing \
                --max-fps=10 --output="$output" \
                --socket="$XDG_RUNTIME_DIR/wayvnc-stage.sock" 127.0.0.1 5901
        fi
    done
    sleep 0.3
done
echo "Cage has no enabled HDMI or ready 1920x440 fallback output" >&2
exit 1
