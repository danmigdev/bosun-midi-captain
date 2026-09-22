# Build and configure Bosun for Android

The application requires Android 10 (API 29) or later. The Windows build script
produces an ARM64 APK. For direct Captain USB access, the Android device needs
USB host support and a suitable USB data/OTG connection.

## Windows build requirements

Complete the [editor source setup](../SETUP.md), then install Android Studio and
these SDK components through its SDK Manager:

- Android SDK Platform 36 and Platform-Tools.
- Android SDK Build-Tools **35.0.0**.
- Android NDK **30.0.15729638**.

These are the versions used by `tools/build-android.ps1`. Configure paths for
your installation in PowerShell, for example:

```powershell
$env:ANDROID_HOME = Join-Path $env:LOCALAPPDATA 'Android\Sdk'
$env:JAVA_HOME = 'C:\Program Files\Android\Android Studio\jbr'
rustup target add aarch64-linux-android
```

Open `editor/src-tauri/gen/android/` in Android Studio once to configure the SDK
location in its local `local.properties`. The generated Android project is
already included; do not rerun `tauri android init` for a normal build.

The build script signs with the local Android development key. If no development
key exists yet, build the project's debug variant once in Android Studio to
create it automatically. Keep that key locally for subsequent APK updates.

## Build and install

From the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File tools/build-android.ps1
```

The result is `bosun-debug.apk` in the repository root. The script builds the
frontend and Rust library, packages the resources, signs the APK and verifies
its application identity and version. The output uses a development signing key.

Copy the APK to the Android device, open it and allow installation from that
source. Alternatively, enable USB debugging and install to a chosen device:

```powershell
& "$env:ANDROID_HOME\platform-tools\adb.exe" devices
& "$env:ANDROID_HOME\platform-tools\adb.exe" -s <device-serial> install -r bosun-debug.apk
```

An APK signed with a different key cannot replace an installed release directly.
Export any configuration you need before removing the existing application.

## Device configuration

Connect the Captain and accept Android's USB permission prompt, or select a
Raspberry Pi network connection in Bosun. USB and network support are included;
no source changes are needed to enable them.

For a direct wireless connection without a separate router, configure the Pi's
[optional hotspot](../../tools/rpi-hub/hotspot/README.md). Join that Wi-Fi on
Android, select **Raspberry Pi (network)** in Bosun and connect to the hotspot's
address on port `9876`. The Captain stays connected to the Pi by USB.

Android can display Stage and edit the connected pedal. The unified firmware
installer runs in Bosun Desktop: first installation and native updates support
direct USB, while migration from existing Bosun CircuitPython uses a Raspberry
Pi. See [firmware updates](../../docs/firmware-updates.md).
