// Desktop serial backend using the serial2 crate.
// Compiled only on non-Android targets (Windows, macOS, Linux).
// This file is the original serial.rs, moved here unchanged to preserve
// the serial2 data-corruption fix (see docs/plans/20260615_084932_serialport_rs_read_corruption.md).
#![cfg(not(target_os = "android"))]

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;
use serial2::SerialPort;
use tauri::{AppHandle, Emitter, State};


// --------------------- shared app state ---------------------

#[derive(Default)]
pub struct AppState {
    pub serial: Mutex<Option<SerialHandle>>,
    pub inbox:  Arc<Mutex<VecDeque<String>>>,
}

pub struct SerialHandle {
    // serial2's SerialPort impls Read+Write for &SerialPort (the OS
    // handle is sync-safe), so we can share one handle between the
    // reader thread and send_command through an Arc without a Mutex.
    // This is the architectural fix that the serialport-rs build needed
    // a Mutex<Box<dyn ...>> for - serial2 gives it for free.
    port: Arc<SerialPort>,
    stop: Arc<Mutex<bool>>,
    // Set to false by the reader thread when it exits (read error or
    // stop request). Allows connect/auto_connect to detect a stale
    // handle whose reader has died and clean up before retrying,
    // instead of returning "already connected" forever.
    alive: Arc<AtomicBool>,
}

impl SerialHandle {
    fn is_alive(&self) -> bool {
        self.alive.load(Ordering::Acquire)
    }
}


// --------------------- list ---------------------

#[derive(Debug, Serialize, Clone)]
pub struct PortInfo {
    pub name: String,
    pub kind: String,
}

#[tauri::command]
pub fn list_ports() -> Vec<PortInfo> {
    SerialPort::available_ports()
        .map(|ports| {
            ports
                .into_iter()
                .map(|p| PortInfo {
                    name: p.to_string_lossy().into_owned(),
                    kind: "serial".to_string(),
                })
                .collect()
        })
        .unwrap_or_default()
}


// --------------------- connect / disconnect ---------------------

// async so Tauri runs it off the main (UI) thread - the blocking port open +
// sentinel PING/read would otherwise freeze the webview. The body has no
// .await, so no MutexGuard is ever held across a suspension point.
#[tauri::command]
pub async fn connect(
    port: String,
    state: State<'_, AppState>,
    app: AppHandle,
) -> Result<(), String> {
    let mut guard = state.serial.lock().map_err(|_| "lock poisoned")?;
    if let Some(existing) = guard.as_ref() {
        // Stale handle whose reader thread already exited (firmware
        // rebooted, port unplugged, etc.) - clean it up and proceed
        // instead of refusing with "already connected".
        if existing.is_alive() {
            return Err("already connected".into());
        }
        if let Ok(mut s) = existing.stop.lock() { *s = true; }
        *guard = None;
    }

    let mut port_opened = SerialPort::open(&port, 115_200)
        .map_err(|e| format!("open {}: {}", port, e))?;

    // Short read timeout keeps the reader thread responsive to stop
    // requests without burning CPU.
    port_opened
        .set_read_timeout(Duration::from_millis(50))
        .map_err(|e| format!("set read timeout: {}", e))?;
    port_opened
        .set_write_timeout(Duration::from_millis(500))
        .map_err(|e| format!("set write timeout: {}", e))?;

    // CircuitPython USB CDC needs DTR asserted before it considers the
    // host "open". Assert RTS too for safety on some adapters.
    let _ = port_opened.set_dtr(true);
    let _ = port_opened.set_rts(true);

    let port_shared = Arc::new(port_opened);

    // Sentinel sync: send a PING with a known id and synchronously read
    // until we see that id ACK'd. Everything that arrives before the
    // sentinel ACK is backlog from a prior editor session and gets
    // dropped. This is what makes Reconnect produce clean state instead
    // of replaying stale DEVICE_INFO/MANIFEST/etc. from the prior session.
    //
    // The drain runs in connect() synchronously (before spawning the
    // reader thread) so we don't return success until the firmware has
    // proven it's caught up. Hard cap 15s to bail out on a hung firmware
    // rather than block the UI forever.
    let sentinel_id = format!("__sync_{}_{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0)
    );
    let sentinel_cmd = format!("\n{{\"type\":\"PING\",\"id\":\"{}\"}}\n", sentinel_id);
    (&*port_shared)
        .write_all(sentinel_cmd.as_bytes())
        .map_err(|e| format!("sentinel write: {}", e))?;
    let sentinel_marker = format!("\"id\":\"{}\"", sentinel_id);
    let sentinel_marker_spaced = format!("\"id\": \"{}\"", sentinel_id);
    {
        let mut drain_buf = Vec::<u8>::with_capacity(8192);
        let mut drain_chunk = [0u8; 4096];
        let drain_deadline = std::time::Instant::now() + Duration::from_secs(10);
        let mut seen_sentinel = false;
        while std::time::Instant::now() < drain_deadline && !seen_sentinel {
            match (&*port_shared).read(&mut drain_chunk) {
                Ok(0) => continue,
                Ok(n) => {
                    drain_buf.extend_from_slice(&drain_chunk[..n]);
                    if drain_buf.windows(sentinel_marker.len())
                        .any(|w| w == sentinel_marker.as_bytes())
                        || drain_buf.windows(sentinel_marker_spaced.len())
                        .any(|w| w == sentinel_marker_spaced.as_bytes())
                    {
                        seen_sentinel = true;
                    }
                }
                Err(e) if e.kind() == std::io::ErrorKind::TimedOut => continue,
                Err(e) => return Err(format!("sentinel drain read: {}", e)),
            }
        }
        let tail_deadline = std::time::Instant::now() + Duration::from_millis(50);
        while std::time::Instant::now() < tail_deadline {
            let _ = (&*port_shared).read(&mut drain_chunk);
        }
    }

    let stop = Arc::new(Mutex::new(false));
    let alive = Arc::new(AtomicBool::new(true));
    let stop_for_thread = stop.clone();
    let alive_for_thread = alive.clone();
    let app_for_thread = app.clone();
    let inbox_for_thread = state.inbox.clone();
    let port_for_thread = port_shared.clone();

    thread::spawn(move || {
        let mut accum = Vec::<u8>::with_capacity(8192);
        let mut chunk = [0u8; 4096];

        loop {
            if *stop_for_thread.lock().unwrap() {
                break;
            }
            match (&*port_for_thread).read(&mut chunk) {
                Ok(0) => continue,
                Ok(n) => {
                    accum.extend_from_slice(&chunk[..n]);
                    let mut new_lines = 0;
                    while let Some(pos) = accum.iter().position(|b| *b == b'\n') {
                        let mut line: Vec<u8> = accum.drain(..=pos).collect();
                        while matches!(line.last(), Some(b'\n') | Some(b'\r')) {
                            line.pop();
                        }
                        if line.is_empty() {
                            continue;
                        }
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
                Err(e) if e.kind() == std::io::ErrorKind::TimedOut => continue,
                Err(_) => {
                    // Port read failed - firmware likely rebooted or USB
                    // re-enumerated. Mark dead, emit event, exit. The next
                    // connect/auto_connect call sees is_alive() == false
                    // and cleans up the stale handle automatically.
                    alive_for_thread.store(false, Ordering::Release);
                    let _ = app_for_thread.emit("firmware-disconnected", ());
                    break;
                }
            }
        }
        // Always mark dead on any exit path so a clean disconnect also
        // leaves the handle in a "not alive" state.
        alive_for_thread.store(false, Ordering::Release);
    });

    *guard = Some(SerialHandle { port: port_shared, stop, alive });
    Ok(())
}

#[tauri::command]
pub fn disconnect(state: State<AppState>) -> Result<(), String> {
    // Recover from poisoned lock so a disconnect always succeeds - the
    // editor should never end up unable to release the port because some
    // earlier panic poisoned the mutex.
    let mut guard = match state.serial.lock() {
        Ok(g) => g,
        Err(poisoned) => poisoned.into_inner(),
    };
    if let Some(handle) = guard.as_ref() {
        if let Ok(mut s) = handle.stop.lock() {
            *s = true;
        }
        handle.alive.store(false, Ordering::Release);
    }
    *guard = None;
    Ok(())
}


// --------------------- send a JSON line ---------------------

#[tauri::command]
pub fn send_command(line: String, state: State<AppState>) -> Result<(), String> {
    let port_arc = {
        let guard = state.serial.lock().map_err(|_| "lock poisoned")?;
        let handle = guard.as_ref().ok_or("not connected")?;
        if !handle.is_alive() {
            return Err("not connected".into());
        }
        handle.port.clone()
    };
    let write_result = (&*port_arc).write_all(line.as_bytes()).and_then(|_| {
        if !line.ends_with('\n') {
            (&*port_arc).write_all(b"\n")
        } else {
            Ok(())
        }
    });
    if let Err(e) = write_result {
        // Write failed - port has gone away. Mark handle dead so the next
        // is_connected / connect call sees the right state.
        if let Ok(guard) = state.serial.lock() {
            if let Some(h) = guard.as_ref() {
                h.alive.store(false, Ordering::Release);
            }
        }
        return Err(format!("write: {}", e));
    }
    Ok(())
}

#[tauri::command]
pub fn is_connected(state: State<AppState>) -> bool {
    state.serial.lock()
        .map(|g| g.as_ref().map(|h| h.is_alive()).unwrap_or(false))
        .unwrap_or(false)
}

/// Drain everything the reader has queued. Returns each complete line as a
/// String. The frontend calls this after each `firmware-data-ready` event.
#[tauri::command]
pub fn drain_inbox(state: State<AppState>) -> Vec<String> {
    let mut q = match state.inbox.lock() {
        Ok(q) => q,
        Err(_) => return Vec::new(),
    };
    q.drain(..).collect()
}


// --------------------- auto-detect data port ---------------------

// async (off the UI thread): probing every port with a PING and blocking-reading
// the reply can take seconds, especially when a stock pedal's console never
// drains. Running it on the main thread froze the editor at startup.
#[tauri::command]
pub async fn auto_connect(
    state: State<'_, AppState>,
    app: AppHandle,
) -> Result<String, String> {
    {
        let mut guard = state.serial.lock().map_err(|_| "lock poisoned")?;
        if let Some(existing) = guard.as_ref() {
            // Same stale-handle recovery as connect(): if the reader is
            // dead, clear and retry; otherwise refuse.
            if existing.is_alive() {
                return Err("already connected".into());
            }
            if let Ok(mut s) = existing.stop.lock() { *s = true; }
            *guard = None;
        }
    }

    let ports = SerialPort::available_ports().map_err(|e| format!("list ports: {}", e))?;
    let mut diag: Vec<String> = Vec::new();

    for p in ports {
        let name = p.to_string_lossy().into_owned();
        let result = probe_ping(&name);
        eprintln!("[auto_connect] {} -> {:?}", name, result);
        match result {
            Ok(_) => {
                std::thread::sleep(Duration::from_millis(500));
                connect(name.clone(), state.clone(), app.clone()).await?;
                return Ok(name);
            }
            Err(why) => diag.push(format!("{}: {}", name, why)),
        }
    }
    Err(format!("no firmware data port found. {}", diag.join("; ")))
}

/// Ask the pedal to reboot into the RP2040 UF2 bootloader (RPI-RP2) so the user
/// doesn't have to do the physical hold-footswitch-while-replugging dance.
///
/// Two mechanisms per port, in order of reliability:
///  1. The 1200-baud touch - opening the CDC at 1200 baud and closing it (DTR
///     drop) makes CircuitPython reset into the UF2 bootloader. This is a USB-
///     stack feature, so it works even when the running firmware never drains
///     its console (the factory PaintAudio build, for one, leaves console
///     writes to time out - the REPL approach alone fails there).
///  2. A REPL Ctrl-C + `on_next_reset(UF2)` write, as a fallback for any build
///     that honors Ctrl-C but ignores the touch.
///
/// Broadcast to every open-able port: console vs data CDC doesn't matter, and a
/// non-CircuitPython device just sees a harmless 1200-baud open. The caller
/// still shows the manual fallback if no RPI-RP2 appears.
#[tauri::command]
pub fn reboot_to_bootloader(state: State<AppState>) -> Result<String, String> {
    // Release any handle we hold so our own reader thread isn't sitting on
    // the console port when we try to open it here.
    if let Ok(mut guard) = state.serial.lock() {
        if let Some(h) = guard.as_ref() {
            if let Ok(mut s) = h.stop.lock() { *s = true; }
            h.alive.store(false, Ordering::Release);
        }
        *guard = None;
    }
    std::thread::sleep(Duration::from_millis(200));

    let ports = SerialPort::available_ports().map_err(|e| format!("list ports: {}", e))?;
    if ports.is_empty() {
        return Err("no serial ports found".into());
    }
    let ctrl_c: &[u8] = b"\x03";
    let cmds: &[u8] = b"\r\nimport microcontroller\r\nmicrocontroller.on_next_reset(getattr(microcontroller.RunMode,'UF2',microcontroller.RunMode.BOOTLOADER))\r\nmicrocontroller.reset()\r\n";
    let mut hit: Vec<String> = Vec::new();
    for p in ports {
        let name = p.to_string_lossy().into_owned();

        // 1) 1200-baud touch. Opening succeeds; the act of closing with DTR
        //    de-asserted is what triggers the reset, so we count this port as
        //    handled even though no bytes are written. The board re-enumerates
        //    as RPI-RP2 (mass storage) and this COM path disappears.
        let mut touched = false;
        if let Ok(port) = SerialPort::open(&p, 1200) {
            let _ = port.set_dtr(false);
            let _ = port.set_rts(false);
            std::thread::sleep(Duration::from_millis(250));
            drop(port); // close -> DTR drop -> CircuitPython resets to bootloader
            touched = true;
            hit.push(name.clone());
            std::thread::sleep(Duration::from_millis(200));
        }

        // 2) REPL Ctrl-C fallback. Skipped silently if the touch already reset
        //    the port away (open fails); useful for builds that ignore the touch.
        if let Ok(mut port) = SerialPort::open(&p, 115_200) {
            let _ = port.set_write_timeout(Duration::from_millis(500));
            let _ = port.set_dtr(true);
            let _ = port.set_rts(true);
            if (&port).write_all(ctrl_c).is_ok() {
                let _ = (&port).flush();
                std::thread::sleep(Duration::from_millis(300));
                let _ = (&port).write_all(cmds);
                let _ = (&port).flush();
                if !touched { hit.push(name); }
            }
        }
    }
    if hit.is_empty() {
        Err("could not reach any serial port".into())
    } else {
        Ok(hit.join(", "))
    }
}

fn probe_ping(port: &str) -> Result<(), String> {
    // Windows USB CDC drivers sometimes hold a port for a few seconds
    // after the firmware reboots / re-enumerates (also if a previous
    // handle was just dropped). Retry briefly on "Access is denied"
    // before giving up - this matches the "wait 5 seconds and try
    // again" workaround that used to be manual.
    let mut last_err = String::new();
    let mut handle: Option<SerialPort> = None;
    for attempt in 0..4 {
        match SerialPort::open(port, 115_200) {
            Ok(h) => { handle = Some(h); break; }
            Err(e) => {
                let msg = format!("{}", e);
                last_err = msg.clone();
                // Only retry on transient access-denied / device-busy
                // errors. Anything else (port doesn't exist, etc.) is
                // probably persistent - bail immediately.
                let transient = msg.contains("Access is denied")
                             || msg.contains("Device or resource busy")
                             || msg.contains("os error 5")
                             || msg.contains("os error 32");
                if !transient { return Err(format!("open failed: {}", msg)); }
                std::thread::sleep(Duration::from_millis(400 + 200 * attempt as u64));
            }
        }
    }
    let mut handle = match handle {
        Some(h) => h,
        None => return Err(format!("open failed: {}", last_err)),
    };
    handle
        .set_read_timeout(Duration::from_millis(150))
        .map_err(|e| format!("set timeout: {}", e))?;
    handle
        .set_write_timeout(Duration::from_millis(500))
        .map_err(|e| format!("set wtimeout: {}", e))?;
    let _ = handle.set_dtr(true);
    let _ = handle.set_rts(true);

    std::thread::sleep(Duration::from_millis(300));

    drain_until_quiet(&handle, Duration::from_millis(300), Duration::from_millis(5000));

    let pid = std::process::id();
    let mut last_received = Vec::<u8>::new();
    for attempt in 0..3 {
        let probe_id = format!("probe-{}-{}", pid, attempt);
        let cmd = format!("\n\n{{\"type\":\"PING\",\"id\":\"{}\"}}\n", probe_id);
        (&handle)
            .write_all(cmd.as_bytes())
            .map_err(|e| format!("write: {}", e))?;

        match wait_for_ack(&handle, &probe_id, Duration::from_millis(1500)) {
            Ok(()) => return Ok(()),
            Err(bytes) => {
                last_received = bytes;
            }
        }
    }

    if last_received.is_empty() {
        Err("no response after 3 PINGs".into())
    } else {
        let snippet: String = last_received.iter().take(200).map(|b| {
            if *b == b'\n' { "\\n".to_string() }
            else if (32..127).contains(b) { (*b as char).to_string() }
            else { format!("\\x{:02x}", b) }
        }).collect();
        Err(format!("3 PINGs sent, no matching ACK. last received {} bytes: [{}]",
            last_received.len(), snippet))
    }
}

fn drain_until_quiet(handle: &SerialPort, quiet: Duration, hard_cap: Duration) {
    let cap_deadline = Instant::now() + hard_cap;
    let mut last_data_at = Instant::now();
    let mut chunk = [0u8; 1024];
    while Instant::now() < cap_deadline {
        match handle.read(&mut chunk) {
            Ok(0) | Err(_) => {
                if last_data_at.elapsed() >= quiet {
                    return;
                }
            }
            Ok(_) => {
                last_data_at = Instant::now();
            }
        }
    }
}

fn wait_for_ack(
    handle: &SerialPort,
    probe_id: &str,
    timeout: Duration,
) -> Result<(), Vec<u8>> {
    // Match the SPECIFIC ACK from the firmware:
    //   {"type":"ACK","id":"<probe_id>","fw":"..."}
    //
    // The previous version matched the substring "type":" which also
    // matches the REPL's echo of our PING command ("type":"PING") on
    // CircuitPython's primary CDC console - that made auto_connect
    // happily attach to the REPL port and then sit on data the
    // protocol layer can't parse. We now require BOTH the probe_id
    // (so it's our response, not someone else's) and "ACK" / "PONG"
    // (so it's a real protocol response, not the REPL echoing us).
    let deadline = Instant::now() + timeout;
    let mut all = Vec::<u8>::with_capacity(1024);
    let id_needle_a = format!("\"id\":\"{}\"", probe_id);
    let id_needle_b = format!("\"id\": \"{}\"", probe_id);
    let ack_needle_a = b"\"type\":\"ACK\"";
    let ack_needle_b = b"\"type\": \"ACK\"";
    while Instant::now() < deadline {
        let mut chunk = [0u8; 256];
        match handle.read(&mut chunk) {
            Ok(0) => {}
            Ok(n) => {
                all.extend_from_slice(&chunk[..n]);
                let has_id = all.windows(id_needle_a.len())
                                 .any(|w| w == id_needle_a.as_bytes())
                          || all.windows(id_needle_b.len())
                                 .any(|w| w == id_needle_b.as_bytes());
                let has_ack = all.windows(ack_needle_a.len())
                                  .any(|w| w == ack_needle_a)
                           || all.windows(ack_needle_b.len())
                                  .any(|w| w == ack_needle_b);
                if has_id && has_ack {
                    return Ok(());
                }
            }
            Err(_) => {}
        }
    }
    Err(all)
}
