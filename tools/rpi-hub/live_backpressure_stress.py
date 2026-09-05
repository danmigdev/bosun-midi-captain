#!/usr/bin/env python3
"""Exercise the real hub/Captain full-duplex backpressure path.

The historical failure needs both directions of the USB CDC link to fill at
once: the Captain streams a MANIFEST while the hub sends a burst of LED_DUMP
requests.  The old hub blocked in its write path, stopped reading the
manifest, then reported every request as ``link_down`` even though the
Captain had not rebooted.

This diagnostic is deliberately read-only.  Every request carries a unique
id and every id must receive exactly one valid, correlated response on the
same TCP connection.  A busy response is valid for excess background jobs;
timeouts, duplicate replies and link errors are not.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import socket
import statistics
import sys
import time
from typing import Callable, TypeVar


class ByteStream:
    """Small common interface shared by TCP and direct serial transports."""

    def write_all(self, data: bytes) -> None:
        raise NotImplementedError

    def read(self, size: int) -> bytes:
        raise NotImplementedError

    def set_timeout(self, timeout: float) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def __enter__(self) -> "ByteStream":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


class TcpStream(ByteStream):
    def __init__(self, sock: socket.socket) -> None:
        self._socket = sock

    @classmethod
    def open(cls, host: str, port: int) -> "TcpStream":
        return cls(socket.create_connection((host, port), timeout=5.0))

    def write_all(self, data: bytes) -> None:
        self._socket.sendall(data)

    def read(self, size: int) -> bytes:
        try:
            return self._socket.recv(size)
        except socket.timeout as exc:
            raise TimeoutError("TCP read timed out") from exc

    def set_timeout(self, timeout: float) -> None:
        self._socket.settimeout(timeout)

    def close(self) -> None:
        self._socket.close()


class SerialStream(ByteStream):
    """Direct Captain data-port transport, intended for an idle/stopped hub."""

    def __init__(self, serial_port) -> None:
        self._serial = serial_port

    @classmethod
    def open(cls, path: str) -> "SerialStream":
        try:
            import serial
        except ImportError as exc:  # pragma: no cover - depends on deployment
            raise RuntimeError(
                "--serial requires pyserial (install the 'pyserial' package)"
            ) from exc
        options = {
            "port": path,
            "baudrate": 115200,
            "timeout": 0.5,
            "write_timeout": 5.0,
        }
        try:
            serial_port = serial.Serial(exclusive=True, **options)
        except TypeError:  # pragma: no cover - compatibility with old pyserial
            serial_port = serial.Serial(**options)
        return cls(serial_port)

    def write_all(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            written = self._serial.write(data[offset:])
            if not isinstance(written, int) or written <= 0:
                raise TimeoutError(
                    f"serial write made no progress ({offset}/{len(data)} bytes)"
                )
            if written > len(data) - offset:
                raise OSError(
                    f"invalid serial write progress ({written} bytes for "
                    f"{len(data) - offset} pending)"
                )
            offset += written

    def read(self, size: int) -> bytes:
        data = self._serial.read(size)
        if not data:
            # pyserial represents an ordinary read timeout as an empty result.
            raise TimeoutError("serial read timed out")
        return data

    def set_timeout(self, timeout: float) -> None:
        self._serial.timeout = timeout

    def close(self) -> None:
        self._serial.close()


@dataclass
class ReceiveTrace:
    """Lossless diagnostics for the expected replies in one receive phase."""

    phase: str
    expected_ids: tuple[str, ...]
    started_at: float
    replies: dict[str, dict] = field(default_factory=dict)
    reply_elapsed_seconds: dict[str, float] = field(default_factory=dict)
    duplicates: list[dict] = field(default_factory=list)
    malformed_lines: list[str] = field(default_factory=list)
    unrelated_types: dict[str, int] = field(default_factory=dict)
    non_object_messages: int = 0
    ended_at: float | None = None
    terminal_exception: dict[str, str] | None = None

    def finish(self, exc: BaseException | None = None) -> None:
        if self.ended_at is None:
            self.ended_at = time.monotonic()
        if exc is not None:
            self.terminal_exception = {
                "type": type(exc).__name__,
                "message": str(exc),
            }

    def as_dict(self, now: float) -> dict:
        ended_at = self.ended_at if self.ended_at is not None else now
        responses = []
        non_error_ids = []
        error_ids = []
        link_down_ids = []
        for request_id in self.expected_ids:
            message = self.replies.get(request_id)
            if message is None:
                continue
            message_type = message.get("type")
            error = message.get("error") if message_type == "ERROR" else None
            if error == "link_down":
                classification = "link_down"
                link_down_ids.append(request_id)
                error_ids.append(request_id)
            elif message_type == "ERROR":
                classification = "error"
                error_ids.append(request_id)
            else:
                classification = "response"
                non_error_ids.append(request_id)
            response = {
                "id": request_id,
                "classification": classification,
                "type": message_type,
                "elapsed_ms": round(
                    self.reply_elapsed_seconds[request_id] * 1000.0, 3
                ),
            }
            if message_type == "ERROR":
                response["error"] = error
                if "of" in message:
                    response["of"] = message["of"]
            responses.append(response)

        missing_ids = [
            request_id
            for request_id in self.expected_ids
            if request_id not in self.replies
        ]
        result = {
            "phase": self.phase,
            "duration_seconds": round(max(0.0, ended_at - self.started_at), 3),
            "expected_count": len(self.expected_ids),
            "received_count": len(self.replies),
            "responses": responses,
            "non_error_response_ids": non_error_ids,
            "error_response_ids": error_ids,
            "link_down_ids": link_down_ids,
            "missing_ids": missing_ids,
            "duplicates": self.duplicates,
            "malformed_lines": self.malformed_lines,
            "unrelated_message_count": sum(self.unrelated_types.values()),
            "unrelated_message_types": dict(sorted(self.unrelated_types.items())),
            "non_object_message_count": self.non_object_messages,
        }
        if self.terminal_exception is not None:
            result["terminal_exception"] = self.terminal_exception
        return result


@dataclass
class StressDiagnostics:
    started_at: float = field(default_factory=time.monotonic)
    receives: list[ReceiveTrace] = field(default_factory=list)
    target: str | None = None

    def begin_receive(self, phase: str, expected_ids: set[str]) -> ReceiveTrace:
        trace = ReceiveTrace(
            phase=phase,
            expected_ids=tuple(sorted(expected_ids)),
            started_at=time.monotonic(),
        )
        self.receives.append(trace)
        return trace

    def failure_report(self, primary: BaseException) -> dict:
        now = time.monotonic()
        return {
            "result": "FAIL",
            "primary_exception": {
                "type": type(primary).__name__,
                "message": str(primary),
            },
            "target": self.target,
            "elapsed_seconds": round(max(0.0, now - self.started_at), 3),
            "receives": [trace.as_dict(now) for trace in self.receives],
        }


def _emit_failure_report(
    diagnostics: StressDiagnostics, primary: BaseException
) -> None:
    """Best-effort diagnostics which can never replace the primary failure."""

    try:
        report = diagnostics.failure_report(primary)
        print(json.dumps(report, indent=2, sort_keys=True), file=sys.stderr, flush=True)
    except Exception as diagnostics_error:  # pragma: no cover - last-resort guard
        print(
            "backpressure diagnostic rendering failed: "
            f"{type(diagnostics_error).__name__}: {diagnostics_error}; "
            f"primary={type(primary).__name__}: {primary}",
            file=sys.stderr,
            flush=True,
        )


_Result = TypeVar("_Result")


def _with_failure_report(
    operation: Callable[[], _Result], diagnostics: StressDiagnostics
) -> _Result:
    try:
        return operation()
    except Exception as primary:
        _emit_failure_report(diagnostics, primary)
        raise


def _send(stream: ByteStream, messages: list[dict]) -> None:
    payload = b"".join(
        json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        for message in messages
    )
    stream.write_all(payload)


def _receive(
    stream: ByteStream,
    buffer: bytearray,
    expected_ids: set[str],
    timeout: float,
    *,
    diagnostics: StressDiagnostics | None = None,
    phase: str = "receive",
) -> dict[str, dict]:
    trace = (
        diagnostics.begin_receive(phase, expected_ids)
        if diagnostics is not None
        else ReceiveTrace(phase, tuple(sorted(expected_ids)), time.monotonic())
    )
    deadline = time.monotonic() + timeout
    try:
        while expected_ids - trace.replies.keys():
            while b"\n" in buffer:
                raw, _, tail = bytes(buffer).partition(b"\n")
                buffer[:] = tail
                if not raw.strip():
                    continue
                try:
                    message = json.loads(raw.decode("utf-8", "strict"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    trace.malformed_lines.append(repr(raw[:160]))
                    raise RuntimeError(
                        f"malformed hub response: {raw[:160]!r}"
                    ) from exc
                if not isinstance(message, dict):
                    trace.non_object_messages += 1
                    continue
                request_id = message.get("id")
                if not isinstance(request_id, str) or request_id not in expected_ids:
                    # CONTEXT/EVENT traffic is legitimate while the Stage is open.
                    message_type = message.get("type")
                    type_name = (
                        message_type
                        if isinstance(message_type, str)
                        else type(message_type).__name__
                    )
                    trace.unrelated_types[type_name] = (
                        trace.unrelated_types.get(type_name, 0) + 1
                    )
                    continue
                elapsed = max(0.0, time.monotonic() - trace.started_at)
                if request_id in trace.replies:
                    trace.duplicates.append(
                        {
                            "id": request_id,
                            "elapsed_ms": round(elapsed * 1000.0, 3),
                            "type": message.get("type"),
                            "error": message.get("error"),
                        }
                    )
                    raise RuntimeError(
                        f"duplicate response for {request_id}: {message!r}"
                    )
                trace.replies[request_id] = message
                trace.reply_elapsed_seconds[request_id] = elapsed

            if not expected_ids - trace.replies.keys():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                missing = sorted(expected_ids - trace.replies.keys())
                raise TimeoutError(f"missing {len(missing)} responses: {missing}")
            stream.set_timeout(min(0.5, remaining))
            try:
                chunk = stream.read(65536)
            except TimeoutError:
                continue
            if not chunk:
                raise ConnectionError(
                    "transport closed the connection during the burst"
                )
            buffer.extend(chunk)
        return trace.replies
    except Exception as exc:
        trace.finish(exc)
        raise
    finally:
        trace.finish()


def _one_request(
    stream: ByteStream,
    buffer: bytearray,
    message: dict,
    timeout: float,
    *,
    diagnostics: StressDiagnostics | None = None,
    phase: str = "request",
) -> dict:
    _send(stream, [message])
    return _receive(
        stream,
        buffer,
        {message["id"]},
        timeout,
        diagnostics=diagnostics,
        phase=phase,
    )[message["id"]]


def _validate_round(replies: dict[str, dict], prefix: str, led_count: int) -> int:
    manifest_id = f"{prefix}-manifest"
    manifest = replies[manifest_id]
    if manifest.get("type") != "MANIFEST":
        raise RuntimeError(f"manifest failed: {manifest!r}")

    busy = 0
    for index in range(led_count):
        request_id = f"{prefix}-led-{index:02d}"
        reply = replies[request_id]
        if reply.get("type") == "LED_DUMP":
            continue
        if reply.get("type") == "ERROR" and reply.get("error") == "background_busy":
            busy += 1
            continue
        raise RuntimeError(f"invalid LED_DUMP response for {request_id}: {reply!r}")
    return busy


def _open_stream(args: argparse.Namespace) -> ByteStream:
    if args.serial_port:
        return SerialStream.open(args.serial_port)
    return TcpStream.open(args.host, args.port)


def _run(args: argparse.Namespace, diagnostics: StressDiagnostics) -> None:
    timings: list[float] = []
    busy_total = 0
    buffer = bytearray()
    with _open_stream(args) as stream:
        for round_index in range(args.rounds):
            prefix = f"bp-{time.monotonic_ns()}-{round_index}"
            messages = [{"type": "GET_MANIFEST", "id": f"{prefix}-manifest"}]
            messages.extend(
                {"type": "LED_DUMP", "id": f"{prefix}-led-{index:02d}"}
                for index in range(args.led_count)
            )
            started = time.monotonic()
            _send(stream, messages)
            replies = _receive(
                stream,
                buffer,
                {message["id"] for message in messages},
                args.timeout,
                diagnostics=diagnostics,
                phase=f"round-{round_index + 1}-burst",
            )
            elapsed = time.monotonic() - started
            busy = _validate_round(replies, prefix, args.led_count)
            timings.append(elapsed)
            busy_total += busy

            ping = _one_request(
                stream,
                buffer,
                {"type": "PING", "id": f"{prefix}-recovery-ping"},
                args.timeout,
                diagnostics=diagnostics,
                phase=f"round-{round_index + 1}-recovery-ping",
            )
            if ping.get("type") != "ACK":
                raise RuntimeError(f"post-burst PING failed: {ping!r}")
            print(
                f"round {round_index + 1}/{args.rounds}: "
                f"{elapsed:.3f}s, LED_DUMP busy={busy}, recovery=ACK",
                flush=True,
            )

        stats_id = f"bp-{time.monotonic_ns()}-stats"
        stats = _one_request(
            stream,
            buffer,
            {"type": "STATS", "id": stats_id},
            args.timeout,
            diagnostics=diagnostics,
            phase="post-stress-stats",
        )
        if stats.get("type") != "STATS":
            raise RuntimeError(f"post-stress STATS failed: {stats!r}")

    ordered = sorted(timings)
    p95_index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95) - 1))
    print(
        json.dumps(
            {
                "result": "PASS",
                "target": diagnostics.target,
                "rounds": args.rounds,
                "requests_per_round": args.led_count + 1,
                "background_busy": busy_total,
                "seconds_min": round(min(timings), 3),
                "seconds_median": round(statistics.median(timings), 3),
                "seconds_p95": round(ordered[p95_index], 3),
                "seconds_max": round(max(timings), 3),
                "mem_free": stats.get("mem_free"),
                "protocol_cmd_count": stats.get("protocol_cmd_count"),
                "loop_iters": stats.get("loop_iters"),
                "uptime_ms": stats.get("uptime_ms"),
                "max_tick_ms": stats.get("max_tick_ms"),
                "usb_tx_dropped": stats.get("usb_tx_dropped"),
            },
            indent=2,
            sort_keys=True,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--host", default="bosun-hub")
    target.add_argument(
        "--serial",
        dest="serial_port",
        metavar="DEVICE",
        help=(
            "talk directly to the Captain data port (for example /dev/ttyACM1); "
            "stop the hub service first"
        ),
    )
    parser.add_argument("--port", type=int, default=9876)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--led-count", type=int, default=32)
    parser.add_argument("--timeout", type=float, default=35.0)
    args = parser.parse_args()
    if args.rounds <= 0 or args.led_count <= 0 or args.timeout <= 0:
        parser.error("rounds, led-count and timeout must be positive")
    if not (1 <= args.port <= 65535):
        parser.error("port must be between 1 and 65535")

    diagnostics = StressDiagnostics(
        target=(
            f"serial:{args.serial_port}"
            if args.serial_port
            else f"tcp:{args.host}:{args.port}"
        )
    )
    _with_failure_report(lambda: _run(args, diagnostics), diagnostics)


if __name__ == "__main__":
    main()
