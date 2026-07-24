// Android serial backend using tauri-plugin-serialplugin v3.
// Compiled only on Android targets.
// Uses pure-Rust nusb + android-usb-serial under the hood; supports CDC-ACM.
#![cfg(target_os = "android")]

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager, State};
use tauri_plugin_serialplugin::commands::{
    available_ports, open, write, read, close,
    write_request_to_send, write_data_terminal_ready,
};
use tauri_plugin_serialplugin::state::{
    DataBits, FlowControl, Parity, StopBits,
};


// --------------------- shared app state ---------------------

#[derive(Default)]
pub struct AppState {
    pub serial: Mutex<Option<SerialHandle>>,
    pub inbox:  Arc<Mutex<VecDeque<String>>>,
}

pub struct SerialHandle {
    path: String,
    stop: Arc<Mutex<bool>>,
    alive: Arc<AtomicBool>,
}

impl SerialHandle {
    fn is_alive(&self) -> bool {
        self.alive.load(Ordering::Acquire)
    }
}


// --------------------- shared types ---------------------

#[derive(Debug, Serialize, Clone)]
pub struct PortInfo {
    pub name: String,
    pub kind: String,
}


// --------------------- list ---------------------

#[tauri::command]
pub fn list_ports(
    app: AppHandle,
    serial: State<'_, tauri_plugin_serialplugin::api::serial::SerialPort<tauri::Wry>>,
) -> Result<Vec<PortInfo>, String> {
    let ports = available_ports(app.clone(), serial.clone(), None::<bool>)
        .map_err(|e| format!("list ports: {}", e))?;
    let out: Vec<PortInfo> = ports
        .into_keys()
        .map(|name| PortInfo {
            name,
            kind: "serial".to_string(),
        })
        .collect();
    Ok(out)
}


// --------------------- connect / disconnect ---------------------

/// Shortcut for the verbose full path of the plugin's state type.
type SpState<'a> = State<'a, tauri_plugin_serialplugin::api::serial::SerialPort<tauri::Wry>>;

#[tauri::command]
pub async fn connect(
    port: String,
    state: State<'_, AppState>,
    app: AppHandle,
    serial: SpState<'_>,
) -> Result<(), String> {
    let mut guard = state.serial.lock().map_err(|_| "lock poisoned")?;
    if let Some(existing) = guard.as_ref() {
        if existing.is_alive() {
            return Err("already connected".into());
        }
        if let Ok(mut s) = existing.stop.lock() { *s = true; }
        *guard = None;
    }

    let canonical = open(
        app.clone(), serial.clone(), port,
        115_200,
        Some(DataBits::Eight), Some(FlowControl::None),
        Some(Parity::None), Some(StopBits::One),
        Some(1000u64),
    )
    .map_err(|e| format!("open: {}", e))?;

    let _ = write_data_terminal_ready(app.clone(), serial.clone(), canonical.clone(), true);
    let _ = write_request_to_send(app.clone(), serial.clone(), canonical.clone(), true);

    // Sentinel sync: drain until we see our PING echo back as an ACK.
    let sentinel_id = format!("__sync_{}_{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0)
    );
    let sentinel_cmd = format!("\n{{\"type\":\"PING\",\"id\":\"{}\"}}\n", sentinel_id);
    write(app.clone(), serial.clone(), canonical.clone(), sentinel_cmd)
        .map_err(|e| format!("sentinel write: {}", e))?;

    let sentinel_marker = format!("\"id\":\"{}\"", sentinel_id);
    let sentinel_marker_spaced = format!("\"id\": \"{}\"", sentinel_id);
    {
        let mut drain_buf = Vec::<u8>::with_capacity(8192);
        let drain_deadline = std::time::Instant::now() + Duration::from_secs(12);
        let mut seen_sentinel = false;
        while std::time::Instant::now() < drain_deadline && !seen_sentinel {
            match read(app.clone(), serial.clone(), canonical.clone(), Some(50u64), Some(4096usize)) {
                Ok(data) if !data.is_empty() => {
                    drain_buf.extend_from_slice(data.as_bytes());
                    if drain_buf.windows(sentinel_marker.len()).any(|w| w == sentinel_marker.as_bytes())
                        || drain_buf.windows(sentinel_marker_spaced.len()).any(|w| w == sentinel_marker_spaced.as_bytes())
                    {
                        seen_sentinel = true;
                    }
                }
                Ok(_) => continue,
                Err(_) => return Err("sentinel drain read failed".into()),
            }
        }
    }

    let path = canonical.clone();
    let stop = Arc::new(Mutex::new(false));
    let alive = Arc::new(AtomicBool::new(true));
    let stop_for_thread = stop.clone();
    let alive_for_thread = alive.clone();
    let app_for_thread = app.clone();
    let inbox_for_thread = state.inbox.clone();

    thread::spawn(move || {
        let mut accum = Vec::<u8>::with_capacity(8192);
        loop {
            if *stop_for_thread.lock().unwrap() { break; }
            match read(
                app_for_thread.clone(),
                app_for_thread.state::<tauri_plugin_serialplugin::api::serial::SerialPort<tauri::Wry>>(),
                path.clone(),
                Some(50u64),
                Some(4096usize),
            ) {
                Ok(data) if !data.is_empty() => {
                    accum.extend_from_slice(data.as_bytes());
                    let mut new_lines = 0;
                    while let Some(pos) = accum.iter().position(|b| *b == b'\n') {
                        let mut line: Vec<u8> = accum.drain(..=pos).collect();
                        while matches!(line.last(), Some(b'\n') | Some(b'\r')) { line.pop(); }
                        if line.is_empty() { continue; }
                        if let Ok(s) = std::str::from_utf8(&line) {
                            if let Ok(mut q) = inbox_for_thread.lock() {
                                q.push_back(s.to_string());
                                new_lines += 1;
                            }
                        }
                    }
                    if new_lines > 0 {
                        let _ = app_for_thread.emit("firmware-data-ready", ());
                    }
                }
                Ok(_) => continue,
                Err(_) => {
                    alive_for_thread.store(false, Ordering::Release);
                    let _ = app_for_thread.emit("firmware-disconnected", ());
                    break;
                }
            }
        }
        alive_for_thread.store(false, Ordering::Release);
    });

    *guard = Some(SerialHandle { path: canonical, stop, alive });
    Ok(())
}

#[tauri::command]
pub fn disconnect(state: State<AppState>, app: AppHandle, serial: SpState<'_>) -> Result<(), String> {
    let mut guard = match state.serial.lock() {
        Ok(g) => g,
        Err(poisoned) => poisoned.into_inner(),
    };
    if let Some(handle) = guard.as_ref() {
        if let Ok(mut s) = handle.stop.lock() { *s = true; }
        handle.alive.store(false, Ordering::Release);
        let _ = close(app, serial, handle.path.clone());
    }
    *guard = None;
    Ok(())
}


// --------------------- send a JSON line ---------------------

#[tauri::command]
pub fn send_command(line: String, state: State<AppState>, app: AppHandle, serial: SpState<'_>) -> Result<(), String> {
    let path = {
        let guard = state.serial.lock().map_err(|_| "lock poisoned")?;
        let handle = guard.as_ref().ok_or("not connected")?;
        if !handle.is_alive() { return Err("not connected".into()); }
        handle.path.clone()
    };
    let mut data = line;
    if !data.ends_with('\n') { data.push('\n'); }
    write(app, serial, path, data)
        .map_err(|e| {
            if let Ok(guard) = state.serial.lock() {
                if let Some(h) = guard.as_ref() { h.alive.store(false, Ordering::Release); }
            }
            format!("write: {}", e)
        })
        .map(|_| ())
}

#[tauri::command]
pub fn is_connected(state: State<AppState>) -> bool {
    state.serial.lock()
        .map(|g| g.as_ref().map(|h| h.is_alive()).unwrap_or(false))
        .unwrap_or(false)
}

#[tauri::command]
pub fn drain_inbox(state: State<AppState>) -> Vec<String> {
    let mut q = match state.inbox.lock() {
        Ok(q) => q,
        Err(_) => return Vec::new(),
    };
    q.drain(..).collect()
}


// --------------------- auto-detect data port ---------------------

#[tauri::command]
pub async fn auto_connect(
    state: State<'_, AppState>,
    app: AppHandle,
    serial: SpState<'_>,
) -> Result<String, String> {
    {
        let mut guard = state.serial.lock().map_err(|_| "lock poisoned")?;
        if let Some(existing) = guard.as_ref() {
            if existing.is_alive() { return Err("already connected".into()); }
            if let Ok(mut s) = existing.stop.lock() { *s = true; }
            *guard = None;
        }
    }

    let ports = available_ports(app.clone(), serial.clone(), None::<bool>)
        .map_err(|e| format!("list ports: {}", e))?;
    if ports.is_empty() { return Err("no USB serial devices found".into()); }

    let mut diag: Vec<String> = Vec::new();
    for port_name in ports.keys() {
        match probe_ping(port_name.clone(), app.clone(), serial.clone()) {
            Ok(_) => {
                std::thread::sleep(Duration::from_millis(500));
                connect(port_name.clone(), state.clone(), app.clone(), serial.clone()).await?;
                return Ok(port_name.clone());
            }
            Err(why) => diag.push(format!("{}: {}", port_name, why)),
        }
    }
    Err(format!("no firmware data port found. {}", diag.join("; ")))
}

fn probe_ping(port: String, app: AppHandle, serial: SpState<'_>) -> Result<(), String> {
    let canonical = open(
        app.clone(), serial.clone(), port,
        115_200,
        Some(DataBits::Eight), Some(FlowControl::None),
        Some(Parity::None), Some(StopBits::One),
        Some(1000u64),
    )
    .map_err(|e| format!("open failed: {}", e))?;

    let _ = write_data_terminal_ready(app.clone(), serial.clone(), canonical.clone(), true);
    let _ = write_request_to_send(app.clone(), serial.clone(), canonical.clone(), true);
    std::thread::sleep(Duration::from_millis(300));
    drain_until_quiet(app.clone(), serial.clone(), canonical.clone());

    let pid = std::process::id();
    for attempt in 0..3 {
        let probe_id = format!("probe-{}-{}", pid, attempt);
        let cmd = format!("\n\n{{\"type\":\"PING\",\"id\":\"{}\"}}\n", probe_id);
        write(app.clone(), serial.clone(), canonical.clone(), cmd.clone())
            .map_err(|e| format!("write: {}", e))?;

        match wait_for_ack(app.clone(), serial.clone(), canonical.clone(), &probe_id) {
            Ok(()) => { let _ = close(app, serial, canonical); return Ok(()); }
            Err(_) => continue,
        }
    }
    let _ = close(app, serial, canonical);
    Err("no response after 3 PINGs".into())
}

fn drain_until_quiet(app: AppHandle, serial: SpState<'_>, path: String) {
    let cap_deadline = std::time::Instant::now() + Duration::from_secs(5);
    let quiet = Duration::from_millis(300);
    let mut last_data_at = std::time::Instant::now();
    while std::time::Instant::now() < cap_deadline {
        match read(app.clone(), serial.clone(), path.clone(), Some(50u64), Some(1024usize)) {
            Ok(data) if !data.is_empty() => { last_data_at = std::time::Instant::now(); }
            _ => { if last_data_at.elapsed() >= quiet { return; } }
        }
    }
}

fn wait_for_ack(app: AppHandle, serial: SpState<'_>, path: String, probe_id: &str) -> Result<(), ()> {
    let deadline = std::time::Instant::now() + Duration::from_millis(2000);
    let mut all = Vec::<u8>::with_capacity(1024);
    let id_needle_a = format!("\"id\":\"{}\"", probe_id);
    let id_needle_b = format!("\"id\": \"{}\"", probe_id);
    let ack_needle_a = b"\"type\":\"ACK\"";
    let ack_needle_b = b"\"type\": \"ACK\"";
    while std::time::Instant::now() < deadline {
        match read(app.clone(), serial.clone(), path.clone(), Some(50u64), Some(256usize)) {
            Ok(data) if !data.is_empty() => {
                all.extend_from_slice(data.as_bytes());
                let has_id = all.windows(id_needle_a.len()).any(|w| w == id_needle_a.as_bytes())
                          || all.windows(id_needle_b.len()).any(|w| w == id_needle_b.as_bytes());
                let has_ack = all.windows(ack_needle_a.len()).any(|w| w == ack_needle_a)
                           || all.windows(ack_needle_b.len()).any(|w| w == ack_needle_b);
                if has_id && has_ack { return Ok(()); }
            }
            _ => continue,
        }
    }
    Err(())
}
