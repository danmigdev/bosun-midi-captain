//! Bounded validation of the native package before acquiring a USB device.
use crate::firmware_update::UpdateManifest;
use sha2::{Digest, Sha256};
use std::{
    io::{Cursor, Read},
    path::Path,
};

pub const FLASH_BASE: u32 = 0x1000_0000;
pub const FLASH_BYTES: usize = 8 * 1024 * 1024;
pub const STORAGE_OFFSET: usize = FLASH_BYTES - 4 * 1024 * 1024;
pub const STORAGE_HEADER_OFFSET: usize = FLASH_BYTES - STORAGE_OFFSET - 512 * 1024;
pub const SECTOR: usize = 4096;
const MAX_ARCHIVE: u64 = 4 * 1024 * 1024;

pub struct NativePackage {
    pub manifest: UpdateManifest,
    pub image: Vec<u8>,
}

pub fn sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn word(data: &[u8], offset: usize) -> u32 {
    u32::from_le_bytes(data[offset..offset + 4].try_into().unwrap())
}

pub fn firmware_image(data: &[u8]) -> Result<Vec<u8>, String> {
    if data.is_empty() || data.len() % 512 != 0 || data.len() > STORAGE_OFFSET * 2 {
        return Err("Invalid native UF2 size".into());
    }
    let count = data.len() / 512;
    let mut image = Vec::with_capacity(count * 256);
    for (index, block) in data.chunks_exact(512).enumerate() {
        if word(block, 0) != 0x0a32_4655
            || word(block, 4) != 0x9e5d_5157
            || word(block, 508) != 0x0ab1_6f30
            || word(block, 8) != 0x2000
            || word(block, 28) != 0xe48b_ff56
            || word(block, 16) != 256
            || word(block, 12) != FLASH_BASE + (index * 256) as u32
            || word(block, 20) != index as u32
            || word(block, 24) != count as u32
        {
            return Err(
                "UF2 must be a complete contiguous RP2040 image, outside the settings region"
                    .into(),
            );
        }
        image.extend_from_slice(&block[32..288]);
    }
    if image.len() < 512 {
        return Err("UF2 has no vector table".into());
    }
    let stack = word(&image, 256);
    let reset = word(&image, 260);
    if stack % 8 != 0
        || stack <= 0x2000_0000
        || stack > 0x2004_2000
        || reset & 1 == 0
        || reset < FLASH_BASE + 256
        || reset >= FLASH_BASE + image.len() as u32
    {
        return Err("UF2 contains an invalid RP2040 vector table".into());
    }
    image.resize(image.len().div_ceil(SECTOR) * SECTOR, 0xff);
    if image.len() > STORAGE_OFFSET {
        return Err("Firmware overlaps saved settings".into());
    }
    Ok(image)
}

pub fn read_native_package(path: &Path) -> Result<NativePackage, String> {
    let file = std::fs::File::open(path).map_err(|e| e.to_string())?;
    if !file.metadata().map_err(|e| e.to_string())?.is_file() {
        return Err("Update package must be a regular file".into());
    }
    let mut bytes = Vec::new();
    file.take(MAX_ARCHIVE + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| e.to_string())?;
    if bytes.len() as u64 > MAX_ARCHIVE {
        return Err("Update package is too large".into());
    }
    let mut archive = zip::ZipArchive::new(Cursor::new(bytes)).map_err(|e| e.to_string())?;
    if archive.len() != 2 {
        return Err("Unexpected files in update package".into());
    }
    let mut manifest = None;
    let mut firmware = None;
    for index in 0..archive.len() {
        let mut entry = archive.by_index(index).map_err(|e| e.to_string())?;
        let limit = match entry.name() {
            "manifest.json" if manifest.is_none() => 4096,
            "firmware.uf2" if firmware.is_none() => (STORAGE_OFFSET * 2) as u64,
            _ => return Err("Unexpected or duplicate update package entry".into()),
        };
        if entry.is_dir()
            || entry.encrypted()
            || entry.size() == 0
            || entry.size() > limit
            || entry.size() > entry.compressed_size().max(1) * 200
            || entry
                .unix_mode()
                .is_some_and(|mode| !matches!(mode & 0o170000, 0 | 0o100000))
        {
            return Err("Unsupported update package entry".into());
        }
        let mut data = Vec::new();
        let is_manifest = entry.name() == "manifest.json";
        (&mut entry)
            .take(limit + 1)
            .read_to_end(&mut data)
            .map_err(|e| e.to_string())?;
        if data.len() as u64 != entry.size() {
            return Err("Invalid update entry size".into());
        }
        if is_manifest {
            manifest = Some(data);
        } else {
            firmware = Some(data);
        }
    }
    let manifest: UpdateManifest =
        serde_json::from_slice(&manifest.ok_or("Missing manifest")?).map_err(|e| e.to_string())?;
    if manifest.schema != 1
        || manifest.board != "midi-captain-rp2040"
        || manifest.family != "native"
        || manifest.flash_bytes != FLASH_BYTES as u32
        || manifest.release.is_empty()
        || !manifest.firmware_version.ends_with("-native")
    {
        return Err("Unsupported native firmware release".into());
    }
    let firmware = firmware.ok_or("Missing firmware")?;
    if sha256(&firmware) != manifest.firmware_sha256.to_lowercase() {
        return Err("Native firmware checksum mismatch".into());
    }
    let image = firmware_image(&firmware)?;
    let marker = format!("{}\0", manifest.firmware_version);
    if !image.windows(marker.len()).any(|v| v == marker.as_bytes()) {
        return Err("Firmware version does not match the package manifest".into());
    }
    Ok(NativePackage { manifest, image })
}

#[cfg(test)]
mod tests {
    use super::*;
    fn uf2() -> Vec<u8> {
        let mut data = vec![0; 1024];
        for (n, block) in data.chunks_exact_mut(512).enumerate() {
            for (offset, v) in [
                (0, 0x0a32_4655),
                (4, 0x9e5d_5157),
                (8, 0x2000),
                (12, FLASH_BASE + n as u32 * 256),
                (16, 256),
                (20, n as u32),
                (24, 2),
                (28, 0xe48b_ff56),
                (508, 0x0ab1_6f30),
            ] {
                block[offset..offset + 4].copy_from_slice(&v.to_le_bytes());
            }
        }
        data[544..548].copy_from_slice(&0x2004_2000u32.to_le_bytes());
        data[548..552].copy_from_slice(&(FLASH_BASE + 265).to_le_bytes());
        data
    }
    #[test]
    fn validates_and_sector_pads_firmware() {
        let image = firmware_image(&uf2()).unwrap();
        assert_eq!(image.len(), SECTOR);
        assert!(image[512..].iter().all(|b| *b == 0xff));
    }
    #[test]
    fn rejects_bad_family_order_vectors_and_settings_overlap() {
        for (offset, value) in [
            (28, 0),
            (524, FLASH_BASE + STORAGE_OFFSET as u32),
            (532, 0),
            (544, 0x2004_2001),
            (548, FLASH_BASE + 264),
        ] {
            let mut data = uf2();
            data[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
            assert!(firmware_image(&data).is_err(), "offset {offset}");
        }
        assert!(firmware_image(&uf2()[..700]).is_err());
    }
    #[test]
    fn validates_bundled_package_when_present() {
        let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("resources/update/bosun-update.zip");
        if path.exists() {
            assert!(read_native_package(&path).unwrap().image.len() < STORAGE_OFFSET);
        }
    }
}
