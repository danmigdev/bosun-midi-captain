#!/usr/bin/env bash
# Build only; never open USB or write a connected device.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export PICO_SDK_PATH="${PICO_SDK_PATH:-$root/firmware-native/.deps/pico-sdk}"
build="$root/firmware-native/.deps/installer-build"
resources="$root/editor/src-tauri/resources"
bash "$root/tools/native-build.sh" rp2040
cmake -S "$root/tools/usb-installer" -B "$build/ram" -G Ninja -DCMAKE_BUILD_TYPE=Release \
    -DPICOTOOL_FETCH_FROM_GIT_PATH="$root/firmware-native/.deps/picotool"
cmake --build "$build/ram" --parallel 8
cc -std=c11 -Wall -Wextra -Werror -fsanitize=undefined,address \
    "$root/tools/usb-installer/protocol.c" "$root/tools/usb-installer/test_protocol.c" -o "$build/test-protocol"
"$build/test-protocol"
cmake -S "$root/firmware-native" -B "$build/host" -G Ninja -DBOSUN_PLATFORM=host -DBOSUN_SANITIZERS=ON -DCMAKE_BUILD_TYPE=Debug
cmake --build "$build/host" --target bosun_storage_image --parallel 8
empty="$(mktemp -d "$build/empty.XXXXXX")/storage.bin"
"$build/host/bosun_storage_image" --empty --output "$empty"
version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$root/editor/package.json")"
python3 "$root/tools/package-native-update.py" --uf2 "${BOSUN_BUILD_ROOT:-$root/firmware-native/build}-rp2040/bosun_native.uf2" \
    --output "$resources/update/bosun-update.zip" --release "$version" --firmware-version "$version-native"
python3 "$root/tools/package-factory-installer.py" --package "$resources/update/bosun-update.zip" --output "$resources/installer" \
    --loader "$build/ram/bosun_usb_installer.uf2" --storage "$empty" --sdk "$PICO_SDK_PATH"
