"""Exercise the compiled application over real loopback sockets, without hardware."""
import argparse
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
