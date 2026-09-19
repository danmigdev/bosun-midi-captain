//! Durable, exclusive native-to-native USB update. Only this worker touches
//! the selected device between the runtime preflight and the final readback.
use crate::{
    firmware_package::{self, NativePackage, FLASH_BYTES},
    picoboot::{Picoboot, UsbIdentity},
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex, MutexGuard,
    },
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tauri::{path::BaseDirectory, AppHandle, Manager, State};

static GATE: Mutex<()> = Mutex::new(());
static BUSY: AtomicBool = AtomicBool::new(false);
static PENDING: AtomicBool = AtomicBool::new(false);
pub fn busy() -> bool {
    BUSY.load(Ordering::Acquire)
}
pub fn normal_operation() -> Result<MutexGuard<'static, ()>, String> {
    let guard = GATE.lock().map_err(|_| "USB operation lock failed")?;
    if busy() || PENDING.load(Ordering::Acquire) {
        return Err("Complete the USB firmware update or recovery first".into());
    }
    Ok(guard)
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Job {
    #[serde(default)]
    pub mode: String,
    #[serde(default)]
    pub loader_sha256: String,
    pub id: String,
    pub phase: String,
    pub message: String,
    pub percent: u8,
    pub identity: UsbIdentity,
    pub port: String,
    pub previous_version: String,
    pub version: String,
    pub backup_path: String,
    pub backup_sha256: String,
    pub write_started: bool,
    pub snapshot: Value,
    revision: u64,
}
impl Job {
    fn terminal(&self) -> bool {
        matches!(self.phase.as_str(), "done" | "restored" | "failed")
    }
}
#[derive(Default)]
pub struct UsbUpdateState {
    current: Arc<Mutex<Option<Job>>>,
    root: Mutex<PathBuf>,
}

fn durable_file(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let mut file = fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(path)
        .map_err(|e| e.to_string())?;
    file.write_all(bytes)
        .and_then(|_| file.sync_all())
        .map_err(|e| e.to_string())?;
    #[cfg(unix)]
    fs::File::open(path.parent().ok_or("Missing parent directory")?)
        .and_then(|f| f.sync_all())
        .map_err(|e| e.to_string())?;
    Ok(())
}

fn durable_rename(from: &Path, to: &Path) -> Result<(), String> {
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Storage::FileSystem::{MoveFileExW, MOVEFILE_WRITE_THROUGH};
        let from: Vec<u16> = from.as_os_str().encode_wide().chain(Some(0)).collect();
        let to: Vec<u16> = to.as_os_str().encode_wide().chain(Some(0)).collect();
        // Both paths are NUL-terminated and live for this synchronous call.
        if unsafe { MoveFileExW(from.as_ptr(), to.as_ptr(), MOVEFILE_WRITE_THROUGH) } == 0 {
            return Err(std::io::Error::last_os_error().to_string());
        }
        Ok(())
    }
    #[cfg(not(windows))]
    {
        fs::rename(from, to).map_err(|e| e.to_string())
    }
}

// Append immutable snapshots: Windows does not atomically replace an existing
// file with std::fs::rename. A torn .tmp never supersedes the last valid record.
fn save_job(root: &Path, job: &mut Job) -> Result<(), String> {
    let dir = root.join(&job.id);
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let path = loop {
        job.revision += 1;
        let candidate = dir.join(format!("{:020}.json", job.revision));
        if !candidate.exists() && !candidate.with_extension("tmp").exists() {
            break candidate;
        }
    };
    let temporary = path.with_extension("tmp");
    durable_file(
        &temporary,
        &serde_json::to_vec(job).map_err(|e| e.to_string())?,
    )?;
    durable_rename(&temporary, &path)?;
    #[cfg(unix)]
    fs::File::open(&dir)
        .and_then(|f| f.sync_all())
        .map_err(|e| e.to_string())?;
    Ok(())
}
fn load_job(dir: &Path) -> Result<Option<Job>, String> {
    let mut records: Vec<_> = fs::read_dir(dir)
        .map_err(|e| e.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?
        .into_iter()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|e| e == "json"))
        .collect();
    records.sort();
    let Some(path) = records.last() else {
        return Ok(None);
    };
    let file = fs::File::open(path).map_err(|e| e.to_string())?;
    let mut bytes = Vec::new();
    file.take(1024 * 1024)
        .read_to_end(&mut bytes)
        .map_err(|e| e.to_string())?;
    serde_json::from_slice(&bytes)
        .map(Some)
        .map_err(|e| format!("Invalid USB recovery journal: {e}"))
}
pub fn initialize(app: &AppHandle) -> Result<(), String> {
    let root = app
        .path()
        .app_data_dir()
        .map_err(|e| e.to_string())?
        .join("usb-updates");
    fs::create_dir_all(&root).map_err(|e| e.to_string())?;
    let mut jobs = Vec::new();
    for entry in fs::read_dir(&root).map_err(|e| e.to_string())? {
        let path = entry.map_err(|e| e.to_string())?.path();
        if path.is_dir() {
            if let Some(job) = load_job(&path)? {
                jobs.push(job);
            }
        }
    }
    jobs.sort_by(|a, b| a.id.cmp(&b.id));
    let pending: Vec<_> = jobs.iter().filter(|j| !j.terminal()).collect();
    if pending.len() > 1 {
        return Err(
            "Multiple unfinished USB updates; preserve the usb-updates backups for recovery".into(),
        );
    }
    let mut current = pending.first().map(|j| (*j).clone()).or_else(|| jobs.pop());
    if let Some(job) = current.as_mut() {
        if !job.terminal() {
            job.phase = if job.write_started {
                "recovery-required"
            } else {
                "failed"
            }
            .into();
            job.message = if job.write_started { "Update interrupted. Restore the saved backup before continuing." }
                else { "Update interrupted before any flash write. Reconnect the Captain to restart its original firmware." }.into();
            save_job(&root, job)?;
        }
    }
    PENDING.store(
        current.as_ref().is_some_and(|j| !j.terminal()),
        Ordering::Release,
    );
    let state = app.state::<UsbUpdateState>();
    *state.current.lock().map_err(|_| "USB state lock failed")? = current;
    *state.root.lock().map_err(|_| "USB path lock failed")? = root;
    Ok(())
}

struct Worker {
    root: PathBuf,
    current: Arc<Mutex<Option<Job>>>,
    job: Job,
}
impl Worker {
    fn publish(&mut self, phase: &str, percent: u8, message: &str) -> Result<(), String> {
        self.job.phase = phase.into();
        self.job.percent = percent;
        self.job.message = message.into();
        save_job(&self.root, &mut self.job)?;
        *self.current.lock().map_err(|_| "USB state lock failed")? = Some(self.job.clone());
        Ok(())
    }
    fn progress(&self, phase: &str, completed: usize, total: usize) {
        if let Ok(mut current) = self.current.lock() {
            if let Some(job) = current.as_mut() {
                job.phase = phase.into();
                job.percent = (completed * 100 / total) as u8;
            }
        }
    }
    fn backup(&self) -> Result<Vec<u8>, String> {
        // Path is derived from the trusted application directory, never from a UI argument.
        let file = fs::File::open(self.root.join(&self.job.id).join("flash-before.bin"))
            .map_err(|e| e.to_string())?;
        let mut bytes = Vec::new();
        file.take(FLASH_BYTES as u64 + 1)
            .read_to_end(&mut bytes)
            .map_err(|e| e.to_string())?;
        if bytes.len() != FLASH_BYTES
            || self.job.backup_sha256.len() != 64
            || firmware_package::sha256(&bytes) != self.job.backup_sha256
        {
            return Err(
                "Recovery backup is missing, incomplete or corrupt; no flash was written".into(),
            );
        }
        Ok(bytes)
    }
    fn install(&mut self, package: NativePackage) -> Result<(), String> {
        self.publish(
            "preflight",
            0,
            "Checking the selected Captain and saved configuration",
        )?;
        let runtime = Runtime::open_retry(&self.job.port)?;
        let info = runtime.request("GET_DEVICE_INFO", "DEVICE_INFO")?;
        let version = info["fw"].as_str().ok_or("Missing firmware version")?;
        if !version.contains("-native")
            || info["native_experimental"] != true
            || !info["reboot_modes"]
                .as_array()
                .is_some_and(|v| v.contains(&json!("bootloader")))
        {
            return Err("Direct USB updates require existing Bosun native firmware. Use the Pi updater to migrate CircuitPython.".into());
        }
        let dirty = runtime.request("GET_DIRTY", "DIRTY")?;
        if dirty["patches"].as_array().is_none_or(|v| !v.is_empty()) {
            return Err("Save or discard pending patch edits before updating firmware".into());
        }
        self.job.previous_version = version.into();
        self.job.snapshot = runtime.snapshot()?;
        // Re-read USB metadata immediately before requesting the selected port's reboot.
        if UsbIdentity::from_serial_port(&self.job.port)? != self.job.identity {
            return Err("Captain USB identity changed".into());
        }
        self.publish(
            "bootloader",
            0,
            "Restarting the Captain in its USB bootloader",
        )?;
        runtime.bootloader()?;
        drop(runtime);
        let mut boot = Picoboot::wait_for(&self.job.identity, Duration::from_secs(30))?;
        let result: Result<(), String> = (|| {
            self.publish(
                "backup",
                0,
                "Saving and verifying a complete 8 MiB recovery backup",
            )?;
            let before = boot.read_flash(0, FLASH_BYTES)?;
            let path = self.root.join(&self.job.id).join("flash-before.bin");
            durable_file(&path, &before)?;
            self.job.backup_sha256 = firmware_package::sha256(&before);
            self.job.backup_path = path.to_string_lossy().into_owned();
            if self.backup()? != before || boot.read_flash(0, FLASH_BYTES)? != before {
                return Err("Full-flash backup could not be verified; no flash was written".into());
            }
            // Durable write intent MUST precede the first erase, including on power loss.
            self.job.write_started = true;
            self.publish(
                "writing",
                0,
                "Installing firmware; saved settings stay in place",
            )?;
            install_image(&mut boot, &before, &package.image, &mut |n, t| {
                self.progress("writing", n, t)
            })?;
            self.publish(
                "rebooting",
                100,
                "Flash verified. Restarting and checking the installed firmware",
            )?;
            boot.reboot()?;
            Ok(())
        })();
        if result.is_err() && !self.job.write_started {
            let _ = boot.reboot();
        }
        drop(boot);
        result?;
        self.job.port = verify_runtime(&self.job.identity, &self.job.version, &self.job.snapshot)?;
        self.publish(
            "done",
            100,
            "Firmware updated and configuration verified. Recovery backup retained.",
        )
    }
    fn restore(&mut self) -> Result<(), String> {
        if self.job.mode == "install" {
            return self.restore_factory();
        }
        let backup = self.backup()?;
        self.publish(
            "restoring",
            0,
            "Restoring the original firmware and configuration from the verified backup",
        )?;
        // If new firmware has already booted, reboot only this same USB identity.
        for port in self.job.identity.serial_ports() {
            if let Ok(runtime) = Runtime::open(&port) {
                if runtime.request("GET_DEVICE_INFO", "DEVICE_INFO").is_ok() {
                    runtime.bootloader()?;
                    break;
                }
            }
        }
        let mut boot = Picoboot::wait_for(&self.job.identity, Duration::from_secs(30))?;
        restore_image(&mut boot, &backup, &mut |n, t| {
            self.progress("restoring", n, t)
        })?;
        boot.reboot()?;
        drop(boot);
        self.job.port = verify_runtime(
            &self.job.identity,
            &self.job.previous_version,
            &self.job.snapshot,
        )?;
        self.publish(
            "restored",
            100,
            "Original firmware and configuration restored and verified.",
        )
    }
    fn run(
        mut self,
        package: Option<NativePackage>,
        factory: Option<crate::factory_install::Assets>,
    ) {
        #[cfg(windows)]
        unsafe {
            windows_sys::Win32::System::Power::SetThreadExecutionState(
                windows_sys::Win32::System::Power::ES_CONTINUOUS
                    | windows_sys::Win32::System::Power::ES_SYSTEM_REQUIRED,
            );
        }
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            if let Some(package) = package {
                if let Some(assets) = factory {
                    self.install_factory(package, assets)
                } else {
                    self.install(package)
                }
            } else {
                self.restore()
            }
        }))
        .unwrap_or_else(|_| Err("USB update worker stopped unexpectedly".into()));
        if let Err(error) = result {
            let recovery_error = if self.job.write_started {
                self.restore().err()
            } else {
                None
            };
            if self.job.phase != "restored" {
                let phase = if self.job.write_started {
                    "recovery-required"
                } else {
                    "failed"
                };
                let message = format!("{error}{}", recovery_error.map(|e| format!(" Recovery: {e}. Keep the backup and reconnect the same Captain on the same USB port in BOOTSEL mode, then choose Restore backup.")).unwrap_or_else(|| " No firmware was written; reconnect the Captain if it remains in BOOTSEL mode.".into()));
                if self.publish(phase, 0, &message).is_err() {
                    // Keep the in-memory error actionable even when the disk has failed.
                    if let Ok(mut current) = self.current.lock() {
                        *current = Some(self.job.clone());
                    }
                }
            }
        }
        PENDING.store(!self.job.terminal(), Ordering::Release);
        BUSY.store(false, Ordering::Release);
        #[cfg(windows)]
        unsafe {
            windows_sys::Win32::System::Power::SetThreadExecutionState(
                windows_sys::Win32::System::Power::ES_CONTINUOUS,
            );
        }
    }

    fn install_factory(
        &mut self,
        package: NativePackage,
        assets: crate::factory_install::Assets,
    ) -> Result<(), String> {
        self.publish(
            "preflight",
            0,
            "Checking the selected Captain before first installation",
        )?;
        if !self.job.port.is_empty() {
            if UsbIdentity::from_serial_port(&self.job.port)? != self.job.identity {
                return Err("Captain USB identity changed".into());
            }
            // Native Bosun has separate console and data CDC ports. Probe every
            // port of this identity so choosing its console cannot bypass the guard.
            for port in self.job.identity.serial_ports() {
                if UsbIdentity::from_serial_port(&port).ok().as_ref() != Some(&self.job.identity) {
                    continue;
                }
                if let Ok(runtime) = Runtime::open_retry(&port) {
                    if let Ok(info) = runtime.request("GET_DEVICE_INFO", "DEVICE_INFO") {
                        if info["fw"].as_str().is_some_and(|v| v.contains("-native")) {
                            return Err("Bosun native is already installed. Connect and use Update firmware to preserve your profiles.".into());
                        }
                        return Err("Bosun is already installed. Use the migration/update procedure to preserve existing profiles.".into());
                    }
                }
            }
        }
        self.publish("bootloader", 0, "Starting the Captain installer. If it does not restart automatically, unplug its USB cable, hold the top-left footswitch and reconnect it to the SAME computer USB port. Release the switch when RPI-RP2 appears.")?;
        touch_bootloader(&self.job.identity, &self.job.port);
        let mut helper = crate::factory_install::Helper::wait(&self.job.identity, &assets.loader)?;
        self.job.identity.serial = helper.uid.clone();
        let result = (|| {
            self.publish(
                "backup",
                0,
                "Saving the complete original firmware and files on this computer",
            )?;
            let before = helper.read(&mut |n, t| self.progress("backup", n, t))?;
            let path = self.root.join(&self.job.id).join("flash-before.bin");
            durable_file(&path, &before)?;
            self.job.backup_path = path.to_string_lossy().into_owned();
            self.job.backup_sha256 = firmware_package::sha256(&before);
            self.publish(
                "backup",
                0,
                "Verifying the saved backup against the complete Captain flash",
            )?;
            if self.backup()? != before
                || helper.read(&mut |n, t| self.progress("backup", n, t))? != before
            {
                return Err(
                    "Original flash backup could not be verified; no firmware was written".into(),
                );
            }
            if before
                .windows(b"native_experimental".len())
                .any(|v| v == b"native_experimental")
            {
                return Err("Native Bosun detected in the backup. Reconnect normally and use Update firmware to preserve profiles.".into());
            }
            let image = factory_image(&before, &package.image, &assets.storage)?;
            self.job.write_started = true;
            self.publish("writing", 0, "Installing native Bosun and preparing a fresh profile area. Keep USB and power connected.")?;
            restore_image(&mut helper, &image, &mut |n, t| {
                self.progress("writing", n, t)
            })?;
            self.publish(
                "rebooting",
                100,
                "Firmware verified. Waiting for native Bosun to start",
            )?;
            helper.reboot()?;
            Ok::<(), String>(())
        })();
        if result.is_err() && !self.job.write_started {
            let _ = helper.reboot();
        }
        drop(helper);
        result?;
        self.job.port = verify_runtime(&self.job.identity, &self.job.version, &Value::Null)?;
        self.publish("done",100,"Native Bosun installed and storage verified. Create a Kemper Player profile to finish setup. The complete original backup is retained on this computer.")
    }
    fn restore_factory(&mut self) -> Result<(), String> {
        let backup = self.backup()?;
        let loader =
            fs::read(self.root.join(&self.job.id).join("loader.uf2")).map_err(|e| e.to_string())?;
        if firmware_package::sha256(&loader) != self.job.loader_sha256 {
            return Err("Recovery helper checksum mismatch".into());
        }
        self.publish("restoring",0,"Restoring the original firmware and files. If needed, reconnect this Captain in BOOTSEL on the SAME USB port.")?;
        for port in self.job.identity.serial_ports() {
            if UsbIdentity::from_serial_port(&port).ok().as_ref() != Some(&self.job.identity) {
                continue;
            }
            if let Ok(runtime) = Runtime::open(&port) {
                if runtime.request("GET_DEVICE_INFO", "DEVICE_INFO").is_ok() {
                    let _ = runtime.bootloader();
                }
            }
        }
        let mut helper = crate::factory_install::Helper::wait(&self.job.identity, &loader)?;
        restore_image(&mut helper, &backup, &mut |n, t| {
            self.progress("restoring", n, t)
        })?;
        helper.reboot()?;
        self.publish("restored",100,"Original firmware and files restored. Every flash byte was verified before reboot. The factory firmware does not report its startup status to Bosun.")
    }
}

fn touch_bootloader(identity: &UsbIdentity, port: &str) {
    if UsbIdentity::from_serial_port(port).ok().as_ref() != Some(identity) {
        return;
    }
    // CircuitPython's supported 1200-baud reset, sent ONLY to the chosen device.
    if let Ok(handle) = serial2::SerialPort::open(port, 1200) {
        let _ = handle.set_dtr(false);
    }
}
fn factory_image(before: &[u8], firmware: &[u8], storage: &[u8]) -> Result<Vec<u8>, String> {
    use firmware_package::{SECTOR, STORAGE_OFFSET};
    if before.len() != FLASH_BYTES
        || firmware.is_empty()
        || firmware.len() > STORAGE_OFFSET
        || firmware.len() % SECTOR != 0
        || storage.len() != FLASH_BYTES - STORAGE_OFFSET
    {
        return Err("Invalid initial installation boundaries".into());
    }
    let mut expected = before.to_vec();
    expected[..firmware.len()].copy_from_slice(firmware);
    expected[STORAGE_OFFSET..].copy_from_slice(storage);
    Ok(expected)
}

trait Flash {
    fn read(&mut self) -> Result<Vec<u8>, String>;
    fn write(&mut self, bytes: &[u8], progress: &mut dyn FnMut(usize, usize))
        -> Result<(), String>;
}
impl Flash for Picoboot {
    fn read(&mut self) -> Result<Vec<u8>, String> {
        self.read_flash(0, FLASH_BYTES)
    }
    fn write(
        &mut self,
        bytes: &[u8],
        progress: &mut dyn FnMut(usize, usize),
    ) -> Result<(), String> {
        self.write_flash(bytes, progress)
    }
}
impl Flash for crate::factory_install::Helper {
    fn read(&mut self) -> Result<Vec<u8>, String> {
        self.read(&mut |_, _| {})
    }
    fn write(&mut self, b: &[u8], p: &mut dyn FnMut(usize, usize)) -> Result<(), String> {
        self.write(b, p)
    }
}
fn install_image(
    flash: &mut impl Flash,
    before: &[u8],
    image: &[u8],
    progress: &mut dyn FnMut(usize, usize),
) -> Result<(), String> {
    if before.len() != FLASH_BYTES
        || image.is_empty()
        || image.len() > firmware_package::STORAGE_OFFSET
        || image.len() % firmware_package::SECTOR != 0
    {
        return Err("Invalid update image boundaries".into());
    }
    let mut expected = before.to_vec();
    expected[..image.len()].copy_from_slice(image);
    flash.write(image, progress)?;
    if flash.read()? != expected {
        return Err("Installed flash does not match firmware plus preserved settings".into());
    }
    Ok(())
}
fn restore_image(
    flash: &mut impl Flash,
    backup: &[u8],
    progress: &mut dyn FnMut(usize, usize),
) -> Result<(), String> {
    if backup.len() != FLASH_BYTES {
        return Err("Invalid recovery image size".into());
    }
    flash.write(backup, progress)?;
    if flash.read()? != backup {
        return Err("Restored flash failed readback verification".into());
    }
    Ok(())
}

struct Runtime(serial2::SerialPort);
impl Runtime {
    fn open_retry(port: &str) -> Result<Self, String> {
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            match Self::open(port) {
                Ok(runtime) => return Ok(runtime),
                Err(error) if Instant::now() >= deadline => return Err(error),
                Err(_) => thread::sleep(Duration::from_millis(200)),
            }
        }
    }
    fn open(port: &str) -> Result<Self, String> {
        let mut handle = serial2::SerialPort::open(port, 115200).map_err(|e| e.to_string())?;
        handle
            .set_read_timeout(Duration::from_millis(100))
            .map_err(|e| e.to_string())?;
        handle
            .set_write_timeout(Duration::from_secs(1))
            .map_err(|e| e.to_string())?;
        handle.set_dtr(true).map_err(|e| e.to_string())?;
        let _ = handle.set_rts(true);
        Ok(Self(handle))
    }
    fn request(&self, command: &str, expected: &str) -> Result<Value, String> {
        let id = format!(
            "usb-{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_err(|e| e.to_string())?
                .as_nanos()
        );
        self.0
            .write_all(format!("\n{}\n", json!({"type":command,"id":id})).as_bytes())
            .map_err(|e| e.to_string())?;
        let deadline = Instant::now() + Duration::from_secs(3);
        let mut buffer = Vec::new();
        let mut chunk = [0; 4096];
        while Instant::now() < deadline {
            match self.0.read(&mut chunk) {
                Ok(n) => buffer.extend_from_slice(&chunk[..n]),
                Err(e) if e.kind() == std::io::ErrorKind::TimedOut => continue,
                Err(e) => return Err(e.to_string()),
            }
            if buffer.len() > 128 * 1024 {
                return Err("Oversized Captain response".into());
            }
            while let Some(n) = buffer.iter().position(|b| *b == b'\n') {
                let line: Vec<_> = buffer.drain(..=n).collect();
                if let Ok(value) = serde_json::from_slice::<Value>(&line) {
                    if value["id"] == id {
                        if value["type"] == expected {
                            return Ok(value);
                        }
                        if value["type"] == "ERROR" {
                            return Err(format!("Captain rejected {command}: {value}"));
                        }
                    }
                }
            }
        }
        Err(format!("Captain did not answer {command}"))
    }
    fn snapshot(&self) -> Result<Value, String> {
        let global = self.request("GET_GLOBAL", "GLOBAL")?;
        let profiles = self.request("LIST_PROFILES", "PROFILE_LIST")?;
        if !global["device"].is_object() || !profiles["profiles"].is_array() {
            return Err("Cannot read saved Captain configuration".into());
        }
        Ok(
            json!({"device":global["device"],"profile":global["profile"],"profiles":profiles["profiles"]}),
        )
    }
    fn bootloader(&self) -> Result<(), String> {
        self.0
            .write_all(b"\n{\"type\":\"REBOOT\",\"mode\":\"bootloader\",\"id\":\"usb-reboot\"}\n")
            .map_err(|e| e.to_string())?;
        // A successful reboot may remove CDC before FlushFileBuffers returns.
        // Bootloader discovery and flash UID verification confirm the transition.
        let _ = self.0.flush();
        Ok(())
    }
}
fn verify_runtime(
    identity: &UsbIdentity,
    version: &str,
    snapshot: &Value,
) -> Result<String, String> {
    let deadline = Instant::now() + Duration::from_secs(30);
    let mut last = "Captain did not reconnect".to_string();
    while Instant::now() < deadline {
        for port in identity.serial_ports() {
            match Runtime::open(&port).and_then(|runtime| {
                let info = runtime.request("GET_DEVICE_INFO", "DEVICE_INFO")?;
                if info["fw"] != version {
                    return Err("Unexpected firmware version after reboot".into());
                }
                let actual = runtime.snapshot()?;
                if (!snapshot.is_null() && actual != *snapshot)
                    || (snapshot.is_null()
                        && actual["profiles"].as_array().is_none_or(|p| !p.is_empty()))
                {
                    return Err("Saved configuration changed after reboot".into());
                }
                let stats = runtime.request("STATS", "STATS")?;
                if stats["storage_ready"] != true {
                    return Err("Captain storage is not ready after reboot".into());
                }
                Ok(())
            }) {
                Ok(()) => return Ok(port),
                Err(e) => last = e,
            }
        }
        thread::sleep(Duration::from_millis(500));
    }
    Err(last)
}

fn detach(app: &AppHandle) -> Result<(), String> {
    crate::midi::midi_bridge_stop(app.state());
    crate::serial::disconnect(app.state())?;
    // Reader uses a 50 ms timeout; open retries below also tolerate a slow driver.
    thread::sleep(Duration::from_millis(200));
    Ok(())
}
#[tauri::command]
pub fn usb_update_status(state: State<UsbUpdateState>) -> Result<Option<Job>, String> {
    Ok(state
        .current
        .lock()
        .map_err(|_| "USB state lock failed")?
        .clone())
}
#[tauri::command]
pub async fn usb_update_start(port: String, app: AppHandle) -> Result<Job, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let _gate = normal_operation()?;
        if crate::tcp_serial::tcp_active() {
            return Err("Connect the Captain directly by USB first".into());
        }
        let path = app
            .path()
            .resolve("update/bosun-update.zip", BaseDirectory::Resource)
            .map_err(|e| e.to_string())?;
        let package = firmware_package::read_native_package(&path)?;
        let identity = UsbIdentity::from_serial_port(&port)?;
        let state = app.state::<UsbUpdateState>();
        let root = state
            .root
            .lock()
            .map_err(|_| "USB path lock failed")?
            .clone();
        let mut job = Job {
            mode: "update".into(),
            loader_sha256: String::new(),
            id: format!(
                "{}-{}",
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map_err(|e| e.to_string())?
                    .as_millis(),
                std::process::id()
            ),
            phase: "preflight".into(),
            message: "Preparing direct USB update".into(),
            percent: 0,
            identity,
            port,
            previous_version: String::new(),
            version: package.manifest.firmware_version.clone(),
            backup_path: String::new(),
            backup_sha256: String::new(),
            write_started: false,
            snapshot: Value::Null,
            revision: 0,
        };
        save_job(&root, &mut job)?;
        detach(&app)?;
        BUSY.store(true, Ordering::Release);
        PENDING.store(true, Ordering::Release);
        *state.current.lock().map_err(|_| "USB state lock failed")? = Some(job.clone());
        let worker = Worker {
            root,
            current: state.current.clone(),
            job: job.clone(),
        };
        thread::spawn(move || worker.run(Some(package), None));
        Ok(job)
    })
    .await
    .map_err(|e| e.to_string())?
}
#[tauri::command]
pub async fn usb_update_recover(app: AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let _gate = GATE.lock().map_err(|_| "USB operation lock failed")?;
        if busy() {
            return Err("USB update is already running".into());
        }
        let state = app.state::<UsbUpdateState>();
        let job = state
            .current
            .lock()
            .map_err(|_| "USB state lock failed")?
            .clone()
            .ok_or("No USB recovery job")?;
        if job.phase != "recovery-required" || !job.write_started {
            return Err("No USB recovery is required".into());
        }
        let mut worker = Worker {
            root: state
                .root
                .lock()
                .map_err(|_| "USB path lock failed")?
                .clone(),
            current: state.current.clone(),
            job,
        };
        worker.backup()?;
        detach(&app)?;
        worker.publish(
            "restoring",
            0,
            "Waiting for the same Captain to restore its backup",
        )?;
        BUSY.store(true, Ordering::Release);
        thread::spawn(move || worker.run(None, None));
        Ok(())
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn factory_install_start(
    candidate_id: String,
    confirmed_model: bool,
    app: AppHandle,
) -> Result<Job, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let _gate = normal_operation()?;
        if !confirmed_model {
            return Err("Confirm the supported 10-switch MIDI Captain before installing".into());
        }
        if crate::tcp_serial::tcp_active() {
            return Err(
                "Disconnect the network session and connect the Captain directly to this computer"
                    .into(),
            );
        }
        let path = app
            .path()
            .resolve("update/bosun-update.zip", BaseDirectory::Resource)
            .map_err(|e| e.to_string())?;
        let package = firmware_package::read_native_package(&path)?;
        let assets = crate::factory_install::assets(&app, &package)?;
        let candidate = crate::factory_install::candidates()?
            .into_iter()
            .find(|c| c.id == candidate_id)
            .ok_or("Selected Captain disconnected; refresh the device list")?;
        let state = app.state::<UsbUpdateState>();
        let root = state
            .root
            .lock()
            .map_err(|_| "USB path lock failed")?
            .clone();
        let mut job = Job {
            id: format!(
                "{}-{}",
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map_err(|e| e.to_string())?
                    .as_millis(),
                std::process::id()
            ),
            mode: "install".into(),
            loader_sha256: firmware_package::sha256(&assets.loader),
            phase: "preflight".into(),
            message: "Preparing first native installation".into(),
            percent: 0,
            identity: candidate.identity,
            port: candidate.port,
            previous_version: String::new(),
            version: package.manifest.firmware_version.clone(),
            backup_path: String::new(),
            backup_sha256: String::new(),
            write_started: false,
            snapshot: Value::Null,
            revision: 0,
        };
        save_job(&root, &mut job)?;
        durable_file(&root.join(&job.id).join("loader.uf2"), &assets.loader)?;
        detach(&app)?;
        *state.current.lock().map_err(|_| "USB state lock failed")? = Some(job.clone());
        BUSY.store(true, Ordering::Release);
        PENDING.store(true, Ordering::Release);
        let worker = Worker {
            root,
            current: state.current.clone(),
            job: job.clone(),
        };
        thread::spawn(move || worker.run(Some(package), Some(assets)));
        Ok(job)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Fake {
        data: Vec<u8>,
        fail: bool,
        corrupt: bool,
        written: usize,
    }
    impl Flash for Fake {
        fn read(&mut self) -> Result<Vec<u8>, String> {
            let mut data = self.data.clone();
            if self.corrupt {
                data[FLASH_BYTES - 1] ^= 1;
            }
            Ok(data)
        }
        fn write(&mut self, b: &[u8], p: &mut dyn FnMut(usize, usize)) -> Result<(), String> {
            self.written += 1;
            let len = if self.fail { b.len() / 2 } else { b.len() };
            self.data[..len].copy_from_slice(&b[..len]);
            if self.fail {
                self.fail = false;
                return Err("USB unplugged mid-write".into());
            }
            p(b.len(), b.len());
            Ok(())
        }
    }
    #[test]
    fn factory_install_initializes_only_firmware_and_storage_and_can_roll_back() {
        let before = vec![0x5a; FLASH_BYTES];
        let storage = vec![0x33; FLASH_BYTES - firmware_package::STORAGE_OFFSET];
        let image = factory_image(&before, &vec![0x42; 8192], &storage).unwrap();
        assert_eq!(
            &image[8192..firmware_package::STORAGE_OFFSET],
            &before[8192..firmware_package::STORAGE_OFFSET]
        );
        assert_eq!(
            &image[firmware_package::STORAGE_OFFSET..],
            storage.as_slice()
        );
        assert!(factory_image(&before, &vec![1; FLASH_BYTES], &storage).is_err());
        assert!(factory_image(&before, &vec![1; 4096], &storage[..10]).is_err());
        let mut flash = Fake {
            data: before.clone(),
            fail: true,
            corrupt: false,
            written: 0,
        };
        assert!(restore_image(&mut flash, &image, &mut |_, _| {}).is_err());
        restore_image(&mut flash, &before, &mut |_, _| {}).unwrap();
        assert_eq!(flash.data, before);
    }
    #[test]
    fn preserves_every_byte_outside_firmware_and_restores_after_partial_write() {
        let before = vec![0xa5; FLASH_BYTES];
        let image = vec![0x42; 8192];
        let mut flash = Fake {
            data: before.clone(),
            fail: false,
            corrupt: false,
            written: 0,
        };
        install_image(&mut flash, &before, &image, &mut |_, _| {}).unwrap();
        assert_eq!(&flash.data[8192..], &before[8192..]);
        flash.fail = true;
        assert!(install_image(&mut flash, &before, &vec![0x33; 8192], &mut |_, _| {}).is_err());
        restore_image(&mut flash, &before, &mut |_, _| {}).unwrap();
        assert_eq!(flash.data, before);
    }
    #[test]
    fn invalid_image_never_writes_and_corrupt_readback_is_failure() {
        let before = vec![0xa5; FLASH_BYTES];
        let mut flash = Fake {
            data: before.clone(),
            fail: false,
            corrupt: true,
            written: 0,
        };
        assert!(install_image(&mut flash, &before, &vec![1; FLASH_BYTES], &mut |_, _| {}).is_err());
        assert_eq!(flash.written, 0);
        assert!(install_image(&mut flash, &before, &vec![1; 4096], &mut |_, _| {}).is_err());
        assert!(restore_image(&mut flash, &before, &mut |_, _| {}).is_err());
    }
    #[test]
    fn torn_journal_does_not_hide_durable_write_intent() {
        let root = std::env::temp_dir().join(format!("bosun-usb-journal-{}", std::process::id()));
        fs::create_dir_all(&root).unwrap();
        let id = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
            .to_string();
        let mut job = Job {
            mode: "update".into(),
            loader_sha256: String::new(),
            id: id.clone(),
            phase: "writing".into(),
            message: String::new(),
            percent: 0,
            identity: UsbIdentity {
                serial: "AA".into(),
                bus: "1".into(),
                ports: vec![1],
            },
            port: "COM1".into(),
            previous_version: "old-native".into(),
            version: "new-native".into(),
            backup_path: String::new(),
            backup_sha256: String::new(),
            write_started: true,
            snapshot: Value::Null,
            revision: 0,
        };
        save_job(&root, &mut job).unwrap();
        let dir = root.join(id);
        fs::write(dir.join("00000000000000000002.tmp"), b"{torn").unwrap();
        assert!(load_job(&dir).unwrap().unwrap().write_started);
        // Retry skips leftover temporary files from the interrupted process.
        job.phase = "recovery-required".into();
        save_job(&root, &mut job).unwrap();
        assert_eq!(load_job(&dir).unwrap().unwrap().phase, "recovery-required");
        for entry in fs::read_dir(&dir).unwrap() {
            fs::remove_file(entry.unwrap().path()).unwrap();
        }
        fs::remove_dir(dir).unwrap();
        let _ = fs::remove_dir(root);
    }
}
