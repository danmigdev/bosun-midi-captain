#!/usr/bin/env python3
"""Contract tests for the hardware smoke runner."""

import contextlib
import io
import unittest

import stress


class FakeClient:
    def __init__(self, overrides=None):
        self.calls = []
        self.overrides = overrides or {}

    def call_sync(self, request_type, timeout=2.0, **kwargs):
        self.calls.append((request_type, timeout, kwargs))
        if request_type in self.overrides:
            return self.overrides[request_type]
        responses = {
            "PING": {"type": "ACK", "id": "1"},
            "GET_DEVICE_INFO": {"type": "DEVICE_INFO", "current": {}},
            "GET_MANIFEST": {
                "type": "MANIFEST", "core_messages": {}, "plugins": {},
            },
            "LIST_PATCHES": {"type": "PATCH_LIST", "patches": []},
            "GET_DIRTY": {"type": "DIRTY", "patches": []},
            "GET_MIDI_LEARN": {"type": "MIDI_LEARN", "table": {}},
            "STATS": {
                "type": "STATS", "uptime_ms": 1000,
                "mem_free": 4096, "loop_iters": 10,
            },
        }
        return responses[request_type]


class SmokeContractTests(unittest.TestCase):
    def test_smoke_allows_real_worst_case_tick_and_checks_every_command(self):
        client = FakeClient()
        with contextlib.redirect_stdout(io.StringIO()):
            stress.smoke(client)

        self.assertEqual(
            [call[0] for call in client.calls],
            ["PING", "GET_DEVICE_INFO", "GET_MANIFEST", "LIST_PATCHES",
             "GET_DIRTY", "GET_MIDI_LEARN", "STATS"],
        )
        for request_type, timeout, _kwargs in client.calls:
            minimum = (stress.SMOKE_MANIFEST_TIMEOUT
                       if request_type == "GET_MANIFEST"
                       else stress.SMOKE_FAST_TIMEOUT)
            self.assertGreaterEqual(timeout, minimum, (request_type, timeout))
        self.assertGreater(
            stress.SMOKE_FAST_TIMEOUT, 3.612,
            "fast timeout regressed below the measured Captain max tick",
        )

    def test_correlated_error_fails_immediately_instead_of_looking_valid(self):
        client = FakeClient({
            "GET_DEVICE_INFO": {
                "type": "ERROR", "id": "2", "error": "device_failed",
            },
        })
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(
                    RuntimeError, "GET_DEVICE_INFO returned 'ERROR'"):
                stress.smoke(client)
        self.assertEqual([call[0] for call in client.calls],
                         ["PING", "GET_DEVICE_INFO"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
