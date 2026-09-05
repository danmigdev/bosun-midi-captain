# Pinned littlefs

Unmodified upstream **v2.11.3**, BSD-3-Clause (see LICENSE.md):
https://github.com/littlefs-project/littlefs/tree/v2.11.3

Downloaded directly from that tag. SHA-256:

| File | SHA-256 |
| --- | --- |
| lfs.c | a36d6a095785ddea9571d541d68d3e4ef01d5b255a99d17d3f07fb6ea60ea132 |
| lfs.h | b1befd7288d08815accc8f9af744c55686c0b9e3ac0061c32cee38a1b3eb96d |
| lfs_util.c | f2fbde533670560434bd9f5a547174cc7c5a4670a02c47b4bd85180dced8b2ec |
| lfs_util.h | 548d46aa524dc7449e16739286c1a422a52f9de727ff0be0c2ffc5593f5ca981 |
| LICENSE.md | 0cb4ff1daf5fdc1359c6a6ee3116092f08fc100c9d58b1b77ab17bfd801f856d |

Build all consumers with the same `LFS_NO_MALLOC`, `LFS_NAME_MAX=63`, and
`LFS_FILE_MAX=262144` definitions. Bosun supplies all buffers, including the
per-file cache required by `lfs_file_opencfg`. Logging is disabled by
`LFS_NO_DEBUG`, `LFS_NO_WARN`, and `LFS_NO_ERROR` so diagnostics cannot enter
the JSON protocol stream. Disk format is littlefs 2.1, not CircuitPython FAT.
Mount failure never invokes format. Migration must be an explicit separate
backup/format/restore workflow; this backend does not perform migration.
