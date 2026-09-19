//! First installation using a RAM-only CDC helper and the OS mass-storage driver.
//! No flash is touched by discovery or loading the helper. The durable worker
//! owns backup, write intent and recovery; this module owns the bounded transport.
use crate::{
    firmware_package::{self, NativePackage, FLASH_BYTES, SECTOR, STORAGE_OFFSET},
    picoboot::UsbIdentity,
};
use nusb::MaybeFuture;
use serde::{Deserialize, Serialize};
use std::{
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
    thread,
    time::{Duration, Instant},
};
use tauri::{path::BaseDirectory, AppHandle, Manager};

const MAGIC: u32 = 0x3155_5342;
const PRODUCT: &str = "Bosun USB Installer";
#[derive(Clone, Serialize)]
pub struct Candidate {
    pub id: String,
    pub label: String,
    pub port: String,
    pub identity: UsbIdentity,
}
#[derive(Serialize)]
pub struct Discovery {
    pub devices: Vec<Candidate>,
    pub version: Option<String>,
    pub problem: Option<String>,
}
#[derive(Deserialize)]
struct Manifest {
    schema: u32,
    firmware_sha256: String,
    loader_sha256: String,
    storage_sha256: String,
}
pub struct Assets {
    pub loader: Vec<u8>,
    pub storage: Vec<u8>,
}
fn word(b: &[u8], n: usize) -> u32 {
    u32::from_le_bytes(b[n..n + 4].try_into().unwrap())
}
fn crc(b: &[u8]) -> u32 {
    let mut c = u32::MAX;
    for byte in b {
        c ^= *byte as u32;
        for _ in 0..8 {
            c = (c >> 1) ^ (0xedb88320 & (0u32.wrapping_sub(c & 1)));
        }
    }
    !c
}
fn bounded(path: &Path, limit: usize) -> Result<Vec<u8>, String> {
    let mut bytes = Vec::new();
    fs::File::open(path)
        .map_err(|e| e.to_string())?
        .take(limit as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| e.to_string())?;
    if bytes.len() > limit {
        return Err("Installer asset exceeds size limit".into());
    }
    Ok(bytes)
}
pub fn validate_loader(data: &[u8]) -> Result<(), String> {
    if data.len() < 1024 || data.len() % 512 != 0 || data.len() > 512 * 1024 {
        return Err("Invalid RAM helper size".into());
    }
    let count = data.len() / 512;
    for (i, b) in data.chunks_exact(512).enumerate() {
        if word(b, 0) != 0x0a324655
            || word(b, 4) != 0x9e5d5157
            || word(b, 508) != 0x0ab16f30
            || word(b, 8) != 0x2000
            || word(b, 16) != 256
            || word(b, 28) != 0xe48bff56
            || word(b, 20) != i as u32
            || word(b, 24) != count as u32
            || word(b, 12) != 0x20000000 + (i * 256) as u32
            || word(b, 12) > 0x20040000 - 256
        {
            return Err(
                "Installer helper must load exclusively into contiguous RP2040 SRAM".into(),
            );
        }
    }
    // SDK no_flash has entry code at SRAM start and vectors at SRAM + 0x100.
    let sp = word(data, 544);
    let pc = word(data, 548);
    if sp % 8 != 0
        || !(0x20000008..=0x20042000).contains(&sp)
        || pc & 1 == 0
        || pc < 0x20000000
        || pc >= 0x20000000 + (count * 256) as u32
    {
        return Err("Invalid RAM helper vectors".into());
    }
    Ok(())
}
pub fn assets(app: &AppHandle, package: &NativePackage) -> Result<Assets, String> {
    read_assets(
        &app.path()
            .resolve("installer", BaseDirectory::Resource)
            .map_err(|e| e.to_string())?,
        package,
    )
}
pub fn read_assets(root: &Path, package: &NativePackage) -> Result<Assets, String> {
    let m: Manifest = serde_json::from_slice(&bounded(&root.join("manifest.json"), 4096)?)
        .map_err(|e| e.to_string())?;
    let loader = bounded(&root.join("loader.uf2"), 512 * 1024)?;
    let storage = bounded(&root.join("storage.bin"), FLASH_BYTES - STORAGE_OFFSET)?;
    if m.schema != 1
        || m.firmware_sha256 != package.manifest.firmware_sha256
        || firmware_package::sha256(&loader) != m.loader_sha256
        || firmware_package::sha256(&storage) != m.storage_sha256
        || storage.len() != FLASH_BYTES - STORAGE_OFFSET
        || !storage[..8192].windows(8).any(|v| v == b"littlefs")
    {
        return Err("Incomplete or mismatched native installer. Download the complete latest Bosun Desktop release.".into());
    }
    validate_loader(&loader)?;
    Ok(Assets { loader, storage })
}
pub fn candidates() -> Result<Vec<Candidate>, String> {
    let mut found = Vec::new();
    for d in nusb::list_devices().wait().map_err(|e| e.to_string())? {
        if d.port_chain().is_empty() {
            continue;
        }
        let id = format!(
            "{}:{:?}:{}",
            d.bus_id(),
            d.port_chain(),
            d.serial_number().unwrap_or("")
        );
        if (d.vendor_id(), d.product_id()) == (0x2e8a, 0x0003) {
            found.push(Candidate {
                id,
                label: "RP2040 in BOOTSEL — confirm this is your 10-switch MIDI Captain".into(),
                port: String::new(),
                identity: UsbIdentity {
                    serial: String::new(),
                    bus: d.bus_id().into(),
                    ports: d.port_chain().to_vec(),
                },
            });
        } else if crate::picoboot::factory_usb_id(d.vendor_id(), d.product_id()) {
            let Some(serial) = d.serial_number() else {
                continue;
            };
            if serial.len() != 16 || !serial.bytes().all(|b| b.is_ascii_hexdigit()) {
                continue;
            }
            let identity = UsbIdentity {
                serial: serial.to_uppercase(),
                bus: d.bus_id().into(),
                ports: d.port_chain().to_vec(),
            };
            if let Some(port) = identity
                .factory_ports()
                .iter()
                .find(|port| UsbIdentity::from_factory_port(port).ok().as_ref() == Some(&identity))
            {
                found.push(Candidate {
                    id,
                    label: format!(
                        "{} — {port} — {serial}",
                        if d.product_id() == 0xcafe {
                            "Factory USB device — confirm your MIDI Captain"
                        } else {
                            "MIDI Captain"
                        }
                    ),
                    port: port.clone(),
                    identity,
                });
            }
        }
    }
    Ok(found)
}
#[tauri::command]
pub async fn factory_install_discover(app: AppHandle) -> Result<Discovery, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let devices = candidates()?;
        let validation = (|| {
            let p = app
                .path()
                .resolve("update/bosun-update.zip", BaseDirectory::Resource)
                .map_err(|e| e.to_string())?;
            let p = firmware_package::read_native_package(&p)?;
            assets(&app, &p)?;
            Ok::<_, String>(p.manifest.firmware_version)
        })();
        Ok(Discovery {
            devices,
            version: validation.as_ref().ok().cloned(),
            problem: validation.err(),
        })
    })
    .await
    .map_err(|e| e.to_string())?
}
fn boot_volume(identity: &UsbIdentity) -> Result<Option<PathBuf>, String> {
    let devices: Vec<_> = nusb::list_devices()
        .wait()
        .map_err(|e| e.to_string())?
        .filter(|d| (d.vendor_id(), d.product_id()) == (0x2e8a, 0x0003))
        .collect();
    if devices.len() > 1 {
        return Err("Disconnect other RP2040 devices before installing".into());
    }
    if !devices.first().is_some_and(|d| identity.matches_boot(d)) {
        return Ok(None);
    }
    let disks = sysinfo::Disks::new_with_refreshed_list();
    let volumes: Vec<_> = disks
        .iter()
        .filter(|d| d.name().to_string_lossy() == "RPI-RP2")
        .map(|d| d.mount_point().to_path_buf())
        .collect();
    if volumes.len() > 1 {
        return Err("More than one RPI-RP2 drive; disconnect other boards".into());
    }
    let Some(path) = volumes.first() else {
        return Ok(None);
    };
    let info = bounded(&path.join("INFO_UF2.TXT"), 4096)?;
    if !String::from_utf8_lossy(&info).contains("Board-ID: RPI-RP2") {
        return Err("Unexpected BOOTSEL drive".into());
    }
    Ok(Some(path.clone()))
}
pub struct Helper {
    port: serial2::SerialPort,
    sequence: u32,
    pub uid: String,
}
impl Helper {
    fn exact(&self, bytes: &mut [u8]) -> Result<(), String> {
        let deadline = Instant::now() + Duration::from_secs(8);
        let mut n = 0;
        while n < bytes.len() && Instant::now() < deadline {
            match self.port.read(&mut bytes[n..]) {
                Ok(count) => n += count,
                Err(e) if e.kind() == std::io::ErrorKind::TimedOut => (),
                Err(e) => return Err(e.to_string()),
            }
        }
        if n != bytes.len() {
            return Err("Captain USB transfer timed out. Keep its power and USB connected.".into());
        }
        Ok(())
    }
    fn request(
        &mut self,
        command: u32,
        offset: u32,
        length: usize,
        payload: &[u8],
    ) -> Result<Vec<u8>, String> {
        self.sequence = self.sequence.wrapping_add(1);
        let mut h: Vec<u8> = [
            MAGIC,
            self.sequence,
            command,
            offset,
            length as u32,
            crc(payload),
        ]
        .into_iter()
        .flat_map(u32::to_le_bytes)
        .collect();
        h.extend(crc(&h).to_le_bytes());
        self.port
            .write_all(&h)
            .and_then(|_| self.port.write_all(payload))
            .map_err(|e| e.to_string())?;
        let mut header = [0; 28];
        self.exact(&mut header)?;
        if word(&header, 0) != MAGIC
            || word(&header, 4) != self.sequence
            || word(&header, 8) != command | 0x80000000
            || word(&header, 24) != crc(&header[..24])
            || word(&header, 16) as usize > SECTOR
        {
            return Err("Invalid installer reply".into());
        }
        if word(&header, 12) != 0 {
            return Err(format!(
                "Captain rejected installer command {command} (status {})",
                word(&header, 12)
            ));
        }
        let expected = match command {
            1 => 16,
            2 => length,
            _ => 0,
        };
        if word(&header, 16) as usize != expected {
            return Err("Incomplete installer reply".into());
        }
        let mut data = vec![0; expected];
        self.exact(&mut data)?;
        if crc(&data) != word(&header, 20) {
            return Err("Installer USB checksum mismatch".into());
        }
        Ok(data)
    }
    fn open(identity: &UsbIdentity) -> Result<Option<Self>, String> {
        let helper = nusb::list_devices()
            .wait()
            .map_err(|e| e.to_string())?
            .find(|d| {
                (d.vendor_id(), d.product_id()) == (0x239a, 0x80f4)
                    && d.bus_id() == identity.bus
                    && d.port_chain() == identity.ports
                    && d.product_string() == Some(PRODUCT)
            });
        let Some(info) = helper else {
            return Ok(None);
        };
        let uid = info
            .serial_number()
            .ok_or("Installer UID unavailable")?
            .to_uppercase();
        if !identity.serial.is_empty() && identity.serial != uid {
            return Err("Captain identity changed; installation stopped".into());
        }
        let actual = UsbIdentity {
            serial: uid.clone(),
            ..identity.clone()
        };
        for name in actual.serial_ports() {
            if UsbIdentity::from_serial_port(&name)? != actual {
                continue;
            }
            let mut port = serial2::SerialPort::open(&name, 115200).map_err(|e| e.to_string())?;
            port.set_read_timeout(Duration::from_millis(100))
                .map_err(|e| e.to_string())?;
            port.set_write_timeout(Duration::from_secs(3))
                .map_err(|e| e.to_string())?;
            port.set_dtr(true).map_err(|e| e.to_string())?;
            let mut h = Self {
                port,
                sequence: 0,
                uid,
            };
            let data = h.request(1, 0, 0, &[])?;
            let flash_uid: String = data[..8].iter().map(|b| format!("{b:02X}")).collect();
            if flash_uid != h.uid || word(&data, 8) as usize != FLASH_BYTES || word(&data, 12) != 1
            {
                return Err(
                    "Unsupported Captain identity, flash size or installer protocol".into(),
                );
            }
            return Ok(Some(h));
        }
        Ok(None)
    }
    pub fn wait(identity: &UsbIdentity, loader: &[u8]) -> Result<Self, String> {
        validate_loader(loader)?;
        let deadline = Instant::now() + Duration::from_secs(90);
        let mut copied = false;
        let mut last = String::new();
        while Instant::now() < deadline {
            match Self::open(identity) {
                Ok(Some(h)) => return Ok(h),
                Ok(None) => (),
                Err(e) => last = e,
            }
            if !copied {
                if let Some(root) = boot_volume(identity)? {
                    // Exactly one ROM device + one validated ROM volume. Only SRAM addresses.
                    let mut file = fs::OpenOptions::new()
                        .write(true)
                        .create_new(true)
                        .open(root.join("BOSUNRAM.UF2"))
                        .map_err(|e| e.to_string())?;
                    // The ROM removes the disk when the final block starts executing. A
                    // final filesystem error is resolved by the authenticated CDC handshake.
                    let _ = file.write_all(loader).and_then(|_| file.sync_all());
                    copied = true;
                }
            }
            thread::sleep(Duration::from_millis(400));
        }
        Err(format!("Captain installer did not connect. {last} Reconnect the same Captain in BOOTSEL on the same USB port and retry."))
    }
    pub fn arm(&mut self) -> Result<(), String> {
        let bytes: Result<Vec<_>, _> = (0..16)
            .step_by(2)
            .map(|n| u8::from_str_radix(&self.uid[n..n + 2], 16))
            .collect();
        self.request(3, 0, 8, &bytes.map_err(|e| e.to_string())?)?;
        Ok(())
    }
    pub fn read(&mut self, progress: &mut dyn FnMut(usize, usize)) -> Result<Vec<u8>, String> {
        let mut bytes = Vec::with_capacity(FLASH_BYTES);
        for offset in (0..FLASH_BYTES).step_by(SECTOR) {
            bytes.extend(self.request(2, offset as u32, SECTOR, &[])?);
            progress(bytes.len(), FLASH_BYTES);
        }
        Ok(bytes)
    }
    pub fn write(
        &mut self,
        bytes: &[u8],
        progress: &mut dyn FnMut(usize, usize),
    ) -> Result<(), String> {
        if bytes.len() != FLASH_BYTES {
            return Err("Invalid complete flash image".into());
        }
        self.arm()?;
        for (i, sector) in bytes.chunks_exact(SECTOR).enumerate() {
            let offset = (i * SECTOR) as u32;
            // Preserve unwritten sectors and reduce wear. Every modified sector is
            // verified by the helper; the worker additionally verifies all 8 MiB.
            if self.request(2, offset, SECTOR, &[])? != sector {
                self.request(4, offset, SECTOR, sector)?;
            }
            progress((i + 1) * SECTOR, FLASH_BYTES);
        }
        Ok(())
    }
    pub fn reboot(&mut self) -> Result<(), String> {
        self.request(5, 0, 0, &[])?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn crc_matches_wire_standard() {
        assert_eq!(crc(b"123456789"), 0xcbf43926);
        assert_eq!(crc(&[]), 0);
    }
    #[test]
    fn checks_bundled_loader_and_rejects_flash_addresses() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("resources/installer");
        if !root.join("loader.uf2").exists() {
            return;
        }
        let mut data = fs::read(root.join("loader.uf2")).unwrap();
        validate_loader(&data).unwrap();
        data[12..16].copy_from_slice(&0x10000000u32.to_le_bytes());
        assert!(validate_loader(&data).is_err());
        let p = firmware_package::read_native_package(&root.join("../update/bosun-update.zip"))
            .unwrap();
        assert!(read_assets(&root, &p).is_ok());
    }
}
