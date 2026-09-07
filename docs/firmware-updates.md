# Update Captain firmware

**Update Bosun** installs the native firmware through a Raspberry Pi and can
migrate supported existing CircuitPython configurations automatically. It
supports the RP2040 MIDI Captain with **8 MiB flash**, using Kemper Player or
Generic MIDI profiles. CircuitPython is no longer maintained separately.

## Requirements

- A Captain already running Bosun, connected by USB to the Pi.
- The [complete Pi installation](../tools/rpi-hub/README.md#install-on-the-pi).
- Bosun Desktop with its matching bundled native update package.

Use the desktop's network connection to the Pi. Direct desktop USB and Android
connections do not perform this update flow. For a factory pedal, follow
[first installation](../README.md#first-installation-from-factory-firmware).

## Install the update

1. Export a configuration backup and save pending edits on the pedal.
2. Connect Bosun Desktop to the Pi that hosts the Captain.
3. Select **Update Bosun** in the top bar or **Maintenance**.
4. Review the target release and choose **Update**.
5. Keep the Pi and Captain powered until the app confirms completion.

The updater checks compatibility, creates a complete recovery backup, transfers
supported settings and verifies the installation. Unsupported configurations
stop the update before firmware is written. Native upgrades preserve the
existing native configuration.

## Reconnect or recover

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

Generate the [native update archive](../firmware-native/README.md#build-the-update-package)
before packaging the desktop application. Without a matching archive, **Update
Bosun** is unavailable. Use the verified Bosun release package rather than an
arbitrary UF2 or a legacy CircuitPython file bundle.
