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

Build the native firmware update archive using the
[native firmware build instructions](../firmware-native/README.md#build-the-update-package)
before packaging an application that must provide **Update Bosun**. The desktop
and Android packaging scripts validate an existing archive but do not create it.

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
add `-OutputSuffix <name>`. The script refreshes and verifies bundled resources.
Use `-SkipBuild` only to package a binary that has already been built from the
intended source checkout.

For frontend-only development, run `npm run dev` from `editor/`. Build the
standalone Stage with `npm run build:stage`; its output is `editor/dist-stage/`.
See [Raspberry Pi installation](../tools/rpi-hub/README.md) for serving Stage.

## Connect

Choose **USB** for a Captain connected directly to the computer. Choose
**Raspberry Pi (network)** when the Captain is connected to a Pi running Bosun
Hub. Select a discovered hub or enter its hostname/IP address and TCP port
(default `9876`). **Find Raspberry Pi** repeats discovery, which uses UDP `9877`
on the local network. Manual connection remains available if broadcasts are
blocked.

Disconnect before changing the saved endpoint. Firmware installation and
migration to native firmware use the
[Update Bosun procedure](../docs/firmware-updates.md) through the Pi.

For Android requirements and APK installation, see
[Android setup](src-tauri/android-config.md).
