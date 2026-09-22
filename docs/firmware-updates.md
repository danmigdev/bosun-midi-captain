# Update Captain firmware

Bosun Desktop updates existing native firmware directly over USB or through a
Raspberry Pi. The Pi updater also migrates supported CircuitPython configurations. It
supports the RP2040 MIDI Captain with **8 MiB flash**, using Kemper Player or
Generic MIDI profiles. CircuitPython is no longer maintained separately.

## Enter the bootloader from Bosun

The following options are implemented on `main`; the startup shortcut requires
firmware built with this change. Older installed firmware does not gain the
shortcut by updating Desktop alone.

- **Bosun Desktop:** connect the Captain directly to the computer by USB, open
  **Maintenance**, and click **Enter bootloader**. Save or discard any unsaved
  patch edits first. The button appears when the connected native firmware
  advertises bootloader support.
- **Without Desktop:** disconnect all power sources. Hold **switch 1 (top-left)**,
  reconnect USB to the computer, and keep holding that switch alone for
  **three seconds**, then release it. The shortcut runs before configuration
  and display initialization.

The computer should show an **RPI-RP2** drive. Entering this mode does not erase
the installed firmware or configuration. Power-cycle without holding a switch
to return to Bosun, or follow PaintAudio's official recovery instructions and
use the firmware for your exact Captain model to reinstall stock firmware.
Back up anything you want to keep before installing another firmware; backing
up and restoring the previous system is the user's responsibility.

## First installation from factory firmware

Open **Setup guide** in Desktop and select your configuration, then **Install
native firmware**. This installs the native version bundled with the current
Desktop release directly on the supported 10-switch, 8 MiB RP2040 Captain.
No older Bosun release, CircuitPython bootstrap or Pi is needed. Follow the
[illustrated first-installation guide](first-setup.md#2-install-native-firmware-on-the-captain).

This experimental path loads a temporary installer into RAM through the
`RPI-RP2` drive and uses standard USB serial drivers. Loading the helper does
not write flash. The app verifies the physical flash identity and capacity,
reads the complete original flash twice, and durably saves a verified backup
before writing. The new native installation starts with empty Bosun profiles;
factory settings remain in the backup and are not converted.

Keep the complete backup directory, including the recovery helper and manifest.
If writing is interrupted, reconnect the same Captain to the same USB port in
BOOTSEL and use **Restore backup**. Recovery checks the complete restored flash;
the original factory firmware cannot report its running state to Bosun.
A complete Windows test from PaintAudio 5.15 passed with the corrected installer.
Use Desktop **0.6.8 or later**, which includes the stock USB detection fix;
0.6.7 cannot discover that stock firmware. See the
[hardware test and release status](stock-installation-test.md).

Already-native Captains use the USB update below, which preserves native
configuration. Existing Bosun CircuitPython installations use Pi migration.

## Direct Desktop USB

This path updates an existing native installation on an **8 MiB RP2040 Captain**.
It uses the native package bundled with Bosun Desktop, including when reinstalling
the same version. CircuitPython migration and factory installation use the paths
described below.

1. Save or discard pending patch edits and export a configuration backup.
2. Connect **Captain USB-B → computer USB-A/USB-C** with a data cable. Choose
   **USB** in Bosun Desktop and connect to the Captain.
3. Select **Update firmware (USB)** in the top bar when the bundled version is
   newer than the installed firmware. For a manual installation or reinstallation,
   choose **Maintenance → Install firmware (USB)**.
4. Review the release, then choose **Update**. Keep the Captain on the same USB
   port and keep the computer awake. MIDI and Stage pause during this operation.
5. Wait for **Bosun updated**, then close the update window to reconnect.

The updater identifies the Captain by its USB serial number and physical USB
path. After entering BOOTSEL it verifies the flash chip's unique ID and 8 MiB
capacity, reads the entire flash twice, and saves a verified full backup on the
computer. It writes only the firmware area, checks the entire flash against the
expected firmware plus original data, then verifies the running version, storage,
active profile, profile list and global configuration after reboot.

The backup and persistent update journal live under the application's data
directory in `usb-updates/<job>/`. On Windows this is normally
`%APPDATA%\com.bosun.app\usb-updates\`. **Show backup** opens the exact location.
Keep the complete job directory, including `flash-before.bin` and the JSON files.
A configuration export cannot replace this full recovery backup.

The bootloader's PICOBOOT interface must be accessible. On Windows it needs
**WinUSB**; if opening the interface fails, install that driver for the Captain's
**RP2 Boot / PICOBOOT interface (normally Interface 1)**, following the
[official picotool Windows driver instructions](https://github.com/raspberrypi/picotool#zadig).
Keep the mass-storage interface's existing driver. On Linux, provide USB device
permissions for `2e8a:0003`. These access failures stop the update before any flash
write; reconnect the Captain to restart its original firmware.

### USB recovery

If writing or verification fails, Bosun attempts to restore and verify the full
original backup. **Original firmware restored** means the update did not complete
and the original installation is running again. The app prevents normal serial
commands, automatic installation and window closure while its worker is active.

If USB disconnects or the app/computer stops during writing, reopen Bosun Desktop.
An unfinished write opens the recovery window automatically. Connect the **same
Captain to the same USB port**, enter BOOTSEL using the pedal's bootloader procedure,
then select **Restore backup**. Bosun checks the backup checksum, device identity,
full flash readback and original running configuration before releasing the editor.
Status polling never starts another write. Preserve the backup if recovery fails.

If an interruption happened before writing began, the original flash is untouched;
reconnect the Captain and start the update again. Direct USB does not require a
Pi, Android, internet connection, Python or a separate picotool executable.

## Raspberry Pi requirements

- A Captain already running Bosun, connected by USB to the Pi.
- The [complete Pi installation](../tools/rpi-hub/README.md#install-on-the-pi).
- Bosun Desktop with its matching bundled native update package.

Use the desktop's network connection to the Pi for this path. Android does not
perform firmware updates. For a factory pedal, follow
[first installation](../README.md#first-installation-from-factory-firmware).

## Install through the Pi

1. Export a configuration backup and save pending edits on the pedal.
2. Connect Bosun Desktop to the Pi that hosts the Captain.
3. Select **Update Bosun** in the top bar or **Maintenance**.
4. Review the target release and choose **Update**.
5. Keep the Pi and Captain powered until the app confirms completion.

The updater checks compatibility, creates a complete recovery backup, transfers
supported settings and verifies the installation. Unsupported configurations
stop the update before firmware is written. Native upgrades preserve the
existing native configuration.

## Reconnect or recover a Pi update

Closing the desktop or losing its connection does not cancel an installation
that has already started. Reconnect to the same Pi and select **Check Bosun
update** to see its progress. Do not start another update while one is running.
An upload can be cancelled before installation starts.

If the app reports **previous firmware restored**, the automatic recovery
completed. If it reports **recovery required**, retain the full backup and the
location shown by the app. A configuration export is not a full firmware backup.
Do not remove those files or retry flashing until recovery is resolved.

For a failure before firmware writing began, reconnect the original Captain
and use **Check recovery**. This checks the existing installation without
writing firmware. A power failure or a pedal that cannot boot may require
physical bootloader access and restoration of its verified full backup.

## Local application builds

Follow the [desktop build guide](../editor/SETUP.md) to generate the matching
native update archive, RAM installer and empty native storage image before
packaging. These resources are verified together; the factory installer cannot
use assets built for another native firmware version. Use the verified Bosun
release package rather than an arbitrary UF2 or a legacy CircuitPython bundle.
