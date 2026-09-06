"""Fault-injected native acceptance workflow tests; no hardware or network."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_native_persistence as persistence
from test_live_hardware_benchmark import FakeSocket, line


def fixture():
    device = {"device_name": "Synthetic test Captain", "unknown": {"preserved": [1, 2]},
              "preset_navigation": {"switches": {"A": 1}},
              "tft": {"layout": [{"field": "patch_name", "color": "#ffffff"},
                                  {"field": "bank", "color": "#aabbcc", "prefix": "BANK "}]}}
    info = {"type": "DEVICE_INFO", "fw": "0.6.4", "device": device["device_name"],
            "profile": "fixture", "preset_navigation": device["preset_navigation"],
            "tft_colors": {"patch_name": "#ffffff", "bank": "#aabbcc"},
            "tft_labels": {"bank": {"prefix": "BANK ", "suffix": ""}}}
    records = [{"request": {"type": "GET_DEVICE_INFO"}, "response": info}]
    for fields in ({}, {"profile": "fixture"}):
        records.append({"request": dict(type="GET_GLOBAL", **fields),
                        "response": {"type": "GLOBAL", "device": device, "profile": "fixture"}})
    for bank, slot in [(1, i) for i in range(1, 6)] + [(2, 4)]:
        records.append({"request": {"type": "GET_PATCH", "profile": "fixture", "bank": bank, "slot": slot},
                        "response": {"type": "PATCH", "patch": {"name": "Synthetic %d:%d" % (bank, slot), "bindings": []}}})
    return {"records": records}


class Device:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.original = copy.deepcopy(snapshot["original"])
        self.device = copy.deepcopy(self.original)
        self.persisted = copy.deepcopy(self.original)
        self.bank, self.slot, self.uptime = 1, 3, 10000
        self.commands, self.clients = [], []
        self.writes, self.reboots = 0, 0
        self.native = self.storage_ready = True
        self.lose_temporary_ack = self.lose_restore_ack = self.lose_reboot_ack = False
        self.lose_rig_ack = False
        self.persist_temporary = True
        self.ignore_first_reboot = self.fail_restore_write = False
        self.link_down_connections = 0
        self.corrupt_connections = set()
        self.info_busy_per_write = self.info_busy_remaining = 0

    def connect(self, args, observations):
        owner = self
        number = len(self.clients)
        class Connection:
            closed = False

            def close(self):
                self.closed = True

            def request(self, kind, timeout, **fields):
                owner.commands.append((number, kind, copy.deepcopy(fields)))
                reply = {"type": persistence.benchmark.KINDS[kind], "id": "fake"}
                if kind == "GET_DEVICE_INFO":
                    if owner.info_busy_remaining:
                        owner.info_busy_remaining -= 1
                        raise persistence.benchmark.ProtocolReplyError(kind, {
                            "type": "ERROR", "error": "background_busy", "of": kind, "id": "fake"})
                    if number in owner.corrupt_connections:
                        raise json.JSONDecodeError("corrupt live response", "{", 1)
                    if number < owner.link_down_connections:
                        raise RuntimeError("{'type': 'ERROR', 'error': 'link_down'}")
                    reply = copy.deepcopy(owner.snapshot["info"])
                    reply.update(fw="0.1.0-native-experimental" if owner.native else "0.6.4",
                                 native_experimental=owner.native)
                    reply["tft_colors"]["patch_name"] = owner.device["tft"]["layout"][0]["color"]
                elif kind == "STATS":
                    owner.uptime += 20
                    reply.update(uptime_ms=owner.uptime, storage_ready=owner.storage_ready)
                elif kind == "GET_GLOBAL":
                    reply["device"] = copy.deepcopy(owner.device)
                elif kind == "GET_PATCH":
                    reply["patch"] = next(copy.deepcopy(r["response"]["patch"]) for r in owner.snapshot["patches"]
                        if (r["request"]["bank"], r["request"]["slot"]) == (fields["bank"], fields["slot"]))
                elif kind == "GET_CONTEXT":
                    reply["context"] = {"bank": owner.bank, "slot": owner.slot, "kemper_bank": owner.bank,
                        "kemper_rig_in_bank": owner.slot, "kemper_connected": "on", "preview": ""}
                elif kind == "GET_RIG_INFO":
                    reply.update(rig=(owner.bank - 1) * 5 + owner.slot, fresh=True, name="Synthetic")
                elif kind == "PUT_GLOBAL":
                    owner.writes += 1
                    owner.info_busy_remaining = owner.info_busy_per_write
                    if owner.writes > 1 and owner.fail_restore_write:
                        raise TimeoutError("restore write not accepted")
                    owner.device = copy.deepcopy(fields["device"])
                    if owner.writes > 1 or owner.persist_temporary:
                        owner.persisted = copy.deepcopy(owner.device)
                    if (owner.writes == 1 and owner.lose_temporary_ack) or (owner.writes > 1 and owner.lose_restore_ack):
                        raise TimeoutError("write committed, ACK lost")
                elif kind == "REBOOT":
                    self.assert_normal(fields)
                    owner.reboots += 1
                    if not (owner.reboots == 1 and owner.ignore_first_reboot):
                        owner.device = copy.deepcopy(owner.persisted)
                        owner.uptime = 0
                        owner.bank, owner.slot = 1, 1
                    if owner.lose_reboot_ack:
                        raise ConnectionError("expected USB reset before ACK")
                elif kind == "SWITCH_PATCH":
                    owner.bank, owner.slot = fields["bank"], fields["slot"]
                    if owner.lose_rig_ack:
                        raise TimeoutError("rig switched, ACK lost")
                return reply, .5, len(json.dumps(reply)) + 1

            def assert_normal(self, fields):
                if fields != {"mode": "normal"}:
                    raise AssertionError("only explicit normal REBOOT is allowed")
        client = Connection()
        self.clients.append(client)
        return client


class NativePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "snapshot.json"
        self.path.write_text(json.dumps(fixture()), encoding="utf-8")
        self.snapshot = persistence.load_snapshot(self.path)

    def args(self, *extra):
        return persistence.parse_args(["--snapshot", str(self.path), "--output", str(self.path.parent / "artifact.json"),
            "--retry-interval", "0", "--recovery-attempts", "3", "--recovery-timeout", "1", *extra])

    def run_device(self, device, *extra):
        return persistence.Acceptance(self.args(*extra), self.snapshot, device.connect).run()

    def test_read_only_compares_both_globals_six_patches_and_projections(self):
        device = Device(self.snapshot)
        result = self.run_device(device)
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(sum(kind == "GET_PATCH" for _, kind, _ in device.commands), 6)
        self.assertEqual(sum(kind == "GET_GLOBAL" for _, kind, _ in device.commands), 2)
        self.assertEqual(device.writes, 0)
        self.assertEqual(device.reboots, 0)
        self.assertNotIn("SWITCH_PATCH", [kind for _, kind, _ in device.commands])
        self.assertTrue(all(client.closed for client in device.clients))

    def test_full_exercise_proves_two_reboots_and_restores_entire_original_and_rig(self):
        device = Device(self.snapshot)
        result = self.run_device(device, "--exercise-writes")
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(device.device, self.snapshot["original"])
        self.assertEqual(device.persisted, self.snapshot["original"])
        self.assertEqual((device.bank, device.slot), (1, 3))
        self.assertTrue(result["restoration"]["succeeded"])
        self.assertTrue(result["checks"]["temporary_json_persisted"])
        self.assertEqual(len(result["expected_reboots"]), 2)
        self.assertTrue(all(reboot["proved"] for reboot in result["expected_reboots"]))
        writes = [fields["device"] for _, kind, fields in device.commands if kind == "PUT_GLOBAL"]
        expected = copy.deepcopy(self.snapshot["original"])
        expected["tft"]["layout"][0]["color"] = "#fffffe"
        self.assertEqual(writes, [expected, self.snapshot["original"]])
        self.assertTrue(set(kind for _, kind, _ in device.commands).isdisjoint({"PUT_PATCH", "SAVE_NOW", "DELETE_PATCH", "DISCARD"}))

    def test_hub_metadata_contention_after_each_write_preserves_persistence_proof(self):
        device = Device(self.snapshot)
        device.info_busy_per_write = 2
        result = self.run_device(device, "--exercise-writes")
        self.assertTrue(result["passed"], result["errors"])
        self.assertTrue(result["checks"]["temporary_json_persisted"])
        self.assertTrue(result["restoration"]["succeeded"])
        self.assertEqual((device.writes, device.reboots), (2, 2))
        self.assertEqual(device.persisted, self.snapshot["original"])
        retries = [r for r in result["records"] if "retry" in r]
        self.assertEqual([r["phase"] for r in retries],
                         ["temporary_readback"] * 2 + ["restore_readback"] * 2)
        self.assertTrue(all(r["response"]["error"] == "background_busy" for r in retries))

    def test_real_decoder_finite_busy_retries_keep_session_ids_and_reply_evidence(self):
        acceptance = persistence.Acceptance(self.args(), self.snapshot)
        count = 0
        def build(request):
            nonlocal count
            count += 1
            reply = ({"type": "ERROR", "error": "background_busy", "of": request["type"]}
                     if count <= 2 else {"type": "DEVICE_INFO", "fw": "0.1.0-native-experimental"})
            data = line(dict(reply, id=request["id"]))
            return [data[:9], data[9:]]
        transport = FakeSocket(build)
        acceptance.client = persistence.benchmark.Client(transport, observations=acceptance.result["observations"])
        reply = acceptance.request("GET_DEVICE_INFO")
        self.assertEqual(reply["type"], "DEVICE_INFO")
        self.assertFalse(transport.closed)
        self.assertEqual(len({r["id"] for r in transport.commands}), 3)
        records = acceptance.result["records"]
        self.assertEqual([r["attempt"] for r in records], [1, 2, 3])
        self.assertEqual([r["response"]["type"] for r in records], ["ERROR", "ERROR", "DEVICE_INFO"])
        self.assertTrue(all(r["retry"]["reason"] == "background_busy" for r in records[:2]))
        self.assertEqual(acceptance.result["observations"]["request_failure_count"], 2)
        self.assertNotIn("retry", records[-1])

    def test_perpetual_busy_respects_one_deadline_and_passes_only_remaining_time(self):
        class Clock:
            now = 100.0
            def monotonic(self):
                return self.now
            def sleep(self, delay):
                self.now += delay
        clock = Clock()
        with patch.object(persistence.time, "monotonic", clock.monotonic), \
             patch.object(persistence.time, "sleep", clock.sleep):
            acceptance = persistence.Acceptance(self.args("--retry-interval", ".1", "--recovery-attempts", "60"), self.snapshot)
            transport = FakeSocket(lambda request: [line({"type": "ERROR", "error": "background_busy",
                                                          "of": request["type"], "id": request["id"]})])
            acceptance.client = persistence.benchmark.Client(transport)
            with self.assertRaisesRegex(persistence.AcceptanceError, "original request deadline"):
                acceptance.request("GET_DEVICE_INFO", timeout=.25)
            self.assertAlmostEqual(clock.now, 100.25)
            self.assertEqual(len(transport.commands), 3)
            self.assertEqual(len(acceptance.result["records"]), 3)
            for actual, expected in zip([r["timeout_s"] for r in acceptance.result["records"]], [.25, .15, .05]):
                self.assertAlmostEqual(actual, expected)
            self.assertEqual(transport.timeouts, sorted(transport.timeouts, reverse=True))
            self.assertAlmostEqual(transport.timeouts[-1], .05)
            self.assertTrue(all("retry" in r for r in acceptance.result["records"]))

    def test_zero_delay_perpetual_busy_stops_at_attempt_budget(self):
        acceptance = persistence.Acceptance(self.args(), self.snapshot)
        transport = FakeSocket(lambda request: [line({"type": "ERROR", "error": "background_busy",
                                                      "of": request["type"], "id": request["id"]})])
        acceptance.client = persistence.benchmark.Client(transport)
        with self.assertRaisesRegex(persistence.AcceptanceError, "bounded read retry budget"):
            acceptance.request("GET_DEVICE_INFO")
        self.assertEqual(len(transport.commands), 3)
        self.assertTrue(acceptance.result["records"][-1]["retry_exhausted"])

    def test_other_errors_and_mutations_never_use_busy_retry(self):
        cases = [
            ("GET_DEVICE_INFO", {"type": "ERROR", "error": "storage_error", "of": "GET_DEVICE_INFO"}),
            ("GET_DEVICE_INFO", {"type": "ERROR", "error": "background_busy", "of": "GET_GLOBAL"}),
            ("GET_DEVICE_INFO", {"type": "ERROR", "error": "background_busy"}),
            ("GET_DEVICE_INFO", {"type": "ACK", "error": "background_busy", "of": "GET_DEVICE_INFO"}),
            ("GET_DEVICE_INFO", TimeoutError("USB response missing")),
            ("GET_DEVICE_INFO", ConnectionError("USB disconnected")),
            ("GET_DEVICE_INFO", b'{"type":"ERROR","error":"background_busy",broken}\n'),
            ("PUT_GLOBAL", {"type": "ERROR", "error": "background_busy", "of": "PUT_GLOBAL"}),
            ("REBOOT", {"type": "ERROR", "error": "background_busy", "of": "REBOOT"}),
            ("SWITCH_PATCH", {"type": "ERROR", "error": "background_busy", "of": "SWITCH_PATCH"}),
        ]
        for kind, response in cases:
            with self.subTest(kind=kind, response=response):
                acceptance = persistence.Acceptance(self.args(), self.snapshot)
                def build(request):
                    return [line(dict(response, id=request["id"])) if isinstance(response, dict) else response]
                transport = FakeSocket(build)
                acceptance.client = persistence.benchmark.Client(transport)
                with self.assertRaises((RuntimeError, ValueError, OSError)):
                    acceptance.request(kind)
                self.assertEqual(len(transport.commands), 1)
                self.assertEqual(len(acceptance.result["records"]), 1)
                self.assertNotIn("retry", acceptance.result["records"][0])
        self.assertFalse(persistence.retryable_read_busy("GET_DEVICE_INFO", RuntimeError(
            "GET_DEVICE_INFO: {'type': 'ERROR', 'error': 'background_busy', 'of': 'GET_DEVICE_INFO'}")))

    def test_circuitpython_identity_blocks_all_mutations(self):
        device = Device(self.snapshot)
        device.native = False
        result = self.run_device(device, "--exercise-writes")
        self.assertFalse(result["passed"])
        self.assertIn("native firmware required", result["errors"][0]["message"])
        self.assertEqual(device.writes + device.reboots, 0)
        self.assertEqual(len(device.clients), 1)

    def test_snapshot_mismatch_prevents_write_or_reset(self):
        device = Device(self.snapshot)
        device.device["unknown"]["preserved"].append(3)
        result = self.run_device(device, "--exercise-writes")
        self.assertFalse(result["passed"])
        self.assertFalse(result["restoration"]["needed"])
        self.assertEqual(device.writes + device.reboots, 0)

    def test_lost_temporary_ack_still_restores_durable_original(self):
        device = Device(self.snapshot)
        device.lose_temporary_ack = True
        result = self.run_device(device, "--exercise-writes")
        self.assertFalse(result["passed"])
        self.assertEqual(result["errors"][0]["phase"], "temporary_write")
        self.assertTrue(result["restoration"]["succeeded"])
        self.assertEqual(device.persisted, self.snapshot["original"])

    def test_lost_restoration_ack_is_resolved_by_exact_persistent_readback(self):
        device = Device(self.snapshot)
        device.lose_restore_ack = True
        result = self.run_device(device, "--exercise-writes")
        self.assertTrue(result["passed"], result["errors"])
        self.assertTrue(result["restoration"]["succeeded"])
        self.assertIn("ack_disconnect_or_timeout", result["restoration"])

    def test_expected_reboot_disconnect_is_not_an_unexpected_failure(self):
        device = Device(self.snapshot)
        device.lose_reboot_ack = True
        result = self.run_device(device, "--exercise-writes")
        self.assertTrue(result["passed"], result["errors"])
        self.assertTrue(all(r["proved"] and not r["ack_received"] for r in result["expected_reboots"]))

    def test_lost_rig_ack_can_be_resolved_by_fresh_kemper_confirmation(self):
        device = Device(self.snapshot)
        device.lose_rig_ack = True
        result = self.run_device(device, "--exercise-writes")
        self.assertTrue(result["passed"], result["errors"])
        self.assertTrue(result["restoration"]["original_rig_restored"])
        self.assertIn("rig_ack_disconnect_or_timeout", result["restoration"])

    def test_early_reboot_with_larger_post_recovery_uptime_uses_boot_epoch_bounds(self):
        device = Device(self.snapshot)
        device.uptime = 100
        # Before reboot the device booted by host time100.0. A delayed
        # reconnect sees120ms uptime at100.2, so its new boot cannot precede
        #100.08 even though120ms exceeds the old pre-reboot10ms reading.
        with patch.object(persistence.time, "monotonic", return_value=100.2):
            acceptance = persistence.Acceptance(self.args(), self.snapshot, device.connect)
            _, stats = acceptance.recover(before_uptime=10, boot_epoch_upper=100.0)
            self.assertGreater(stats["uptime_ms"], 10)
            self.assertEqual(acceptance.result["recovery"][0]["reboot_proof"], "boot epoch intervals advanced")
            acceptance.close()

    def test_volatile_only_write_is_detected_after_actual_reboot(self):
        device = Device(self.snapshot)
        device.persist_temporary = False
        result = self.run_device(device, "--exercise-writes")
        self.assertFalse(result["passed"])
        self.assertEqual(result["errors"][0]["phase"], "temporary_persisted_readback")
        self.assertTrue(result["restoration"]["succeeded"])

    def test_reconnect_without_reboot_cannot_prove_persistence_and_recovery_is_bounded(self):
        device = Device(self.snapshot)
        device.ignore_first_reboot = True
        result = self.run_device(device, "--exercise-writes")
        self.assertFalse(result["passed"])
        self.assertFalse(result["expected_reboots"][0]["proved"])
        self.assertEqual(len(result["recovery"][1]["attempts"]), 3)
        self.assertTrue(result["restoration"]["succeeded"])

    def test_failed_json_restore_does_not_skip_independent_rig_restore(self):
        device = Device(self.snapshot)
        device.fail_restore_write = True
        result = self.run_device(device, "--exercise-writes")
        self.assertFalse(result["passed"])
        self.assertFalse(result["restoration"]["succeeded"])
        self.assertTrue(result["restoration"]["original_rig_restored"])
        self.assertEqual((device.bank, device.slot), (1, 3))

    def test_link_down_can_retry_but_malformed_json_is_never_hidden(self):
        device = Device(self.snapshot)
        device.link_down_connections = 2
        result = self.run_device(device)
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(len(result["recovery"][0]["attempts"]), 3)
        device = Device(self.snapshot)
        device.corrupt_connections = {0}
        result = self.run_device(device)
        self.assertFalse(result["passed"])
        self.assertEqual(len(device.clients), 1)
        self.assertEqual(result["errors"][0]["type"], "JSONDecodeError")

    def test_no_format_or_writes_when_native_storage_is_unavailable(self):
        device = Device(self.snapshot)
        device.storage_ready = False
        result = self.run_device(device, "--exercise-writes")
        self.assertFalse(result["passed"])
        self.assertEqual(device.writes + device.reboots, 0)

    def test_duplicate_or_incomplete_snapshot_and_invalid_color_rejected(self):
        for payload in ({"records": []}, {"records": fixture()["records"] * 2}):
            self.path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                persistence.load_snapshot(self.path)
        original = copy.deepcopy(self.snapshot["original"])
        original["tft"]["layout"][0]["color"] = "not-a-color"
        with self.assertRaises(ValueError):
            persistence.changed_color(original)

    def test_existing_artifact_is_not_overwritten_or_hardware_opened(self):
        output = self.path.parent / "artifact.json"
        output.write_text("preserve", encoding="utf-8")
        with patch.object(persistence, "Acceptance") as acceptance:
            with self.assertRaises(FileExistsError):
                persistence.main(["--snapshot", str(self.path), "--output", str(output)])
            acceptance.assert_not_called()
        self.assertEqual(output.read_text(encoding="utf-8"), "preserve")


if __name__ == "__main__":
    unittest.main()
