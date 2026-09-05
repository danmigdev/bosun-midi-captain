#!/usr/bin/env bash
# Compile and test only. This helper never opens a device or installs firmware.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
platform="${1:-host}"
if (($#)); then shift; fi
fetch_sdk=false
for argument in "$@"; do
    case "$argument" in
        --fetch-sdk) fetch_sdk=true ;;
        *) printf 'Unknown argument: %s\n' "$argument" >&2; exit 2 ;;
    esac
done
case "$platform" in host|rp2040|all) ;; *) printf 'Usage: %s [host|rp2040|all] [--fetch-sdk]\n' "$0" >&2; exit 2 ;; esac

sdk_commit=98a542c1a62fb549ffb5d66a3e5892b06276b670
tinyusb_commit=86ad6e56c1700e85f1c5678607a762cfe3aa2f47
export PICO_SDK_PATH="${PICO_SDK_PATH:-$repo_root/firmware-native/.deps/pico-sdk}"
if "$fetch_sdk" && [[ ! -e "$PICO_SDK_PATH" ]]; then
    mkdir -p -- "$(dirname -- "$PICO_SDK_PATH")"
    git clone --branch 2.3.0 --depth 1 https://github.com/raspberrypi/pico-sdk.git "$PICO_SDK_PATH"
    git -C "$PICO_SDK_PATH" submodule update --init --depth 1 lib/tinyusb
fi
if [[ -e "$PICO_SDK_PATH" ]]; then
    [[ "$(git -C "$PICO_SDK_PATH" rev-parse HEAD)" == "$sdk_commit" ]] || { printf 'Expected the exact Pico SDK 2.3.0 commit\n' >&2; exit 1; }
    [[ "$(git -C "$PICO_SDK_PATH/lib/tinyusb" rev-parse HEAD)" == "$tinyusb_commit" ]] || { printf 'Initialize the pinned TinyUSB submodule\n' >&2; exit 1; }
elif [[ "$platform" != host ]]; then
    printf 'Set PICO_SDK_PATH or pass --fetch-sdk to obtain SDK 2.3.0\n' >&2
    exit 1
fi

build_root="${BOSUN_BUILD_ROOT:-$repo_root/firmware-native/build}"
jobs="${CMAKE_BUILD_PARALLEL_LEVEL:-8}"
if [[ "$platform" == host || "$platform" == all ]]; then
    cmake -S "$repo_root/firmware-native" -B "$build_root-host" -G Ninja \
        -DBOSUN_PLATFORM=host -DCMAKE_BUILD_TYPE=Debug \
        -DBOSUN_SANITIZERS="${BOSUN_SANITIZERS:-ON}" -DPICO_SDK_PATH="$PICO_SDK_PATH"
    cmake --build "$build_root-host" --parallel "$jobs"
    ctest --test-dir "$build_root-host" --output-on-failure
fi
if [[ "$platform" == rp2040 || "$platform" == all ]]; then
    cmake -S "$repo_root/firmware-native" -B "$build_root-rp2040" -G Ninja \
        -DBOSUN_PLATFORM=rp2040 -DCMAKE_BUILD_TYPE=Release \
        -DPICO_SDK_PATH="$PICO_SDK_PATH" -DPICO_NO_PICOTOOL=OFF \
        -DPICOTOOL_FETCH_FROM_GIT_PATH="$repo_root/firmware-native/.deps/picotool" \
        -DPICO_FLASH_SIZE_BYTES="${BOSUN_FLASH_BYTES:-8388608}"
    cmake --build "$build_root-rp2040" --target bosun_native --parallel "$jobs"
    test -s "$build_root-rp2040/bosun_native.uf2"
    printf 'Built (not installed): %s\n' "$build_root-rp2040/bosun_native.uf2"
fi
