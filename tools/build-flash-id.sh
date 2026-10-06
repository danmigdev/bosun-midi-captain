#!/usr/bin/env bash
# Rebuild editor/src-tauri/vendor/picotool/flash_id.bin from flash_id.c with
# the pinned Pico SDK and require the vendored SHA-256. Source-only builds
# (F-Droid deletes prebuilt binaries before building) use this to restore the
# exact helper bytes that Bosun embeds.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
vendor="$repo_root/editor/src-tauri/vendor/picotool"
expected_sha256=0c598d8a4dc02ede332a65f96aff27a410fd65d8aaa9fa6dc971539c720725b8
sdk_commit=98a542c1a62fb549ffb5d66a3e5892b06276b670
fetch_sdk=false
for argument in "$@"; do
    case "$argument" in
        --fetch-sdk) fetch_sdk=true ;;
        *) printf 'Usage: %s [--fetch-sdk]\n' "$0" >&2; exit 2 ;;
    esac
done

export PICO_SDK_PATH="${PICO_SDK_PATH:-$repo_root/firmware-native/.deps/pico-sdk}"
if "$fetch_sdk" && [[ ! -e "$PICO_SDK_PATH" ]]; then
    mkdir -p -- "$(dirname -- "$PICO_SDK_PATH")"
    git clone --branch 2.3.0 --depth 1 https://github.com/raspberrypi/pico-sdk.git "$PICO_SDK_PATH"
fi
[[ -e "$PICO_SDK_PATH" ]] || { printf 'Set PICO_SDK_PATH or pass --fetch-sdk to obtain SDK 2.3.0\n' >&2; exit 1; }
[[ "$(git -C "$PICO_SDK_PATH" rev-parse HEAD)" == "$sdk_commit" ]] || { printf 'Expected the exact Pico SDK 2.3.0 commit\n' >&2; exit 1; }

build_dir="$(mktemp -d)"
trap 'rm -rf -- "$build_dir"' EXIT
cmake -S "$vendor" -B "$build_dir" -G Ninja -DPICO_SDK_PATH="$PICO_SDK_PATH" \
    -DPICO_PLATFORM=rp2040 -DUSE_PRECOMPILED=FALSE
cmake --build "$build_dir"
actual_sha256="$(sha256sum "$build_dir/flash_id.bin" | cut -d ' ' -f 1)"
[[ "$actual_sha256" == "$expected_sha256" ]] || {
    printf 'Rebuilt flash_id.bin has SHA-256 %s, expected %s\n' "$actual_sha256" "$expected_sha256" >&2
    exit 1
}
cp -- "$build_dir/flash_id.bin" "$vendor/flash_id.bin"
printf 'Rebuilt %s (SHA-256 %s)\n' "$vendor/flash_id.bin" "$actual_sha256"
