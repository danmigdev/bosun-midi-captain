//! The desktop sends the bundled Raspberry Pi package and its release
//! signature to a hub that can update itself. The Pi verifies the signature
//! as root; a build without one (local builds without the release key)
//! simply does not offer the Pi update.
use std::io::Read;
use std::path::Path;
use base64::{engine::general_purpose::STANDARD, Engine};
use serde::Serialize;
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Manager, path::BaseDirectory};

const MAX_PACKAGE: u64 = 4 * 1024 * 1024;
const MAX_SIGNATURE: u64 = 4 * 1024;
const PACKAGE: &str = "pi/bosun-pi-setup.tar.gz";
const SIGNATURE: &str = "pi/bosun-pi-setup.tar.gz.sig";
const BEGIN: &str = "-----BEGIN SSH SIGNATURE-----";
const END: &str = "-----END SSH SIGNATURE-----";

#[derive(Serialize)]
pub struct PiPackageInfo {
    sha256: String,
    size: usize,
}

#[derive(Serialize)]
pub struct PiPackage {
    data: String,
    sha256: String,
    size: usize,
    signature: String,
}

fn read_bounded(path: &Path, limit: u64, what: &str) -> Result<Vec<u8>, String> {
    let mut file = std::fs::File::open(path).map_err(|e| e.to_string())?;
    if file.metadata().map_err(|e| e.to_string())?.len() > limit {
        return Err(format!("Bundled {what} exceeds the size limit"));
    }
    let mut bytes = Vec::new();
    Read::by_ref(&mut file).take(limit + 1).read_to_end(&mut bytes).map_err(|e| e.to_string())?;
    if bytes.len() as u64 > limit {
        return Err(format!("Bundled {what} exceeds the size limit"));
    }
    Ok(bytes)
}

/// The package and signature text, or None when either file is missing.
fn read_signed(package: &Path, signature: &Path) -> Result<Option<(Vec<u8>, String)>, String> {
    if !package.is_file() || !signature.is_file() {
        return Ok(None);
    }
    let bytes = read_bounded(package, MAX_PACKAGE, "Raspberry Pi package")?;
    if bytes.len() < 2 || bytes[..2] != [0x1f, 0x8b] {
        return Err("The bundled Raspberry Pi package is not a gzip archive".into());
    }
    let text = String::from_utf8(read_bounded(signature, MAX_SIGNATURE, "Raspberry Pi signature")?)
        .map_err(|_| "The bundled Raspberry Pi signature is not text".to_string())?;
    let armor = text.trim();
    if !text.is_ascii() || !armor.starts_with(BEGIN) || !armor.ends_with(END) {
        return Err("The bundled Raspberry Pi signature is not an SSH signature".into());
    }
    Ok(Some((bytes, text)))
}

fn resolve(app: &AppHandle, path: &str) -> Result<std::path::PathBuf, String> {
    app.path().resolve(path, BaseDirectory::Resource).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn bundled_pi_update(app: AppHandle) -> Result<Option<PiPackageInfo>, String> {
    let signed = read_signed(&resolve(&app, PACKAGE)?, &resolve(&app, SIGNATURE)?)?;
    Ok(signed.map(|(bytes, _)| PiPackageInfo {
        sha256: format!("{:x}", Sha256::digest(&bytes)), size: bytes.len() }))
}

#[tauri::command]
pub fn read_bundled_pi_update(app: AppHandle) -> Result<PiPackage, String> {
    let (bytes, signature) = read_signed(&resolve(&app, PACKAGE)?, &resolve(&app, SIGNATURE)?)?
        .ok_or("This Bosun build has no signed Raspberry Pi package")?;
    Ok(PiPackage { sha256: format!("{:x}", Sha256::digest(&bytes)), size: bytes.len(),
        data: STANDARD.encode(bytes), signature })
}

#[cfg(test)]
mod tests {
    use super::*;

    const SIG: &str = "-----BEGIN SSH SIGNATURE-----\nU1NIU0lH\n-----END SSH SIGNATURE-----\n";

    struct Fixture(std::path::PathBuf);
    impl Fixture {
        fn new(name: &str) -> Self {
            let dir = std::env::temp_dir().join(format!("bosun-pi-update-{name}-{}", std::process::id()));
            std::fs::create_dir_all(&dir).unwrap();
            Fixture(dir)
        }
        fn write(&self, name: &str, data: &[u8]) -> std::path::PathBuf {
            let path = self.0.join(name);
            std::fs::write(&path, data).unwrap();
            path
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) { let _ = std::fs::remove_dir_all(&self.0); }
    }

    #[test]
    fn missing_package_or_signature_disables_the_offer() {
        let dir = Fixture::new("missing");
        let package = dir.0.join("p.tar.gz");
        let signature = dir.0.join("p.tar.gz.sig");
        assert!(read_signed(&package, &signature).unwrap().is_none());
        dir.write("p.tar.gz", b"\x1f\x8bdata");
        assert!(read_signed(&package, &signature).unwrap().is_none());
    }

    #[test]
    fn returns_the_package_bytes_and_signature_text() {
        let dir = Fixture::new("valid");
        let package = dir.write("p.tar.gz", b"\x1f\x8bdata");
        let signature = dir.write("p.tar.gz.sig", SIG.as_bytes());
        let (bytes, text) = read_signed(&package, &signature).unwrap().unwrap();
        assert_eq!(bytes, b"\x1f\x8bdata");
        assert_eq!(text, SIG);
    }

    #[test]
    fn rejects_oversized_or_non_gzip_packages() {
        let dir = Fixture::new("package");
        let signature = dir.write("p.tar.gz.sig", SIG.as_bytes());
        let package = dir.write("p.tar.gz", b"not gzip");
        assert!(read_signed(&package, &signature).is_err());
        std::fs::File::create(&package).unwrap().set_len(MAX_PACKAGE + 1).unwrap();
        assert!(read_signed(&package, &signature).is_err());
    }

    #[test]
    fn rejects_oversized_or_malformed_signatures() {
        let dir = Fixture::new("signature");
        let package = dir.write("p.tar.gz", b"\x1f\x8bdata");
        for bad in [b"not a signature".to_vec(), vec![b'A'; MAX_SIGNATURE as usize + 1],
                    format!("{SIG}\u{e9}").into_bytes(), b"\xff\xfe".to_vec()] {
            let signature = dir.write("p.tar.gz.sig", &bad);
            assert!(read_signed(&package, &signature).is_err());
        }
    }
}
