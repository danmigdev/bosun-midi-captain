"""Offline tests for the real-hardware backpressure diagnostic."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import live_backpressure_stress as stress  # noqa: E402


class FakeStream(stress.ByteStream):
    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.timeouts = []
        self.writes = []
        self.closed = False

    def write_all(self, data: bytes) -> None:
        self.writes.append(data)

    def read(self, _size: int) -> bytes:
        if not self.chunks:
            raise AssertionError("unexpected read")
        chunk = self.chunks.pop(0)
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk

    def set_timeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def close(self) -> None:
        self.closed = True


def _lines(*messages) -> bytes:
    return b"".join(
        json.dumps(message, separators=(",", ":")).encode() + b"\n"
        for message in messages
    )


def test_receive_correlates_and_classifies_every_expected_reply():
    stream = FakeStream(
        [
            _lines(
                42,
                {"type": "EVENT", "id": "somebody-else"},
                {"type": "EVENT", "id": []},
                {"type": "LED_DUMP", "id": "a", "leds": []},
                {
                    "type": "ERROR",
                    "id": "b",
                    "error": "background_busy",
                    "of": "LED_DUMP",
                },
                {
                    "type": "ERROR",
                    "id": "c",
                    "error": "link_down",
                    "of": "LED_DUMP",
                },
            )
        ]
    )
    diagnostics = stress.StressDiagnostics(target="fake")

    replies = stress._receive(
        stream,
        bytearray(),
        {"a", "b", "c"},
        1.0,
        diagnostics=diagnostics,
        phase="burst",
    )

    assert set(replies) == {"a", "b", "c"}
    summary = diagnostics.receives[0].as_dict(stress.time.monotonic())
    assert summary["missing_ids"] == []
    assert summary["non_error_response_ids"] == ["a"]
    assert summary["error_response_ids"] == ["b", "c"]
    assert summary["link_down_ids"] == ["c"]
    assert [item["classification"] for item in summary["responses"]] == [
        "response",
        "error",
        "link_down",
    ]
    assert all(item["elapsed_ms"] >= 0 for item in summary["responses"])
    assert summary["unrelated_message_count"] == 2
    assert summary["unrelated_message_types"] == {"EVENT": 2}
    assert summary["non_object_message_count"] == 1


def test_receive_preserves_duplicate_details_and_primary_exception():
    duplicate = {"type": "LED_DUMP", "id": "same", "leds": [1]}
    stream = FakeStream([_lines(duplicate, duplicate)])
    diagnostics = stress.StressDiagnostics()

    with pytest.raises(RuntimeError, match="duplicate response for same") as caught:
        stress._receive(
            stream,
            bytearray(),
            {"same"},
            1.0,
            diagnostics=diagnostics,
            phase="duplicate-test",
        )

    trace = diagnostics.receives[0]
    assert trace.replies["same"] is not None
    assert trace.duplicates == [
        {
            "id": "same",
            "elapsed_ms": trace.duplicates[0]["elapsed_ms"],
            "type": "LED_DUMP",
            "error": None,
        }
    ]
    assert trace.terminal_exception == {
        "type": "RuntimeError",
        "message": str(caught.value),
    }


def test_receive_reports_malformed_input_without_hiding_parser_cause():
    stream = FakeStream([b'{"type": nope}\n'])
    diagnostics = stress.StressDiagnostics()

    with pytest.raises(RuntimeError, match="malformed hub response") as caught:
        stress._receive(
            stream,
            bytearray(),
            {"wanted"},
            1.0,
            diagnostics=diagnostics,
        )

    assert isinstance(caught.value.__cause__, json.JSONDecodeError)
    summary = diagnostics.receives[0].as_dict(stress.time.monotonic())
    assert summary["malformed_lines"] == ['b\'{"type": nope}\'']
    assert summary["missing_ids"] == ["wanted"]


def test_receive_reports_partial_results_when_connection_closes():
    stream = FakeStream([_lines({"type": "LED_DUMP", "id": "received"}), b""])
    diagnostics = stress.StressDiagnostics()

    with pytest.raises(ConnectionError, match="transport closed the connection"):
        stress._receive(
            stream,
            bytearray(),
            {"missing", "received"},
            1.0,
            diagnostics=diagnostics,
            phase="connection-close",
        )

    summary = diagnostics.receives[0].as_dict(stress.time.monotonic())
    assert summary["non_error_response_ids"] == ["received"]
    assert summary["missing_ids"] == ["missing"]
    assert summary["terminal_exception"]["type"] == "ConnectionError"


def test_failure_reporting_reraises_the_same_primary_exception(capsys):
    class PrimaryFailure(RuntimeError):
        pass

    primary = PrimaryFailure("original failure")
    diagnostics = stress.StressDiagnostics(target="tcp:test:9876")

    def fail():
        raise primary

    with pytest.raises(PrimaryFailure) as caught:
        stress._with_failure_report(fail, diagnostics)

    assert caught.value is primary
    report = json.loads(capsys.readouterr().err)
    assert report["result"] == "FAIL"
    assert report["target"] == "tcp:test:9876"
    assert report["primary_exception"] == {
        "type": "PrimaryFailure",
        "message": "original failure",
    }


def test_serial_stream_write_all_handles_partial_progress_and_common_io():
    class FakeSerial:
        def __init__(self):
            self.write_sizes = iter((2, 4))
            self.write_inputs = []
            self.reads = [b"reply", b""]
            self.timeout = None
            self.closed = False

        def write(self, data):
            self.write_inputs.append(data)
            return next(self.write_sizes)

        def read(self, size):
            assert size == 99
            return self.reads.pop(0)

        def close(self):
            self.closed = True

    serial_port = FakeSerial()
    stream = stress.SerialStream(serial_port)
    stream.write_all(b"abcdef")
    assert serial_port.write_inputs == [b"abcdef", b"cdef"]
    stream.set_timeout(0.125)
    assert serial_port.timeout == 0.125
    assert stream.read(99) == b"reply"
    with pytest.raises(TimeoutError, match="serial read timed out"):
        stream.read(99)
    stream.close()
    assert serial_port.closed


def test_serial_stream_write_all_rejects_zero_or_impossible_progress():
    class NoProgress:
        @staticmethod
        def write(_data):
            return 0

    with pytest.raises(TimeoutError, match="made no progress"):
        stress.SerialStream(NoProgress()).write_all(b"x")

    class ImpossibleProgress:
        @staticmethod
        def write(_data):
            return 2

    with pytest.raises(OSError, match="invalid serial write progress"):
        stress.SerialStream(ImpossibleProgress()).write_all(b"x")


def test_send_uses_common_write_all_with_one_framed_payload():
    stream = FakeStream()
    stress._send(stream, [{"type": "PING", "id": "p"}, {"type": "STATS"}])
    assert stream.writes == [
        b'{"type":"PING","id":"p"}\n{"type":"STATS"}\n'
    ]
