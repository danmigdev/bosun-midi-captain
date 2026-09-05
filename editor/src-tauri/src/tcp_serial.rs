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
use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, LazyLock, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Emitter, Manager};

use crate::serial::{AppState, PortInfo, SerialHandle};

const MAX_INBOX_LINES: usize = 4096;
const MAX_ACCUM_BYTES: usize = 1024 * 1024;
static TCP_GENERATION: AtomicU64 = AtomicU64::new(0);

fn push_bounded(q: &mut VecDeque<String>, line: String) {
    while q.len() >= MAX_INBOX_LINES { q.pop_front(); }
    q.push_back(line);
}

fn append_bounded(accum: &mut Vec<u8>, bytes: &[u8]) {
    if bytes.len() >= MAX_ACCUM_BYTES {
        accum.clear();
        accum.extend_from_slice(&bytes[bytes.len() - MAX_ACCUM_BYTES..]);
        return;
    }
    let overflow = accum.len().saturating_add(bytes.len()).saturating_sub(MAX_ACCUM_BYTES);
    if overflow > 0 { accum.drain(..overflow); }
    accum.extend_from_slice(bytes);
}

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
    TCP_GENERATION.fetch_add(1, Ordering::AcqRel);
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

fn parse_endpoint(addr: &str) -> Result<(String, u16), String> {
    let addr = addr.trim().strip_prefix("tcp://").unwrap_or(addr.trim());
    if addr.is_empty() || addr.len() > 512 || addr.chars().any(char::is_whitespace) {
        return Err("Enter a TCP address as hostname:port, IP:port, or [IPv6]:port".into());
    }
    if let Ok(socket) = addr.parse::<SocketAddr>() {
        if socket.port() == 0 {
            return Err("TCP port must be between 1 and 65535".into());
        }
        return Ok((socket.ip().to_string(), socket.port()));
    }
    let (host, port) = addr.rsplit_once(':')
        .ok_or("TCP address needs a port, for example bosun-hub.local:9876")?;
    let port = port.parse::<u16>().ok().filter(|port| *port != 0)
        .ok_or("TCP port must be between 1 and 65535")?;
    if host.is_empty() || host.len() > 253
        || !host.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'-' | b'_'))
    {
        return Err("Invalid TCP hostname; use hostname:port or [IPv6]:port".into());
    }
    Ok((host.to_owned(), port))
}

fn connect_stream(addr: &str) -> Result<TcpStream, String> {
    let (host, port) = parse_endpoint(addr)?;
    // System hostname resolution (including .local where supported) runs on
    // the blocking task, never the webview thread. Try both address families.
    let mut resolved = Vec::new();
    let address = addr.trim().strip_prefix("tcp://").unwrap_or(addr.trim());
    if let Ok(socket) = address.parse::<SocketAddr>() {
        // Preserve a numeric IPv6 scope ID when the caller supplied one.
        resolved.push(socket);
    } else {
        for socket in (host.as_str(), port).to_socket_addrs()
            .map_err(|e| format!("Cannot resolve TCP host '{host}': {e}"))?.take(8)
        {
            if !resolved.contains(&socket) { resolved.push(socket); }
        }
    }
    if resolved.is_empty() {
        return Err(format!("TCP host '{host}' resolved to no addresses"));
    }
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut last_error = String::new();
    for socket in resolved {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() { break; }
        match TcpStream::connect_timeout(&socket, remaining.min(Duration::from_secs(2))) {
            Ok(stream) => return Ok(stream),
            Err(error) => last_error = format!("{socket}: {error}"),
        }
    }
    Err(format!("Cannot connect to TCP address '{addr}': {last_error}"))
}

fn sync_ping(stream: &mut TcpStream, timeout: Duration) -> Result<(), String> {
    stream.set_read_timeout(Some(Duration::from_millis(50)))
        .map_err(|e| format!("TCP read timeout: {e}"))?;
    stream.set_write_timeout(Some(Duration::from_secs(2)))
        .map_err(|e| format!("TCP write timeout: {e}"))?;
    let sync_id = format!("__tcp_{}_{}", std::process::id(),
        std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos()).unwrap_or(0));
    let cmd = format!("{{\"type\":\"PING\",\"id\":\"{sync_id}\"}}\n");
    stream.write_all(cmd.as_bytes()).map_err(|e| format!("TCP handshake write: {e}"))?;
    stream.flush().map_err(|e| format!("TCP handshake flush: {e}"))?;

    let deadline = Instant::now() + timeout;
    let mut buffer = [0u8; 4096];
    let mut accumulated = Vec::new();
    while Instant::now() < deadline {
        match stream.read(&mut buffer) {
            Ok(0) => return Err("TCP connection closed before the Captain acknowledged PING".into()),
            Ok(length) => {
                append_bounded(&mut accumulated, &buffer[..length]);
                while let Some(end) = accumulated.iter().position(|b| *b == b'\n') {
                    let line: Vec<u8> = accumulated.drain(..=end).collect();
                    let Ok(reply) = serde_json::from_slice::<serde_json::Value>(&line) else { continue; };
                    if reply.get("id").and_then(|value| value.as_str()) != Some(sync_id.as_str()) {
                        continue;
                    }
                    match reply.get("type").and_then(|value| value.as_str()) {
                        Some("ACK") => return Ok(()),
                        Some("ERROR") => {
                            let reason = reply.get("error").and_then(|value| value.as_str()).unwrap_or("unknown error");
                            return Err(format!("TCP hub rejected PING: {}", reason.chars().take(256).collect::<String>()));
                        }
                        _ => return Err("TCP endpoint returned an unexpected response to PING".into()),
                    }
                }
            }
            Err(error) if matches!(error.kind(), std::io::ErrorKind::WouldBlock
                | std::io::ErrorKind::TimedOut | std::io::ErrorKind::Interrupted) => continue,
            Err(error) => return Err(format!("TCP handshake read: {error}")),
        }
    }
    Err("TCP connection opened, but the Captain did not acknowledge PING".into())
}

#[tauri::command]
pub async fn tcp_connect(addr: String, app: AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state_app = app.clone();
        let state = state_app.state::<AppState>();
        tcp_connect_blocking(addr, state.inner(), app)
    }).await.map_err(|e| format!("TCP connection task failed: {e}"))?
}

fn tcp_connect_blocking(
    addr: String,
    state: &AppState,
    app: AppHandle,
) -> Result<(), String> {
    let mut guard = state.serial.lock().map_err(|_| "lock poisoned")?;
    if let Some(existing) = guard.as_ref() {
        if existing.is_alive() {
            return Err("already connected".into());
        }
        existing.stop_thread();
        *guard = None;
        tcp_close();
    }

    let mut stream = connect_stream(&addr)?;
    sync_ping(&mut stream, Duration::from_secs(8))?;

    if let Ok(mut inbox) = state.inbox.lock() {
        inbox.clear();
    }

    // --- Sync succeeded: clone a writer, then spawn the reader thread ---
    let writer = stream.try_clone()
        .map_err(|e| format!("tcp clone: {}", e))?;
    let generation = TCP_GENERATION.fetch_add(1, Ordering::AcqRel) + 1;
    *TCP_WRITER.lock().unwrap() = Some(writer);

    let stop = Arc::new(Mutex::new(false));
    let alive = Arc::new(AtomicBool::new(true));
    let stop_for_thread = stop.clone();
    let alive_for_thread = alive.clone();
    let app_for_thread = app.clone();
    let inbox_for_thread = state.inbox.clone();

    thread::spawn(move || {
        let mut accum = Vec::<u8>::with_capacity(8192);
        let current = || TCP_GENERATION.load(Ordering::Acquire) == generation
            && stop_for_thread.lock().map(|stop| !*stop).unwrap_or(false);
        loop {
            if !current() { break; }
            let mut buf = [0u8; 4096];
            let received = stream.read(&mut buf);
            // Stop/reconnect can happen while read() waits. An old socket
            // must neither publish data nor disconnect its replacement.
            if !current() { break; }
            match received {
                Ok(0) => {
                    // EOF -- peer closed
                    alive_for_thread.store(false, Ordering::Release);
                    if current() { let _ = app_for_thread.emit("firmware-disconnected", ()); }
                    break;
                }
                Ok(n) => {
                    append_bounded(&mut accum, &buf[..n]);
                    let mut new_lines = 0;
                    while let Some(pos) = accum.iter().position(|b| *b == b'\n') {
                        let mut line: Vec<u8> = accum.drain(..=pos).collect();
                        while matches!(line.last(), Some(b'\n') | Some(b'\r')) { line.pop(); }
                        if line.is_empty() { continue; }
                        if let Ok(s) = std::str::from_utf8(&line) {
                            if let Ok(mut q) = inbox_for_thread.lock() {
                                if !current() { break; }
                                push_bounded(&mut q, s.to_string());
                                new_lines += 1;
                            }
                        }
                    }
                    if new_lines > 0 && current() {
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
                    if current() { let _ = app_for_thread.emit("firmware-disconnected", ()); }
                    break;
                }
            }
        }
        alive_for_thread.store(false, Ordering::Release);
        if let Ok(mut writer) = TCP_WRITER.lock() {
            if TCP_GENERATION.load(Ordering::Acquire) == generation { *writer = None; }
        }
    });

    *guard = Some(SerialHandle {
        #[cfg(target_os = "android")]
        path: format!("tcp://{}", addr),
        stop,
        alive,
        #[cfg(target_os = "android")]
        generation: 0,
        #[cfg(not(target_os = "android"))]
        port: None,
        #[cfg(not(target_os = "android"))]
        write_lock: Arc::new(Mutex::new(())),
    });

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader};
    use std::net::TcpListener;

    #[test]
    fn accepts_hostnames_and_numeric_endpoints_without_losing_port() {
        assert_eq!(parse_endpoint("bosun-hub.local:9876").unwrap(), ("bosun-hub.local".into(), 9876));
        assert_eq!(parse_endpoint(" tcp://127.0.0.1:9876 ").unwrap(), ("127.0.0.1".into(), 9876));
        assert_eq!(parse_endpoint("[::1]:1234").unwrap(), ("::1".into(), 1234));
        for addr in ["", "bosun-hub", "localhost:0", "localhost:65536", "localhost:abc",
            "[::1]", "::1:9876", "localhost:9876/path", "bad host:9876", "tcp://:9876"]
        {
            assert!(parse_endpoint(addr).is_err(), "{addr}");
        }
    }

    fn test_server(response: impl FnOnce(&mut TcpStream, &str) + Send + 'static) -> (String, thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = format!("localhost:{}", listener.local_addr().unwrap().port());
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
            let mut line = String::new();
            BufReader::new(stream.try_clone().unwrap()).read_line(&mut line).unwrap();
            let ping: serde_json::Value = serde_json::from_str(&line).unwrap();
            assert_eq!(ping["type"], "PING");
            response(&mut stream, ping["id"].as_str().unwrap());
        });
        (address, handle)
    }

    #[test]
    fn resolves_localhost_and_accepts_only_the_correlated_complete_ack() {
        let (address, server) = test_server(|stream, id| {
            stream.write_all(b"{\"type\":\"ERROR\",\"id\":\"unrelated\",\"error\":\"ignored\"}\n").unwrap();
            let ack = serde_json::to_vec(&serde_json::json!({"type":"ACK","id":id})).unwrap();
            let split = ack.len() / 2;
            stream.write_all(&ack[..split]).unwrap();
            thread::sleep(Duration::from_millis(20));
            stream.write_all(&ack[split..]).unwrap();
            stream.write_all(b"\r\n").unwrap();
        });
        let mut stream = connect_stream(&address).unwrap();
        sync_ping(&mut stream, Duration::from_secs(2)).unwrap();
        server.join().unwrap();
    }

    #[test]
    fn eof_during_handshake_reports_closed_connection() {
        let (address, server) = test_server(|_, _| {});
        let mut stream = connect_stream(&address).unwrap();
        let error = sync_ping(&mut stream, Duration::from_secs(2)).unwrap_err();
        assert!(error.contains("closed"), "{error}");
        server.join().unwrap();
    }

    #[test]
    fn correlated_hub_error_is_reported_instead_of_ack_timeout() {
        let (address, server) = test_server(|stream, id| {
            let error = serde_json::json!({"type":"ERROR","id":id,"error":"link_down"});
            writeln!(stream, "{error}").unwrap();
        });
        let mut stream = connect_stream(&address).unwrap();
        let error = sync_ping(&mut stream, Duration::from_secs(2)).unwrap_err();
        assert!(error.contains("link_down"), "{error}");
        server.join().unwrap();
    }
}
