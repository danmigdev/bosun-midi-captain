export type SetupId = "direct" | "android" | "desktop" | "pi" | "pi-display" | "pi-wireless";
export type Setup = { id: SetupId; title: string; outcome: string; needs: string[]; steps: string[]; check: string; pi?: boolean; display?: boolean };
export const setups: Setup[] = [
  { id: "direct", title: "Captain + Kemper Player", outcome: "Play using the pedal and its own display, without a phone or Raspberry Pi.",
    needs: ["10-switch MIDI Captain", "Kemper PROFILER Player and power supply", "USB-A to USB-B data cable", "Computer for initial firmware installation and configuration"],
    steps: ["On your computer, connect the Captain and create a Kemper Player profile. Assign the footswitches and save.", "Move the Captain cable from the computer to the Player USB-A port. Power on the Player.", "To edit later, reconnect the Captain to your computer. This direct connection does not provide an external Bosun Stage display."],
    check: "Press a footswitch assigned to a rig: the Player changes rig and the Captain display receives feedback." },
  { id: "android", title: "Captain + Android + Kemper", outcome: "Your phone or tablet runs the MIDI bridge, editor and Stage display. No Raspberry Pi needed.",
    needs: ["Captain and Kemper Player", "Android 10 or later with USB host/OTG support", "Compatible USB hub and OTG adapter", "Two USB-A to USB-B data cables; Player power supply"],
    steps: ["Install bosun.apk from the latest release. Connect the hub to Android through OTG; connect Captain USB-B and Player USB-B to the hub.", "Open Bosun, select USB and accept the USB permissions. Connect the Captain and create a Kemper Player profile if needed.", "Check Bridge ON. Bosun tries to start it automatically; tap Bridge OFF to start it manually if needed. Open Stage and keep Bosun running while playing."],
    check: "Change rig from the Captain: both the Player and Android Stage should update. Check effect feedback too." },
  { id: "desktop", title: "Captain + computer + Kemper", outcome: "Bosun Desktop runs the editor, MIDI bridge and Stage on your computer.",
    needs: ["Captain and Kemper Player", "Computer with Bosun Desktop", "Two USB ports or a USB hub", "Two USB data cables; Player power supply"],
    steps: ["Connect Captain USB-B and Player USB-B to your computer. The Player USB-A port is not used in this setup.", "In Bosun select USB, connect the Captain and create a Kemper Player profile. Check Bridge ON.", "Open Stage. Keep your computer and Bosun running while playing."],
    check: "Change a rig and toggle an effect from the Captain: the Player responds and Stage follows the changes." },
  { id: "pi", title: "Captain + Raspberry Pi 3 + Kemper", outcome: "The Raspberry Pi handles MIDI and shares the Captain over your network. An HDMI display is optional.", pi: true,
    needs: ["Raspberry Pi 3, power supply and microSD card of at least 16 GB", "microSD reader and computer to prepare the card", "Captain and Player, with two USB data cables", "Network with Internet access for installation"],
    steps: ["Prepare the microSD card using the Raspberry step in this guide.", "Connect Captain USB-B and Player USB-B to two Pi USB-A ports. Power the Pi and Player with their own supplies.", "For editing, connect your computer or Android to the same network. In Bosun choose Raspberry Pi (network) > Find Raspberry Pi and select your Pi."],
    check: "The Captain changes rigs on the Player. After setup, close the app: the Pi continues handling MIDI." },
  { id: "pi-display", title: "Raspberry Pi 3 + HDMI display", outcome: "A standalone setup: Raspberry Pi as the hub, with Stage starting automatically on the HDMI display.", pi: true, display: true,
    needs: ["Everything needed for the Raspberry Pi setup", "HDMI display and HDMI cable for Pi 3", "Separate display power supply", "USB touch connection or mouse only if you want on-screen controls"],
    steps: ["Prepare the microSD card in the Raspberry step. Connect Captain USB-B and Player USB-B to two Pi USB-A ports.", "Connect Pi HDMI to the display for video. Power the display separately: HDMI does not power it. For a touchscreen, also connect its USB data cable to the Pi.", "Power on the setup: Stage opens automatically. To edit profiles and footswitches, connect Bosun on your computer or Android to the Pi over the network."],
    check: "Stage appears on the display and follows the Player rig. Without touch or a mouse, the screen provides viewing only." },
  { id: "pi-wireless", title: "Raspberry Pi + Android over Wi-Fi", outcome: "The Pi handles MIDI; Android provides the editor and Stage without a USB cable to the phone.", pi: true,
    needs: ["Raspberry Pi setup, with or without HDMI", "Android phone or tablet with Bosun", "Pi and Android on the same local network"],
    steps: ["Prepare the Pi and leave Captain and Player connected to its USB ports.", "Connect Android to the same Wi-Fi network as the Pi. In Bosun choose Raspberry Pi (network) > Find Raspberry Pi and connect.", "Open Stage or the editor on Android. Closing the app does not interrupt MIDI. To work without a router, you can separately configure the Pi hotspot."],
    check: "Stage follows the Player on your phone. Disconnect Android from Wi-Fi: the Captain still controls the Player through the Pi." },
];
export const faq = [
  ["Do I need CircuitPython or an older Bosun version first?", "No. First installation uses the native firmware included with Bosun Desktop. Download the latest release and check the version shown before installing. Opening the wizard does not flash the Captain: you must press Install."],
  ["Can Android replace the Raspberry Pi?", "Yes. With a compatible USB hub and OTG adapter, Android runs the MIDI bridge and displays Stage. Keep Bosun running. Captain firmware installation still requires Desktop. Android does not share the Captain over the network as the Pi does."],
  ["What are a USB hub, the Bosun hub and Stage?", "A USB hub adds ports. The Bosun MIDI bridge forwards messages between Captain and Player. The Raspberry Pi service also shares the Captain over your network. Stage shows rigs, effects and controls. A USB hub alone does not route MIDI."],
  ["Can I connect the Captain to the Player and computer at once?", "The Captain USB-B port connects to one host at a time. Choose one layout: directly to the Player, or both devices to Android, a computer or a Pi. A USB splitter cannot share the connection between hosts."],
  ["Do I need Internet to play?", "You need it to download the apps and prepare the Pi. Once configured, MIDI and local Stage work without Internet. Network access requires the same local network or a configured Pi hotspot."],
  ["Is the original firmware backed up?", "First installation saves and verifies the entire Captain flash on your computer before writing. The app shows the backup location. Factory settings are not converted into Bosun profiles: create a Kemper Player profile afterwards. If writing is interrupted, reopen Desktop and choose Restore backup with the same Captain on the same USB port."],
  ["Bosun cannot find the Captain. What should I check?", "Use a USB data cable, not a charge-only cable, and connect directly to the computer for installation. If the wizard asks, unplug USB, hold the top-left footswitch and reconnect to the same USB port. Release the switch when RPI-RP2 appears. Close other apps using the pedal."],
  ["Stage opens but rigs do not change. What should I check?", "Check the Kemper Player profile, Player cable and ports shown in your diagram. On Android or Desktop check Bridge ON. On Pi the MIDI bridge starts automatically. A visible Stage page alone does not confirm a working MIDI connection."],
  ["Which devices and systems are verified?", "This setup targets the 10-switch RP2040 MIDI Captain with 8 MiB flash and the Kemper PROFILER Player. Do not assume these instructions apply to other models. The macOS and Linux apps have not been tested. The new first-installation path also needs hardware validation before it can be described as verified."],
];
export function readSetup(): SetupId | null {
  try { const id = localStorage.getItem("BOSUN_SETUP"); return setups.find(s => s.id === id)?.id ?? null; } catch { return null; }
}
export function saveSetup(id: SetupId) { try { localStorage.setItem("BOSUN_SETUP", id); } catch {} }
export function piInstallCommand(path: string, user: string, host: string): string {
  if (!/^[a-z_][a-z0-9_-]{0,31}$/.test(user) || !/^[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}$/.test(host) || !path || /[\r\n\0]/.test(path)) return "";
  const quote = (v: string) => "'" + v.replace(/'/g, "''") + "'";
  const remote = `${user}@${host}`;
  return `scp ${quote(path)} ${quote(remote + ":bosun-pi-setup.tar.gz")}; if ($LASTEXITCODE -eq 0) { ssh -t ${quote(remote)} 'd=$(mktemp -d) && tar -xzf bosun-pi-setup.tar.gz -C "$d" && sudo bash "$d/tools/rpi-hub/quick-install.sh"' }`;
}
