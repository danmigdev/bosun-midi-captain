# Raspberry Pi setup

Connect the MIDI Captain and Kemper Player to a Raspberry Pi to use Bosun over the local network and show Stage on an HDMI display. The installer sets up the hub, automatic MIDI connection, Stage startup, firmware update support and the read-only display viewer.

## Requirements

- Raspberry Pi with Raspberry Pi OS based on Debian Trixie, a configured network connection and internet access for installation.
- MIDI Captain running Bosun, plus the USB devices you want to connect.
- A full copy of this repository. Keep the Pi software and desktop app on matching releases.
- For remote administration, enable SSH using your own account and connection settings.

Native firmware updates require the supported RP2040 Captain with 8 MiB flash. The automatic USB-MIDI connection is configured for the Captain and Kemper Player.

## Install on the Pi

Run these commands in a terminal on the Pi. They build the Stage page and install the services; they do not flash Captain firmware.

```bash
sudo apt-get update
sudo apt-get install -y git nodejs npm
git clone https://github.com/danmigdev/bosun-midi-captain.git bosun
cd bosun
npm --prefix editor install --no-audit --no-fund
npm --prefix editor run build:stage
sudo bash tools/rpi-hub/install.sh
```

The installer handles the remaining system packages, application account, USB access and service startup. Keep the Stage build step: the installer copies a built page and does not build it itself.

Connect the Captain and Kemper by USB. The installed services start automatically at boot and reconnect the devices when they become available.

Check the installation with:

```bash
systemctl is-active bosun-hub.service bosun-midi.timer bosun-kiosk.service
```

Each service should report `active`. Keep the Pi powered during a firmware update.

## Connect the app and browser

In Bosun Desktop or Android, select **Raspberry Pi (network) → Find Raspberry Pi** and choose your Pi. If discovery does not find it, enter its hostname and port `9876` manually.

Open `http://YOUR_PI_HOSTNAME:8080/` to use Stage from a browser on the same network. Replace `YOUR_PI_HOSTNAME` with your Pi's address. This page uses the connected pedal's live state and accepts Stage inputs.

If a firewall is enabled, allow these ports from your local network:

| Port | Purpose |
| --- | --- |
| TCP 9876 | App connection |
| UDP 9877 | Automatic Pi discovery |
| TCP 8080 | Stage browser page |
| TCP 8081 | Live browser connection |

Keep these services on your local network; they are not a public internet service.

## HDMI display

Connect and power an HDMI screen. Stage is configured to open automatically at startup and select the screen when it is connected later. The screen's advertised preferred resolution is used; a 1920 × 440 panel should advertise that mode. Check the actual image and supported resolution on your screen after connecting it.

Without an HDMI screen, the Pi keeps a virtual 1920 × 440 display available for the viewer below. The kiosk uses reduced motion to keep its light bars steady.

## View the actual Pi display on Windows

![Actual Stage picture rendered by the Pi](../../docs/ui-test-screenshots/stage_vnc.png)

Configure an SSH host alias for your Pi and confirm that key-based login works without an interactive prompt. Supply your own hostname, account and key in your SSH configuration.

From a copy of this repository on Windows, run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\rpi-hub\open-stage-vnc.ps1 -HostName "YOUR_SSH_ALIAS"
```

Replace `YOUR_SSH_ALIAS` with your configured alias. The helper opens the SSH connection in the background and launches the viewer. Run the same command to reopen it later. If local port 6080 is occupied, add `-LocalPort 6082`.

The viewer shows the actual picture rendered on the Pi. It is read-only: clicking it cannot change rigs or effects, and resizing the browser does not change the Pi's display resolution. The stream is limited to 10 frames per second; that does not set the HDMI screen's refresh rate.

The separate `http://YOUR_PI_HOSTNAME:8080/display-preview.html` page simulates an 11.3-inch, 1920 × 440 layout inside your browser. Its **Adatta**, **1:1** and **Schermo intero** buttons change the preview size. That page renders on the viewing computer, has its own appearance preferences and can control the pedal. Use the VNC viewer to inspect the Pi's actual display.

## Updates

For Captain firmware, follow [Update firmware](../../README.md#update-firmware) in the main guide.

To update the complete Pi installation, first finish any running firmware update. In its existing repository copy, run:

```bash
git pull --ff-only
npm --prefix editor install --no-audit --no-fund
npm --prefix editor run build:stage
sudo bash tools/rpi-hub/install.sh
```

For an already installed Pi, the Windows helpers can update only the hub or Stage. They require an existing SSH alias with key-based login and permission to run their administrative commands without an interactive prompt; they do not replace the first installation above.

```powershell
python -m pip install -r .\tools\rpi-hub\requirements.txt pytest
npm --prefix editor install --no-audit --no-fund
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\rpi-hub\deploy-hub.ps1 -HostName "YOUR_SSH_ALIAS"
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\rpi-hub\deploy-stage.ps1 -HostName "YOUR_SSH_ALIAS"
```

The commands require Python and Node.js/npm on Windows. The Stage helper builds the page before sending it.

## Troubleshooting

- **App cannot find the Pi:** check that both are on the same network, then try its hostname and port `9876` directly. Guest Wi-Fi isolation can block discovery and connections.
- **Pi connects but the pedal is missing:** check the Captain's power and USB data cable, then reconnect it.
- **Stage page is missing:** repeat the Stage build and installation steps.
- **Viewer will not open:** verify the SSH alias and rerun the Windows helper. The viewer's localhost URL works only while that helper's tunnel is running.
- **A service is inactive:** inspect its recent log, for example `journalctl -u bosun-hub.service -n 30 --no-pager`.
