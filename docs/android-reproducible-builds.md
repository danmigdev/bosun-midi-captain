# Android reproducible builds

Bosun uses stable Tauri 2.12.0 with `tauri-build` and `tauri-codegen` 2.7.0,
`tauri-utils` 2.10.0, and JavaScript API/CLI 2.12.0. The Rust dependencies
come from crates.io; there are no Tauri Git overrides.

The stable [codegen 2.7.0 release](https://github.com/tauri-apps/tauri/releases/tag/tauri-codegen-v2.7.0)
and [utils 2.10.0 release](https://github.com/tauri-apps/tauri/releases/tag/tauri-utils-v2.10.0)
include the official [reproducible-build fix from
tauri-apps/tauri#15777](https://github.com/tauri-apps/tauri/pull/15777).
These replace the previous Git pin to commit
`29c87c3d3f5bbcf5a7ae9de01af7e6bb738c1d01`, which was needed because
published `tauri-codegen` 2.6.3 did not contain the fix.

The fix makes embedded assets and CSP hash emission deterministic. Use the
committed lockfiles (`cargo build --locked` and `npm ci`) when preparing
builds.

The Android release workflow pins Rust 1.97.1, Node 22.23.2 and NDK
29.0.14206865, selects GCC 13 for host compilation, and builds with the
locked Cargo dependencies. Match these versions in the F-Droid recipe.

## How the release and F-Droid builds match

Both builds run the same Tauri CLI command from `editor/`:
`npx tauri android init --ci` followed by
`npx tauri android build --apk --ci -- --locked`. The F-Droid recipe in
[`fdroid-com.bosun.app.yml`](fdroid-com.bosun.app.yml) contains no
version-specific data apart from its build entry, so F-Droid's
`checkupdates` can add each new tag automatically (`AutoUpdateMode:
Version`, `UpdateCheckMode: Tags`). It reads the version code from
`editor/src-tauri/android-version-code.txt` and the version name from
`tauri.conf.json`; the build writes `$$VERCODE$$` into
`bundle.android.versionCode`, as `release.yml` does with the tracked file.

Two details keep the two APKs identical:

- **Sorted APK configuration.** The CLI writes `assets/tauri.conf.json` with
  `serde_json`. `BundleResources::Map` in tauri-utils 2.10.0 is still a
  `HashMap`, so `bundle.resources` changes order on every run.
  `gen/android/app/build.gradle.kts` registers `sortTauriConfigKeys`, which
  runs before `preBuild` and rewrites the file with every object's keys
  sorted. Tauri reads only `plugins` from this asset at runtime
  (`PluginManager.loadConfig`).
- **Gradle without the wrapper.** F-Droid's scanner deletes `gradlew` and
  `gradle-wrapper.jar`. The recipe writes a two-line `gradlew` that runs
  `gradlew-fdroid`, which downloads the Gradle version from
  `gradle-wrapper.properties` with a verified checksum. The CLI calls
  `<project>/gradlew --project-dir <project>`, so it uses this script.

The recipe downloads no firmware images or library bundles. The only
prebuilt binary in the build tree that Bosun uses,
`vendor/picotool/flash_id.bin`, is embedded by the desktop-only `picoboot`
module and is not compiled for Android, so the recipe deletes it with
`scandelete`. [`tools/build-flash-id.sh`](../tools/build-flash-id.sh)
rebuilds it from `flash_id.c` with Pico SDK 2.3.0 and requires the vendored
SHA-256; GCC 13.2 (Ubuntu 24.04) and 14.2 (Debian trixie) produce the same
152 bytes, and the release workflow runs the script on every release. The
recipe also deletes `bosun-stand/`, whose CAD ZIP archives the scanner
rejects and the app does not use.

The recipe's `commit:` is a full commit hash. `checkupdates` resolves later
tags to their hashes itself.

## Stable release validation (2026-09-26)

The stable dependency update passed the frontend production build, all 889
frontend tests, all 44 Rust library tests, and the Windows release build.
Svelte checking reported zero errors and 28 warnings. Android ARM64 Rust
checking, all 39 app JVM tests, the Tauri JVM test, SDK compatibility checks,
and app/plugin Android lint also passed. Windows validation used Node
22.23.2, Rust 1.96.0, Java 21 and Gradle 8.14.3.

Two independent Linux source/target directories were built with the real
Tauri CLI (`npm run tauri -- android build --apk --ci -- --locked`). Both
used Rust 1.97.1, Node 22.23.2, NDK 29.0.14206865, GCC 13, Java 21 and
Gradle 8.14.3, identical frontend/resources, and separate clean Cargo build
directories. Each source root was remapped to `/build/bosun`; Cargo and
rustup paths were remapped to `/build/.cargo` and `/build/.rustup`.

All four release libraries matched byte-for-byte:

| ABI | SHA-256 |
| --- | --- |
| `arm64-v8a` | `8b599752071162837b05d9e80ea731c6bc7fda66c365595038458fd1e3357c85` |
| `armeabi-v7a` | `d9cb8925836b9f3b8ea1f7452808d58442c9297da8dbaaaa271e564e590b8bbc` |
| `x86` | `e878a27634252457d1f91e9ea70caaa4c975aca0990967e6d2e8003e9dcc7295` |
| `x86_64` | `8e191d75ee035b717f0f68d7e25f15ecc7e39f85189b12ddd43407a77653c4be` |

The two universal unsigned APKs initially differed only in
`assets/tauri.conf.json`, whose JSON values were equal but resource key
order differed. After copying that file from the first APK and repackaging
the second with Gradle (without rebuilding Rust), both complete APKs were
24,074,478 bytes with SHA-256
`0fc4290374614e9b9908a2d09e0f80bb4488ff84d741edb6be2663ee2bd3d4df`.
Local logs, comparison reports and the unsigned APK are retained under
`dist/tauri-2.12-verification/`.

This validates the stable update locally on all distributed ABIs. It does
not replace F-Droid buildserver verification against the next signed
published release. The configuration copy used here is now replaced by
the `sortTauriConfigKeys` Gradle task described above.

## Historical validation of the Git pin

The pinned dependencies passed the frontend build, 40 Rust library tests,
Android ARM64 compilation, Android JVM unit tests, and Android SDK/lint
checks on 2026-09-13. Two clean ARM64 release builds in separate Linux
source and target directories produced identical native libraries. Two
separate unsigned APK assemblies differed only in `assets/tauri.conf.json`
resource key order. After applying the existing config copy step and
repackaging the second APK, both complete unsigned APKs matched byte for
byte (SHA-256 `cfb4d40839ae5f0c34169421a14d420c6e2312593b8648ec3393bf512b15e8cd`).
These initial local comparisons covered ARM64 only.

## Historical F-Droid buildserver image verification

On 2026-09-13, [pipeline 2844489018 in the F-Droid metadata
fork](https://gitlab.com/danilo.migliarino/fdroiddata/-/pipelines/2844489018)
passed a full build and comparison for all four Android ABIs. The tested
source was Bosun commit `012bcdc16bb1bba1d09058cc3011b63f9a0977f3`
(an earlier development build, version code 33) with the
[Tauri dependency patch](https://gitlab.com/danilo.migliarino/fdroiddata/-/blob/8153ead064c69e24195dfd1de314fd1815440a5d/tools/bosun-verify/tauri-repro.patch)
applied.

The [reference job](https://gitlab.com/danilo.migliarino/fdroiddata/-/jobs/16469451620)
built a universal APK using the Tauri CLI. A separate runner performed
`fdroid build --verbose --test --refresh-scanner --on-server --no-tarball
com.bosun.app:33`, including the source scan and the existing configuration
copy step. Each job had a fresh checkout and Cargo build directory, under
`/home/reference` and `/home/vagrant` respectively, with both home paths
remapped to `/build`.

Both jobs used the official image
`registry.gitlab.com/fdroid/fdroidserver:buildserver-trixie`, digest
`sha256:f81172f142454bccb6e198739d40bf3a98a393f09805140c1aa8b49807d0e3b7`,
with Rust 1.97.1, NDK 29.0.14206865, GCC 13, Java 21, and Node 22.23.2.
The F-Droid server code was pinned to
`6416542655477ad44adbc59878e8e302366d3803`.

The complete unsigned APKs matched byte for byte, including native
libraries for `arm64-v8a`, `armeabi-v7a`, `x86`, and `x86_64`. Both APKs
were 23,131,341 bytes with SHA-256
`7ee72d602a9083d155ea881d8a65d7f7efc8fd21a87077544f7a0f52dc79ee49`.
The downloaded artifacts were also compared locally. The
[verification job](https://gitlab.com/danilo.migliarino/fdroiddata/-/jobs/16469559100)
records the comparison and each native library's hash in its log.

This validates the fixed dependencies in the official buildserver image.
The next published release still needs F-Droid verification against its
signed release APK, using the matching release toolchain and configuration.

## Validating future dependency updates

When updating Tauri or its transitive dependencies:

1. Update the Rust and JavaScript lockfiles together, keeping the stable
   codegen/configuration fixes included.
2. Run the desktop and Android build/tests.
3. Build Android release libraries independently from clean source and
   target directories for each distributed ABI. Match Rust, NDK, frontend
   dependencies, resources and build inputs; retain the same source-path
   remapping in the release workflow and F-Droid recipe.
4. Compare the libraries byte-for-byte, then compare the complete unsigned
   APKs. Investigate any difference before shipping the dependency update.
5. After a Tauri CLI update, check that `inject_resources` still writes
   `assets/tauri.conf.json` before Gradle runs and that only `plugins` is read
   from it at runtime.
