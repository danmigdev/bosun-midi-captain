// Android serial backend using tauri-plugin-serialplugin v3.
// Compiled only on Android targets.
//
// Architecture: a single I/O thread owns the port and serialises all
// reads and writes.  The plugin's internal hub thread holds the port
// mutex while polling for incoming data; because the Android USB stack
// may not honour the 10 ms read timeout, that lock can be held
// indefinitely.  By routing every write through the same thread we
// avoid lock contention entirely: the thread writes any queued commands
// immediately after each read cycle, while the hub's lock is released.
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
};
use tauri_plugin_serialplugin::state::{
    DataBits, FlowControl, Parity, StopBits,
};

use super::android_helpers::{
    call_with_timeout, is_transient_error, is_write_only_stall, marker_found, sort_ports_desc,
};

/// Consecutive successful writes with zero intervening successful reads
/// before treating the link as a write-only black hole. At StageView's
/// 2 s GET_CONTEXT poll cadence this is ~10 s of pure silence despite
/// asking - independent of the wall-clock `last_ok` staleness check
/// below, which a write-only link never trips (see is_write_only_stall).
const WRITE_ONLY_STALL_THRESHOLD: u32 = 5;

/// Upper bound on how long a single plugin write()/read() call may run
/// before we treat it as hung and force a reconnect. The module already
/// documents the plugin's own worst-case NAK-retry window as "up to 5 s
/// (plugin URB watchdog)" during a legitimate large-response stream, so
/// this sits comfortably above that to avoid preempting a real (if slow)
/// in-plugin recovery - while still bounding the outage from a genuine
/// hang, which previously froze the I/O thread forever (2026-08-15: the
/// app stopped responding to everything and never recovered, even
/// minutes later, because the write() call itself never returned and the
/// surrounding stall watchdog only runs between loop iterations it never
/// got to). See `call_with_timeout` in android_helpers.rs.
const IO_CALL_TIMEOUT: Duration = Duration::from_secs(8);


// --------------------- shared app state ---------------------

#[derive(Default)]
pub struct AppState {
    pub serial: Mutex<Option<SerialHandle>>,
    pub inbox:  Arc<Mutex<VecDeque<String>>>,
    /// Commands queued for the I/O thread to send.
    pub outbox: Arc<Mutex<VecDeque<String>>>,
    pub last_dead_reason: Arc<Mutex<Option<String>>>,
}

pub struct SerialHandle {
    pub path: String,
    pub stop: Arc<Mutex<bool>>,
    pub alive: Arc<AtomicBool>,
}

impl SerialHandle {
    pub fn is_alive(&self) -> bool {
        self.alive.load(Ordering::Acquire)
    }
    pub fn stop_thread(&self) {
        if let Ok(mut s) = self.stop.lock() { *s = true; }
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

type SpState<'a> = State<'a, tauri_plugin_serialplugin::api::serial::SerialPort<tauri::Wry>>;

#[tauri::command]
pub async fn connect(
    port: String,
    state: State<'_, AppState>,
    app: AppHandle,
    serial: SpState<'_>,
) -> Result<(), String> {
    let last_dead_reason = state.last_dead_reason.clone();

    let mut guard = state.serial.lock().map_err(|_| "lock poisoned")?;
    if let Some(existing) = guard.as_ref() {
        if existing.is_alive() {
            return Err("already connected".into());
        }
        if let Ok(mut s) = existing.stop.lock() { *s = true; }
        *guard = None;
    }

    let canonical = open(
        app.clone(), serial.clone(), port.clone(),
        115_200,
        Some(DataBits::Eight), Some(FlowControl::None),
        Some(Parity::None), Some(StopBits::One),
        Some(1000u64),
    )
    .map_err(|e| format!("open: {}", e))?;

    // Single I/O thread: reads incoming data, writes queued commands.
    // A channel lets connect() wait for the sentinel sync to finish
    // before returning to the frontend, so refetchAll doesn't race
    // the firmware's reboot.  The thread also self-heals: if no write
    // succeeds and no data arrives for STALL_RECOVERY_MS, it closes
    // and reopens the port and re-runs the sentinel sync.
    let path = canonical.clone();
    let stop = Arc::new(Mutex::new(false));
    let alive = Arc::new(AtomicBool::new(true));
    let stop_for_thread = stop.clone();
    let alive_for_thread = alive.clone();
    let app_for_thread = app.clone();
    let inbox_for_thread = state.inbox.clone();
    let outbox_for_thread = state.outbox.clone();
    let sync_sent = Arc::new(Mutex::new(false));
    let sync_sent_for_thread = sync_sent.clone();
    let (sync_tx, sync_rx) = std::sync::mpsc::channel::<bool>();

    thread::spawn(move || {
        let mut accum = Vec::<u8>::with_capacity(8192);
        let mut path = path.clone();
        let mut first_cycle = true;
        // Tracks a stall-recovery episode in progress. The recovery itself
        // never touches `alive` (dropping it would race the frontend's
        // explicit disconnect() into killing this very thread mid-heal -
        // see the comment on `alive_for_thread.store` below), so nothing
        // previously told the frontend a recovery was even happening. That
        // silence is why the Kemper<->Captain MIDI bridge and the pedal's
        // switch/effect LEDs went stale until a manual reconnect: the
        // stall-triggered reopen() re-asserts DTR, which reboots the RP2040
        // and re-enumerates its whole composite USB device (see the
        // "CRITICAL: re-enumerate" note below) - including the USB-MIDI
        // interface BosunMidiBridge relays through. Android's MidiManager
        // reacts to that vanish/reappear by tearing the Kotlin bridge down
        // (its DeviceCallback.onDeviceRemoved), but nothing ever brought it
        // back up because the frontend's `connected` flag never blinked.
        // Emitting firmware-reconnecting/-reconnected around the episode
        // (2026-08-14) lets App.svelte's existing `_bridgeAutoDone` reset
        // fire again on the "reconnected" transition, and lets Stage view's
        // stale CONTEXT/latched state get refreshed the same way a manual
        // reconnect already does.
        let mut recovering = false;

        // Outer loop: one iteration per connection lifecycle.  A stall
        // (no successful write and no inbound data for 15 s) closes and
        // reopens the port, which re-asserts DTR, resets the CP and
        // re-runs the sentinel sync -- self-healing without user action.
        'connection: loop {
            if *stop_for_thread.lock().unwrap() { break; }

            if !first_cycle {
                if !recovering {
                    recovering = true;
                    let _ = app_for_thread.emit("firmware-reconnecting", ());
                }
                // Recover: drop the old port entirely and reopen.
                // CRITICAL: re-enumerate - the device path changes after
                // a CP reset re-enumerates the USB bus.  Reusing the old
                // path makes the plugin's openDeviceFd throw an uncaught
                // IOException on a Kotlin worker thread, which kills the
                // whole app (2026-08-14 disconnect crash).
                eprintln!("[io] stall recovery: closing and reopening");
                let _ = close(
                    app_for_thread.clone(),
                    app_for_thread.state::<tauri_plugin_serialplugin::api::serial::SerialPort<tauri::Wry>>(),
                    path.clone(),
                );
                std::thread::sleep(Duration::from_millis(1500));

                let mut reopened = false;
                match available_ports(
                    app_for_thread.clone(),
                    app_for_thread.state::<tauri_plugin_serialplugin::api::serial::SerialPort<tauri::Wry>>(),
                    None::<bool>,
                ) {
                    Ok(ports) => {
                        let mut names: Vec<String> = ports.keys().cloned().collect();
                        sort_ports_desc(&mut names);
                        for name in names {
                            let name_for_log = name.clone();
                            match open(
                                app_for_thread.clone(),
                                app_for_thread.state::<tauri_plugin_serialplugin::api::serial::SerialPort<tauri::Wry>>(),
                                name,
                                115_200,
                                Some(DataBits::Eight), Some(FlowControl::None),
                                Some(Parity::None), Some(StopBits::One),
                                Some(1000u64),
                            ) {
                                Ok(p) => { path = p; reopened = true; break; }
                                Err(e) => {
                                    eprintln!("[io] reopen {} failed: {}", name_for_log, e);
                                }
                            }
                        }
                    }
                    Err(e) => eprintln!("[io] re-enumerate failed: {}", e),
                }
                if !reopened {
                    eprintln!("[io] reopen failed on all ports");
                    std::thread::sleep(Duration::from_secs(2));
                    continue;
                }
            }
            first_cycle = false;

            // --- Sentinel PING/ACK phase ---
            // The plugin asserts DTR during open(), which triggers a CP
            // soft-reset.  Until the firmware finishes rebooting (~9 s),
            // every bulk-out write is NAKed by the RP2040 and the plugin's
            // 5 s watchdog cancels the URB.  Keep retrying the PING until
            // the firmware ACKs it -- only then start serving the outbox.
            let sync_id = format!("__sync_{}_{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_millis()).unwrap_or(0));
            let ping_line = format!("{{\"type\":\"PING\",\"id\":\"{}\"}}\n", sync_id);
            let sentinel_deadline = std::time::Instant::now() + Duration::from_secs(20);
            let mut synced = false;
            while std::time::Instant::now() < sentinel_deadline && !synced {
                if *stop_for_thread.lock().unwrap() { break; }
                let _ = write(
                    app_for_thread.clone(),
                    app_for_thread.state::<tauri_plugin_serialplugin::api::serial::SerialPort<tauri::Wry>>(),
                    path.clone(),
                    ping_line.clone(),
                );
                let mut deadline = std::time::Instant::now() + Duration::from_secs(3);
                while std::time::Instant::now() < deadline && !synced {
                    match read(
                        app_for_thread.clone(),
                        app_for_thread.state::<tauri_plugin_serialplugin::api::serial::SerialPort<tauri::Wry>>(),
                        path.clone(),
                        Some(100u64),
                        Some(4096usize),
                    ) {
                        Ok(data) if !data.is_empty() => {
                            accum.extend_from_slice(data.as_bytes());
                            if marker_found(&accum, &sync_id) {
                                synced = true;
                            }
                            if accum.len() > 16384 {
                                accum.drain(..accum.len() - 8192);
                            }
                        }
                        Ok(_) => break,
                        Err(_) => break,
                    }
                }
                if !synced {
                    std::thread::sleep(Duration::from_millis(200));
                }
            }
            if synced {
                let mut pushed = 0;
                while let Some(pos) = accum.iter().position(|b| *b == b'\n') {
                    let mut line: Vec<u8> = accum.drain(..=pos).collect();
                    while matches!(line.last(), Some(b'\n') | Some(b'\r')) { line.pop(); }
                    if line.is_empty() { continue; }
                    if let Ok(s) = std::str::from_utf8(&line) {
                        if let Ok(mut q) = inbox_for_thread.lock() {
                            q.push_back(s.to_string());
                            pushed += 1;
                        }
                    }
                }
                if pushed > 0 {
                    let _ = app_for_thread.emit("firmware-data-ready", ());
                }
            }
            accum.clear();

            // Tell connect() whether the sync succeeded so it can decide
            // to return or fail.  Only the first cycle's result matters.
            if !*sync_sent_for_thread.lock().unwrap() {
                let _ = sync_tx.send(synced);
                *sync_sent_for_thread.lock().unwrap() = true;
            }

            if !synced {
                // Firmware never answered -- give up this cycle and
                // reconnect after a pause.
                eprintln!("[io] sentinel failed, reconnecting");
                std::thread::sleep(Duration::from_secs(2));
                continue;
            }

            if recovering {
                recovering = false;
                let _ = app_for_thread.emit("firmware-reconnected", ());
            }

            // --- Main I/O loop with stall detection ---
            let mut last_ok = std::time::Instant::now();
            // Tracks a write-only black hole: writes keep succeeding (each
            // one resets `last_ok` below, same as a read) while nothing
            // ever comes back, so the wall-clock check above never fires
            // even though the link has been silent for minutes (2026-08-14:
            // confirmed live via logcat - GET_CONTEXT sent every 2 s for
            // 5+ minutes, zero read activity, self-heal never triggered).
            let mut writes_since_last_read: u32 = 0;
            'main: loop {
                if *stop_for_thread.lock().unwrap() { break 'connection; }

                // Stall check: no write success and no inbound data for
                // 15 s means the USB link is wedged; force a reconnect.
                if last_ok.elapsed() > Duration::from_secs(15) {
                    eprintln!("[io] stall detected");
                    break 'main;
                }
                if is_write_only_stall(writes_since_last_read, WRITE_ONLY_STALL_THRESHOLD) {
                    eprintln!("[io] write-only stall detected ({} unanswered writes)", writes_since_last_read);
                    break 'main;
                }

                // 1) Write ONE queued command per cycle (see rationale
                // in the module header).  SKIP the write entirely while a
                // multi-KB response is streaming in: the firmware's main
                // loop is blocked sending it, its RX buffer fills, the
                // write NAKs for up to 5 s (plugin URB watchdog), the hub
                // loses the port mutex, the host stops draining the TX,
                // the firmware stalls and the response is truncated.
                // Received data within the last 500 ms = stream in flight.
                if last_ok.elapsed() > Duration::from_millis(500) {
                    let cmd: Option<String> = outbox_for_thread.lock().unwrap().pop_front();
                    if let Some(data) = cmd {
                        let mut line = data;
                        if !line.ends_with('\n') { line.push('\n'); }
                        let line_for_retry = line.trim_end().to_string();
                        let write_app = app_for_thread.clone();
                        let write_path = path.clone();
                        let write_result = call_with_timeout(IO_CALL_TIMEOUT, move || {
                            let sp = write_app.state::<tauri_plugin_serialplugin::api::serial::SerialPort<tauri::Wry>>();
                            write(write_app.clone(), sp, write_path, line)
                        });
                        match write_result {
                            Ok(Ok(_)) => {
                                last_ok = std::time::Instant::now();
                                writes_since_last_read += 1;
                                eprintln!("[io] write ok, writes_since_last_read={}", writes_since_last_read);
                            }
                            Ok(Err(e)) => {
                                let msg = format!("{}", e);
                                if !is_transient_error(&msg) {
                                    let reason = format!("io write: {}", msg);
                                    *last_dead_reason.lock().unwrap() = Some(reason.clone());
                                    eprintln!("[io] fatal write error: {}", reason);
                                    break 'main;
                                }
                                outbox_for_thread.lock().unwrap().push_front(line_for_retry);
                            }
                            Err(timeout_msg) => {
                                // The write() call itself never returned - the
                                // hang this whole wrapper exists to catch (see
                                // IO_CALL_TIMEOUT). The spawned thread is
                                // abandoned; we can't wait for it. Treat this
                                // exactly like a fatal write error: force a
                                // reconnect rather than freezing forever.
                                let reason = format!("io write hung: {}", timeout_msg);
                                *last_dead_reason.lock().unwrap() = Some(reason.clone());
                                eprintln!("[io] fatal write error: {}", reason);
                                break 'main;
                            }
                        }
                    }
                }

                // 2) Read any incoming data.
                let mut new_lines = 0;
                let mut chunk_seq: u64 = 0;
                loop {
                    let read_app = app_for_thread.clone();
                    let read_path = path.clone();
                    let read_result = call_with_timeout(IO_CALL_TIMEOUT, move || {
                        let sp = read_app.state::<tauri_plugin_serialplugin::api::serial::SerialPort<tauri::Wry>>();
                        read(read_app.clone(), sp, read_path, Some(150u64), Some(4096usize))
                    });
                    let read_result = match read_result {
                        Ok(inner) => inner,
                        Err(timeout_msg) => {
                            // Same hang class as the write side above: the
                            // plugin's own 150 ms timeout can go unhonoured
                            // by the Android USB stack (see module doc
                            // comment), wedging this read() call forever.
                            let reason = format!("io read hung: {}", timeout_msg);
                            *last_dead_reason.lock().unwrap() = Some(reason.clone());
                            eprintln!("[io] fatal read error: {}", reason);
                            break 'main;
                        }
                    };
                    match read_result {
                        Ok(data) if !data.is_empty() => {
                            last_ok = std::time::Instant::now();
                            writes_since_last_read = 0;
                            chunk_seq += 1;
                            eprintln!("[chunk] #{} {} bytes", chunk_seq, data.len());
                            accum.extend_from_slice(data.as_bytes());
                            while let Some(pos) = accum.iter().position(|b| *b == b'\n') {
                                let mut line: Vec<u8> = accum.drain(..=pos).collect();
                                while matches!(line.last(), Some(b'\n') | Some(b'\r')) { line.pop(); }
                                if line.is_empty() { continue; }
                                if let Ok(s) = std::str::from_utf8(&line) {
                                    // Compact type-only log for diagnosing
                                    // which responses arrive.
                                    match serde_json::from_str::<serde_json::Value>(&s) {
                                        Ok(v) => {
                                            let ty = v.get("type").and_then(|t| t.as_str()).unwrap_or("?");
                                            eprintln!("[io] {} ({} bytes) accum={}", ty, s.len(), accum.len());
                                            if ty == "CONTEXT" {
                                                let rig = v.get("context").and_then(|c| c.get("kemper_rig_name"));
                                                let blk = v.get("context").and_then(|c| c.get("kemper_block_Delay"));
                                                eprintln!("[io] ctx rig_name={:?} delay={:?}", rig, blk);
                                            }
                                        }
                                        Err(e) => {
                                            eprintln!("[io] BAD JSON ({} bytes): {} | tail: {}", s.len(), e, &s[s.len().saturating_sub(40)..]);
                                        }
                                    }
                                    if let Ok(mut q) = inbox_for_thread.lock() {
                                        q.push_back(s.to_string());
                                        new_lines += 1;
                                    }
                                }
                            }
                        }
                        Ok(_) => break,
                        Err(e) => {
                            let msg = format!("{}", e);
                            if is_transient_error(&msg) {
                                break;
                            }
                            let reason = format!("io read: {}", msg);
                            *last_dead_reason.lock().unwrap() = Some(reason.clone());
                            eprintln!("[io] fatal read error: {}", reason);
                            break 'main;
                        }
                    }
                }

                if new_lines > 0 {
                    let _ = app_for_thread.emit("firmware-data-ready", ());
                }

                std::thread::sleep(Duration::from_millis(5));
            }
        }
        alive_for_thread.store(false, Ordering::Release);
    });

    // Wait for the sentinel sync result before returning.  The I/O
    // thread retries the PING for up to 20 s while the firmware
    // reboots (DTR-triggered CP reset); refetchAll must not start
    // until the firmware has ACKed.
    match sync_rx.recv_timeout(Duration::from_secs(25)) {
        Ok(true) => {
            *guard = Some(SerialHandle { path: canonical, stop, alive });
            Ok(())
        }
        Ok(false) | Err(_) => {
            let _ = close(app, serial, canonical);
            Err("firmware did not respond to PING within 20s".into())
        }
    }
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
    // Drain any pending outbox so the next connect starts clean.
    if let Ok(mut q) = state.outbox.lock() { q.clear(); }
    *guard = None;
    Ok(())
}


// --------------------- send a JSON line ---------------------

#[tauri::command]
pub fn send_command(line: String, state: State<AppState>, _app: AppHandle, _serial: SpState<'_>) -> Result<(), String> {
    // Check that we're connected.
    {
        let guard = state.serial.lock().map_err(|_| "lock poisoned")?;
        let handle = guard.as_ref().ok_or_else(|| {
            let reason = state.last_dead_reason.lock().unwrap().clone()
                .unwrap_or_else(|| "disconnected".into());
            format!("link dead ({})", reason)
        })?;
        if !handle.is_alive() {
            let reason = state.last_dead_reason.lock().unwrap().clone()
                .unwrap_or_else(|| "unknown".into());
            return Err(format!("reader died: {}", reason));
        }
    }
    // Push to the I/O thread's outbox.  The thread will write it in its
    // next cycle.  If the write fails transiently, the thread re-queues
    // and retries; if it fails fatally, the thread marks us dead and
    // we'll report that on the next command.
    if let Ok(mut q) = state.outbox.lock() {
        q.push_back(line.clone());
        let preview: String = line.chars().take(40).collect();
        eprintln!("[send] {} bytes: {}", line.len(), preview);
    }
    Ok(())
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
    let lines: Vec<String> = q.drain(..).collect();
    if !lines.is_empty() {
        eprintln!("[drain] frontend pulled {} line(s), biggest {} bytes",
            lines.len(),
            lines.iter().map(|l| l.len()).max().unwrap_or(0));
    }
    lines
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

    // Port names use #N format (/dev/bus/usb/001/002#0, ...#1).
    // Port 0 = console CDC (DTR triggers CP reset), port 1+ = data CDC.
    // Sort descending so the data port is tried first.
    let mut port_names: Vec<String> = ports.keys().cloned().collect();
    sort_ports_desc(&mut port_names);

    let mut diag: Vec<String> = Vec::new();
    for port_name in port_names {
        match connect(port_name.clone(), state.clone(), app.clone(), serial.clone()).await {
            Ok(()) => return Ok(port_name.clone()),
            Err(why) => {
                let _ = disconnect(state.clone(), app.clone(), serial.clone());
                diag.push(format!("{}: {}", port_name, why));
            }
        }
    }

    Err(format!("auto-connect failed: {}", diag.join("; ")))
}

