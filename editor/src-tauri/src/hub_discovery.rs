//! Discover Bosun hubs with a short UDP exchange, without opening Captain links.

use serde::{Deserialize, Serialize};
use std::io::ErrorKind;
use std::net::{IpAddr, Ipv4Addr, SocketAddr, UdpSocket};
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const DISCOVERY_PORT: u16 = 9877;
const DISCOVERY_TIMEOUT: Duration = Duration::from_secs(2);
const RETRY_INTERVAL: Duration = Duration::from_millis(500);
const MAX_REPLY_BYTES: usize = 1024;
const MAX_HUBS: usize = 64;
static DISCOVERY_SEQUENCE: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct DiscoveredHub {
    pub name: String,
    pub host: String,
    pub tcp_port: u16,
}

#[derive(Deserialize)]
struct Announcement {
    #[serde(rename = "type")]
    kind: String,
    version: u8,
    nonce: String,
    name: String,
    tcp_port: u16,
}

fn parse_announcement(bytes: &[u8], sender: SocketAddr, nonce: &str) -> Option<DiscoveredHub> {
    if bytes.len() > MAX_REPLY_BYTES || sender.ip().is_unspecified() || sender.ip().is_multicast() {
        return None;
    }
    let reply: Announcement = serde_json::from_slice(bytes).ok()?;
    let name = reply.name.trim();
    if reply.kind != "BOSUN_HUB" || reply.version != 1 || reply.nonce != nonce
        || reply.tcp_port == 0 || name.is_empty() || reply.name.len() > 255
        || reply.name.chars().any(char::is_control)
    {
        return None;
    }
    Some(DiscoveredHub {
        name: name.to_owned(),
        // Never trust an advertised address: connect to the actual responder.
        host: sender.ip().to_string(),
        tcp_port: reply.tcp_port,
    })
}

fn fresh_nonce() -> String {
    let time = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos();
    let sequence = DISCOVERY_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    format!("{}-{time}-{sequence}", std::process::id())
}

struct DiscoverySocket {
    socket: UdpSocket,
    targets: Vec<SocketAddr>,
}

fn discovery_sockets(hint: Option<&str>) -> Result<Vec<DiscoverySocket>, String> {
    let socket = UdpSocket::bind((Ipv4Addr::UNSPECIFIED, 0))
        .map_err(|e| format!("Cannot open hub discovery socket: {e}"))?;
    socket.set_broadcast(true).map_err(|e| format!("Cannot enable hub discovery broadcast: {e}"))?;
    socket.set_nonblocking(true).map_err(|e| format!("Cannot configure hub discovery socket: {e}"))?;
    let mut sockets = vec![DiscoverySocket {
        socket,
        targets: vec![
            SocketAddr::from((Ipv4Addr::BROADCAST, DISCOVERY_PORT)),
            SocketAddr::from((Ipv4Addr::LOCALHOST, DISCOVERY_PORT)),
        ],
    }];
    // Discovery never performs DNS lookups or subnet/TCP scans. A remembered
    // numeric address is useful on networks that suppress UDP broadcasts.
    if let Some(ip) = hint.and_then(|value| value.trim().parse::<IpAddr>().ok()) {
        if !ip.is_unspecified() && !ip.is_multicast() {
            let target = SocketAddr::new(ip, DISCOVERY_PORT);
            if ip.is_ipv4() {
                if !sockets[0].targets.contains(&target) {
                    sockets[0].targets.push(target);
                }
            } else if let Ok(socket) = UdpSocket::bind("[::]:0") {
                if socket.set_nonblocking(true).is_ok() {
                    sockets.push(DiscoverySocket { socket, targets: vec![target] });
                }
            }
        }
    }
    Ok(sockets)
}

fn discover_on(sockets: Vec<DiscoverySocket>, timeout: Duration, retry: Duration) -> Result<Vec<DiscoveredHub>, String> {
    let nonce = fresh_nonce();
    let request = serde_json::to_vec(&serde_json::json!({
        "type": "BOSUN_DISCOVER", "version": 1, "nonce": nonce,
    })).map_err(|e| format!("Cannot encode hub discovery request: {e}"))?;
    let deadline = Instant::now() + timeout;
    let mut next_send = Instant::now();
    let mut sent = false;
    let mut send_error = String::new();
    let mut hubs: Vec<DiscoveredHub> = Vec::new();
    let mut buffer = [0u8; MAX_REPLY_BYTES + 1];
    while Instant::now() < deadline {
        if Instant::now() >= next_send {
            for endpoint in &sockets {
                for target in &endpoint.targets {
                    match endpoint.socket.send_to(&request, target) {
                        Ok(_) => sent = true,
                        Err(error) => send_error = error.to_string(),
                    }
                }
            }
            next_send = Instant::now() + retry;
        }
        for endpoint in &sockets {
            // Bound work even if a peer floods the discovery port.
            for _ in 0..64 {
                match endpoint.socket.recv_from(&mut buffer) {
                    Ok((length, sender)) => {
                        if let Some(hub) = parse_announcement(&buffer[..length], sender, &nonce) {
                            if hubs.len() < MAX_HUBS && !hubs.iter().any(|old| {
                                old.host == hub.host && old.tcp_port == hub.tcp_port
                            }) {
                                hubs.push(hub);
                            }
                        }
                    }
                    Err(error) if error.kind() == ErrorKind::Interrupted => continue,
                    // WouldBlock is normal; platforms can also report an
                    // oversized UDP datagram as an error. Skip that packet.
                    Err(_) => break,
                }
            }
        }
        thread::sleep(Duration::from_millis(20).min(deadline.saturating_duration_since(Instant::now())));
    }
    if !sent {
        return Err(format!("Could not send hub discovery request: {send_error}"));
    }
    hubs.sort_by(|a, b| a.name.cmp(&b.name).then_with(|| a.host.cmp(&b.host)).then_with(|| a.tcp_port.cmp(&b.tcp_port)));
    Ok(hubs)
}

#[tauri::command]
pub async fn discover_hubs(hint: Option<String>) -> Result<Vec<DiscoveredHub>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        discover_on(discovery_sockets(hint.as_deref())?, DISCOVERY_TIMEOUT, RETRY_INTERVAL)
    }).await.map_err(|e| format!("Hub discovery task failed: {e}"))?
}

#[cfg(test)]
mod tests {
    use super::*;

    fn reply(nonce: &str) -> serde_json::Value {
        serde_json::json!({"type":"BOSUN_HUB","version":1,"nonce":nonce,"name":"bosun-hub","tcp_port":9876})
    }

    #[test]
    fn validates_reply_and_uses_sender_address() {
        let sender = "192.0.2.8:50000".parse().unwrap();
        let mut message = reply("test");
        message["host"] = serde_json::json!("attacker.invalid");
        let hub = parse_announcement(&serde_json::to_vec(&message).unwrap(), sender, "test").unwrap();
        assert_eq!(hub.host, "192.0.2.8");
        assert_eq!(hub.tcp_port, 9876);
        for (field, value) in [
            ("type", serde_json::json!("ACK")),
            ("version", serde_json::json!(2)),
            ("version", serde_json::json!(true)),
            ("nonce", serde_json::json!("other-search")),
            ("name", serde_json::json!("  ")),
            ("name", serde_json::json!("bad\nname")),
            ("name", serde_json::json!("x".repeat(256))),
            ("tcp_port", serde_json::json!(0)),
            ("tcp_port", serde_json::json!(65536)),
            ("tcp_port", serde_json::json!("9876")),
        ] {
            let mut invalid = reply("test");
            invalid[field] = value;
            assert!(parse_announcement(&serde_json::to_vec(&invalid).unwrap(), sender, "test").is_none(), "{invalid}");
        }
        assert!(parse_announcement(b"not json", sender, "test").is_none());
        assert!(parse_announcement(&vec![b' '; MAX_REPLY_BYTES + 1], sender, "test").is_none());
    }

    #[test]
    fn discovery_retries_and_deduplicates_loopback_responses() {
        let server = UdpSocket::bind("127.0.0.1:0").unwrap();
        server.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        let target = server.local_addr().unwrap();
        let responder = thread::spawn(move || {
            let mut buffer = [0u8; 1024];
            let (_, first_sender) = server.recv_from(&mut buffer).unwrap();
            // Deliberately drop the first request to exercise retransmission.
            let (size, sender) = server.recv_from(&mut buffer).unwrap();
            assert_eq!(sender, first_sender);
            let request: serde_json::Value = serde_json::from_slice(&buffer[..size]).unwrap();
            assert_eq!(request["type"], "BOSUN_DISCOVER");
            assert_eq!(request["version"], 1);
            let nonce = request["nonce"].as_str().unwrap();
            server.send_to(&serde_json::to_vec(&reply("stale-nonce")).unwrap(), sender).unwrap();
            let response = serde_json::to_vec(&reply(nonce)).unwrap();
            server.send_to(&response, sender).unwrap();
            server.send_to(&response, sender).unwrap();
        });
        let client = UdpSocket::bind("127.0.0.1:0").unwrap();
        client.set_nonblocking(true).unwrap();
        let hubs = discover_on(vec![DiscoverySocket { socket: client, targets: vec![target] }],
            Duration::from_millis(500), Duration::from_millis(60)).unwrap();
        responder.join().unwrap();
        assert_eq!(hubs, vec![DiscoveredHub { name: "bosun-hub".into(), host: "127.0.0.1".into(), tcp_port: 9876 }]);
    }

    #[test]
    fn only_numeric_hints_add_discovery_destinations() {
        let sockets = discovery_sockets(Some("host.invalid")).unwrap();
        assert_eq!(sockets[0].targets.len(), 2);
        let sockets = discovery_sockets(Some("192.0.2.9")).unwrap();
        assert!(sockets[0].targets.contains(&"192.0.2.9:9877".parse().unwrap()));
        assert_ne!(fresh_nonce(), fresh_nonce());
    }
}
