//! Pure helpers used by the Android serial backend.
//!
//! Kept in a separate module WITHOUT the `cfg(target_os = "android")`
//! gate so the regression tests below run on the host (`cargo test`).
//! The android.rs module itself only compiles on Android targets.
//!
//! Every function here pins a regression fixed during the 2026-08-12
//! Android connectivity work; each has a test named after the bug.

/// Extract the port index from a serialplugin port path.
/// Port paths look like /dev/bus/usb/001/002#0, /dev/bus/usb/001/002#1.
/// Returns -1 when the name has no #N suffix.
pub fn port_index(name: &str) -> i32 {
    name.rsplit('#').next()
        .and_then(|n| n.parse::<i32>().ok())
        .unwrap_or(-1)
}

/// Sort port names in descending index order so the data CDC (index 1+)
/// is tried before the console CDC (index 0).  Opening the console CDC
/// asserts DTR and soft-resets CircuitPython, killing the connection.
pub fn sort_ports_desc(ports: &mut Vec<String>) {
    ports.sort_by(|a, b| port_index(b).cmp(&port_index(a)));
}

/// Does `buf` contain the PING ACK marker for `id`?  The firmware
/// serializes JSON with a space after the colon ("id": "..."), while
/// our PING uses compact form ("id":"...").  Match BOTH variants -
/// matching only the compact form made the sentinel sync miss every
/// ACK (2026-08-12 regression).
pub fn marker_found(buf: &[u8], id: &str) -> bool {
    let compact = format!("\"id\":\"{}\"", id);
    let spaced = format!("\"id\": \"{}\"", id);
    buf.windows(compact.len()).any(|w| w == compact.as_bytes())
        || buf.windows(spaced.len()).any(|w| w == spaced.as_bytes())
}

/// Classify a serialplugin error message.  The plugin returns read
/// timeouts as Err("no data received within N ms") rather than an
/// empty Ok(..) like desktop serial2; treating those as fatal killed
/// the connection on the very first idle read (2026-08-12 regression).
pub fn is_transient_error(msg: &str) -> bool {
    msg.contains("lock timeout")
        || msg.contains("no data received")
        || msg.contains("timeout")
        || msg.contains("timed out")
}

/// A link that can WRITE but never READ looks "alive" if staleness is
/// judged from successful I/O of EITHER kind: each outbound write (e.g.
/// StageView's 2 s GET_CONTEXT poll) resets the same clock a successful
/// read would, so a write-only black hole - bytes go out, the far end
/// never answers - never trips the wall-clock stall timer. Live logcat
/// (2026-08-14) showed GET_CONTEXT sent successfully every 2 s for 5+
/// minutes straight with zero read activity, and self-heal never fired:
/// the whole point of a stall timer is to detect a dead link, and this
/// counted "I sent a byte" as proof the link works. Counting consecutive
/// writes with no intervening successful read catches that case
/// independently of the wall-clock check, which still covers the
/// complementary "nothing sent and nothing received" idle-but-dead case.
pub fn is_write_only_stall(writes_since_last_read: u32, threshold: u32) -> bool {
    writes_since_last_read >= threshold
}

/// Run `f` on its own thread and wait at most `timeout` for it to finish.
///
/// The Android serial plugin's `write`/`read` calls can block forever: the
/// plugin's internal hub thread holds the port mutex while polling, and the
/// module-level doc comment already notes "the Android USB stack may not
/// honour the [read] timeout - that lock can be held indefinitely." When
/// that happens on the I/O thread's OWN write/read call (not just the
/// plugin's internal hub thread), the whole I/O thread freezes mid-call -
/// and the stall watchdog in the surrounding loop, which only runs BETWEEN
/// iterations, never gets a chance to fire (2026-08-15: confirmed live,
/// writes silently stopped succeeding and the app never recovered even
/// minutes later and even after removing all MIDI traffic, because the
/// thread was frozen inside one blocking call, not looping and failing to
/// notice staleness).
///
/// This makes that class of hang detectable: if `f` hasn't returned within
/// `timeout`, the caller gets `Err` back and can treat it exactly like any
/// other I/O failure (log it, break out, reconnect). The spawned thread is
/// abandoned if `f` never returns - a leaked thread is a strictly better
/// outcome than a permanently frozen I/O loop, and it only happens on the
/// rare occasion the underlying call actually wedges.
pub fn call_with_timeout<T, F>(timeout: std::time::Duration, f: F) -> Result<T, String>
where
    T: Send + 'static,
    F: FnOnce() -> T + Send + 'static,
{
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let _ = tx.send(f());
    });
    rx.recv_timeout(timeout)
        .map_err(|_| format!("timed out after {:?}", timeout))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn port_sort_tries_data_cdc_before_console() {
        // Real port names captured from the Pixel 8 Pro (adb logcat).
        let mut ports = vec![
            "/dev/bus/usb/001/002#0".to_string(),
            "/dev/bus/usb/001/002#1".to_string(),
        ];
        sort_ports_desc(&mut ports);
        assert_eq!(ports[0], "/dev/bus/usb/001/002#1", "data CDC must be first");
        assert_eq!(ports[1], "/dev/bus/usb/001/002#0", "console CDC must be last");
    }

    #[test]
    fn port_sort_handles_single_port() {
        let mut ports = vec!["/dev/bus/usb/001/002#1".to_string()];
        sort_ports_desc(&mut ports);
        assert_eq!(ports.len(), 1);
    }

    #[test]
    fn port_sort_tolerates_names_without_index() {
        let mut ports = vec![
            "/dev/bus/usb/001/002".to_string(),   // no #N suffix -> -1
            "/dev/bus/usb/001/002#1".to_string(), // data CDC
            "COM4".to_string(),                   // desktop-style name
        ];
        sort_ports_desc(&mut ports);
        // The indexed port must sort first regardless of the others.
        assert_eq!(ports[0], "/dev/bus/usb/001/002#1");
    }

    #[test]
    fn marker_matches_firmware_spaced_format() {
        // Real ACK payload from the firmware (see [sync] debug logs).
        let ack = b"{\"fw\": \"0.5.2\", \"type\": \"ACK\", \"id\": \"__sync_123_456\"}\n";
        assert!(marker_found(ack, "__sync_123_456"));
    }

    #[test]
    fn marker_matches_compact_format() {
        let ack = b"{\"type\":\"ACK\",\"id\":\"__sync_123_456\"}\n";
        assert!(marker_found(ack, "__sync_123_456"));
    }

    #[test]
    fn marker_rejects_other_ids() {
        let ack = b"{\"type\":\"ACK\",\"id\": \"__sync_999_999\"}\n";
        assert!(!marker_found(ack, "__sync_123_456"));
    }

    #[test]
    fn marker_matches_across_chunk_boundary() {
        // The ACK may arrive split across USB reads; the marker search
        // runs on the accumulated buffer.
        let mut buf = Vec::new();
        buf.extend_from_slice(b"{\"type\":\"ACK\",\"id\": \"__sync_");
        assert!(!marker_found(&buf, "__sync_123_456"));
        buf.extend_from_slice(b"123_456\"}\n");
        assert!(marker_found(&buf, "__sync_123_456"));
    }

    #[test]
    fn read_timeout_is_transient() {
        // Exact error the plugin returns when the 100/150 ms read
        // deadline expires with no data.
        assert!(is_transient_error("no data received within 150 ms"));
        assert!(is_transient_error("serial port lock timeout after 250 ms"));
        assert!(is_transient_error("exchange timed out after 5000 ms"));
    }

    #[test]
    fn real_errors_are_not_transient() {
        assert!(!is_transient_error("Port '/dev/bus/usb/001/002#1' not found"));
        assert!(!is_transient_error("Serial port disconnected: No such device (os error 19)"));
        assert!(!is_transient_error("Cannot read while watch is active"));
    }

    // Regression for the 2026-08-14 report: Stage never updated, a
    // long-press bank change never reflected, and (very plausibly) the
    // pedal's own outbound MIDI to the Kemper got stuck the same way -
    // all three trace back to a write-only link never tripping self-heal.

    #[test]
    fn a_handful_of_unanswered_writes_is_not_yet_a_stall() {
        // One or two misses can be a transient hiccup (a response landing
        // just after the next write goes out, USB scheduling jitter, ...).
        assert!(!is_write_only_stall(0, 5));
        assert!(!is_write_only_stall(4, 5));
    }

    #[test]
    fn five_get_context_polls_in_a_row_with_zero_responses_is_a_stall() {
        // Real-world shape: StageView polls GET_CONTEXT every 2 s. Five
        // sends with nothing ever read back (~10 s of pure silence despite
        // asking) is the write-only black hole, independent of whatever
        // the wall-clock wrote-or-read `last_ok` timer thinks.
        assert!(is_write_only_stall(5, 5));
        assert!(is_write_only_stall(200, 5), "must not self-heal only once and then give up counting");
    }

    #[test]
    fn a_single_successful_read_resets_the_would_be_stall() {
        // Mirrors the intended call site: the counter is reset to 0 the
        // instant ANY read returns data, regardless of which outstanding
        // write it happens to answer.
        let mut writes_since_last_read: u32 = 4;
        writes_since_last_read = 0; // a read arrived
        assert!(!is_write_only_stall(writes_since_last_read, 5));
    }

    // Regression for the 2026-08-15 report: the app stopped responding to
    // ANYTHING (patches list empty, stats never updated) and never
    // recovered even minutes later - traced to the I/O thread's write()
    // call itself hanging forever with nothing bounding it.

    #[test]
    fn a_fast_call_returns_its_value_well_within_the_timeout() {
        let result = call_with_timeout(std::time::Duration::from_millis(500), || 42);
        assert_eq!(result, Ok(42));
    }

    #[test]
    fn a_call_that_never_returns_times_out_instead_of_hanging_the_caller() {
        let start = std::time::Instant::now();
        let result = call_with_timeout(std::time::Duration::from_millis(100), || {
            std::thread::sleep(std::time::Duration::from_secs(3600)); // "forever"
            42
        });
        let elapsed = start.elapsed();
        assert!(result.is_err(), "a hung call must surface as an error, not a value");
        assert!(
            elapsed < std::time::Duration::from_secs(2),
            "the CALLER must not block anywhere near as long as the hung closure - waited {:?}",
            elapsed
        );
    }

    #[test]
    fn a_slow_but_eventually_returning_call_still_succeeds_if_under_the_timeout() {
        let result = call_with_timeout(std::time::Duration::from_millis(500), || {
            std::thread::sleep(std::time::Duration::from_millis(50));
            "ok"
        });
        assert_eq!(result, Ok("ok"));
    }
}
