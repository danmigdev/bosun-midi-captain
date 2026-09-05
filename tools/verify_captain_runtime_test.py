#!/usr/bin/env python3
"""Offline readiness tests for verify_captain_runtime.py."""

import importlib.util
import json
import sys
import types
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().with_name("verify_captain_runtime.py")


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        if duration < 0:
            raise AssertionError("negative sleep")
        self.now += duration


class FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.input = bytearray()
        self.output = []
        self.requests = []
        self.closed = 0

    def write(self, data):
        payload = bytes(data)
        self.input.extend(payload)
        while b"\n" in self.input:
            raw, _, tail = bytes(self.input).partition(b"\n")
            self.input[:] = tail
            if not raw.strip():
                continue
            request = json.loads(raw)
            self.requests.append(request)
            response = self.handler(request)
            if response is not None:
                self.output.append(
                    (json.dumps(response, separators=(",", ":")) + "\n").encode()
                )
        return len(payload)

    def read(self, _size):
        return self.output.pop(0) if self.output else b""

    def close(self):
        self.closed += 1


class VerifyCaptainRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fake_push = types.ModuleType("push_firmware")
        fake_push.open_transport = lambda *_args, **_kwargs: None
        spec = importlib.util.spec_from_file_location(
            "verify_captain_runtime_under_test", SCRIPT,
        )
        module = importlib.util.module_from_spec(spec)
        previous = sys.modules.get("push_firmware")
        sys.modules["push_firmware"] = fake_push
        try:
            spec.loader.exec_module(module)
        finally:
            if previous is None:
                sys.modules.pop("push_firmware", None)
            else:
                sys.modules["push_firmware"] = previous
        cls.module = module

    @staticmethod
    def valid_response(request):
        kind = request["type"]
        ident = request["id"]
        if kind == "PING":
            return {"type": "ACK", "id": ident}
        if kind == "GET_DEVICE_INFO":
            return {
                "type": "DEVICE_INFO", "id": ident, "fw": "test",
                "profile": "default", "current": {"bank": 1, "slot": 2},
                "preset_navigation": {},
            }
        if kind == "STATS":
            return {"type": "STATS", "id": ident, "mem_free": 7000}
        if kind == "LIST_PATCHES":
            return {
                "type": "PATCH_LIST", "id": ident,
                "patches": [{"bank": 1, "slot": 2, "name": "Clean"}],
            }
        if kind == "GET_CONTEXT":
            return {
                "type": "CONTEXT", "id": ident,
                "context": {"kemper_block_X": "on"},
            }
        raise AssertionError("unexpected request " + kind)

    def run_main(self, transport, clock, timeout="1", standalone=False,
                 opener=None):
        if opener is None:
            opener = mock.Mock(return_value=transport)
        argv = [str(SCRIPT), "--port", "socket://hub:9876",
                "--timeout", timeout]
        if standalone:
            argv.append("--standalone")
        output = StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(self.module, "open_transport", opener), \
                mock.patch.object(self.module, "time", clock), \
                redirect_stdout(output):
            self.module.main()
        return output.getvalue(), opener

    def test_initial_link_down_is_retried_until_ack(self):
        pings = 0

        def handler(request):
            nonlocal pings
            if request["type"] == "PING":
                pings += 1
                if pings <= 2:
                    return {
                        "type": "ERROR", "id": request["id"],
                        "error": "link_down", "of": "PING",
                    }
            return self.valid_response(request)

        clock = FakeClock()
        transport = FakeTransport(handler)
        output, opener = self.run_main(transport, clock)

        self.assertIn('"fw": "test"', output)
        self.assertEqual(pings, 3)
        self.assertEqual(opener.call_count, 1)
        self.assertAlmostEqual(clock.now, 0.2)
        self.assertEqual(transport.closed, 1)

    def test_transient_open_failures_share_the_same_deadline(self):
        clock = FakeClock()
        transport = FakeTransport(self.valid_response)
        opener = mock.Mock(side_effect=[
            OSError("refused"), OSError("refused"), transport,
        ])

        output, _ = self.run_main(transport, clock, opener=opener)

        self.assertIn('"patches": 1', output)
        self.assertEqual(opener.call_count, 3)
        self.assertAlmostEqual(clock.now, 0.2)

    def test_non_link_down_ping_error_fails_without_retry(self):
        def handler(request):
            return {
                "type": "ERROR", "id": request["id"],
                "error": "exception", "detail": "MemoryError",
            }

        clock = FakeClock()
        transport = FakeTransport(handler)
        with self.assertRaisesRegex(RuntimeError, "MemoryError"):
            self.run_main(transport, clock)

        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(transport.closed, 1)
        self.assertEqual(clock.now, 0)

    def test_initial_link_down_cannot_exceed_global_deadline(self):
        def handler(request):
            return {
                "type": "ERROR", "id": request["id"],
                "error": "link_down", "of": "PING",
            }

        clock = FakeClock()
        transport = FakeTransport(handler)
        with self.assertRaisesRegex(TimeoutError, "link_down"):
            self.run_main(transport, clock, timeout="0.25")

        self.assertAlmostEqual(clock.now, 0.25)
        self.assertGreaterEqual(len(transport.requests), 2)
        self.assertEqual(transport.closed, 1)

    def test_open_failures_cannot_exceed_global_deadline(self):
        clock = FakeClock()
        opener = mock.Mock(side_effect=OSError("connection refused"))
        with self.assertRaisesRegex(TimeoutError, "connection refused"):
            self.run_main(None, clock, timeout="0.25", opener=opener)

        self.assertAlmostEqual(clock.now, 0.25)
        self.assertGreaterEqual(opener.call_count, 2)

    def test_link_down_after_readiness_is_not_retried(self):
        def handler(request):
            if request["type"] == "GET_DEVICE_INFO":
                return {
                    "type": "ERROR", "id": request["id"],
                    "error": "link_down", "of": "GET_DEVICE_INFO",
                }
            return self.valid_response(request)

        clock = FakeClock()
        transport = FakeTransport(handler)
        with self.assertRaisesRegex(RuntimeError, "expected DEVICE_INFO"):
            self.run_main(transport, clock)

        self.assertEqual(
            [request["type"] for request in transport.requests],
            ["PING", "GET_DEVICE_INFO"],
        )
        self.assertEqual(clock.now, 0)

    def test_later_request_uses_remaining_global_budget(self):
        def handler(request):
            if request["type"] == "GET_DEVICE_INFO":
                return None
            return self.valid_response(request)

        clock = FakeClock()
        transport = FakeTransport(handler)
        with self.assertRaisesRegex(TimeoutError, "GET_DEVICE_INFO"):
            self.run_main(transport, clock, timeout="0.3")

        self.assertAlmostEqual(clock.now, 0.3)
        self.assertEqual(
            [request["type"] for request in transport.requests],
            ["PING", "GET_DEVICE_INFO"],
        )

    def test_standalone_still_checks_exactly_twenty_pings_and_context(self):
        clock = FakeClock()
        transport = FakeTransport(self.valid_response)

        output, _ = self.run_main(
            transport, clock, standalone=True,
        )

        kinds = [request["type"] for request in transport.requests]
        self.assertEqual(kinds.count("PING"), 20)
        self.assertEqual(kinds[-1], "GET_CONTEXT")
        self.assertIn('"pings": 20', output)
        self.assertIn('"kemper_block_X": "on"', output)

    def test_non_positive_or_non_finite_timeout_is_rejected_before_open(self):
        for timeout in ("0", "-1", "nan", "inf"):
            with self.subTest(timeout=timeout):
                clock = FakeClock()
                opener = mock.Mock()
                with self.assertRaises(SystemExit):
                    self.run_main(None, clock, timeout=timeout, opener=opener)
                opener.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
