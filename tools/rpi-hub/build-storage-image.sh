#!/usr/bin/env bash
# Compile the storage-image converter from the firmware sources that ship in the
# Pi setup package. It needs only a C compiler: no CMake, Python or generated
# files. Never opens or flashes a Captain.
#   bash build-storage-image.sh OUTPUT
set -euo pipefail
if [[ $# -ne 1 ]]; then
    printf 'usage: %s OUTPUT\n' "${0##*/}" >&2
    exit 2
fi
native="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../firmware-native" && pwd)"
cc -std=c11 -O2 -DNDEBUG -DBOSUN_PLATFORM_RP2040=1 \
    -DLFS_NO_MALLOC -DLFS_NO_DEBUG -DLFS_NO_WARN -DLFS_NO_ERROR \
    -DLFS_NAME_MAX=63 -DLFS_FILE_MAX=262144 \
    -I"$native/include" -I"$native/third_party/littlefs" \
    "$native/platform/host/storage_image.c" "$native/platform/rp2040/storage.c" \
    "$native/src/storage_path.c" "$native/src/config.c" "$native/src/json.c" \
    "$native/third_party/littlefs/lfs.c" "$native/third_party/littlefs/lfs_util.c" \
    -lm -o "$1"
