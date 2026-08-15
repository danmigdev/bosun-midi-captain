// TCP-based serial backend for debugging without the physical pedal.
// Uses the same AppState/inbox pattern as the serial module.
// The serial commands (send_command, drain_inbox, etc.) check for an
// active TCP stream and use it instead of the serial port.
//
// After tcp_connect, a persistent reader thread reads from the socket,
// accumulates partial lines, pushes complete lines to the shared inbox,
// and emits firmware-data-ready per batch -- exactly like the serial
// backends do. tcp_send uses a cloned writer handle so the reader
// thread can own the read half without contention.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, LazyLock, Mutex};
use std::thread;
use std::time::Duration;

use tauri::{AppHandle, Emitter, State};

use crate::serial::{AppState, PortInfo, SerialHandle};

/// Writer half of the TCP connection (cloned from the connect-time stream).
/// The reader thread owns the original stream exclusively.
pub static TCP_WRITER: LazyLock<Mutex<Option<TcpStream>>> =
    LazyLock::new(|| Mutex::new(None));

/// True while a TCP connection is active.
pub fn tcp_active() -> bool {
    TCP_WRITER.lock().map(|g| g.is_some()).unwrap_or(false)
}

pub fn tcp_send(line: &str) -> Result<(), String> {
    let mut guard = TCP_WRITER.lock().map_err(|_| "lock poisoned")?;
    let Some(ref mut stream) = *guard else {
        return Err("not connected".into());
    };
    let mut data = line.to_string();
    if !data.ends_with('\n') {
        data.push('\n');
    }
    stream.write_all(data.as_bytes()).map_err(|e| format!("tcp write: {}", e))?;
    stream.flush().map_err(|e| format!("tcp flush: {}", e))?;
    Ok(())
}

pub fn tcp_close() {
    if let Ok(mut g) = TCP_WRITER.lock() {
        *g = None;
    }
}

#[tauri::command]
pub fn tcp_list_ports() -> Vec<PortInfo> {
    let mut ports = vec![
        PortInfo {
            name: "tcp://10.0.2.2:9876".into(),
            kind: "tcp (emulator host)".into(),
        },
        PortInfo {
            name: "tcp://127.0.0.1:9876".into(),
            kind: "tcp (localhost)".into(),
        },
    ];
    // Add the host machine's LAN IP so real devices on the same WiFi
    // can reach the serial bridge without typing the address manually.
    if let Ok(lan) = local_lan_ip() {
        let name = format!("tcp://{}:9876", lan);
        if !ports.iter().any(|p| p.name == name) {
            ports.push(PortInfo { name, kind: "tcp (PC bridge)".into() });
        }
    }
    ports
}

fn local_lan_ip() -> Result<String, ()> {
    let s = std::net::UdpSocket::bind("0.0.0.0:0").map_err(|_| ())?;
    s.connect("10.0.2.2:9876").map_err(|_| ())?;     // emulator host gateway
    let addr = s.local_addr().map_err(|_| ())?;
    Ok(addr.ip().to_string())
}

#[tauri::command]
pub fn tcp_connect(
    addr: String,
    state: State<'_, AppState>,
    app: AppHandle,
) -> Result<(), String> {
    let mut guard = state.serial.lock().map_err(|_| "lock poisoned")?;
    if let Some(existing) = guard.as_ref() {
        if existing.is_alive() {
            return Err("already connected".into());
        }
        existing.stop_thread();
        *guard = None;
    }

    let mut stream = TcpStream::connect_timeout(
        &addr.parse().map_err(|e| format!("bad addr: {}", e))?,
        Duration::from_secs(5),
    )
    .map_err(|e| format!("TCP connect: {}", e))?;

    stream.set_read_timeout(Some(Duration::from_millis(50)))
        .map_err(|e| format!("timeout: {}", e))?;

    // --- Sync PING/ACK ---
    let sync_id = format!("__tcp_{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis()).unwrap_or(0));
    let cmd = format!("{{\"type\":\"PING\",\"id\":\"{}\"}}\n", sync_id);
    stream.write_all(cmd.as_bytes()).map_err(|e| format!("write: {}", e))?;
    stream.flush().map_err(|e| format!("flush: {}", e))?;

    let ma = format!("\"id\":\"{}\"", sync_id);
    let mb = format!("\"id\": \"{}\"", sync_id);
    let mut buf = [0u8; 4096];
    let mut acc = String::new();
    let dl = std::time::Instant::now() + Duration::from_secs(8);
    let mut ok = false;
    while std::time::Instant::now() < dl && !ok {
        match stream.read(&mut buf) {
            Ok(0) => continue,
            Ok(n) => {
                acc.push_str(&String::from_utf8_lossy(&buf[..n]));
                if (acc.contains(&ma) || acc.contains(&mb))
                    && (acc.contains("\"type\":\"ACK\"") || acc.contains("\"type\": \"ACK\""))
                { ok = true; }
            }
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => continue,
            Err(ref e) if e.kind() == std::io::ErrorKind::TimedOut => continue,
            Err(ref e) => {
                let m = format!("{}", e);
                if m.contains("EOF") || m.contains("closed") {
                    return Err(format!("TCP closed: {}", m));
                }
                continue;
            }
        }
    }
    if !ok {
        return Err(format!("no ACK (got {} chars)", acc.len()));
    }

    // --- Sync succeeded: clone a writer, then spawn the reader thread ---
    let writer = stream.try_clone()
        .map_err(|e| format!("tcp clone: {}", e))?;
    *TCP_WRITER.lock().unwrap() = Some(writer);

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
            let mut buf = [0u8; 4096];
            match stream.read(&mut buf) {
                Ok(0) => {
                    // EOF -- peer closed
                    alive_for_thread.store(false, Ordering::Release);
                    let _ = app_for_thread.emit("firmware-disconnected", ());
                    break;
                }
                Ok(n) => {
                    accum.extend_from_slice(&buf[..n]);
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
                Err(ref e)
                    if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::TimedOut =>
                {
                    continue;
                }
                Err(_) => {
                    alive_for_thread.store(false, Ordering::Release);
                    let _ = app_for_thread.emit("firmware-disconnected", ());
                    break;
                }
            }
        }
        alive_for_thread.store(false, Ordering::Release);
    });

    *guard = Some(SerialHandle {
        path: format!("tcp://{}", addr),
        stop,
        alive,
        #[cfg(not(target_os = "android"))]
        port: None,
    });

    Ok(())
}
