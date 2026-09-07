#!/usr/bin/env bash
# kanshi calls this after applying a profile; never change display modes here.
set -eu
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/bosun-kiosk}"
control_socket="$XDG_RUNTIME_DIR/wayvnc-stage.sock"

# The first profile may finish before VNC starts. Retry briefly, then stop.
# Read the current enabled output on each attempt so overlapping hotplug hooks
# follow the newest profile rather than restoring an old event's output.
deadline=$((SECONDS + 12))
while ((SECONDS < deadline)); do
    if [[ -S "$control_socket" ]]; then
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
        if [[ -n "$output" ]] &&
            timeout 1 wayvncctl --socket="$control_socket" output-set "$output" >/dev/null 2>&1; then
            exit 0
        fi
    fi
    sleep 0.3
done
# VNC is optional: a stopped mirror must not turn a valid HDMI profile into an error.
exit 0
