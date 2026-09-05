#!/usr/bin/env python3
"""Offline safety tests for the firmware deployment helpers."""

import importlib.util
import base64
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().with_name("push_firmware.py")
LEGACY_SCRIPT = Path(__file__).resolve().with_name("flash_firmware.py")
REPL_WRITER = Path(__file__).resolve().with_name("write_via_repl.py")
REPL_READER = Path(__file__).resolve().with_name("read_repl.py")


class PushFirmwareTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.serial_calls = []
        cls.url_calls = []
        cls.com_transport = object()
        cls.url_transport = object()

        fake_serial = types.ModuleType("serial")

        def open_serial(*args, **kwargs):
            cls.serial_calls.append((args, kwargs))
            return cls.com_transport

        def open_url(*args, **kwargs):
            cls.url_calls.append((args, kwargs))
            return cls.url_transport

        fake_serial.Serial = open_serial
        fake_serial.serial_for_url = open_url
        spec = importlib.util.spec_from_file_location("push_firmware_under_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        previous = sys.modules.get("serial")
        sys.modules["serial"] = fake_serial
        try:
            spec.loader.exec_module(module)
        finally:
            if previous is None:
                sys.modules.pop("serial", None)
            else:
                sys.modules["serial"] = previous
        cls.module = module

        # Load the compatibility frontend against this exact canonical module
        # so delegation, rather than merely similar behaviour, is tested.
        legacy_spec = importlib.util.spec_from_file_location(
            "flash_firmware_under_test", LEGACY_SCRIPT,
        )
        legacy_module = importlib.util.module_from_spec(legacy_spec)
        previous_push = sys.modules.get("push_firmware")
        sys.modules["push_firmware"] = module
        try:
            legacy_spec.loader.exec_module(legacy_module)
        finally:
            if previous_push is None:
                sys.modules.pop("push_firmware", None)
            else:
                sys.modules["push_firmware"] = previous_push
        cls.legacy_module = legacy_module

    def setUp(self):
        self.serial_calls.clear()
        self.url_calls.clear()

    def test_com_port_preserves_direct_serial_transport(self):
        result = self.module.open_transport("COM4")

        self.assertIs(result, self.com_transport)
        self.assertEqual(self.serial_calls, [(
            ('COM4', 115200),
            {'timeout': 0.1, 'write_timeout': self.module.WRITE_TIMEOUT_S},
        )])
        self.assertEqual(self.url_calls, [])

    def test_socket_url_uses_pyserial_url_handler(self):
        url = "socket://192.168.1.91:9876"
        result = self.module.open_transport(url)

        self.assertIs(result, self.url_transport)
        self.assertEqual(self.serial_calls, [])
        self.assertEqual(
            self.url_calls,
            [((url,), {
                'baudrate': 115200, 'timeout': 0.1,
                'write_timeout': self.module.WRITE_TIMEOUT_S,
            })],
        )

    def test_other_pyserial_urls_use_same_handler_and_options(self):
        result = self.module.open_transport(
            "loop://", baudrate=57600, timeout=0.25, write_timeout=0.75,
        )

        self.assertIs(result, self.url_transport)
        self.assertEqual(
            self.url_calls,
            [(('loop://',), {
                'baudrate': 57600, 'timeout': 0.25,
                'write_timeout': 0.75,
            })],
        )

    def test_transport_rejects_unbounded_write_timeout(self):
        for value in (None, 0, -1, float("inf"), float("nan")):
            with self.subTest(write_timeout=value), self.assertRaisesRegex(
                ValueError, "write_timeout",
            ):
                self.module.open_transport("COM4", write_timeout=value)
        self.assertEqual(self.serial_calls, [])
        self.assertEqual(self.url_calls, [])

    def test_upload_chunks_stay_below_captain_heap_pressure_boundary(self):
        payload = bytes(range(256)) * 3
        calls = []
        original_call = self.module.call

        def ack(_transport, message, timeout=5.0):
            calls.append(message)
            return {"type": "ACK", "id": message["id"]}

        self.module.call = ack
        try:
            with tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / "protocol.mpy"
                source.write_bytes(payload)
                self.module.push_file(object(), source,
                                      "/lib/captain/protocol.mpy", [0])
        finally:
            self.module.call = original_call

        chunks = [base64.b64decode(m["data_b64"])
                  for m in calls if m["type"] == "PUT_FILE_CHUNK"]
        begin = next(m for m in calls if m["type"] == "PUT_FILE_BEGIN")
        self.assertEqual(begin["size"], len(payload))
        self.assertEqual(b"".join(chunks), payload)
        self.assertTrue(chunks)
        self.assertLessEqual(max(map(len, chunks)), 96)

    def test_lost_chunk_ack_continues_without_resending_and_keeps_integrity(self):
        """An ACK can vanish after the append; resending would duplicate data."""
        payload = bytes(range(251)) * 2
        commands = []
        staged = bytearray()
        original_call = self.module.call
        lost_offset = self.module.CHUNK_SIZE

        def protocol_call(_transport, message, timeout=5.0):
            commands.append(dict(message))
            kind = message["type"]
            if kind == "PUT_FILE_BEGIN":
                staged.clear()
                return {"type": "ACK", "id": message["id"],
                        "size_check": True, "size": message["size"]}
            if kind == "PUT_FILE_CHUNK":
                offset = len(staged)
                staged.extend(base64.b64decode(message["data_b64"]))
                if offset == lost_offset:
                    # The firmware committed the append, but its ACK was lost.
                    raise TimeoutError("simulated lost ACK after append")
                return {"type": "ACK", "id": message["id"]}
            if kind == "PUT_FILE_END":
                if len(staged) != len(payload):
                    return {
                        "type": "ERROR", "id": message["id"],
                        "error": "size_mismatch",
                    }
                return {"type": "ACK", "id": message["id"]}
            raise AssertionError(message)

        self.module.call = protocol_call
        warnings = StringIO()
        try:
            with tempfile.TemporaryDirectory() as tmp, redirect_stderr(warnings):
                source = Path(tmp) / "app.mpy"
                source.write_bytes(payload)
                self.module.push_file(
                    object(), source, "/lib/captain/app.mpy", [0],
                )
        finally:
            self.module.call = original_call

        chunks = [m for m in commands if m["type"] == "PUT_FILE_CHUNK"]
        self.assertEqual(bytes(staged), payload)
        self.assertEqual(
            b"".join(base64.b64decode(m["data_b64"]) for m in chunks),
            payload,
        )
        self.assertEqual(len(commands), 2 + len(chunks))
        self.assertIn(f"offset {lost_offset}", warnings.getvalue())
        # The uncertain chunk is sent exactly once.  Its unique request id is
        # never reused, so a late ACK cannot complete a different command.
        self.assertEqual(len({m["id"] for m in commands}), len(commands))

    def test_missing_chunk_command_is_caught_by_end_and_full_retry_recovers(self):
        """Only a genuine missing append should require restarting the file."""
        payload = bytes(range(256)) * 2
        attempts = []
        staged = bytearray()
        installed = bytearray()
        original_call = self.module.call
        drop_offset = self.module.CHUNK_SIZE * 2

        def protocol_call(_transport, message, timeout=5.0):
            kind = message["type"]
            if kind == "PUT_FILE_BEGIN":
                staged.clear()
                attempts.append([])
                return {"type": "ACK", "id": message["id"],
                        "size_check": True, "size": message["size"]}
            if kind == "PUT_FILE_CHUNK":
                chunk = base64.b64decode(message["data_b64"])
                logical_offset = sum(len(part) for part in attempts[-1])
                attempts[-1].append(chunk)
                if len(attempts) == 1 and logical_offset == drop_offset:
                    # Unlike a lost ACK, this command never reached firmware.
                    raise TimeoutError("simulated lost command")
                staged.extend(chunk)
                return {"type": "ACK", "id": message["id"]}
            if kind == "PUT_FILE_END":
                if len(staged) != message_expected_size[0]:
                    return {
                        "type": "ERROR", "id": message["id"],
                        "error": "size_mismatch", "expected": len(payload),
                        "actual": len(staged),
                    }
                installed[:] = staged
                return {"type": "ACK", "id": message["id"]}
            raise AssertionError(message)

        message_expected_size = [len(payload)]
        self.module.call = protocol_call
        try:
            with tempfile.TemporaryDirectory() as tmp, redirect_stderr(StringIO()):
                source = Path(tmp) / "app.mpy"
                source.write_bytes(payload)
                self.module.push_file_with_retries(
                    object(), source, "/lib/captain/app.mpy", [0],
                    retries=2, retry_delay=0,
                )
        finally:
            self.module.call = original_call

        self.assertEqual(len(attempts), 2)
        self.assertEqual(bytes(installed), payload)
        # Neither attempt retries an individual append.  Attempt 1 is shorter
        # on-device because one command vanished; attempt 2 is byte-exact.
        expected_chunks = [
            payload[offset:offset + self.module.CHUNK_SIZE]
            for offset in range(0, len(payload), self.module.CHUNK_SIZE)
        ]
        self.assertEqual(attempts[0], expected_chunks)
        self.assertEqual(attempts[1], expected_chunks)

    def test_legacy_firmware_restarts_immediately_after_uncertain_chunk(self):
        """A BEGIN ACK without size_check must never rely on legacy END."""
        payload = bytes(range(220))
        attempts = []
        staged = bytearray()
        installed = bytearray()
        end_calls = [0]
        original_call = self.module.call

        def protocol_call(_transport, message, timeout=5.0):
            kind = message["type"]
            if kind == "PUT_FILE_BEGIN":
                attempts.append([])
                staged.clear()
                # Exact shape returned by older Captain firmware: it accepts
                # the unknown request size but does not promise to use it.
                return {"type": "ACK", "id": message["id"]}
            if kind == "PUT_FILE_CHUNK":
                chunk = base64.b64decode(message["data_b64"])
                attempts[-1].append(chunk)
                staged.extend(chunk)
                if len(attempts) == 1 and len(attempts[-1]) == 2:
                    raise TimeoutError("legacy ACK lost after append")
                return {"type": "ACK", "id": message["id"]}
            if kind == "PUT_FILE_END":
                end_calls[0] += 1
                installed[:] = staged
                return {"type": "ACK", "id": message["id"]}
            raise AssertionError(message)

        self.module.call = protocol_call
        try:
            with tempfile.TemporaryDirectory() as tmp, redirect_stderr(StringIO()):
                source = Path(tmp) / "legacy.mpy"
                source.write_bytes(payload)
                self.module.push_file_with_retries(
                    object(), source, "/lib/legacy.mpy", [0],
                    retries=2, retry_delay=0,
                )
        finally:
            self.module.call = original_call

        self.assertEqual(len(attempts), 2)
        self.assertEqual(end_calls[0], 1, "unsafe legacy END ran on partial attempt")
        self.assertEqual(bytes(installed), payload)

    def test_ack_loss_matrix_preserves_boundaries_and_exact_payload(self):
        """Exercise empty, final-short and realistic compiled-module sizes."""
        sizes = (0, 1, 95, 96, 97, 511, 512, 13_557)
        original_call = self.module.call
        try:
            for size in sizes:
                with self.subTest(size=size):
                    payload = bytes((i * 37 + 11) & 0xFF for i in range(size))
                    staged = bytearray()
                    chunk_index = [0]

                    def protocol_call(_transport, message, timeout=5.0):
                        kind = message["type"]
                        if kind == "PUT_FILE_BEGIN":
                            staged.clear()
                            return {"type": "ACK", "id": message["id"],
                                    "size_check": True,
                                    "size": message["size"]}
                        elif kind == "PUT_FILE_CHUNK":
                            staged.extend(base64.b64decode(message["data_b64"]))
                            index = chunk_index[0]
                            chunk_index[0] += 1
                            # Lose ACKs at the start, around a boundary and at
                            # the final chunk.  The append itself still lands.
                            chunk_count = (size + self.module.CHUNK_SIZE - 1) \
                                // self.module.CHUNK_SIZE
                            if index in {0, 1, max(0, chunk_count - 1)}:
                                raise TimeoutError("matrix ACK loss")
                        elif kind == "PUT_FILE_END":
                            if len(staged) != size:
                                return {
                                    "type": "ERROR", "id": message["id"],
                                    "error": "size_mismatch",
                                }
                        return {"type": "ACK", "id": message["id"]}

                    self.module.call = protocol_call
                    with tempfile.TemporaryDirectory() as tmp, \
                            redirect_stderr(StringIO()), redirect_stdout(StringIO()):
                        source = Path(tmp) / "module.mpy"
                        source.write_bytes(payload)
                        self.module.push_file(
                            object(), source, "/lib/module.mpy", [0],
                        )
                    self.assertEqual(bytes(staged), payload)
        finally:
            self.module.call = original_call

    def test_call_ignores_duplicate_and_late_acks_without_id_confusion(self):
        old = b'{"type":"ACK","id":"old"}\n'
        current = b'{"type":"ACK","id":"current"}\n'
        following = b'{"type":"ACK","id":"following"}\n'

        class LateAckTransport:
            def __init__(self):
                # The first read contains a late ACK twice, then the requested
                # ACK and a duplicate of it.  The duplicate remains buffered
                # when the next command starts.
                self.chunks = [old + old + current + current, following]
                self.writes = []

            def write(self, data):
                self.writes.append(data)
                return len(data)

            def read(self, _size):
                return self.chunks.pop(0) if self.chunks else b""

        transport = LateAckTransport()
        response = self.module.call(
            transport, {"type": "PING", "id": "current"}, timeout=0.5,
        )
        self.assertEqual(response["id"], "current")

        response = self.module.call(
            transport, {"type": "PING", "id": "following"}, timeout=0.5,
        )
        self.assertEqual(response["id"], "following")
        self.assertEqual(len(transport.writes), 2)

    def test_call_completes_partial_writes_before_waiting_for_ack(self):
        class PartialWriteTransport:
            def __init__(self):
                self.received = bytearray()
                self.replies = []
                self.write_count = 0

            def write(self, data):
                self.write_count += 1
                accepted = min(7, len(data))
                self.received.extend(bytes(data[:accepted]))
                if self.received.endswith(b"\n"):
                    message = __import__("json").loads(self.received)
                    self.replies.append(
                        (f'{{"type":"ACK","id":"{message["id"]}"}}\n').encode()
                    )
                return accepted

            def read(self, _size):
                return self.replies.pop(0) if self.replies else b""

        transport = PartialWriteTransport()
        message = {"type": "PING", "id": "partial-write"}

        response = self.module.call(transport, message, timeout=0.1)

        self.assertEqual(response, {"type": "ACK", "id": "partial-write"})
        self.assertEqual(
            bytes(transport.received),
            (__import__("json").dumps(message) + "\n").encode(),
        )
        self.assertGreater(transport.write_count, 1)

    def test_call_fails_immediately_when_write_makes_no_progress(self):
        for stalled_result in (None, 0):
            with self.subTest(result=stalled_result):
                class StalledTransport:
                    def __init__(self):
                        self.read_called = False

                    def write(self, _data):
                        return stalled_result

                    def read(self, _size):
                        self.read_called = True
                        return b""

                transport = StalledTransport()
                with self.assertRaisesRegex(OSError, "made no progress"):
                    self.module.call(
                        transport, {"type": "PING", "id": "stalled"},
                        timeout=0.1,
                    )
                self.assertFalse(transport.read_called)

    def test_call_propagates_transport_write_error_without_reading(self):
        class BrokenWriteTransport:
            def __init__(self):
                self.read_called = False

            def write(self, _data):
                raise OSError("bounded write timed out")

            def read(self, _size):
                self.read_called = True
                return b""

        transport = BrokenWriteTransport()
        with self.assertRaisesRegex(OSError, "bounded write timed out"):
            self.module.call(
                transport, {"type": "PING", "id": "write-error"},
                timeout=0.1,
            )
        self.assertFalse(transport.read_called)

    def test_call_preserves_partial_unsolicited_line_between_requests(self):
        ack1 = b'{"type":"ACK","id":"one"}\n'
        event_prefix = b'{"type":"EVENT","event":"PATCH_CHANGED","patch":'
        event_suffix = b'7}\n'
        ack2 = b'{"type":"ACK","id":"two"}\n'

        class CoalescingTransport:
            def __init__(self):
                self.chunks = [ack1 + event_prefix, event_suffix + ack2]
                self.writes = []

            def write(self, data):
                self.writes.append(data)
                return len(data)

            def read(self, _size):
                return self.chunks.pop(0) if self.chunks else b""

        transport = CoalescingTransport()
        first = self.module.call(
            transport, {"type": "PING", "id": "one"}, timeout=0.5,
        )

        # The ACK and the beginning of the following event arrived in one
        # read. Returning the ACK must not throw that partial frame away.
        self.assertEqual(first, {"type": "ACK", "id": "one"})
        self.assertEqual(
            getattr(transport, self.module._RX_BUFFER_ATTR), event_prefix,
        )

        second = self.module.call(
            transport, {"type": "PING", "id": "two"}, timeout=0.5,
        )

        self.assertEqual(second, {"type": "ACK", "id": "two"})
        self.assertEqual(
            getattr(transport, self.module._RX_BUFFER_ATTR), bytearray(),
        )
        self.assertEqual(len(transport.writes), 2)

    def test_reboot_disconnect_after_complete_write_is_expected(self):
        import serial as real_serial

        class ResettingTransport:
            def __init__(self, error_type):
                self.received = bytearray()
                self.error_type = error_type

            def write(self, data):
                self.received.extend(bytes(data))
                return len(data)

            def read(self, _size):
                # pyserial reports the USB CDC disappearance this way on
                # Windows; SerialException is an OSError subclass.
                raise self.error_type("device rebooted")

        for error_type in (OSError, real_serial.SerialException):
            with self.subTest(error_type=error_type.__name__):
                transport = ResettingTransport(error_type)
                response = self.module.request_reboot(
                    transport, request_id="reset-test", timeout=0.1,
                )

                self.assertIsNone(response)
                frame = __import__("json").loads(transport.received)
                self.assertEqual(
                    frame, {"type": "REBOOT", "id": "reset-test"},
                )

    def test_reboot_write_failure_before_any_byte_is_not_masked(self):
        class NeverStartedTransport:
            def __init__(self):
                self.read_called = False

            def write(self, _data):
                raise OSError("port already disconnected")

            def read(self, _size):
                self.read_called = True
                return b""

        transport = NeverStartedTransport()
        with self.assertRaisesRegex(OSError, "already disconnected"):
            self.module.request_reboot(transport, timeout=0.1)
        self.assertFalse(transport.read_called)

    def test_reboot_partial_frame_write_failure_is_not_claimed_as_reset(self):
        class PartialWriteThenDisconnect:
            def __init__(self):
                self.received = bytearray()
                self.calls = 0

            def write(self, data):
                self.calls += 1
                if self.calls > 1:
                    raise OSError("disconnected mid-frame")
                accepted = min(4, len(data))
                self.received.extend(bytes(data[:accepted]))
                return accepted

            def read(self, _size):
                raise AssertionError("must not await ACK for a partial frame")

        transport = PartialWriteThenDisconnect()
        with self.assertRaisesRegex(OSError, "mid-frame"):
            self.module.request_reboot(transport, timeout=0.1)
        self.assertEqual(len(transport.received), 4)

    def test_full_deploy_installs_dependencies_then_app_then_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captain = root / "lib" / "captain"
            plugins = root / "lib" / "plugins"
            captain.mkdir(parents=True)
            plugins.mkdir(parents=True)
            (captain / "app.mpy").write_bytes(b"app")
            (captain / "store.mpy").write_bytes(b"store")
            (captain / "protocol.mpy").write_bytes(b"protocol")
            (root / "lib" / "captain_ota.mpy").write_bytes(b"ota")
            (plugins / "kemper.mpy").write_bytes(b"kemper")
            (root / "code.py").write_text("entry", encoding="utf-8")

            destinations = [destination for _, destination in
                            self.module.collect_files(root, None)]

        # A plain alphabetical sort puts app.mpy first and reproduces the
        # real mixed-version crash.  The deploy order must not do that.
        alphabetical_libraries = sorted(
            destination for destination in destinations
            if destination.startswith("/lib/")
        )
        self.assertEqual(
            alphabetical_libraries[0], "/lib/captain/app.mpy",
        )
        self.assertNotEqual(destinations[0], "/lib/captain/app.mpy")
        self.assertEqual(
            destinations,
            [
                "/lib/captain_ota.mpy",
                "/lib/captain/protocol.mpy",
                "/lib/captain/store.mpy",
                "/lib/plugins/kemper.mpy",
                "/lib/captain/app.mpy",
                "/code.py",
            ],
        )

    def test_restricted_deploy_reorders_app_first_input_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for relative in (
                "lib/captain/app.mpy",
                "lib/captain/store.mpy",
                "lib/plugins/kemper.mpy",
                "code.py",
                "lib/captain_ota.mpy",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(relative.encode())

            files = self.module.collect_files(
                root,
                [
                    "lib/captain/app.mpy",
                    "code.py",
                    "lib/captain/store.mpy",
                    "lib/plugins/kemper.mpy",
                    "lib/captain_ota.mpy",
                ],
            )

        self.assertEqual(
            [destination for _, destination in files],
            [
                "/lib/captain_ota.mpy",
                "/lib/captain/store.mpy",
                "/lib/plugins/kemper.mpy",
                "/lib/captain/app.mpy",
                "/code.py",
            ],
        )

    def test_explicit_missing_file_fails_before_transport_is_opened(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                sys,
                "argv",
                [
                    str(SCRIPT),
                    "--port", "socket://127.0.0.1:9876",
                    "--firmware", tmp,
                    "--files", "lib/missing.mpy",
                ],
            ), mock.patch.object(self.module, "open_transport") as opener:
                with self.assertRaisesRegex(
                    SystemExit, "requested firmware file not found",
                ):
                    self.module.main()

        opener.assert_not_called()

    def test_main_closes_transport_when_file_upload_raises(self):
        class ClosingTransport:
            def __init__(self):
                self.close_count = 0

            def close(self):
                self.close_count += 1

        transport = ClosingTransport()
        with tempfile.TemporaryDirectory() as tmp:
            firmware = Path(tmp)
            (firmware / "code.py").write_text("raise SystemExit", encoding="utf-8")
            argv = [
                str(SCRIPT), "--port", "COM4", "--firmware", tmp,
                "--no-reboot",
            ]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(
                        self.module, "open_transport", return_value=transport,
                    ), mock.patch.object(self.module.time, "sleep"), \
                    mock.patch.object(
                        self.module, "call",
                        return_value={"type": "ACK", "id": "ping"},
                    ), mock.patch.object(
                        self.module, "push_file",
                        side_effect=RuntimeError("file upload failed"),
                    ), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                with self.assertRaisesRegex(RuntimeError, "file upload failed"):
                    self.module.main()

        self.assertEqual(transport.close_count, 1)

    def test_legacy_cli_has_no_second_wire_protocol_implementation(self):
        source = LEGACY_SCRIPT.read_text(encoding="utf-8")

        self.assertNotIn("import base64", source)
        self.assertNotIn("import json", source)
        self.assertNotIn('"PUT_FILE_CHUNK"', source)
        self.assertNotIn('"PUT_FILE_BEGIN"', source)
        self.assertIn("ota.push_file_with_retries", source)
        self.assertIn("ota.request_reboot", source)

    def test_legacy_cli_delegates_each_file_to_canonical_transaction(self):
        transport = object()
        calls = []

        def record(used_transport, source, destination, ids):
            calls.append((used_transport, source, destination, ids[0]))
            ids[0] += 7

        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "one.mpy"
            second = Path(tmp) / "two.json"
            first.write_bytes(b"compiled")
            second.write_bytes(b"{}")
            files = [
                (first, "/one.mpy", first.stat().st_size),
                (second, "/two.json", second.stat().st_size),
            ]
            with mock.patch.object(
                self.module, "push_file_with_retries", side_effect=record,
            ):
                total, elapsed = self.legacy_module.push_files(transport, files)

        self.assertEqual(total, len(b"compiled") + len(b"{}"))
        self.assertGreaterEqual(elapsed, 0)
        self.assertEqual(
            [(used, source, destination) for used, source, destination, _ in calls],
            [
                (transport, first, "/one.mpy"),
                (transport, second, "/two.json"),
            ],
        )
        self.assertEqual([initial_id for *_, initial_id in calls], [0, 7])

    def test_legacy_listing_inherits_compiled_sibling_and_config_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / "lib" / "captain"
            config = root / "config"
            library.mkdir(parents=True)
            config.mkdir()
            (library / "app.py").write_text("source", encoding="utf-8")
            (library / "app.mpy").write_bytes(b"compiled")
            (library / "bindings.py").write_text("only source", encoding="utf-8")
            (config / "patches.json").write_text("{}", encoding="utf-8")

            listed = self.legacy_module.list_firmware_files(root)

        destinations = [destination for _, destination, _ in listed]
        self.assertIn("/lib/captain/app.mpy", destinations)
        self.assertIn("/lib/captain/bindings.py", destinations)
        self.assertNotIn("/lib/captain/app.py", destinations)
        self.assertNotIn("/config/patches.json", destinations)

    def test_legacy_open_closes_transport_if_initial_drain_fails(self):
        class BrokenTransport:
            write_timeout = None

            def __init__(self):
                self.closed = False

            def read(self, _size):
                raise OSError("disconnected while draining")

            def close(self):
                self.closed = True

        transport = BrokenTransport()
        with mock.patch.object(
            self.module, "open_transport", return_value=transport,
        ), mock.patch.object(self.legacy_module.time, "sleep"):
            with self.assertRaisesRegex(OSError, "disconnected while draining"):
                self.legacy_module._open_and_drain("COM-test")

        self.assertTrue(transport.closed)


class ReplWriterSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = REPL_WRITER.read_text(encoding="utf-8")
        spec = importlib.util.spec_from_file_location(
            "write_via_repl_under_test", REPL_WRITER,
        )
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_staged_size_and_hash_are_checked_before_live_file_is_moved(self):
        staged_check = self.source.index("staged_identity != expected")
        live_move = self.source.index("os.rename(%r,%r)", staged_check)

        self.assertLess(staged_check, live_move)
        self.assertIn("hashlib.sha256(data).hexdigest()", self.source)
        self.assertIn("range(0, len(data), CHUNK_SIZE)", self.source)

    def test_installed_size_is_checked_before_success_is_reported(self):
        installed_check = self.source.index("installed_identity != expected")
        success = self.source.index('print("wrote"', installed_check)

        self.assertLess(installed_check, success)

    def test_writer_exits_raw_repl_before_reloading_code(self):
        finally_clause = self.source.index("finally:", self.source.index("def install"))
        resume_call = self.source.index("_resume_code(port)", finally_clause)
        resume_definition = self.source.index("def _resume_code(port)")
        exit_raw = self.source.index(r'write_all(port, b"\x03\x03\x02")', resume_definition)
        reload_code = self.source.index(r'write_all(port, b"\x04")', exit_raw)

        self.assertLess(finally_clause, resume_call)
        self.assertLess(exit_raw, reload_code)

    def test_source_sibling_removal_is_explicit_and_after_install_check(self):
        self.assertIn('"--remove-source-sibling"', self.source)
        installed_check = self.source.index("installed_identity != expected")
        sibling_block = self.source.index("if source_sibling is not None", installed_check)
        sibling_remove = self.source.index("os.remove(%r)", sibling_block)
        absence_check = self.source.index("stale source remains", sibling_remove)
        finally_clause = self.source.index("finally:", absence_check)

        self.assertLess(installed_check, sibling_remove)
        self.assertLess(sibling_remove, absence_check)
        self.assertLess(absence_check, finally_clause)

    def test_partial_serial_writes_are_completed(self):
        self.assertIn("while view:", self.source)
        self.assertIn("view = view[written:]", self.source)
        self.assertIn("serial write made no progress", self.source)

    def test_error_path_still_attempts_to_resume_saved_code(self):
        installed_check = self.source.index("installed_identity != expected")
        finally_clause = self.source.index("finally:", installed_check)
        resume_call = self.source.index("_resume_code(port)", finally_clause)

        self.assertLess(finally_clause, resume_call)

    def test_existing_backup_is_never_overwritten_automatically(self):
        backup_guard = self.source.index("if _remote_exists(port, backup)")
        staged_create = self.source.index("f=open(%r,'wb')", backup_guard)

        self.assertLess(backup_guard, staged_create)
        self.assertNotIn("os.remove(%r)\" % backup", self.source[:staged_create])

    def test_raw_reply_requires_ack_stdout_and_stderr_framing(self):
        self.assertIn('reply.startswith(b"OK")', self.source)
        self.assertIn('body.split(b"\\x04")', self.source)
        self.assertIn("not stderr", self.source)
        self.assertIn('read_until(port, b"\\x04>", timeout)', self.source)

    def test_execute_raw_accepts_only_complete_success_frame(self):
        class ReplyPort:
            def __init__(self, reply):
                self.reply = [reply]
                self.written = bytearray()

            def write(self, data):
                accepted = min(3, len(data))
                self.written.extend(bytes(data[:accepted]))
                return accepted

            def read(self, _size):
                return self.reply.pop(0) if self.reply else b""

        port = ReplyPort(b"OKverified\r\n\x04\x04>")
        output = self.module.execute_raw(port, "print('verified')", timeout=0.1)

        self.assertEqual(output, b"verified\r\n")
        self.assertEqual(port.written, b"print('verified')\x04")

        error_port = ReplyPort(b"OK\x04Traceback: failed\r\n\x04>")
        with self.assertRaisesRegex(RuntimeError, "Traceback: failed"):
            self.module.execute_raw(error_port, "raise Exception", timeout=0.1)

    def test_destination_validation_rejects_ambiguous_or_staging_paths(self):
        invalid = (
            "", "relative.py", "/", "/lib//module.py", "/lib/../code.py",
            "/lib/module.py/", "/lib/module.py.recovery", "/lib/module.tmp",
            "/lib/bad\nname.py", "/lib/bad\x00name.py",
        )
        for destination in invalid:
            with self.subTest(destination=repr(destination)):
                with self.assertRaises(ValueError):
                    self.module.validate_destination(destination)

        self.assertEqual(
            self.module.validate_destination("/lib/captain/app.mpy"),
            "/lib/captain/app.mpy",
        )

    def test_stage_mismatch_never_moves_live_file_and_always_resumes(self):
        payload = b"new module"
        expected = (len(payload), __import__("hashlib").sha256(payload).hexdigest())
        commands = []

        with mock.patch.object(self.module, "write_all"), \
                mock.patch.object(
                    self.module, "read_until", return_value=b"raw REPL; CTRL-B to exit\r\n>",
                ), mock.patch.object(
                    self.module, "execute_raw",
                    side_effect=lambda _port, command, **_kwargs: commands.append(command),
                ), mock.patch.object(
                    self.module, "_remote_exists", return_value=False,
                ), mock.patch.object(
                    self.module, "_remote_identity",
                    return_value=(expected[0], "0" * 64),
                ), mock.patch.object(self.module, "_resume_code") as resume:
            with self.assertRaisesRegex(RuntimeError, "staged identity mismatch"):
                self.module.install(object(), payload, "/lib/module.mpy")

        self.assertFalse(any("os.rename" in command for command in commands))
        resume.assert_called_once()

    def test_preexisting_backup_aborts_before_staging_and_resumes(self):
        commands = []
        with mock.patch.object(self.module, "write_all"), \
                mock.patch.object(
                    self.module, "read_until", return_value=b"raw REPL; CTRL-B to exit\r\n>",
                ), mock.patch.object(
                    self.module, "execute_raw",
                    side_effect=lambda _port, command, **_kwargs: commands.append(command),
                ), mock.patch.object(
                    self.module, "_remote_exists", return_value=True,
                ), mock.patch.object(self.module, "_resume_code") as resume:
            with self.assertRaisesRegex(RuntimeError, "backup already exists"):
                self.module.install(object(), b"data", "/lib/module.mpy")

        self.assertFalse(any("'wb'" in command for command in commands))
        resume.assert_called_once()

    def test_success_keeps_backup_through_installed_identity_check(self):
        payload = b"new module"
        expected = (len(payload), __import__("hashlib").sha256(payload).hexdigest())
        events = []

        def execute(_port, command, **_kwargs):
            compile(command, "<raw-repl-command>", "exec")
            events.append(("execute", command))

        def identity(_port, path):
            events.append(("identity", path))
            return expected

        with mock.patch.object(self.module, "write_all"), \
                mock.patch.object(
                    self.module, "read_until", return_value=b"raw REPL; CTRL-B to exit\r\n>",
                ), mock.patch.object(
                    self.module, "execute_raw", side_effect=execute,
                ), mock.patch.object(
                    self.module, "_remote_exists", side_effect=(False, True),
                ), mock.patch.object(
                    self.module, "_remote_identity", side_effect=identity,
                ), mock.patch.object(self.module, "_resume_code") as resume:
            result = self.module.install(
                object(), payload, "/lib/module.mpy",
            )

        swap = next(
            index for index, event in enumerate(events)
            if event[0] == "execute" and
            "os.rename('/lib/module.mpy','/lib/module.mpy.backup.recovery')" in event[1]
        )
        installed_check = events.index(("identity", "/lib/module.mpy"))
        backup_cleanup = events.index((
            "execute", "os.remove('/lib/module.mpy.backup.recovery')",
        ))
        self.assertEqual(result, expected)
        self.assertLess(swap, installed_check)
        self.assertLess(installed_check, backup_cleanup)
        resume.assert_called_once()

    def test_positive_installed_mismatch_rolls_back_known_good_live(self):
        payload = b"new module"
        expected = (len(payload), __import__("hashlib").sha256(payload).hexdigest())
        commands = []

        def execute(_port, command, **_kwargs):
            compile(command, "<raw-repl-command>", "exec")
            commands.append(command)

        with mock.patch.object(self.module, "write_all"), \
                mock.patch.object(
                    self.module, "read_until", return_value=b"raw REPL; CTRL-B to exit\r\n>",
                ), mock.patch.object(
                    self.module, "execute_raw",
                    side_effect=execute,
                ), mock.patch.object(
                    self.module, "_remote_exists", side_effect=(False, True),
                ), mock.patch.object(
                    self.module, "_remote_identity",
                    side_effect=(expected, (expected[0], "f" * 64)),
                ), mock.patch.object(self.module, "_resume_code") as resume:
            with self.assertRaisesRegex(RuntimeError, "installed identity mismatch"):
                self.module.install(object(), payload, "/lib/module.mpy")

        rollback = next(command for command in commands
                        if "os.remove('/lib/module.mpy')" in command)
        self.assertIn(
            "os.rename('/lib/module.mpy.backup.recovery','/lib/module.mpy')",
            rollback,
        )
        resume.assert_called_once()


class ReplReaderSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "read_repl_under_test", REPL_READER,
        )
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_duration_must_be_finite_and_nonnegative(self):
        for value in ("-1", "nan", "inf", "-inf"):
            with self.subTest(value=value):
                with self.assertRaises(self.module.argparse.ArgumentTypeError):
                    self.module.finite_nonnegative(value)
        self.assertEqual(self.module.finite_nonnegative("0.25"), 0.25)

    def test_console_writes_complete_partial_progress(self):
        class PartialPort:
            def __init__(self):
                self.received = bytearray()

            def write(self, data):
                accepted = min(1, len(data))
                self.received.extend(bytes(data[:accepted]))
                return accepted

        port = PartialPort()
        self.module.write_all(port, b"\x03\x03\x04")
        self.assertEqual(port.received, b"\x03\x03\x04")

    def test_console_write_no_progress_fails_closed(self):
        class StalledPort:
            def write(self, _data):
                return None

        with self.assertRaisesRegex(RuntimeError, "made no progress"):
            self.module.write_all(StalledPort(), b"\x04")

    def test_console_transport_has_bounded_write_and_error_recovery(self):
        source = REPL_READER.read_text(encoding="utf-8")
        self.assertIn("write_timeout=WRITE_TIMEOUT_S", source)
        finally_clause = source.index("finally:")
        self.assertIn("if interrupted and not resumed:", source[finally_clause:])
        self.assertIn("resume_code(ser)", source[finally_clause:])

if __name__ == "__main__":
    unittest.main(verbosity=2)
