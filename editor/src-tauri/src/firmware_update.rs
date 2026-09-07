//! The desktop sends a release package to the Pi; it never treats a native
//! binary as a CircuitPython file tree. Missing optional assets disable the
//! unified update offer (for development and builds without a native release).
use std::io::Read;
use std::path::Path;
use base64::{engine::general_purpose::STANDARD, Engine};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Manager, path::BaseDirectory};

const MAX_PACKAGE: u64 = 4 * 1024 * 1024;

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct UpdateManifest {
    schema: u32,
    board: String,
    release: String,
    family: String,
    firmware_version: String,
    flash_bytes: u32,
    firmware_sha256: String,
}

#[derive(Serialize)]
pub struct UpdateArchive {
    data: String,
    sha256: String,
    size: usize,
}

fn read_package(path: &Path) -> Result<(UpdateManifest, Vec<u8>), String> {
    let mut file = std::fs::File::open(path).map_err(|e| e.to_string())?;
    if file.metadata().map_err(|e| e.to_string())?.len() > MAX_PACKAGE {
        return Err("Bundled update exceeds the package size limit".into());
    }
    let mut bytes = Vec::new();
    Read::by_ref(&mut file).take(MAX_PACKAGE + 1).read_to_end(&mut bytes).map_err(|e| e.to_string())?;
    if bytes.len() as u64 > MAX_PACKAGE {
        return Err("Bundled update exceeds the package size limit".into());
    }
    let manifest = {
        let mut zip = zip::ZipArchive::new(std::io::Cursor::new(&bytes)).map_err(|e| e.to_string())?;
        if zip.len() != 2 {
            return Err("Invalid bundled update contents".into());
        }
        let mut entry = zip.by_name("manifest.json").map_err(|e| e.to_string())?;
        if entry.size() > 4096 { return Err("Update manifest is too large".into()); }
        let mut text = String::new();
        Read::by_ref(&mut entry).take(4097).read_to_string(&mut text).map_err(|e| e.to_string())?;
        if text.len() > 4096 { return Err("Update manifest is too large".into()); }
        let manifest: UpdateManifest = serde_json::from_str(&text).map_err(|e| e.to_string())?;
        if manifest.schema != 1 || manifest.board != "midi-captain-rp2040"
            || manifest.family != "native" || manifest.flash_bytes != 8 * 1024 * 1024
            || manifest.release.is_empty() || manifest.firmware_version.is_empty()
            || manifest.firmware_sha256.len() != 64
            || !manifest.firmware_sha256.bytes().all(|c| c.is_ascii_hexdigit()) {
            return Err("Unsupported bundled firmware update".into());
        }
        manifest
    };
    Ok((manifest, bytes))
}

#[tauri::command]
pub fn bundled_update_manifest(app: AppHandle) -> Result<Option<UpdateManifest>, String> {
    let path = app.path().resolve("update/bosun-update.zip", BaseDirectory::Resource).map_err(|e| e.to_string())?;
    if !path.exists() { return Ok(None); }
    read_package(&path).map(|(manifest, _)| Some(manifest))
}

#[tauri::command]
pub fn read_bundled_update(app: AppHandle) -> Result<UpdateArchive, String> {
    let path = app.path().resolve("update/bosun-update.zip", BaseDirectory::Resource).map_err(|e| e.to_string())?;
    let (_, bytes) = read_package(&path)?;
    Ok(UpdateArchive { sha256: format!("{:x}", Sha256::digest(&bytes)),
        size: bytes.len(), data: STANDARD.encode(bytes) })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_non_archive_and_oversized_resources() {
        let dir = std::env::temp_dir().join(format!("bosun-update-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("update.zip");
        std::fs::write(&path, b"not a ZIP").unwrap();
        assert!(read_package(&path).is_err());
        std::fs::File::create(&path).unwrap().set_len(MAX_PACKAGE + 1).unwrap();
        assert!(read_package(&path).is_err());
        std::fs::remove_file(path).unwrap();
        std::fs::remove_dir(dir).unwrap();
    }
}
