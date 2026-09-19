//! RP2040 BootROM USB transport; no mass-storage file copying or external tools.
//! Wire specification: raspberrypi/pico-sdk, boot/picoboot.h. Transaction order:
//! https://github.com/raspberrypi/picotool/blob/2041936441b48a3cc53ae3da9e805229fe8f4e18/picoboot_connection/picoboot_connection.c
use crate::firmware_package::{FLASH_BASE, FLASH_BYTES, SECTOR};
use nusb::{
    transfer::{Buffer, Bulk, ControlIn, ControlOut, ControlType, In, Out, Recipient},
    DeviceInfo, Endpoint, Interface, MaybeFuture,
};
use serde::{Deserialize, Serialize};
use std::{
    thread,
    time::{Duration, Instant},
};

const TIMEOUT: Duration = Duration::from_secs(10);
const FLASH_ID_CODE: &[u8; 152] = include_bytes!("../vendor/picotool/flash_id.bin");
const RAM: u32 = 0x1500_0000; // XIP SRAM, as used by picotool while XIP is disabled.

pub(crate) fn factory_usb_id(vid: u16, pid: u16) -> bool {
    // PaintAudio FW 5 uses Arduino/TinyUSB's 239a:cafe identity. This also
    // appears on other boards: model confirmation and flash checks still apply.
    matches!((vid, pid), (0x239a, 0x80f4) | (0x239a, 0xcafe))
}

fn runtime_usb_id(vid: u16, pid: u16, first_install: bool) -> bool {
    (vid, pid) == (0x239a, 0x80f4) || (first_install && factory_usb_id(vid, pid))
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct UsbIdentity {
    pub serial: String,
    pub bus: String,
    pub ports: Vec<u8>,
}

impl UsbIdentity {
    pub fn from_serial_port(port: &str) -> Result<Self, String> {
        Self::runtime_identity(port, false)
    }
    pub fn from_factory_port(port: &str) -> Result<Self, String> {
        Self::runtime_identity(port, true)
    }
    fn runtime_identity(port: &str, first_install: bool) -> Result<Self, String> {
        let ports = serialport::available_ports().map_err(|e| e.to_string())?;
        let info = ports
            .into_iter()
            .find(|p| p.port_name == port)
            .ok_or("Selected USB port is unavailable")?;
        let serialport::SerialPortType::UsbPort(usb) = info.port_type else {
            return Err("Select the Captain USB data port".into());
        };
        if !runtime_usb_id(usb.vid, usb.pid, first_install) {
            return Err("Selected port is not a MIDI Captain".into());
        }
        let serial = usb
            .serial_number
            .ok_or("The Captain USB serial number is unavailable")?
            .to_uppercase();
        if serial.len() != 16 || !serial.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err("The Captain has no usable unique USB identity".into());
        }
        let devices: Vec<_> = nusb::list_devices()
            .wait()
            .map_err(|e| e.to_string())?
            .filter(|d| {
                (d.vendor_id(), d.product_id()) == (usb.vid, usb.pid)
                    && d.serial_number()
                        .is_some_and(|s| s.eq_ignore_ascii_case(&serial))
            })
            .collect();
        if devices.len() != 1 || devices[0].port_chain().is_empty() {
            return Err("Cannot identify a unique physical Captain USB connection".into());
        }
        Ok(Self {
            serial,
            bus: devices[0].bus_id().into(),
            ports: devices[0].port_chain().to_vec(),
        })
    }
    pub fn matches_boot(&self, device: &DeviceInfo) -> bool {
        (device.vendor_id(), device.product_id()) == (0x2e8a, 0x0003)
            && device.bus_id() == self.bus
            && device.port_chain() == self.ports
    }
    pub fn serial_ports(&self) -> Vec<String> {
        self.runtime_ports(false)
    }
    pub fn factory_ports(&self) -> Vec<String> {
        self.runtime_ports(true)
    }
    fn runtime_ports(&self, first_install: bool) -> Vec<String> {
        serialport::available_ports()
            .unwrap_or_default()
            .into_iter()
            .filter_map(|p| match p.port_type {
                serialport::SerialPortType::UsbPort(u)
                    if runtime_usb_id(u.vid, u.pid, first_install)
                        && u.serial_number
                            .as_deref()
                            .is_some_and(|s| s.eq_ignore_ascii_case(&self.serial)) =>
                {
                    Some(p.port_name)
                }
                _ => None,
            })
            .collect()
    }
}

pub struct Picoboot {
    interface: Interface,
    input: Endpoint<Bulk, In>,
    output: Endpoint<Bulk, Out>,
    token: u32,
    xip: bool,
}

fn packet(token: u32, command: u8, args: &[u8], transfer: usize) -> [u8; 32] {
    assert!(args.len() <= 16);
    let mut packet = [0; 32];
    packet[..4].copy_from_slice(&0x431f_d10bu32.to_le_bytes());
    packet[4..8].copy_from_slice(&token.to_le_bytes());
    packet[8] = command;
    packet[9] = args.len() as u8;
    packet[12..16].copy_from_slice(&(transfer as u32).to_le_bytes());
    packet[16..16 + args.len()].copy_from_slice(args);
    packet
}

fn range(address: u32, size: usize) -> Vec<u8> {
    [address.to_le_bytes(), (size as u32).to_le_bytes()].concat()
}

impl Picoboot {
    pub fn wait_for(identity: &UsbIdentity, timeout: Duration) -> Result<Self, String> {
        let deadline = Instant::now() + timeout;
        let mut last = "Waiting for the Captain USB bootloader".to_string();
        while Instant::now() < deadline {
            let devices: Vec<_> = nusb::list_devices()
                .wait()
                .map_err(|e| e.to_string())?
                .filter(|d| identity.matches_boot(d))
                .collect();
            if devices.len() > 1 {
                return Err("Ambiguous Captain bootloader connection".into());
            }
            if let Some(info) = devices.first() {
                match Self::open(info) {
                    Ok(mut boot) => {
                        boot.check_identity(identity)?;
                        return Ok(boot);
                    }
                    Err(error) => last = error,
                }
            }
            thread::sleep(Duration::from_millis(250));
        }
        Err(format!("{last}. Keep the Captain on the same USB port. On Windows its PICOBOOT interface needs WinUSB; on Linux check USB permissions."))
    }
    fn open(info: &DeviceInfo) -> Result<Self, String> {
        let device = info
            .open()
            .wait()
            .map_err(|e| format!("Open Captain bootloader: {e}"))?;
        let config = device.active_configuration().map_err(|e| e.to_string())?;
        let descriptor = config
            .interface_alt_settings()
            .find(|i| i.class() == 0xff && i.num_endpoints() == 2)
            .ok_or("RP2040 PICOBOOT interface is missing")?;
        let endpoints: Vec<_> = descriptor.endpoints().collect();
        let in_addr = endpoints
            .iter()
            .find(|e| e.address() & 0x80 != 0)
            .ok_or("Missing bootloader IN endpoint")?
            .address();
        let out_addr = endpoints
            .iter()
            .find(|e| e.address() & 0x80 == 0)
            .ok_or("Missing bootloader OUT endpoint")?
            .address();
        let interface = device
            .claim_interface(descriptor.interface_number())
            .wait()
            .map_err(|e| format!("Claim PICOBOOT interface: {e}"))?;
        let input = interface
            .endpoint::<Bulk, In>(in_addr)
            .map_err(|e| e.to_string())?;
        let output = interface
            .endpoint::<Bulk, Out>(out_addr)
            .map_err(|e| e.to_string())?;
        let mut result = Self {
            interface,
            input,
            output,
            token: 0,
            xip: false,
        };
        result.reset()?;
        Ok(result)
    }
    pub fn reset(&mut self) -> Result<(), String> {
        self.input.clear_halt().wait().map_err(|e| e.to_string())?;
        self.output.clear_halt().wait().map_err(|e| e.to_string())?;
        self.interface
            .control_out(
                ControlOut {
                    control_type: ControlType::Vendor,
                    recipient: Recipient::Interface,
                    request: 0x41,
                    value: 0,
                    index: self.interface.interface_number() as u16,
                    data: &[],
                },
                TIMEOUT,
            )
            .wait()
            .map_err(|e| e.to_string())?;
        self.xip = false;
        self.command(1, &[1], None, 0)?; // Exclude mass-storage writes for the entire transaction.
        self.command(6, &[], None, 0)?;
        Ok(())
    }
    fn send(&mut self, data: &[u8]) -> Result<(), String> {
        let result = self.output.transfer_blocking(data.to_vec().into(), TIMEOUT);
        result.status.map_err(|e| format!("PICOBOOT write: {e}"))?;
        if result.actual_len != data.len() {
            return Err("Incomplete PICOBOOT write".into());
        }
        Ok(())
    }
    fn receive(&mut self, length: usize, ack: bool) -> Result<Vec<u8>, String> {
        let result = self
            .input
            .transfer_blocking(Buffer::new(length.max(64).div_ceil(64) * 64), TIMEOUT);
        result.status.map_err(|e| format!("PICOBOOT read: {e}"))?;
        if (ack && result.actual_len > 1) || (!ack && result.actual_len != length) {
            return Err("Incomplete PICOBOOT response".into());
        }
        Ok(result.buffer[..result.actual_len].to_vec())
    }
    fn command(
        &mut self,
        id: u8,
        args: &[u8],
        output: Option<&[u8]>,
        input: usize,
    ) -> Result<Vec<u8>, String> {
        self.token = self.token.wrapping_add(1);
        self.send(&packet(
            self.token,
            id,
            args,
            output.map_or(input, |d| d.len()),
        ))?;
        let mut response = Vec::new();
        if input > 0 {
            response = self.receive(input, false)?;
        }
        if let Some(data) = output {
            self.send(data)?;
        }
        if id & 0x80 != 0 {
            self.send(&[0])?;
        } else {
            self.receive(0, true)?;
        }
        if id != 2 {
            // REBOOT can remove the interface before a status request.
            let status = self
                .interface
                .control_in(
                    ControlIn {
                        control_type: ControlType::Vendor,
                        recipient: Recipient::Interface,
                        request: 0x42,
                        value: 0,
                        index: self.interface.interface_number() as u16,
                        length: 16,
                    },
                    TIMEOUT,
                )
                .wait()
                .map_err(|e| e.to_string())?;
            if status.len() != 16
                || u32::from_le_bytes(status[0..4].try_into().unwrap()) != self.token
                || status[4..8] != [0; 4]
                || status[8] != id
                || status[9] != 0
            {
                return Err(format!("PICOBOOT command {id:02x} was not confirmed"));
            }
        }
        Ok(response)
    }
    fn set_xip(&mut self, enabled: bool) -> Result<(), String> {
        if self.xip != enabled {
            self.command(if enabled { 7 } else { 6 }, &[], None, 0)?;
            self.xip = enabled;
        }
        Ok(())
    }
    fn flash_query(&mut self, opcode: u8, length: usize) -> Result<Vec<u8>, String> {
        self.set_xip(false)?;
        let mut code = FLASH_ID_CODE.to_vec();
        // The vendored stub accepts a bounded SPI command in its two buffers.
        code[8..12].copy_from_slice(&(length as u32).to_le_bytes());
        code[12] = opcode;
        self.command(5, &range(RAM, code.len()), Some(&code), 0)?;
        self.command(8, &RAM.to_le_bytes(), None, 0)?;
        self.command(0x84, &range(RAM + 28, length), None, length)
    }
    fn check_identity(&mut self, identity: &UsbIdentity) -> Result<(), String> {
        let data = self.flash_query(0x4b, 13)?;
        let serial = data[5..13]
            .iter()
            .map(|b| format!("{b:02X}"))
            .collect::<String>();
        if serial != identity.serial {
            return Err("The bootloader flash identity differs from the selected Captain; no flash was written".into());
        }
        let jedec = self.flash_query(0x9f, 4)?;
        if jedec[3] != 23 {
            return Err("Direct native updates require an 8 MiB flash chip".into());
        }
        Ok(())
    }
    pub fn read_flash(&mut self, offset: usize, size: usize) -> Result<Vec<u8>, String> {
        if offset.checked_add(size).is_none_or(|end| end > FLASH_BYTES) {
            return Err("Flash read out of bounds".into());
        }
        self.set_xip(true)?;
        let mut data = Vec::with_capacity(size);
        while data.len() < size {
            let len = (size - data.len()).min(64 * 1024);
            data.extend(self.command(
                0x84,
                &range(FLASH_BASE + (offset + data.len()) as u32, len),
                None,
                len,
            )?);
        }
        Ok(data)
    }
    pub fn write_flash(
        &mut self,
        image: &[u8],
        progress: &mut dyn FnMut(usize, usize),
    ) -> Result<(), String> {
        if image.is_empty() || image.len() > FLASH_BYTES || image.len() % SECTOR != 0 {
            return Err("Unaligned flash image".into());
        }
        self.set_xip(false)?;
        for (n, sector) in image.chunks_exact(SECTOR).enumerate() {
            let address = FLASH_BASE + (n * SECTOR) as u32;
            self.command(3, &range(address, SECTOR), None, 0)?;
            self.command(5, &range(address, SECTOR), Some(sector), 0)?;
            progress((n + 1) * SECTOR, image.len());
        }
        Ok(())
    }
    pub fn reboot(&mut self) -> Result<(), String> {
        self.set_xip(false)?;
        self.command(
            2,
            &[0u32.to_le_bytes(), 0u32.to_le_bytes(), 500u32.to_le_bytes()].concat(),
            None,
            0,
        )?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn stock_fw5_is_discoverable_only_for_first_installation() {
        // USB device descriptor extracted from PaintAudio's official 10S 5.15 UF2.
        let descriptor = [
            0x12, 1, 0, 2, 0, 0, 0, 64, 0x9a, 0x23, 0xfe, 0xca, 0, 1, 1, 2, 3, 1,
        ];
        let vid = u16::from_le_bytes([descriptor[8], descriptor[9]]);
        let pid = u16::from_le_bytes([descriptor[10], descriptor[11]]);
        assert!(factory_usb_id(vid, pid));
        assert!(runtime_usb_id(vid, pid, true));
        assert!(!runtime_usb_id(vid, pid, false));
        assert!(runtime_usb_id(0x239a, 0x80f4, false));
        assert!(!runtime_usb_id(0x133e, 0x0004, true)); // Never touch the Kemper.
        assert!(!runtime_usb_id(0x2e8a, 0x000a, true)); // Unrelated Pico serial.
    }
    #[test]
    fn command_layout_matches_bootrom_wire_format() {
        let p = packet(42, 0x84, &range(FLASH_BASE, 4096), 4096);
        assert_eq!(&p[0..4], &0x431f_d10bu32.to_le_bytes());
        assert_eq!(&p[4..10], &[42, 0, 0, 0, 0x84, 8]);
        assert_eq!(&p[12..24], &[0, 16, 0, 0, 0, 0, 0, 16, 0, 16, 0, 0]);
        assert_eq!(&p[24..], &[0; 8]);
    }
    #[test]
    fn pinned_read_only_flash_query_stub_is_unchanged() {
        assert_eq!(
            crate::firmware_package::sha256(FLASH_ID_CODE),
            "0c598d8a4dc02ede332a65f96aff27a410fd65d8aaa9fa6dc971539c720725b8"
        );
        assert_eq!(FLASH_ID_CODE[8], 13);
        assert_eq!(FLASH_ID_CODE[12], 0x4b);
    }
}
