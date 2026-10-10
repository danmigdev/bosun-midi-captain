use std::path::{Component, Path, PathBuf};

use serde::Serialize;
use sysinfo::Disks;
use tauri::path::BaseDirectory;
use tauri::{AppHandle, Manager};


// ---------------------- detection ----------------------

// The bundled CircuitPython UF2 is 9.2.x and the firmware uses 9.x-only
// APIs (e.g. the `fourwire` module, split out of `displayio` in 9.0). A
// pedal still on an older CircuitPython (factory MIDI Captains ship 7.3.3)
// cannot run the firmware - the import crashes on boot. So we refuse to
// install onto an incompatible CircuitPython and route the user to a
// CircuitPython flash first.
const REQUIRED_CP_MAJOR: u32 = 9;

#[derive(Debug, Serialize, Default)]
pub struct DeviceState {
    pub bootloader_drive: Option<String>,
    pub circuitpy_drive: Option<String>,
    pub has_captain_firmware: bool,
    pub captain_version: Option<String>,
    // CircuitPython version read from the CIRCUITPY drive's boot_out.txt,
    // and whether it's compatible with the bundled firmware. `ok` is true
    // when we cannot read/parse a version (don't block on uncertainty);
    // it's only false when we positively detect an incompatible major.
    pub circuitpython_version: Option<String>,
    pub circuitpython_ok: bool,
    // True when a pedal-class USB serial device is plugged in (CircuitPython
    // VID 239A or the RP2 ROM-bootloader VID 2E8A), regardless of whether it
    // speaks the bosun protocol. The frontend combines this with "not
    // connected" + "no captain firmware" to spot an unflashed pedal and offer
    // to install. Confirmation-gated, because this can't tell a MIDI Captain
    // from a bare Pico.
    pub usb_pedal_present: bool,
}

/// USB vendor ids that identify a pedal-class device: Adafruit (CircuitPython
/// runtime) and Raspberry Pi (the RP2040 ROM/UF2 bootloader).
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

/// Pull "9.2.7" out of a boot_out.txt whose first line looks like
/// "Adafruit CircuitPython 9.2.7 on 2025-..; Raspberry Pi Pico with rp2040".
fn parse_cp_version(content: &str) -> Option<String> {
    let line = content.lines().next()?;
    let idx = line.find("CircuitPython ")?;
    let rest = &line[idx + "CircuitPython ".len()..];
    let ver: String = rest
        .chars()
        .take_while(|c| c.is_ascii_digit() || *c == '.')
        .collect();
    if ver.is_empty() {
        None
    } else {
        Some(ver)
    }
}

fn cp_major(ver: &str) -> Option<u32> {
    ver.split('.').next()?.parse().ok()
}

// async (off the UI thread): volume + serial enumeration is polled every few
// seconds while disconnected; on the main thread it would periodically jank.
#[tauri::command]
pub async fn detect_pedal() -> DeviceState {
    let mut state = DeviceState::default();
    let disks = Disks::new_with_refreshed_list();
    for disk in disks.list() {
        let label = disk.name().to_string_lossy().to_string();
        let mount = disk.mount_point().to_path_buf();
        match label.as_str() {
            "RPI-RP2" => state.bootloader_drive = Some(mount.to_string_lossy().into_owned()),
            "CIRCUITPY" => {
                state.circuitpy_drive = Some(mount.to_string_lossy().into_owned());
                // CircuitPython version + compatibility, from boot_out.txt.
                // Default ok=true so we never block on a drive we can't read.
                state.circuitpython_ok = true;
                if let Ok(content) = std::fs::read_to_string(mount.join("boot_out.txt")) {
                    if let Some(ver) = parse_cp_version(&content) {
                        state.circuitpython_ok =
                            cp_major(&ver).map(|m| m == REQUIRED_CP_MAJOR).unwrap_or(true);
                        state.circuitpython_version = Some(ver);
                    }
                }
                let init = mount.join("lib").join("captain").join("__init__.py");
                if init.exists() {
                    state.has_captain_firmware = true;
                    if let Ok(content) = std::fs::read_to_string(&init) {
                        for line in content.lines() {
                            let trim = line.trim();
                            if let Some(rest) = trim.strip_prefix("VERSION") {
                                if let Some(eq) = rest.find('=') {
                                    let v = rest[eq + 1..]
                                        .trim()
                                        .trim_matches(|c| c == '"' || c == '\'');
                                    state.captain_version = Some(v.to_string());
                                }
                            }
                        }
                    }
                }
            }
            _ => {}
        }
    }

    state.usb_pedal_present = usb_pedal_present();

    state
}


#[derive(Debug, Serialize)]
pub struct FirmwareFile {
    pub rel: String,            // path relative to firmware/
    pub dst: String,            // device path, e.g. "/lib/captain/app.py"
    pub size: u64,
}

fn sort_firmware_files(files: &mut [FirmwareFile]) {
    // Install the lazy OTA dependency before protocol, then application roots.
    // An interrupted update must retain the ability to resume via PUT_FILE.
    fn rank(dst: &str) -> u8 {
        match dst {
            "/lib/captain_ota.py" | "/lib/captain_ota.mpy" => 0,
            "/lib/captain/app.py" | "/lib/captain/app.mpy" => 2,
            "/code.py" | "/code.mpy" => 3,
            _ => 1,
        }
    }
    files.sort_by(|a, b| rank(&a.dst).cmp(&rank(&b.dst)).then(a.dst.cmp(&b.dst)));
}

#[tauri::command]
pub fn list_firmware_files(app: AppHandle) -> Result<Vec<FirmwareFile>, String> {
    let root = app
        .path()
        .resolve("firmware", BaseDirectory::Resource)
        .map_err(|e| format!("resource path: {}", e))?;
    if !root.exists() {
        return Err(format!("firmware tree missing at {:?}", root));
    }
    let mut out = Vec::new();
    walk_collect(&root, &root, &mut out).map_err(|e| format!("walk: {}", e))?;
    sort_firmware_files(&mut out);
    Ok(out)
}

fn walk_collect(root: &Path, dir: &Path, out: &mut Vec<FirmwareFile>) -> std::io::Result<()> {
    for entry in std::fs::read_dir(dir)? {
        let entry = entry?;
        let path = entry.path();
        let name = entry.file_name();
        let s = name.to_string_lossy();
        let file_type = entry.file_type()?;
        if file_type.is_symlink() {
            continue;
        }
        if file_type.is_dir() {
            if s == "__pycache__" || s.starts_with('.') {
                continue;
            }
            // /config is intentionally NOT shipped with the firmware:
            // it's where the user's profiles, patches, MIDI Learn and
            // device.json live. Pushing it would clobber every
            // customization on every firmware update. A virgin pedal
            // boots with no profiles and the editor's Onboarding
            // wizard creates the first one.
            if dir == root && s == "config" {
                continue;
            }
            walk_collect(root, &path, out)?;
        } else if file_type.is_file() {
            if s.ends_with(".pyc") || s.ends_with(".tmp") || s == ".DS_Store" || s == "Thumbs.db" {
                continue;
            }
            // Source remains in the repository for review and host-side
            // tests, but shipping both forms defeats precompilation: a later
            // app.py upload can shadow/recreate the large source which the
            // validated app.mpy transaction deliberately removed.
            if has_compiled_sibling(&path) {
                continue;
            }
            let rel = path.strip_prefix(root).unwrap().to_string_lossy().replace('\\', "/");
            let dst = "/".to_string() + &rel;
            let size = entry.metadata()?.len();
            out.push(FirmwareFile { rel, dst, size });
        }
    }
    Ok(())
}

#[tauri::command]
pub fn read_firmware_file_b64(rel: String, app: AppHandle) -> Result<String, String> {
    use base64::Engine;
    let root = app
        .path()
        .resolve("firmware", BaseDirectory::Resource)
        .map_err(|e| format!("resource path: {}", e))?;
    let path = secure_existing_file(&root, &rel)?;
    let mut data = std::fs::read(&path).map_err(|e| format!("read {:?}: {}", path, e))?;
    // Strip UTF-8 BOM from .json / .py files - CircuitPython's json refuses BOMs.
    let strip = rel.ends_with(".json") || rel.ends_with(".py");
    if strip && data.starts_with(&[0xEF, 0xBB, 0xBF]) {
        data.drain(..3);
    }
    Ok(base64::engine::general_purpose::STANDARD.encode(&data))
}

/// Version string of the firmware bundled with this editor build, read from
/// the resource `firmware/lib/captain/__init__.py` (`VERSION = "x.y.z"`).
/// The frontend compares it against the version the pedal reports so an
/// "update available" can be surfaced offline, without a GitHub round-trip -
/// the editor can always install what it ships. Returns an empty string if
/// the resource is missing or has no VERSION line.
#[tauri::command]
pub fn bundled_firmware_version(app: AppHandle) -> Result<String, String> {
    let path = app
        .path()
        .resolve("firmware/lib/captain/__init__.py", BaseDirectory::Resource)
        .map_err(|e| format!("resource path: {}", e))?;
    let content = std::fs::read_to_string(&path)
        .map_err(|e| format!("read {:?}: {}", path, e))?;
    for line in content.lines() {
        let trim = line.trim();
        if let Some(rest) = trim.strip_prefix("VERSION") {
            if let Some(eq) = rest.find('=') {
                let v = rest[eq + 1..].trim().trim_matches(|c| c == '"' || c == '\'');
                return Ok(v.to_string());
            }
        }
    }
    Ok(String::new())
}

// ---------------------- user-selected firmware source ----------------------

/// Open a native picker for a firmware source: a folder (`zip == false`) or
/// a `.zip` archive (`zip == true`). Returns the chosen path, or None if the
/// user cancelled.
#[tauri::command]
pub fn pick_firmware_source(zip: bool) -> Result<Option<String>, String> {
    let dlg = rfd::FileDialog::new();
    let picked = if zip {
        dlg.set_title("Choose a firmware .zip")
            .add_filter("Zip archive", &["zip"])
            .pick_file()
    } else {
        dlg.set_title("Choose the firmware folder").pick_folder()
    };
    Ok(picked.map(|p| p.to_string_lossy().to_string()))
}

/// Resolve a user-selected source (folder or .zip) to a firmware root
/// directory that holds `code.py` + `lib/captain`. Zips are extracted to a
/// temp dir first. Returns the resolved root path for list/read commands.
#[tauri::command]
pub fn prepare_firmware_source(source: String) -> Result<String, String> {
    let p = PathBuf::from(&source);
    let dir = if p.is_dir() {
        p
    } else if p
        .extension()
        .map(|e| e.eq_ignore_ascii_case("zip"))
        .unwrap_or(false)
    {
        let tmp = unique_temp_dir()?;
        let f = std::fs::File::open(&p).map_err(|e| format!("open zip: {}", e))?;
        let mut archive = zip::ZipArchive::new(f).map_err(|e| format!("read zip: {}", e))?;
        extract_zip_safely(&mut archive, &tmp)?;
        tmp
    } else {
        return Err("Source must be a folder or a .zip file".into());
    };

    find_firmware_root(&dir)
        .map(|r| r.to_string_lossy().to_string())
        .ok_or_else(|| {
            "No firmware found in the selection (need code.py and lib/captain)".to_string()
        })
}

/// Find the directory that actually holds the firmware (code.py + lib/captain).
/// Checks the dir itself, a `firmware/` subdir, and one level of children -
/// covers zips with a wrapper folder or a repo checkout.
fn find_firmware_root(dir: &Path) -> Option<PathBuf> {
    fn is_root(d: &Path) -> bool {
        d.join("code.py").exists() && d.join("lib").join("captain").is_dir()
    }
    if is_root(dir) {
        return Some(dir.to_path_buf());
    }
    let fw = dir.join("firmware");
    if is_root(&fw) {
        return Some(fw);
    }
    if let Ok(entries) = std::fs::read_dir(dir) {
        for entry in entries.flatten() {
            let child = entry.path();
            if child.is_dir() {
                if is_root(&child) {
                    return Some(child);
                }
                let cfw = child.join("firmware");
                if is_root(&cfw) {
                    return Some(cfw);
                }
            }
        }
    }
    None
}

/// Like `list_firmware_files` but for an arbitrary firmware root (from
/// `prepare_firmware_source`) instead of the bundled resources.
#[tauri::command]
pub fn list_firmware_files_at(root: String) -> Result<Vec<FirmwareFile>, String> {
    let root = PathBuf::from(&root);
    if !root.exists() {
        return Err(format!("firmware tree missing at {:?}", root));
    }
    let mut out = Vec::new();
    walk_collect(&root, &root, &mut out).map_err(|e| format!("walk: {}", e))?;
    sort_firmware_files(&mut out);
    Ok(out)
}

/// Like `read_firmware_file_b64` but for an arbitrary firmware root.
#[tauri::command]
pub fn read_firmware_file_at_b64(root: String, rel: String) -> Result<String, String> {
    use base64::Engine;
    let root = PathBuf::from(&root);
    let path = secure_existing_file(&root, &rel)?;
    let mut data = std::fs::read(&path).map_err(|e| format!("read {:?}: {}", path, e))?;
    let strip = rel.ends_with(".json") || rel.ends_with(".py");
    if strip && data.starts_with(&[0xEF, 0xBB, 0xBF]) {
        data.drain(..3);
    }
    Ok(base64::engine::general_purpose::STANDARD.encode(&data))
}

fn safe_relative(rel: &str) -> Result<&Path, String> {
    let path = Path::new(rel);
    if path.as_os_str().is_empty()
        || path.is_absolute()
        || path.components().any(|c| !matches!(c, Component::Normal(_)))
    {
        return Err("path escapes firmware root".into());
    }
    Ok(path)
}

fn secure_existing_file(root: &Path, rel: &str) -> Result<PathBuf, String> {
    let rel = safe_relative(rel)?;
    let canonical_root = root.canonicalize().map_err(|e| format!("firmware root: {e}"))?;
    let path = canonical_root.join(rel);
    let canonical_path = path.canonicalize().map_err(|e| format!("read {:?}: {}", path, e))?;
    if !canonical_path.starts_with(&canonical_root) || !canonical_path.is_file() {
        return Err("path escapes firmware root".into());
    }
    Ok(canonical_path)
}

fn unique_temp_dir() -> Result<PathBuf, String> {
    let base = std::env::temp_dir();
    for attempt in 0..100u32 {
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let path = base.join(format!("bosun-fw-{}-{nonce}-{attempt}", std::process::id()));
        match std::fs::create_dir(&path) {
            Ok(()) => return Ok(path),
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(format!("temp dir: {e}")),
        }
    }
    Err("could not allocate unique firmware temp directory".into())
}

fn extract_zip_safely<R: std::io::Read + std::io::Seek>(
    archive: &mut zip::ZipArchive<R>,
    target: &Path,
) -> Result<(), String> {
    for i in 0..archive.len() {
        let mut entry = archive.by_index(i).map_err(|e| format!("zip entry: {e}"))?;
        let Some(rel) = entry.enclosed_name() else {
            return Err(format!("unsafe zip path: {}", entry.name()));
        };
        // Reject Unix symlinks; following one during a later read/copy could escape target.
        if entry.unix_mode().map(|m| m & 0o170000 == 0o120000).unwrap_or(false) {
            return Err(format!("zip symlink not allowed: {}", entry.name()));
        }
        let out = target.join(rel);
        if entry.is_dir() {
            std::fs::create_dir_all(&out).map_err(|e| format!("extract dir: {e}"))?;
        } else {
            if let Some(parent) = out.parent() {
                std::fs::create_dir_all(parent).map_err(|e| format!("extract dir: {e}"))?;
            }
            let mut file = std::fs::OpenOptions::new()
                .write(true).create_new(true).open(&out)
                .map_err(|e| format!("extract file: {e}"))?;
            std::io::copy(&mut entry, &mut file).map_err(|e| format!("extract file: {e}"))?;
        }
    }
    Ok(())
}

fn has_compiled_sibling(path: &Path) -> bool {
    path.extension().and_then(|ext| ext.to_str()) == Some("py")
        && path.with_extension("mpy").is_file()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn upload_order_keeps_dependencies_present_after_each_file() {
        for extension in ["py", "mpy"] {
            let root = unique_temp_dir().unwrap();
            let names = [
                format!("lib/captain/app.{extension}"),
                format!("code.{extension}"),
                format!("lib/captain/protocol.{extension}"),
                format!("lib/captain_ota.{extension}"),
            ];
            for name in &names {
                let file = root.join(name);
                std::fs::create_dir_all(file.parent().unwrap()).unwrap();
                std::fs::write(file, b"fixture").unwrap();
            }
            let files = list_firmware_files_at(root.to_string_lossy().into_owned()).unwrap();
            let mut installed = std::collections::HashSet::new();
            for file in files {
                let dependency = if file.dst == format!("/code.{extension}") {
                    Some(format!("/lib/captain/app.{extension}"))
                } else if file.dst == format!("/lib/captain/app.{extension}") {
                    Some(format!("/lib/captain/protocol.{extension}"))
                } else if file.dst == format!("/lib/captain/protocol.{extension}") {
                    Some(format!("/lib/captain_ota.{extension}"))
                } else { None };
                if let Some(dependency) = dependency {
                    assert!(installed.contains(&dependency), "{} preceded {}", file.dst, dependency);
                }
                installed.insert(file.dst);
            }
            assert_eq!(installed.len(), names.len());
            for name in names { std::fs::remove_file(root.join(name)).unwrap(); }
            std::fs::remove_dir(root.join("lib/captain")).unwrap();
            std::fs::remove_dir(root.join("lib")).unwrap();
            std::fs::remove_dir(root).unwrap();
        }
    }

    #[test]
    fn relative_paths_reject_traversal_and_absolute_paths() {
        assert!(safe_relative("lib/captain/app.py").is_ok());
        assert!(safe_relative("../secret").is_err());
        assert!(safe_relative("lib/../secret").is_err());
        assert!(safe_relative("/secret").is_err());
        assert!(safe_relative("").is_err());
    }

    #[test]
    fn temp_directories_are_unique() {
        let a = unique_temp_dir().unwrap();
        let b = unique_temp_dir().unwrap();
        assert_ne!(a, b);
        std::fs::remove_dir(a).unwrap();
        std::fs::remove_dir(b).unwrap();
    }

    #[test]
    fn compiled_modules_exclude_source_from_ota() {
        let temp = unique_temp_dir().unwrap();
        let source = temp.join("source");
        let modules = source.join("lib").join("captain");
        std::fs::create_dir_all(&modules).unwrap();
        std::fs::write(modules.join("app.py"), b"large source").unwrap();
        std::fs::write(modules.join("app.mpy"), b"C\x06\0\x1fcompiled").unwrap();
        std::fs::write(modules.join("bindings.py"), b"source only").unwrap();

        let mut listed = Vec::new();
        walk_collect(&source, &source, &mut listed).unwrap();
        let names: Vec<_> = listed.iter().map(|file| file.rel.as_str()).collect();
        assert!(names.contains(&"lib/captain/app.mpy"));
        assert!(names.contains(&"lib/captain/bindings.py"));
        assert!(!names.contains(&"lib/captain/app.py"));

        std::fs::remove_dir_all(temp).unwrap();
    }
}
