"""Offline safety/measurement tests; no serial ports or network connections."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_hardware_benchmark as benchmark


class FakeSocket:
    def __init__(self, build):
        self.build = build
        self.commands, self.chunks, self.timeouts = [], [], []
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append(value)

    def sendall(self, payload):
        request = json.loads(payload)
        self.commands.append(request)
        self.chunks.extend(self.build(request))

    def recv(self, size):
        value = self.chunks.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self):
        self.closed = True


def line(obj):
    return json.dumps(obj).encode() + b"\n"


class FakeDevice:
    def __init__(self):
        self.bank, self.slot = 1, 2
        self.commands, self.clients = [], []
        self.stats_calls = 0
        self.fail_switch = False
        self.fail_restore = False
        self.drops = False
        self.native = False

    def connect(self, args, observations):
        owner = self
        number = len(self.clients)
        class Connection:
            closed = False

            def close(self):
                self.closed = True

            def request(self, kind, timeout, **fields):
                owner.commands.append((number, kind, fields))
                if kind == "SWITCH_PATCH":
                    owner.bank, owner.slot = fields["bank"], fields["slot"]
                    if number == 0 and owner.fail_switch:
                        raise TimeoutError("ACK lost after accepted switch")
                    if number == 1 and owner.fail_restore:
                        raise ConnectionError("restoration connection failed")
                reply = {"type": benchmark.KINDS[kind], "id": "fake"}
                if kind == "GET_GLOBAL":
                    reply["device"] = {"name": "baseline", "nested": {"x": [1, 2]}}
                if kind == "GET_CONTEXT":
                    reply["context"] = {"bank": owner.bank, "slot": owner.slot,
                        "kemper_bank": owner.bank, "kemper_rig_in_bank": owner.slot,
                        "kemper_connected": "on", "preview": ""}
                if kind == "GET_RIG_INFO":
                    reply.update(rig=(owner.bank - 1) * 5 + owner.slot, fresh=True, name="CLEAN")
                if kind == "STATS":
                    owner.stats_calls += 1
                    reply.update(uptime_ms=1000 + owner.stats_calls * 100)
                    if owner.native:
                        reply.update(queue_overflows=0, storage_ready=True)
                    else:
                        reply.update(mem_free=7000 - owner.stats_calls,
                                     usb_tx_dropped=int(owner.drops and owner.stats_calls > 1))
                return copy.deepcopy(reply), .25, len(line(reply))
        connection = Connection()
        self.clients.append(connection)
        return connection


class BenchmarkTests(unittest.TestCase):
    def args(self, *extra):
        return benchmark.parse_args(["--output", "unused.json", "--samples", "2", "--warmup", "0", *extra])

    def test_correlates_fragmented_response_while_recording_events_and_old_ids(self):
        def build(request):
            data = (line({"type": "HUB", "link": "up"})
                    + line({"type": "ACK", "id": "old"})
                    + line({"type": "ACK", "id": request["id"], "fw": "test"}))
            return [data[:4], data[4:19], data[19:]]
        sock = FakeSocket(build)
        observations = {}
        client = benchmark.Client(sock, observations=observations)
        reply, elapsed, count = client.request("PING", .5)
        self.assertEqual(reply["fw"], "test")
        self.assertEqual(count, len(line(reply)))
        self.assertGreaterEqual(elapsed, 0)
        self.assertEqual(observations["ignored_message_types"], {"HUB": 1, "ACK": 1})
        self.assertTrue(all(0 < value <= .5 for value in sock.timeouts))

    def test_late_reply_after_timeout_cannot_satisfy_next_request(self):
        state = {}
        def build(request):
            if not state:
                state["old"] = request["id"]
                return [TimeoutError("first timeout")]
            return [line({"type": "ACK", "id": state["old"], "fw": "stale"})
                    + line({"type": "ACK", "id": request["id"], "fw": "new"})]
        client = benchmark.Client(FakeSocket(build))
        with self.assertRaises(TimeoutError):
            client.request("PING", .5)
        self.assertEqual(client.request("PING", .5)[0]["fw"], "new")

    def test_wrong_response_type_and_malformed_json_fail(self):
        for build in (lambda req: [line({"type": "ERROR", "id": req["id"], "error": "busy"})],
                      lambda req: [b"{broken\n"]):
            with self.subTest(build=build), self.assertRaises((RuntimeError, ValueError)):
                benchmark.Client(FakeSocket(build)).request("PING", .5)

    def test_unterminated_frame_is_bounded(self):
        client = benchmark.Client(FakeSocket(lambda req: [b"x" * (benchmark.MAX_LINE + 1)]))
        with self.assertRaisesRegex(ValueError, "exceeds"):
            client.request("PING", .5)

    def test_serial_partial_write_fails_without_waiting_for_reply(self):
        class Serial:
            def write(self, payload):
                return len(payload) - 1
        with self.assertRaisesRegex(IOError, "partial serial"):
            benchmark.Client(Serial(), True).request("PING", .5)

    def test_startup_acquires_sentinel_after_truncated_prefix_and_records_raw_bytes(self):
        fragment = b'age=ready\xff"}\n'
        def build(request):
            data = (fragment + line({"type": "CONTEXT", "context": {"bank": 1}})
                    + line({"type": "ACK", "id": "old-session"})
                    + line({"type": "ACK", "id": request["id"], "fw": "native"}))
            return [data[:3], data[3:8], data[8:]]
        sock = FakeSocket(build)
        observations = {}
        client = benchmark.Client(sock, observations=observations)
        reply = client.synchronize(.5)
        self.assertEqual(reply["fw"], "native")
        record = observations["startup_sync"][0]
        self.assertTrue(record["completed"])
        self.assertEqual(record["discarded_partial_frames"], 1)
        self.assertEqual(record["discarded_bytes"], len(fragment))
        self.assertEqual(record["discarded_frames"][0]["raw_prefix_hex"], fragment[:-1].hex())
        self.assertTrue(bytes.fromhex(record["rx_prefix_hex"]).startswith(fragment))
        self.assertEqual(observations["ignored_message_types"], {"CONTEXT": 1, "ACK": 1})
        self.assertTrue(all(0 < timeout <= .5 for timeout in sock.timeouts))

    def test_startup_retains_following_fragment_and_later_malformed_frame_is_strict(self):
        calls = []
        def build(request):
            calls.append(request)
            if len(calls) == 1:
                return [line({"type": "ACK", "id": request["id"]}) + b'{"type":"EVENT",']
            return [b'"event":"ready"}\n{broken-mid-session\n'
                    + line({"type": "ACK", "id": request["id"]})]
        client = benchmark.Client(FakeSocket(build))
        client.synchronize(.5)
        with self.assertRaises(json.JSONDecodeError):
            client.request("PING", .5)
        self.assertEqual(client.observations["ignored_message_types"], {"EVENT": 1})
        self.assertEqual(client.observations["startup_sync"][0]["discarded_partial_frames"], 0)
        with self.assertRaisesRegex(RuntimeError, "first request"):
            client.synchronize(.5)

    def test_post_sync_malformed_json_retains_full_wire_frame_then_fails(self):
        calls = []
        malformed = b'{"type":"CONTEXT","context":{"bad":' + b'x' * 897 + b'}}\r\n'
        def build(request):
            calls.append(request)
            return [line({"type": "ACK", "id": request["id"]})] if len(calls) == 1 else [malformed]
        observations = {}
        client = benchmark.Client(FakeSocket(build), observations=observations)
        client.synchronize(.5)
        with self.assertRaises(json.JSONDecodeError):
            client.request("GET_CONTEXT", .5)
        self.assertEqual(observations["malformed_frame_count"], 1)
        frame = observations["malformed_frames"][0]
        self.assertEqual(frame["phase"], "session")
        self.assertEqual(frame["request_kind"], "GET_CONTEXT")
        self.assertEqual(frame["request_id"], calls[-1]["id"])
        self.assertEqual(frame["frame_bytes"], len(malformed))
        self.assertEqual(frame["raw_utf8"].encode("utf-8"), malformed)
        self.assertIn("json_error_position", frame)
        self.assertTrue(observations["startup_sync"][0]["completed"])

    def test_invalid_utf8_wire_evidence_uses_hex_and_remains_bounded(self):
        malformed = b'{"type":"CONTEXT","bad":"\xff"}\n'
        observations = {}
        for _ in range(benchmark.MALFORMED_FRAMES_RETAINED + 2):
            client = benchmark.Client(FakeSocket(lambda req: [malformed]), observations=observations)
            with self.assertRaises(UnicodeDecodeError):
                client.request("GET_CONTEXT", .5)
        self.assertEqual(observations["malformed_frame_count"], benchmark.MALFORMED_FRAMES_RETAINED + 2)
        self.assertEqual(len(observations["malformed_frames"]), benchmark.MALFORMED_FRAMES_RETAINED)
        for frame in observations["malformed_frames"]:
            self.assertEqual(bytes.fromhex(frame["raw_hex"]), malformed)
            self.assertNotIn("raw_utf8", frame)

    def test_malformed_correlated_startup_ack_is_not_discarded(self):
        sock = FakeSocket(lambda req: [b'{"id":"' + req["id"].encode() + b'","type":ACK}\n'])
        client = benchmark.Client(sock)
        with self.assertRaises(json.JSONDecodeError):
            client.synchronize(.5)
        record = client.observations["startup_sync"][0]
        self.assertFalse(record["completed"])
        self.assertEqual(record["discarded_partial_frames"], 0)
        self.assertEqual(record["error"]["type"], "JSONDecodeError")

    def test_startup_discard_has_frame_and_byte_limits(self):
        for data in (b'broken\n' * (benchmark.STARTUP_DISCARD_MAX_FRAMES + 1),
                     b'x' * benchmark.STARTUP_DISCARD_MAX_BYTES + b'\n'):
            with self.subTest(length=len(data)):
                sock = FakeSocket(lambda req: [data + line({"type": "ACK", "id": req["id"]})])
                client = benchmark.Client(sock)
                with self.assertRaisesRegex(ValueError, "discard budget"):
                    client.synchronize(.5)
                record = client.observations["startup_sync"][0]
                self.assertFalse(record["completed"])
                self.assertLessEqual(len(bytes.fromhex(record["rx_prefix_hex"])), benchmark.STARTUP_PREFIX_BYTES)
                self.assertLessEqual(len(record["discarded_frames"]), benchmark.STARTUP_DISCARD_MAX_FRAMES + 1)

    def test_startup_requires_matching_ack_and_times_out_without_it(self):
        client = benchmark.Client(FakeSocket(lambda req: [line({"type": "ACK", "id": "stale"}), TimeoutError("no sentinel")]))
        with self.assertRaises(TimeoutError):
            client.synchronize(.05)
        record = client.observations["startup_sync"][0]
        self.assertFalse(record["completed"])
        self.assertEqual(record["error"]["type"], "TimeoutError")
        with self.assertRaisesRegex(RuntimeError, "first request"):
            client.synchronize(.05)

    def test_connect_synchronizes_and_closes_failed_acquisition(self):
        for success in (True, False):
            sock = FakeSocket(lambda req: [line({"type": "ACK", "id": req["id"]})]
                              if success else [TimeoutError("startup timeout")])
            observations = {}
            with self.subTest(success=success), patch.object(benchmark.socket, "create_connection", return_value=sock):
                if success:
                    client = benchmark.connect(self.args(), observations)
                    self.assertTrue(observations["startup_sync"][0]["completed"])
                    self.assertEqual([c["type"] for c in sock.commands], ["PING"])
                    self.assertFalse(sock.closed)
                    client.close()
                else:
                    with self.assertRaises(TimeoutError):
                        benchmark.connect(self.args(), observations)
                    self.assertTrue(sock.closed)
                    self.assertFalse(observations["startup_sync"][0]["completed"])

    def test_default_run_never_mutates_and_preserves_complete_snapshots(self):
        device = FakeDevice()
        result = benchmark.run(self.args("--warmup", "1", "--stats-every", "1"), device.connect)
        self.assertTrue(result["passed"], result["errors"])
        self.assertFalse(result["restoration"]["needed"])
        self.assertEqual(set(kind for _, kind, _ in device.commands), {"PING", "GET_GLOBAL", "GET_CONTEXT", "STATS"})
        self.assertEqual(result["initial"]["GET_GLOBAL"]["device"]["nested"], {"x": [1, 2]})
        self.assertEqual(result["distributions"]["PING"]["count"], 2)
        self.assertEqual(len(result["stats_samples"]), 4)
        self.assertEqual(result["memory_observations"]["mem_free"]["min_bytes"], 6996)
        self.assertTrue(all(c.closed for c in device.clients))

    def test_failed_ack_restores_original_on_fresh_connection(self):
        device = FakeDevice()
        device.fail_switch = True
        result = benchmark.run(self.args("--switch-rigs", "1:3", "--verify-kemper"), device.connect)
        self.assertFalse(result["passed"])
        self.assertTrue(result["restoration"]["succeeded"])
        self.assertEqual((device.bank, device.slot), (1, 2))
        self.assertEqual([c for c in device.commands if c[1] == "SWITCH_PATCH"],
                         [(0, "SWITCH_PATCH", {"bank": 1, "slot": 3}),
                          (1, "SWITCH_PATCH", {"bank": 1, "slot": 2})])
        self.assertIn("ACK lost", result["errors"][0]["error"])

    def test_restoration_failure_preserves_both_errors(self):
        device = FakeDevice()
        device.fail_switch = device.fail_restore = True
        result = benchmark.run(self.args("--switch-rigs", "1:3"), device.connect)
        self.assertFalse(result["restoration"]["succeeded"])
        self.assertEqual([e["phase"] for e in result["errors"][:2]], ["measurement", "restoration"])

    def test_confirmed_kemper_requires_fresh_matching_rig_and_no_preview(self):
        for wrong in ("fresh", "rig", "preview"):
            class BadConfirmation:
                def request(self, kind, timeout, **fields):
                    if kind == "GET_CONTEXT":
                        reply = {"context": {"bank": 1, "slot": 2, "kemper_bank": 1,
                            "kemper_rig_in_bank": 2, "kemper_connected": "on",
                            "preview": "on" if wrong == "preview" else ""}}
                    else:
                        reply = {"rig": 3 if wrong == "rig" else 2, "fresh": wrong != "fresh"}
                    return reply, 0, 0
            with self.subTest(wrong=wrong), self.assertRaises(TimeoutError):
                benchmark.confirmed(BadConfirmation(), 1, 2, .002, True)

    def test_error_counter_growth_fails_and_native_does_not_invent_heap_readings(self):
        device = FakeDevice()
        device.drops = True
        result = benchmark.run(self.args(), device.connect)
        self.assertFalse(result["passed"])
        self.assertEqual(result["counter_deltas"]["usb_tx_dropped"], 1)
        device = FakeDevice()
        device.native = True
        result = benchmark.run(self.args(), device.connect)
        self.assertTrue(result["passed"])
        self.assertEqual(result["memory_observations"], {})

    def test_percentiles_and_transport_scope_are_explicit(self):
        self.assertEqual(benchmark.distribution([]), {"count": 0})
        self.assertAlmostEqual(benchmark.distribution([1, 2, 3, 100])["p95_ms"], 85.45)
        self.assertIn("not cached", benchmark.timing_scope(self.args())["GET_GLOBAL"])
        self.assertIn("in-flight", benchmark.timing_scope(self.args())["GET_CONTEXT"])
        self.assertEqual(benchmark.timing_scope(self.args("--serial", "/dev/ttyACM1"))["transport"], "direct_serial")

    def test_output_is_reserved_before_hardware_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "baseline.json"
            artifact.write_text("keep me", encoding="utf-8")
            with patch.object(benchmark, "run") as run:
                with self.assertRaises(FileExistsError):
                    benchmark.main(["--output", str(artifact)])
                run.assert_not_called()
            self.assertEqual(artifact.read_text(encoding="utf-8"), "keep me")

    def test_invalid_limits_and_targets_rejected_without_device_access(self):
        for extra in (("--duration", "nan"), ("--timeout", "inf"),
                      ("--samples", "0"), ("--switch-rigs", "1:6"),
                      ("--switch-rigs", "1:2:3"), ("--verify-kemper",)):
            with self.subTest(extra=extra), patch("sys.stderr"), self.assertRaises(SystemExit):
                self.args(*extra)


if __name__ == "__main__":
    unittest.main()
