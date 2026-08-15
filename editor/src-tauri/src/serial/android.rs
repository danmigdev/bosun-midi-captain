// Android serial backend using a raw-USB JNI bridge (android_native.rs /
// Kotlin BosunSerialBridge) instead of tauri-plugin-serialplugin.
// Compiled only on Android targets.
//
// Architecture: a single I/O thread owns the port and serialises all
// reads and writes.  All USB access happens via UsbDeviceConnection.
// bulkTransfer(), Android's official synchronous transfer API with real
// SDK-level timeout enforcement - see android_native.rs and
// BosunSerialDevice.kt's doc comments for why this replaced the plugin's
// own transport (its per-call timeout was not reliably honoured by
// Android's USB host stack, a well-documented platform limitation).
#![cfg(target_os = "android")]

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde::Serialize;
use tauri::{AppHandle, Emitter, State};

use super::android_helpers::{
    call_with_timeout, is_transient_error, is_write_only_stall, marker_found, sort_ports_desc,
};
use super::android_native::{available_ports, close, open, read, write};

/// Consecutive successful writes with zero intervening successful reads
/// before treating the link as a write-only black hole. At StageView's
/// 2 s GET_CONTEXT poll cadence this is ~10 s of pure silence despite
/// asking - independent of the wall-clock `last_ok` staleness check
/// below, which a write-only link never trips (see is_write_only_stall).
const WRITE_ONLY_STALL_THRESHOLD: u32 = 5;

/// Upper bound on how long a single JNI open/close/read/write round-trip
/// may run before we treat it as hung and force a reconnect. Even with
/// the raw-USB transport's per-call bulkTransfer timeout, the surrounding
/// JNI dispatch itself (or a genuinely wedged USB endpoint) could still
/// stall - this stays as a second line of defense, matching the same
/// reasoning as when it guarded the old plugin-based transport (2026-08-15:
/// the app stopped responding to everything and never recovered, even
/// minutes later, because a call never returned and the surrounding stall
/// watchdog only runs between loop iterations it never got to). See
/// `call_with_timeout` in android_helpers.rs.
const IO_CALL_TIMEOUT: Duration = Duration::from_secs(8);

/// Per-call write timeout passed down to BosunSerialBridge.write(), which
/// hands it straight to UsbDeviceConnection.bulkTransfer(). Matches the
/// write_timeout the old plugin's open() was configured with.
const WRITE_TIMEOUT_MS: u64 = 1000;

/// How long the link may sit completely idle - no outbound command
/// queued, no unsolicited firmware push - before the I/O thread sends
/// its own lightweight PING to prove the link is still alive. Kept
/// comfortably below the 15 s stall threshold so a keepalive always has
/// time to land and reset `last_ok` before that timer would misfire.
/// Needed because StageView deliberately stopped polling GET_CONTEXT on
/// a timer (see its module comment) in favour of the firmware's
/// change-triggered `_push_context` - so on Stage, with nothing changing
/// on the pedal, minutes can pass with zero legitimate traffic, which
/// the wall-clock stall check otherwise can't tell apart from a dead
/// link (2026-08-15: confirmed live - a full reconnect cycle, including
/// a DTR-triggered USB re-enumeration that tears down the Kemper MIDI
/// bridge, was firing every ~15-30 s purely from Stage-view idleness).
const KEEPALIVE_IDLE: Duration = Duration::from_secs(6);


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
pub fn list_ports(_app: AppHandle) -> Result<Vec<PortInfo>, String> {
    let names = available_ports().map_err(|e| format!("list ports: {}", e))?;
    let out: Vec<PortInfo> = names
        .into_iter()
        .map(|name| PortInfo {
            name,
            kind: "serial".to_string(),
        })
        .collect();
    Ok(out)
}


// --------------------- connect / disconnect ---------------------

#[tauri::command]
pub async fn connect(
    port: String,
    state: State<'_, AppState>,
    app: AppHandle,
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

    let canonical = open(port.clone(), 115_200)
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
        eprintln!("[io] thread started");
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
                eprintln!("[io] stall recovery: closing and reopening");
                // Every call below is wrapped in call_with_timeout - even
                // with the raw-USB transport's own per-call bulkTransfer
                // timeout, a genuinely wedged USB endpoint or a JNI-level
                // hang can still block close()/available_ports()/open()
                // during recovery (2026-08-15: confirmed live on the old
                // plugin-based transport - the thread logged this exact
                // line and then went silent forever because an unguarded
                // call never returned; kept guarded here as a second line
                // of defense).
                let close_path = path.clone();
                let close_result = call_with_timeout(IO_CALL_TIMEOUT, move || close(close_path));
                if let Err(timeout_msg) = close_result {
                    eprintln!("[io] close hung: {}", timeout_msg);
                }
                std::thread::sleep(Duration::from_millis(1500));

                // The close() above dropped/re-asserted DTR, which triggers
                // the RP2040's own hardware reset - not just a
                // protocol-level reboot - so the device can vanish from the
                // OS's USB device list entirely for several seconds while
                // it re-enumerates (the sentinel PING/ACK phase below
                // already budgets up to 20 s for the firmware itself to
                // finish booting on top of that). Poll for it to reappear
                // instead of checking once and giving up (2026-08-15:
                // confirmed live on the raw-USB transport - checking only
                // once, 1.5 s after close(), missed the re-enumeration
                // window essentially every time, producing a tight
                // closing-and-reopening loop every ~3.5 s that never
                // actually recovered).
                let mut reopened = false;
                let reenumerate_deadline = std::time::Instant::now() + Duration::from_secs(12);
                while std::time::Instant::now() < reenumerate_deadline && !reopened {
                    if *stop_for_thread.lock().unwrap() { break; }
                    let ports_result = call_with_timeout(IO_CALL_TIMEOUT, available_ports);
                    match ports_result {
                        Ok(Ok(names)) => {
                            let mut names = names;
                            sort_ports_desc(&mut names);
                            for name in names {
                                let name_for_log = name.clone();
                                let open_name = name.clone();
                                let open_result = call_with_timeout(IO_CALL_TIMEOUT, move || {
                                    open(open_name, 115_200)
                                });
                                match open_result {
                                    Ok(Ok(p)) => { path = p; reopened = true; break; }
                                    Ok(Err(e)) => {
                                        eprintln!("[io] reopen {} failed: {}", name_for_log, e);
                                    }
                                    Err(timeout_msg) => {
                                        eprintln!("[io] reopen {} hung: {}", name_for_log, timeout_msg);
                                    }
                                }
                            }
                        }
                        Ok(Err(e)) => eprintln!("[io] re-enumerate failed: {}", e),
                        Err(timeout_msg) => eprintln!("[io] re-enumerate hung: {}", timeout_msg),
                    }
                    if !reopened {
                        std::thread::sleep(Duration::from_millis(500));
                    }
                }
                if !reopened {
                    eprintln!("[io] reopen failed on all ports");
                    std::thread::sleep(Duration::from_secs(2));
                    continue;
                }
            }
            first_cycle = false;

            // --- Sentinel PING/ACK phase ---
            // open() asserts DTR, which triggers a CP soft-reset.  Until
            // the firmware finishes rebooting (~9 s), writes may be NAKed
            // or simply unanswered.  Keep retrying the PING until the
            // firmware ACKs it -- only then start serving the outbox.
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
                // Same hang class as the recovery path above and the main
                // loop's write()/read() - wrap both calls so a wedged PING
                // or ACK read can't freeze the thread before it ever
                // reaches the instrumented main loop.
                let ping_line_for_call = ping_line.clone();
                let write_result = call_with_timeout(IO_CALL_TIMEOUT, move || {
                    write(ping_line_for_call, WRITE_TIMEOUT_MS)
                });
                if let Err(timeout_msg) = write_result {
                    eprintln!("[io] sentinel write hung: {}", timeout_msg);
                }
                let deadline = std::time::Instant::now() + Duration::from_secs(3);
                while std::time::Instant::now() < deadline && !synced {
                    let read_result = call_with_timeout(IO_CALL_TIMEOUT, || read(100, 4096));
                    match read_result {
                        Ok(Ok(data)) if !data.is_empty() => {
                            accum.extend_from_slice(data.as_bytes());
                            if marker_found(&accum, &sync_id) {
                                synced = true;
                            }
                            if accum.len() > 16384 {
                                accum.drain(..accum.len() - 8192);
                            }
                        }
                        Ok(Ok(_)) => break,
                        Ok(Err(_)) => break,
                        Err(timeout_msg) => {
                            eprintln!("[io] sentinel read hung: {}", timeout_msg);
                            break;
                        }
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
                // write NAKs, the host stops draining the TX, the firmware
                // stalls and the response is truncated.  Received data
                // within the last 500 ms = stream in flight.
                if last_ok.elapsed() > Duration::from_millis(500) {
                    let queued: Option<String> = outbox_for_thread.lock().unwrap().pop_front();
                    let (cmd, is_keepalive) = match queued {
                        Some(data) => (Some(data), false),
                        None if last_ok.elapsed() > KEEPALIVE_IDLE => {
                            (Some("{\"type\":\"PING\",\"id\":\"__keepalive\"}".to_string()), true)
                        }
                        None => (None, false),
                    };
                    if let Some(data) = cmd {
                        let mut line = data;
                        if !line.ends_with('\n') { line.push('\n'); }
                        let line_for_retry = line.trim_end().to_string();
                        let write_result = call_with_timeout(IO_CALL_TIMEOUT, move || {
                            write(line, WRITE_TIMEOUT_MS)
                        });
                        match write_result {
                            Ok(Ok(_)) => {
                                last_ok = std::time::Instant::now();
                                writes_since_last_read += 1;
                                if is_keepalive {
                                    eprintln!("[io] keepalive ping sent");
                                } else {
                                    eprintln!("[io] write ok, writes_since_last_read={}", writes_since_last_read);
                                }
                            }
                            Ok(Err(e)) => {
                                let msg = format!("{}", e);
                                if !is_transient_error(&msg) {
                                    let reason = format!("io write: {}", msg);
                                    *last_dead_reason.lock().unwrap() = Some(reason.clone());
                                    eprintln!("[io] fatal write error: {}", reason);
                                    break 'main;
                                }
                                if !is_keepalive {
                                    outbox_for_thread.lock().unwrap().push_front(line_for_retry);
                                }
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
                    let read_result = call_with_timeout(IO_CALL_TIMEOUT, || read(150, 4096));
                    let read_result = match read_result {
                        Ok(inner) => inner,
                        Err(timeout_msg) => {
                            // Same hang class as the write side above: a
                            // genuinely wedged USB endpoint or JNI-level
                            // stall can still block this call despite the
                            // raw transport's own bulkTransfer timeout.
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
            let _ = close(canonical);
            Err("firmware did not respond to PING within 20s".into())
        }
    }
}

#[tauri::command]
pub fn disconnect(state: State<AppState>, _app: AppHandle) -> Result<(), String> {
    let mut guard = match state.serial.lock() {
        Ok(g) => g,
        Err(poisoned) => poisoned.into_inner(),
    };
    if let Some(handle) = guard.as_ref() {
        if let Ok(mut s) = handle.stop.lock() { *s = true; }
        handle.alive.store(false, Ordering::Release);
        let _ = close(handle.path.clone());
    }
    // Drain any pending outbox so the next connect starts clean.
    if let Ok(mut q) = state.outbox.lock() { q.clear(); }
    *guard = None;
    Ok(())
}


// --------------------- send a JSON line ---------------------

#[tauri::command]
pub fn send_command(line: String, state: State<AppState>, _app: AppHandle) -> Result<(), String> {
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
) -> Result<String, String> {
    {
        let mut guard = state.serial.lock().map_err(|_| "lock poisoned")?;
        if let Some(existing) = guard.as_ref() {
            if existing.is_alive() { return Err("already connected".into()); }
            if let Ok(mut s) = existing.stop.lock() { *s = true; }
            *guard = None;
        }
    }

    let mut port_names = available_ports().map_err(|e| format!("list ports: {}", e))?;
    if port_names.is_empty() { return Err("no USB serial devices found".into()); }

    // Sort descending so the data port is tried first (matters if this
    // ever reports more than one synthetic port name).
    sort_ports_desc(&mut port_names);

    let mut diag: Vec<String> = Vec::new();
    for port_name in port_names {
        match connect(port_name.clone(), state.clone(), app.clone()).await {
            Ok(()) => return Ok(port_name.clone()),
            Err(why) => {
                let _ = disconnect(state.clone(), app.clone());
                diag.push(format!("{}: {}", port_name, why));
            }
        }
    }

    Err(format!("auto-connect failed: {}", diag.join("; ")))
}
