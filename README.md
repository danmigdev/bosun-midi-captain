# Bosun

Bosun turns the 10-switch PaintAudio MIDI Captain into a configurable controller for Kemper Player and generic MIDI devices. Use the desktop or Android app to edit your sounds, and Stage to view the current rig and effects on a tablet, browser or Raspberry Pi display.

The maintained pedal firmware runs natively in C. The supported update target is the RP2040 MIDI Captain with 8 MiB flash.

![Bosun Stage on the Raspberry Pi](docs/ui-test-screenshots/stage_vnc.png)

## Download and install

Download the package for your system from [GitHub Releases](https://github.com/danmigdev/bosun-midi-captain/releases).

These instructions describe source version **0.6.5**. Until a matching release
is published, use the [desktop build guide](editor/SETUP.md) or
[Android build guide](editor/src-tauri/android-config.md) for the new native
update and Stage features. Keep the app and Pi on matching versions.

| System | Package | Installation |
| --- | --- | --- |
| Windows x64 | `Bosun-<version>-portable-x64.zip` | Extract the whole ZIP and run `Bosun.exe`. |
| macOS, Apple Silicon | `.dmg` | Open it and copy Bosun to Applications. |
| Linux x64 | `.AppImage` | Make it executable, then open it. |
| Android 10 or later | `bosun.apk` | Install the APK and connect using USB-OTG or a Raspberry Pi hub. |

Windows requires WebView2. Allow the Android USB permission prompt when connecting the pedal directly. Use a desktop computer for initial pedal setup and firmware updates.

## Set up the pedal

### A pedal already running Bosun

1. Connect the Captain directly to the computer by USB, or connect it to a [Raspberry Pi hub](tools/rpi-hub/README.md).
2. Open Bosun. For direct USB, the app selects the data port automatically. For a Pi connection, choose **Raspberry Pi (network)**, select **Find Raspberry Pi**, then **Connect**.
3. Create a profile for your device when prompted. Native firmware supports **Kemper Player** and **Generic MIDI**.
4. Open **Patches**, select a bank and rig, and configure its switches. Choose **Save** to keep changes on the pedal.

### First installation from factory firmware

Back up the pedal's original files before installing: the setup wizard can erase them.

Connect the pedal directly to a desktop computer and follow **Install firmware**. The existing wizard prepares a legacy bootstrap installation; it does not install the final native firmware. If automatic detection fails, follow its instructions to enter the pedal's bootloader.

Once Bosun connects, create a supported profile and move the Captain's USB connection to a configured Raspberry Pi. Use **Update Bosun** below to complete the migration to native firmware. The first-install backup remains necessary: the updater's later backup contains the bootstrap installation, not the original factory firmware.

## Configure your sounds

- **Profiles** keep settings for different devices or setups separate.
- **Patches** contains your banks and rigs. Open a rig to assign messages to a switch's press, release or hold action, and set its label and LED colour.
- **Quick setup** applies the available setup recipe for the active profile.
- **Screen layout** changes the Captain's display fields and colours.
- **Settings** contains device-wide options, including expression pedals and preset navigation.
- **Maintenance → Export config…** backs up profiles, settings, patches and MIDI Learn data. **Import config…** restores an exported configuration.

Save edits before disconnecting or updating. **Discard** restores the saved version of pending patch changes.

## Stage

Stage shows the current rig, bank, effects and expression mode. With compatible native firmware, tap or click a configured switch tile to operate the pedal.

Use **+ / −** to move between banks, or tap **BANK** to open the bank grid. The selector offers two behaviours:

- **Load rig immediately** changes to the selected bank straight away.
- **Preselect bank** previews that bank while the current sound keeps playing. Tap a rig to load it, or **CANCEL** to leave the preview.

The settings button opens Stage appearance controls for fonts, colours and sizes. Preferences are saved separately in each browser or app.

A Raspberry Pi can show Stage automatically over HDMI. Its browser page also works from another screen on the same network. The optional read-only VNC viewer shows the actual Pi-rendered picture, including when no HDMI screen is connected. Follow the [Raspberry Pi installation and display guide](tools/rpi-hub/README.md).

## Update firmware

1. Export a configuration backup and save all pending edits.
2. Connect the Captain by USB to a configured Raspberry Pi, then connect Bosun Desktop to that Pi.
3. Select **Update Bosun**, review the bundled release and choose **Update**.
4. Keep the Pi and Captain powered until the app confirms completion.

The updater supports native upgrades and migration from supported existing CircuitPython configurations. It checks compatibility before writing firmware, makes a complete recovery backup and verifies the result. Unsupported profiles or configurations stop the update before installation.

Use a current desktop package with its matching Pi hub. Direct desktop USB and Android connections do not perform this update flow. To reopen an update after reconnecting, use **Check Bosun update**; do not start another installation while one is still running. If recovery is required, retain the backup shown by the app.

## Connection help

- **Pedal not found:** use a USB data cable, check power and reconnect it. Close other programs that may hold the same USB connection.
- **Pi not found:** keep the app and Pi on the same local network. Enter the Pi's hostname and port `9876` manually if network discovery is unavailable.
- **No patches:** create a profile and add your first rig.
- **Update unavailable:** connect the desktop through the Pi and check that its update support and the desktop's bundled release are installed.

## License

Bosun is available under [GPL-3.0-or-later](LICENSE).
