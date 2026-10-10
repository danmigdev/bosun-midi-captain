use serde::Serialize;
use sysinfo::Disks;


// ---------------------- detection ----------------------

#[derive(Debug, Serialize, Default)]
pub struct DeviceState {
    pub bootloader_drive: Option<String>,
    // Native Bosun exposes no USB drive, so a CIRCUITPY drive always means
    // stock (factory) or other foreign firmware.
    pub circuitpy_drive: Option<String>,
    // True when a pedal-class USB serial device is plugged in (VID 239A, which
    // both the stock firmware and native Bosun report, or the RP2 ROM-bootloader
    // VID 2E8A), regardless of whether it speaks the bosun protocol. The
    // frontend combines this with "not connected" to spot an unflashed pedal
    // and offer to install. Confirmation-gated, because this can't tell a MIDI
    // Captain from a bare Pico.
    pub usb_pedal_present: bool,
}

/// USB vendor ids that identify a pedal-class device: Adafruit (the MIDI
/// Captain's runtime VID) and Raspberry Pi (the RP2040 ROM/UF2 bootloader).
const PEDAL_USB_VIDS: [u16; 2] = [0x239A, 0x2E8A];

fn usb_pedal_present() -> bool {
    serialport::available_ports()
        .map(|ports| {
            ports.iter().any(|p| match &p.port_type {
                serialport::SerialPortType::UsbPort(info) => PEDAL_USB_VIDS.contains(&info.vid),
                _ => false,
            })
        })
        .unwrap_or(false)
}

// async (off the UI thread): volume + serial enumeration is polled every few
// seconds while disconnected; on the main thread it would periodically jank.
#[tauri::command]
pub async fn detect_pedal() -> DeviceState {
    let mut state = DeviceState::default();
    let disks = Disks::new_with_refreshed_list();
    for disk in disks.list() {
        let label = disk.name().to_string_lossy().to_string();
        let mount = disk.mount_point().to_string_lossy().into_owned();
        match label.as_str() {
            "RPI-RP2" => state.bootloader_drive = Some(mount),
            "CIRCUITPY" => state.circuitpy_drive = Some(mount),
            _ => {}
        }
    }

    state.usb_pedal_present = usb_pedal_present();

    state
}
