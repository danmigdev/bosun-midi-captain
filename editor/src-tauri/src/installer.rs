use std::path::{Path, PathBuf};

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
    pub assets_present: bool,
    pub asset_problems: Vec<String>,
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
pub async fn detect_pedal(app: AppHandle) -> DeviceState {
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

    // Asset health check
    let (ok, problems) = assets_status(&app);
    state.assets_present = ok;
    state.asset_problems = problems;

    state.usb_pedal_present = usb_pedal_present();

    state
}


fn assets_status(app: &AppHandle) -> (bool, Vec<String>) {
    let mut problems = Vec::new();
    let required = [
        "circuitpython.uf2",
        "firmware/boot.py",
        "firmware/code.py",
        "firmware/lib/captain/__init__.py",
        "lib/neopixel.mpy",
        "lib/adafruit_pixelbuf.mpy",
        "lib/adafruit_st7789.mpy",
        "lib/adafruit_display_text",
    ];
    let resource_root = match app.path().resolve("", BaseDirectory::Resource) {
        Ok(p) => p,
        Err(e) => return (false, vec![format!("resource dir: {}", e)]),
    };
    for rel in &required {
        let p = resource_root.join(rel);
        if !p.exists() {
            problems.push(format!("missing: {}", rel));
        }
    }
    (problems.is_empty(), problems)
}


// ---------------------- flash + install ----------------------

#[tauri::command]
pub fn flash_circuitpython(target: String, app: AppHandle) -> Result<(), String> {
    let resource = app
        .path()
        .resolve("circuitpython.uf2", BaseDirectory::Resource)
        .map_err(|e| format!("resource path: {}", e))?;
    if !resource.exists() {
        return Err(format!("UF2 asset missing at {:?}. Run tools/download-assets.ps1.", resource));
    }
    let target_path = PathBuf::from(&target).join("CURRENT.UF2");
    std::fs::copy(&resource, &target_path)
        .map_err(|e| format!("copy UF2: {}", e))?;
    Ok(())
}

#[tauri::command]
pub fn install_firmware(target: String, app: AppHandle) -> Result<Vec<String>, String> {
    let resource_root = app
        .path()
        .resolve("", BaseDirectory::Resource)
        .map_err(|e| format!("resource path: {}", e))?;
    let target = PathBuf::from(&target);

    let mut written: Vec<String> = Vec::new();

    // Firmware tree → root of CIRCUITPY
    let firmware_src = resource_root.join("firmware");
    if !firmware_src.exists() {
        return Err(format!("firmware asset missing at {:?}", firmware_src));
    }
    copy_dir_recursive(&firmware_src, &target, &mut written)
        .map_err(|e| format!("copy firmware: {}", e))?;

    // Adafruit libs → CIRCUITPY/lib
    let libs_src = resource_root.join("lib");
    if !libs_src.exists() {
        return Err(format!("lib assets missing at {:?}", libs_src));
    }
    let lib_target = target.join("lib");
    std::fs::create_dir_all(&lib_target).map_err(|e| format!("mkdir lib: {}", e))?;
    copy_dir_recursive(&libs_src, &lib_target, &mut written)
        .map_err(|e| format!("copy libs: {}", e))?;

    Ok(written)
}


#[derive(Debug, Serialize)]
pub struct FirmwareFile {
    pub rel: String,            // path relative to firmware/
    pub dst: String,            // device path, e.g. "/lib/captain/app.py"
    pub size: u64,
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
    out.sort_by(|a, b| a.dst.cmp(&b.dst));
    Ok(out)
}

fn walk_collect(root: &Path, dir: &Path, out: &mut Vec<FirmwareFile>) -> std::io::Result<()> {
    for entry in std::fs::read_dir(dir)? {
        let entry = entry?;
        let path = entry.path();
        let name = entry.file_name();
        let s = name.to_string_lossy();
        if path.is_dir() {
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
        } else if path.is_file() {
            if s.ends_with(".pyc") || s.ends_with(".tmp") || s == ".DS_Store" || s == "Thumbs.db" {
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
    let path = root.join(&rel);
    if !path.starts_with(&root) {
        return Err("path escapes firmware root".into());
    }
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
        let tmp = std::env::temp_dir().join(format!("bosun-fw-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        std::fs::create_dir_all(&tmp).map_err(|e| format!("temp dir: {}", e))?;
        let f = std::fs::File::open(&p).map_err(|e| format!("open zip: {}", e))?;
        let mut archive = zip::ZipArchive::new(f).map_err(|e| format!("read zip: {}", e))?;
        archive
            .extract(&tmp)
            .map_err(|e| format!("extract zip: {}", e))?;
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
    out.sort_by(|a, b| a.dst.cmp(&b.dst));
    Ok(out)
}

/// Like `read_firmware_file_b64` but for an arbitrary firmware root.
#[tauri::command]
pub fn read_firmware_file_at_b64(root: String, rel: String) -> Result<String, String> {
    use base64::Engine;
    let root = PathBuf::from(&root);
    let path = root.join(&rel);
    if !path.starts_with(&root) {
        return Err("path escapes firmware root".into());
    }
    let mut data = std::fs::read(&path).map_err(|e| format!("read {:?}: {}", path, e))?;
    let strip = rel.ends_with(".json") || rel.ends_with(".py");
    if strip && data.starts_with(&[0xEF, 0xBB, 0xBF]) {
        data.drain(..3);
    }
    Ok(base64::engine::general_purpose::STANDARD.encode(&data))
}

fn copy_dir_recursive(src: &Path, dst: &Path, written: &mut Vec<String>) -> std::io::Result<()> {
    std::fs::create_dir_all(dst)?;
    for entry in std::fs::read_dir(src)? {
        let entry = entry?;
        let src_path = entry.path();
        let dst_path = dst.join(entry.file_name());
        let file_type = entry.file_type()?;
        if file_type.is_dir() {
            copy_dir_recursive(&src_path, &dst_path, written)?;
        } else if file_type.is_file() {
            // Skip __pycache__ leftovers and .DS_Store
            if let Some(name) = src_path.file_name().and_then(|n| n.to_str()) {
                if name == ".DS_Store" || name == "Thumbs.db" {
                    continue;
                }
            }
            std::fs::copy(&src_path, &dst_path)?;
            written.push(dst_path.to_string_lossy().into_owned());
        }
    }
    Ok(())
}
