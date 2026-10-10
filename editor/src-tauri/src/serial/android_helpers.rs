//! Pure helpers used by the Android serial backend.
//!
//! Kept in a separate module WITHOUT the `cfg(target_os = "android")`
//! gate so the regression tests below run on the host (`cargo test`).
//! The android.rs module itself only compiles on Android targets.
//!
//! Every function here pins a regression fixed during the 2026-08-12
//! Android connectivity work; each has a test named after the bug.

/// Does `buf` contain the PING ACK marker for `id`?  The firmware writes
/// compact JSON ({"type":"ACK","id":"..."}); the ACK type and our id must
/// be on the same protocol line.
pub fn marker_found(buf: &[u8], id: &str) -> bool {
    let id_needle = format!("\"id\":\"{}\"", id);
    let ack_needle = b"\"type\":\"ACK\"";
    buf.split(|b| *b == b'\n').any(|line| {
        line.windows(id_needle.len()).any(|w| w == id_needle.as_bytes())
            && line.windows(ack_needle.len()).any(|w| w == ack_needle)
    })
}

/// Classify a transport error message.  BosunSerialBridge reports a write
/// that runs out of time as "write timeout: sent N/M bytes", which is worth
/// a retry; a stale session or a missing device is fatal.  Read timeouts
/// never get here: the bridge returns an empty read instead.  Treating
/// timeouts as fatal killed the connection on the very first idle I/O
/// (2026-08-12 regression).
#[cfg(any(target_os = "android", test))]
pub fn is_transient_error(msg: &str) -> bool {
    msg.contains("timeout") || msg.contains("timed out")
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
#[cfg(any(target_os = "android", test))]
pub fn is_write_only_stall(writes_since_last_read: u32, threshold: u32) -> bool {
    writes_since_last_read >= threshold
}

/// Run `f` on its own thread and wait at most `timeout` for it to finish.
///
/// A JNI call into BosunSerialBridge can still block despite bulkTransfer's
/// own timeout (a wedged USB endpoint or a stuck JNI dispatch). When that
/// happens on the I/O thread's own write/read call, the whole I/O thread
/// freezes mid-call - and the stall watchdog in the surrounding loop, which
/// only runs BETWEEN iterations, never gets a chance to fire (2026-08-15,
/// with the previous plugin-based transport: writes silently stopped
/// succeeding and the app never recovered even minutes later, because the
/// thread was frozen inside one blocking call, not looping and failing to
/// notice staleness).
///
/// This makes that class of hang detectable: if `f` hasn't returned within
/// `timeout`, the caller gets `Err` back and can treat it exactly like any
/// other I/O failure (log it, break out, reconnect). The spawned thread is
/// abandoned if `f` never returns - a leaked thread is a strictly better
/// outcome than a permanently frozen I/O loop, and it only happens on the
/// rare occasion the underlying call actually wedges.
#[cfg(any(target_os = "android", test))]
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
    fn marker_matches_firmware_ack() {
        let ack = b"{\"type\":\"ACK\",\"id\":\"__sync_123_456\",\"fw\":\"0.8.0-native\"}\n";
        assert!(marker_found(ack, "__sync_123_456"));
    }

    #[test]
    fn marker_rejects_non_ack_with_same_id() {
        let event = b"{\"type\":\"EVENT\",\"id\":\"__sync_123_456\"}\n";
        assert!(!marker_found(event, "__sync_123_456"));
    }

    #[test]
    fn marker_requires_ack_and_id_on_same_protocol_line() {
        let mixed = b"{\"type\":\"ACK\",\"id\":\"other\"}\n{\"type\":\"EVENT\",\"id\":\"__sync_123_456\"}\n";
        assert!(!marker_found(mixed, "__sync_123_456"));
    }

    #[test]
    fn marker_rejects_other_ids() {
        let ack = b"{\"type\":\"ACK\",\"id\":\"__sync_999_999\"}\n";
        assert!(!marker_found(ack, "__sync_123_456"));
    }

    #[test]
    fn marker_matches_across_chunk_boundary() {
        // The ACK may arrive split across USB reads; the marker search
        // runs on the accumulated buffer.
        let mut buf = Vec::new();
        buf.extend_from_slice(b"{\"type\":\"ACK\",\"id\":\"__sync_");
        assert!(!marker_found(&buf, "__sync_123_456"));
        buf.extend_from_slice(b"123_456\"}\n");
        assert!(marker_found(&buf, "__sync_123_456"));
    }

    #[test]
    fn write_timeout_is_transient() {
        // BosunSerialBridge.write throws this when bulkTransfer runs out
        // of time part-way through a line.
        assert!(is_transient_error("write timeout: sent 12/40 bytes"));
        assert!(is_transient_error("exchange timed out after 5000 ms"));
    }

    #[test]
    fn real_errors_are_not_transient() {
        assert!(!is_transient_error("stale or disconnected session"));
        assert!(!is_transient_error("Captain USB device not found"));
        assert!(!is_transient_error("failed to open/claim the Captain's data CDC interface (see logcat)"));
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
        let mut writes_since_last_read: u32 = 5;
        assert!(is_write_only_stall(writes_since_last_read, 5));
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
