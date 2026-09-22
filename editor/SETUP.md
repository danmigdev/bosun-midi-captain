# Build and configure Bosun Desktop

For a packaged application, extract the complete portable ZIP and run `Bosun.exe`.
Keep its resource folders beside the executable. A build toolchain is only needed
when building from source.

## Windows build requirements

- Node.js 22.13 or newer within the 22.x series, with npm.
- Rust stable with the MSVC toolchain, installed through rustup.
- Visual Studio Build Tools with **Desktop development with C++** and a Windows SDK.
- Python 3.12, PowerShell and the Microsoft Edge WebView2 Runtime.

Run the following from the repository root:

```powershell
npm --prefix editor install --no-audit --no-fund
powershell -ExecutionPolicy Bypass -File tools/download-assets.ps1
```

The asset download prepares the resources still required by the installer and
migration tooling. It does not install firmware on a connected pedal.

Prepare the pinned Pico SDK and ARM build tools using the
[native firmware build instructions](../firmware-native/README.md), then run
`bash tools/build-factory-installer.sh` from a Linux environment (WSL on Windows).
This builds the matching native firmware archive, RAM-only USB helper and empty
native storage image, with a verified manifest. It does not access a connected
pedal. The portable packager requires these resources and checks their hashes.

## Run and package

From `editor/`:

```powershell
npx tauri dev
```

From the repository root, build the portable application:

```powershell
powershell -ExecutionPolicy Bypass -File tools/package-portable.ps1
```

The output is `dist/Bosun-<version>-portable-x64.zip`. To retain several builds,
add `-OutputSuffix <name>`. The script builds Stage and the offline setup guide,
packages the Pi installer, and refreshes and verifies bundled resources.
Use `-SkipBuild` only to package a binary that has already been built from the
intended source checkout.

For frontend-only development, run `npm run dev` from `editor/`. Build the
standalone Stage with `npm run build:stage`; its output is `editor/dist-stage/`.
See [Raspberry Pi installation](../tools/rpi-hub/README.md) for serving Stage.

Build the standalone setup guide with `npm --prefix editor run build:guide`;
open `dist/setup-guide/setup-guide.html` offline in a browser. It shares its
screens and wiring diagrams with the Desktop wizard. After building Stage,
`python tools/package-pi-setup.py` prepares the package exported by Desktop.
For macOS/Linux source builds, run these steps before `npx tauri build` too.

## Connect

Choose **USB** for a Captain connected directly to the computer. Choose
**Raspberry Pi (network)** when the Captain is connected to a Pi running Bosun
Hub. Select a discovered hub or enter its hostname/IP address and TCP port
(default `9876`). **Find Raspberry Pi** repeats discovery, which uses UDP `9877`
on the local network. Manual connection remains available if broadcasts are
blocked.

Disconnect before changing the saved endpoint. **Setup guide** offers direct
factory-to-native installation over USB. Existing native firmware can be
updated over USB or through the Pi; existing CircuitPython configurations
migrate through the Pi. See [firmware installation and updates](../docs/firmware-updates.md)
for the different requirements and recovery procedures. Factory installation
is experimental; macOS and Linux application operation remains untested.

For Android requirements and APK installation, see
[Android setup](src-tauri/android-config.md).

## Repository layout and local files

| Location | Purpose |
| --- | --- |
| `editor/` | Shared Desktop/Android UI, Stage and the setup wizard; native app code is in `src-tauri/`. |
| `firmware-native/` | Maintained C firmware, host tests and RP2040 build tooling. |
| `firmware/` | Frozen CircuitPython resources, shared schemas and migration/regression references. Still required by builds. |
| `tools/` | Packaging, release checks, USB installer and Raspberry Pi services. |
| `docs/` | User guides, hardware validation, release notes and the pinned F-Droid reference recipe. |
| `samples/`, `fastlane/` | Example configurations and Android store descriptions. |
| `dist/` | Ignored release packages, previews and local validation evidence. |
| `.worktrees/` | Ignored additional Git worktrees, kept inside the project folder. |

Create additional checkouts under `.worktrees/`, for example
`git worktree add .worktrees/my-fix -b fix/my-fix`. Use Git's worktree commands
to move or remove them so their registration stays valid. Review uncommitted
changes before retiring a checkout.

Old build logs, downloaded release copies and compiler output can be removed
when no running process uses them. Keep recovery backups, installation journals,
signing keys and validation evidence. In particular, `dist/` may contain hardware
backups as well as disposable packages; do not erase it wholesale. Generated
installer resources under `editor/src-tauri/resources/` are required for packaging
and must be rebuilt using the instructions above if removed.
