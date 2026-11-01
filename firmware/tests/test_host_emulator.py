"""Exercise the compiled application over real loopback sockets, without hardware."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import select
import socket
import subprocess
import tempfile
import time
import unittest


def seed(root):
    profile = root / "config/profiles/test"
    (profile / "patches/01").mkdir(parents=True)
    files = {
        root / "config/active_profile.json": {"id": "test"},
        profile / "manifest.json": {"name": "Offline test", "kind": "generic_midi"},
        profile / "device.json": {
            "device_name": "Offline Captain", "autosave": {"enabled": False},
            "preset_navigation": {"switch_to_slot": {"A": 1, "B": 2}},
        },
        profile / "patches/01/01.json": {"name": "CLEAN", "bindings": []},
        profile / "patches/01/02.json": {"name": "CRUNCH", "bindings": []},
    }
    for path, value in files.items():
        path.write_text(json.dumps(value), encoding="utf-8")
    (root / "untouched.txt").write_text("original sentinel", encoding="utf-8")


class Emulator:
    def __init__(self, executable, root, chunk=256, log=None):
        command = [str(executable), "--root", str(root), "--port", "0", "--io-chunk", str(chunk)]
        if log:
            command += ["--midi-log", str(log)]
        self.errors = tempfile.TemporaryFile(mode="w+")
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                        stderr=self.errors, text=True)
        try:
            if not select.select([self.process.stdout], [], [], 15)[0]:
                raise AssertionError("emulator did not announce readiness")
            line = self.process.stdout.readline()
            match = re.fullmatch(r"READY tcp://127\.0\.0\.1:(\d+) storage=ready\n", line)
            if not match:
                self.errors.seek(0)
                raise AssertionError(f"invalid readiness: {line!r}\n{self.errors.read()}")
            self.port = int(match[1])
        except BaseException:
            self.close(check=False)
            raise

    def close(self, check=True):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            code = self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            code = self.process.wait(timeout=5)
        self.errors.seek(0)
        diagnostic = self.errors.read()
        self.errors.close()
        self.process.stdout.close()
        if check:
            assert code == 0, (code, diagnostic)
            assert "AddressSanitizer" not in diagnostic and "runtime error:" not in diagnostic, diagnostic


class Client:
    def __init__(self, port):
        self.socket = socket.create_connection(("127.0.0.1", port), timeout=15)
        self.stream = self.socket.makefile("rb")
        self.counter = 0
        self.events = []

    def close(self):
        self.stream.close()
        self.socket.close()

    def send(self, message):
        self.socket.sendall(json.dumps(message, separators=(",", ":")).encode() + b"\n")

    def receive(self, request_id):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self.socket.settimeout(max(0.01, deadline - time.monotonic()))
            line = self.stream.readline(65536)
            assert line.endswith(b"\n"), ("closed/truncated response", line[:200])
            message = json.loads(line)
            if message.get("id") == request_id:
                return message
            self.events.append(message)
        raise AssertionError(f"response timeout for {request_id!r}")

    def screensaver_event(self):
        """The oldest unsolicited SCREENSAVER frame, buffered or still to come."""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            for index, message in enumerate(self.events):
                if message.get("type") == "SCREENSAVER":
                    return self.events.pop(index)
            self.events.append(self.receive(None))
        raise AssertionError("no SCREENSAVER event")

    def request(self, kind, expected, **fields):
        self.counter += 1
        request_id = f"socket-{self.counter}"
        self.send(dict(type=kind, id=request_id, **fields))
        response = self.receive(request_id)
        assert response["type"] == expected, response
        return response


class HostApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        seed(self.root)

    def start(self, chunk=256, log=None):
        emulator = Emulator(ARGS.emulator, self.root, chunk, log)
        self.addCleanup(emulator.close)
        client = Client(emulator.port)
        self.addCleanup(client.close)
        return emulator, client

    def test_boot_never_creates_missing_root(self):
        missing = self.root / "missing"
        result = subprocess.run([str(ARGS.emulator), "--root", str(missing)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(missing.exists())
        self.assertEqual((self.root / "untouched.txt").read_text(), "original sentinel")

    def test_fragmented_large_roundtrip_and_durable_save(self):
        _, client = self.start(chunk=7)
        self.assertEqual(client.request("GET_DEVICE_INFO", "DEVICE_INFO")["profile"], "test")
        patch = {"name": "unicode \u00e9 \u266a", "bindings": [], "unknown": "x" * 24000}
        client.request("PUT_PATCH", "ACK", bank=1, slot=2, patch=patch)
        self.assertEqual(client.request("GET_PATCH", "PATCH", bank=1, slot=2)["patch"], patch)
        client.request("SAVE_NOW", "SAVED")
        saved = self.root / "config/profiles/test/patches/01/02.json"
        self.assertEqual(json.loads(saved.read_text()), patch)
        self.assertEqual((self.root / "untouched.txt").read_text(), "original sentinel")

    def test_macro_midi_on_both_outputs_and_reboot_ack(self):
        midi_log = self.root / "midi-test.log"
        emulator, client = self.start(chunk=1, log=midi_log)
        patch = {"name": "macro", "bindings": [], "on_enter": {"messages": [
            {"type": "cc", "channel": 3, "cc": 7, "value": 99},
            {"type": "delay", "ms": 5}, {"type": "pc", "channel": 3, "program": 42}]}}
        client.request("PUT_PATCH", "ACK", bank=1, slot=2, patch=patch)
        client.request("SWITCH_PATCH", "ACK", bank=1, slot=2)
        client.request("PING", "ACK")
        streams = {"USB": bytearray(), "DIN": bytearray()}
        for line in midi_log.read_text().splitlines():
            port, *data = line.split()
            streams[port].extend(int(byte, 16) for byte in data)
        expected = bytes([0xb2, 7, 99, 0xc2, 42])
        self.assertEqual(streams["USB"], expected)
        self.assertEqual(streams["DIN"], expected)
        client.request("REBOOT", "ACK")
        self.assertEqual(emulator.process.wait(timeout=5), 0)

    def test_default_hardware_is_the_10_switch_captain_and_boot_writes_no_record(self):
        _, client = self.start()
        hardware = client.request("GET_DEVICE_INFO", "DEVICE_INFO")["hardware"]
        self.assertEqual((hardware["model"], hardware["configured"]), ("captain10", False))
        self.assertEqual(hardware["rows"], [["1", "2", "3", "4", "up"], ["A", "B", "C", "D", "down"]])
        self.assertEqual(len(client.request("LED_DUMP", "LED_DUMP")["pixels"]), 30)
        self.assertFalse((self.root / "config/hardware.json").exists())

    def test_mini6_record_selects_its_layout_until_set_hardware_restarts(self):
        record = self.root / "config/hardware.json"
        record.write_text(json.dumps({"version": 1, "model": "mini6"}), encoding="utf-8")
        emulator, client = self.start()
        info = client.request("GET_DEVICE_INFO", "DEVICE_INFO")
        self.assertEqual(info["device"], "Offline Captain")
        self.assertEqual(info["hardware"], {
            "model": "mini6", "name": "MIDI Captain Mini 6", "configured": True,
            "switches": ["1", "2", "3", "A", "B", "C"], "rows": [["1", "2", "3"], ["A", "B", "C"]],
            "led_count": 18, "expression_jacks": 0, "models": ["captain10", "mini6"],
        })
        leds = client.request("LED_DUMP", "LED_DUMP")
        self.assertEqual(len(leds["pixels"]), 18)
        self.assertEqual(leds["switch_indices"]["C"], [15, 17, 16])
        self.assertNotIn("up", leds["switch_indices"])
        rejected = client.request("ACTIVATE_SWITCH", "ERROR", switch="up", bank=1, slot=1)
        self.assertEqual(rejected["error"], "invalid_request")
        context = client.request("GET_CONTEXT", "CONTEXT")["context"]
        self.assertEqual(list(context["switches"]), ["1", "2", "3", "A", "B", "C"])
        # Confirming the running model persists without a restart.
        self.assertFalse(client.request("SET_HARDWARE", "ACK", model="mini6")["reboot"])
        self.assertIsNone(emulator.process.poll())
        self.assertEqual(client.request("SET_HARDWARE", "ERROR", model="nano4")["error"], "unsupported_hardware")
        acknowledged = client.request("SET_HARDWARE", "ACK", model="captain10")
        self.assertEqual((acknowledged["model"], acknowledged["reboot"]), ("captain10", True))
        self.assertEqual(emulator.process.wait(timeout=5), 0)
        self.assertEqual(json.loads(record.read_text(encoding="utf-8")), {"version": 1, "model": "captain10"})
        _, client = self.start()
        hardware = client.request("GET_DEVICE_INFO", "DEVICE_INFO")["hardware"]
        self.assertEqual((hardware["model"], hardware["configured"]), ("captain10", True))
        self.assertEqual(len(client.request("LED_DUMP", "LED_DUMP")["pixels"]), 30)
        self.assertEqual((self.root / "untouched.txt").read_text(), "original sentinel")

    def test_screensaver_preview_and_wake(self):
        _, client = self.start()
        self.assertIs(client.request("GET_DEVICE_INFO", "DEVICE_INFO")["screensaver"], True)
        state = client.request("GET_SCREENSAVER", "SCREENSAVER")
        self.assertEqual(state, {"type": "SCREENSAVER", "id": state["id"], "active": False})
        rejected = client.request("SCREENSAVER_START", "ERROR", effect="sparkle")
        self.assertEqual(rejected["error"], "invalid_request")
        client.request("SCREENSAVER_START", "ACK", effect="breathe")
        self.assertEqual(client.screensaver_event(),
                         {"type": "SCREENSAVER", "id": None, "active": True, "effect": "breathe"})
        # Polling and reads leave it on; a state-changing request ends it.
        client.request("PING", "ACK")
        client.request("STATS", "STATS")
        state = client.request("GET_SCREENSAVER", "SCREENSAVER")
        self.assertEqual((state["active"], state["effect"]), (True, "breathe"))
        client.request("SET_MIDI_MONITOR", "ACK", on=False)
        self.assertEqual(client.screensaver_event(), {"type": "SCREENSAVER", "id": None, "active": False})
        self.assertFalse(client.request("GET_SCREENSAVER", "SCREENSAVER")["active"])

    def test_startup_logo_upload_commit_and_delete(self):
        _, client = self.start()
        self.assertEqual(client.request("GET_DEVICE_INFO", "DEVICE_INFO")["boot_logo"], "default")
        image = bytes((i * 31 + (i >> 9)) & 255 for i in range(96000))
        for offset in range(0, len(image), 12288):
            chunk = base64.b64encode(image[offset:offset + 12288]).decode()
            reply = client.request("PUT_LOGO", "ACK", offset=offset, data=chunk)
            self.assertEqual(reply["received"], min(offset + 12288, len(image)))
        fnv1a = 0x811c9dc5
        for byte in image:
            fnv1a = ((fnv1a ^ byte) * 0x01000193) & 0xffffffff
        rejected = client.request("COMMIT_LOGO", "ERROR", size=len(image), fnv1a=fnv1a ^ 1)
        self.assertEqual(rejected["error"], "checksum_mismatch")
        client.request("COMMIT_LOGO", "ACK", size=len(image), fnv1a=fnv1a)
        self.assertEqual(client.request("GET_DEVICE_INFO", "DEVICE_INFO")["boot_logo"], "custom")
        logo = self.root / "config/boot_logo/tft.rgb565"
        self.assertEqual(logo.read_bytes(), image)
        client.request("DELETE_LOGO", "ACK")
        self.assertEqual(list(logo.parent.iterdir()), [])
        self.assertEqual(client.request("GET_DEVICE_INFO", "DEVICE_INFO")["boot_logo"], "default")

    def test_batched_requests_and_reconnect_drop_partial_session(self):
        emulator, client = self.start()
        for iteration in range(100):
            for number in range(10):
                client.send({"type": "PING", "id": f"batch-{iteration}-{number}"})
            for number in range(10):
                self.assertEqual(client.receive(f"batch-{iteration}-{number}")["type"], "ACK")
        stats = client.request("STATS", "STATS")
        self.assertEqual(stats["protocol_errors"], 0)
        client.socket.sendall(b'{"type":"REBOOT","id":"old')
        client.close()
        time.sleep(0.1)
        replacement = Client(emulator.port)
        self.addCleanup(replacement.close)
        replacement.request("PING", "ACK")
        self.assertIsNone(emulator.process.poll())
        self.assertFalse(any(message.get("id") == "old" for message in replacement.events))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emulator", type=Path, required=True)
    ARGS, remaining = parser.parse_known_args()
    ARGS.emulator = ARGS.emulator.resolve(strict=True)
    unittest.main(argv=[os.path.basename(__file__)] + remaining, verbosity=2)
