# Connect a phone directly to the Pi's Wi-Fi

This optional setup lets an Android device connect directly to a Raspberry Pi
hotspot and edit the USB-connected MIDI Captain. A separate router is not
required during use. Install the [Bosun hub](../README.md) first.

The configuration was verified on a Raspberry Pi 3 running Raspberry Pi OS
Trixie, including automatic hotspot startup after reboot and an Android app
connection. It uses hostapd for Wi-Fi authentication and NetworkManager for the
shared IP network. On the tested Pi, NetworkManager's own access point failed
to complete a phone's WPA2 handshake even after disabling PMF.

## First setup

Run these commands from the repository root, using an Ethernet SSH connection
or a local terminal: this procedure dedicates `wlan0` to the hotspot and ends
any existing Wi-Fi client connection. Internet access is needed to install the
packages. These instructions assume the built-in interface is `wlan0` and that
`bosunap` / `bosun-ap` are available for the new bridge and connection.

Back up existing network settings locally before changing them. Such backups
can contain passwords; keep them outside the repository.

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends hostapd dnsmasq-base network-manager
sudo install -d -m 700 /var/backups/bosun-hotspot
sudo tar -czf /var/backups/bosun-hotspot/network-before-$(date +%Y%m%d-%H%M%S).tar.gz \
  -C / etc/NetworkManager etc/netplan etc/hostapd

if ! sudo test -e /etc/hostapd/bosun.conf; then
  sudo install -m 600 tools/rpi-hub/hotspot/hostapd.conf.example /etc/hostapd/bosun.conf
fi
sudoedit /etc/hostapd/bosun.conf
```

Set `country_code` to the two-letter code for the country where the Pi is used,
and `wpa_passphrase` to your own 8–63 character ASCII password. You may also
change `ssid`. The template intentionally has no country or password filled in;
finish these edits before continuing. Keep the configured file on the Pi.

Install the Wi-Fi ownership rule and service:

```bash
sudo install -m 644 tools/rpi-hub/hotspot/80-bosun-hotspot.conf /etc/NetworkManager/conf.d/
sudo install -m 644 tools/rpi-hub/systemd/bosun-hotspot.service /etc/systemd/system/
sudo nmcli device set wlan0 managed no
sudo nmcli general reload conf
```

Create the shared bridge once. Its default local address is `10.42.0.1`; choose
a different subnet and adapt the instructions if that subnet is already in use.
If a previous setup already runs hostapd on `wlan0`, stop that service before
starting the Bosun unit below.

```bash
sudo nmcli connection add type bridge ifname bosunap con-name bosun-ap \
  bridge.stp no ipv4.method shared ipv4.addresses 10.42.0.1/24 \
  ipv4.never-default yes ipv6.method disabled \
  connection.autoconnect yes connection.autoconnect-retries 0
sudo systemctl daemon-reload
sudo systemctl enable --now bosun-hotspot.service
```

The stock `hostapd.service` is masked on a fresh package installation; the
service to enable here is **bosun-hotspot.service**.
NetworkManager manages DHCP and connection sharing on `bosunap` and continues
to manage Ethernet. The hotspot service starts the bridge before hostapd and
restarts hostapd after a failure. Its enablement survives reboot.

## Connect Android

1. Join the Wi-Fi name and password configured on the Pi. If Android reports
   that the network has no internet, keep the connection to use Bosun locally.
2. In Bosun, select **Raspberry Pi (network)**, then **Find Raspberry Pi**.
3. Select the Pi and choose **Connect**. If discovery is unavailable, enter
   `10.42.0.1` and port `9876` (or the address chosen above).
4. Open **Settings** to edit the Captain. Leave the Captain connected to the Pi
   by USB and save changes as usual.

Browser Stage is available at `http://10.42.0.1:8080/` on the hotspot. Firmware
updates still use the supported desktop update flow. Direct Android USB-OTG
editing remains available by selecting **USB** in the app.

## Verify and maintain

```bash
systemctl is-enabled bosun-hotspot.service
systemctl is-active bosun-hotspot.service bosun-hub.service
ip -brief address
sudo hostapd_cli -i wlan0 all_sta
sudo journalctl -u bosun-hotspot.service -n 20 --no-pager
```

Check that a phone can connect, then reboot the Pi and check again. Do not reboot
during a Captain firmware update. For routine Bosun updates, preserve
`/etc/hostapd/bosun.conf`; the main hub installer leaves this optional setup in
place. To change the Wi-Fi password, edit that file locally and restart
`bosun-hotspot.service`, then update the saved Wi-Fi password on the phone.

An authentication failure can look like a wrong password on Android. Check the
hotspot journal before changing credentials. This configuration uses WPA2 with
CCMP, WMM and 802.11n, with PMF disabled for compatibility with the tested Pi's
Wi-Fi chipset. See the [reported Pi hotspot startup issue](https://github.com/raspberrypi/linux/issues/7247)
and [NetworkManager's PMF settings](https://www.networkmanager.dev/docs/api/latest/settings-802-11-wireless-security.html).

Share only redacted diagnostics when reporting problems: station output,
network files and logs can contain credentials or device identifiers.
