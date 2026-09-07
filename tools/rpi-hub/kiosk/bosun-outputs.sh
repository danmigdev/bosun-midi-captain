#!/usr/bin/env bash
# Apply HDMI/fallback output policy in Cage, then wait for Wayland events.
set -eu

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/bosun-kiosk}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_CONFIG="${BOSUN_OUTPUTS_CONFIG:-$SCRIPT_DIR/bosun-outputs.conf}"

if ! command -v kanshi >/dev/null 2>&1; then
    echo "kanshi missing; kiosk output policy cannot start" >&2
    exit 1
fi
if [[ ! -r "$OUTPUT_CONFIG" ]]; then
    echo "kiosk output policy is not readable: $OUTPUT_CONFIG" >&2
    exit 1
fi

# The kiosk owns the runtime directory. Wait only during startup; kanshi then
# sleeps on the Wayland connection and reacts to output changes without polling.
shopt -s nullglob
for ((attempt = 0; attempt < 30; attempt++)); do
    for socket_path in "$XDG_RUNTIME_DIR"/wayland-*; do
        [[ -S "$socket_path" ]] || continue
        export WAYLAND_DISPLAY="${socket_path##*/}"
        exec kanshi --config "$OUTPUT_CONFIG"
    done
    sleep 0.3
done

echo "Cage Wayland socket did not become available in $XDG_RUNTIME_DIR" >&2
exit 1
