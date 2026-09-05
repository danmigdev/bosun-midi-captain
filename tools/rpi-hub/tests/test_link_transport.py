"""Regression tests for USB-CDC transport stalls and shutdown races."""

from __future__ import annotations

import errno
import json
import logging
import queue
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bosun_hub import link as link_module  # noqa: E402
from bosun_hub.link import SerialTransport, UpstreamLink  # noqa: E402


def _wait(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _serial_transport(serial_port, name: str = "/dev/ttyACM-test") -> SerialTransport:
    """Build a SerialTransport around a deterministic pyserial stand-in."""

    transport = object.__new__(SerialTransport)
    transport.name = name
    transport._s = serial_port
    transport._close_lock = threading.Lock()
    transport._closed = False
    return transport


def test_serial_write_does_not_call_pyserial_write_or_unbounded_flush(monkeypatch):
    class FakeSerial:
        def __init__(self):
            self.writes = []

        @staticmethod
        def fileno():
            return 42

        @staticmethod
        def write(_data):
            raise AssertionError("pyserial write may spin forever on EAGAIN")

        def flush(self):
            raise AssertionError("flush must not run after a bounded write")

    serial_port = FakeSerial()
    monkeypatch.setattr(
        link_module.os, "write",
        lambda fd, data: serial_port.writes.append((fd, data)) or len(data),
    )
    transport = _serial_transport(serial_port)
    transport.write(b'{"type":"PING"}\n')
    assert serial_port.writes == [(42, b'{"type":"PING"}\n')]


def test_serial_write_rejects_a_partial_transfer(monkeypatch):
    class PartialSerial:
        @staticmethod
        def fileno():
            return 42

    monkeypatch.setattr(link_module.os, "write", lambda _fd, data: len(data) - 1)
    with pytest.raises(OSError, match=r"short serial write \(3/4 bytes\)"):
        _serial_transport(PartialSerial()).write(b"test")


def test_serial_write_some_reports_exact_partial_progress_and_zero_backpressure(
    monkeypatch,
):
    class PartialSerial:
        @staticmethod
        def fileno():
            return 42

        @staticmethod
        def write(_data):
            raise AssertionError("pyserial write must never be called")

    results = iter((3, BlockingIOError(errno.EAGAIN, "would block"), 2))

    def os_write(_fd, _data):
        result = next(results)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(link_module.os, "write", os_write)
    transport = _serial_transport(PartialSerial())
    assert transport.write_some(b"abcdef") == 3
    assert transport.write_some(b"def") == 0
    assert transport.write_some(b"de") == 2


def test_serial_write_some_propagates_a_real_device_error(monkeypatch):
    class BrokenSerial:
        @staticmethod
        def fileno():
            return 42

    def os_write(_fd, _data):
        raise OSError(errno.ENODEV, "device disappeared")

    monkeypatch.setattr(link_module.os, "write", os_write)
    with pytest.raises(OSError) as error:
        _serial_transport(BrokenSerial()).write_some(b"test")
    assert error.value.errno == errno.ENODEV


@pytest.mark.parametrize("invalid", (None, -1, 5))
def test_serial_write_some_rejects_invalid_driver_progress(monkeypatch, invalid):
    class InvalidSerial:
        @staticmethod
        def fileno():
            return 42

    monkeypatch.setattr(link_module.os, "write", lambda _fd, _data: invalid)
    with pytest.raises(OSError, match="invalid serial write result"):
        _serial_transport(InvalidSerial()).write_some(b"test")


@pytest.mark.parametrize(
    ("available", "limit", "expected"),
    (
        (64, 4096, 64),
        (8192, 4096, 4096),
        (1, 4096, 1),
    ),
)
def test_serial_read_immediately_drains_only_available_bytes(
    available, limit, expected
):
    """Never ask pyserial to wait for a mostly-empty 4096-byte buffer."""

    class SizeWaitingSerial:
        def __init__(self):
            self.in_waiting = available
            self.read_sizes = []

        def read(self, size):
            self.read_sizes.append(size)
            if size > self.in_waiting:
                raise AssertionError("read would wait for its timeout")
            return b"x" * size

    serial_port = SizeWaitingSerial()
    result = _serial_transport(serial_port).read(limit)

    assert serial_port.read_sizes == [expected]
    assert result == b"x" * expected


def test_serial_read_waits_for_one_byte_when_driver_is_idle():
    class IdleSerial:
        in_waiting = 0

        def __init__(self):
            self.read_sizes = []

        def read(self, size):
            self.read_sizes.append(size)
            return b""

    serial_port = IdleSerial()
    assert _serial_transport(serial_port).read(4096) == b""
    assert serial_port.read_sizes == [1]


def test_serial_read_available_is_strictly_nonblocking_when_idle():
    class IdleSerial:
        in_waiting = 0

        @staticmethod
        def read(_size):
            raise AssertionError("non-blocking probe must not enter read")

    assert _serial_transport(IdleSerial()).read_available(4096) == b""


def test_serial_read_with_zero_limit_never_touches_the_driver():
    class MustNotReadSerial:
        @property
        def in_waiting(self):
            raise AssertionError("driver must not be queried")

        @staticmethod
        def read(_size):
            raise AssertionError("driver must not be read")

    assert _serial_transport(MustNotReadSerial()).read(0) == b""


def test_serial_close_cancels_io_before_close_and_is_idempotent():
    class FakeSerial:
        def __init__(self):
            self.calls = []

        def cancel_read(self):
            self.calls.append("cancel_read")

        def cancel_write(self):
            self.calls.append("cancel_write")

        def close(self):
            self.calls.append("close")

    serial_port = FakeSerial()
    transport = _serial_transport(serial_port)
    transport.close()
    transport.close()
    assert serial_port.calls == ["cancel_read", "cancel_write", "close"]


def test_fatal_write_error_reopens_without_sticking_in_sentinel_flush(monkeypatch):
    """Reproduce a broken CDC link followed by a reconnect sentinel.

    The first CDC instance accepts its sentinel and then fails a command.
    The replacement instance deliberately has a flush which blocks until close.
    Calling flush after its successful sentinel write used to strand the link in
    sync; a bounded pyserial write is sufficient to queue the complete line.
    """

    class ScriptedSerial:
        next_fd = 100

        def __init__(self, *, fail_command=False, block_flush=False):
            self.fail_command = fail_command
            self.block_flush = block_flush
            self.failed = threading.Event()
            self.flush_entered = threading.Event()
            self.release = threading.Event()
            self.reads = queue.Queue()
            self.fd = self.next_fd
            ScriptedSerial.next_fd += 1

        def fileno(self):
            return self.fd

        def write(self, data):
            messages = [line for line in data.splitlines() if line.strip()]
            for raw in messages:
                message = json.loads(raw)
                request_id = str(message.get("id", ""))
                if message.get("type") == "PING" and request_id.startswith("__hub_sync_"):
                    ack = json.dumps({"type": "ACK", "id": request_id}).encode() + b"\n"
                    self.reads.put(ack)
                    continue
                if self.fail_command:
                    self.failed.set()
                    raise ConnectionError("simulated USB disconnect")
            return len(data)

        def flush(self):
            self.flush_entered.set()
            if self.block_flush:
                self.release.wait(10)

        @property
        def in_waiting(self):
            # SerialTransport now avoids pyserial's size-or-timeout latency by
            # querying availability before choosing its bounded read size.
            return 0 if self.reads.empty() else 1

        def read(self, _size):
            try:
                return self.reads.get(timeout=0.01)
            except queue.Empty:
                return b""

        def cancel_read(self):
            self.release.set()

        def cancel_write(self):
            self.release.set()

        def close(self):
            self.release.set()

    serial_ports = []
    serial_by_fd = {}

    def os_write(fd, data):
        return serial_by_fd[fd].write(data)

    def open_transport(_target):
        serial_port = ScriptedSerial(
            fail_command=not serial_ports,
            block_flush=bool(serial_ports),
        )
        serial_ports.append(serial_port)
        serial_by_fd[serial_port.fd] = serial_port
        return _serial_transport(serial_port)

    monkeypatch.setattr(link_module, "discover_candidates", lambda _target: ["test"])
    monkeypatch.setattr(link_module, "_open_transport", open_transport)
    monkeypatch.setattr(link_module.os, "write", os_write)
    monkeypatch.setattr(link_module, "REOPEN_BACKOFF", (0.01,))
    states = []
    link = UpstreamLink(None, on_line=lambda _line: None,
                        on_state=lambda up, _detail: states.append(up))
    link.start()
    try:
        assert _wait(lambda: link.connected)
        assert link.send('{"type":"GET_CONTEXT","id":"after-ota"}')
        assert serial_ports[0].failed.wait(1)
        assert _wait(lambda: states.count(True) >= 2), states
        assert link.connected
        assert len(serial_ports) >= 2
        assert not serial_ports[1].flush_entered.is_set()
    finally:
        link.stop()


def test_pump_interleaves_rx_with_partial_tx_without_loss_duplication():
    """Reproduce the MANIFEST-response + host-command-burst deadlock.

    The stand-in refuses every host write while its device-to-host FIFO is
    full, then accepts only small prefixes.  A write-first/blocking pump fails
    immediately here.  The duplex pump must drain RX first and keep the exact
    unwritten suffix across iterations.
    """

    class DuplexPressureTransport(link_module.Transport):
        name = "/dev/ttyACM-test"

        def __init__(self, owner):
            self.owner = owner
            self.device_fifo = bytearray(
                b'{"type":"MANIFEST","id":"manifest","files":[]}\n'
            )
            self.accepted = bytearray()
            self.write_arguments = []
            self.read_before_first_write = False
            self.backpressured_once = False

        def read_available(self, n):
            if not self.device_fifo:
                return b""
            self.read_before_first_write = not self.write_arguments
            chunk = bytes(self.device_fifo[:n])
            del self.device_fifo[:n]
            return chunk

        def write_some(self, data):
            if self.device_fifo:
                raise TimeoutError("both CDC directions are full")
            self.write_arguments.append(bytes(data))
            if not self.backpressured_once:
                self.backpressured_once = True
                return 0
            accepted = min(3, len(data))
            self.accepted.extend(data[:accepted])
            return accepted

        def read(self, _n):
            if bytes(self.accepted) == expected:
                self.owner._stop.set()
            return b""

        def write(self, _data):
            raise AssertionError("pump must use progress-reporting write_some")

        def close(self):
            pass

    link = UpstreamLink(None, on_line=lambda line: received.append(line))
    link._connected = True
    expected_lines = (
        '{"type":"LED_DUMP","id":"led-1"}\n',
        '{"type":"LED_DUMP","id":"led-2"}\n',
    )
    expected = "".join(expected_lines).encode()
    received = []
    transport = DuplexPressureTransport(link)
    link._transport = transport
    for line in expected_lines:
        assert link.send(line)

    link._pump()

    assert transport.read_before_first_write
    assert received == ['{"type":"MANIFEST","id":"manifest","files":[]}']
    assert bytes(transport.accepted) == expected
    # Every next call is exactly the prior unwritten suffix: no replayed
    # prefix, skipped byte, reordered command, or dequeue of command 2 early.
    assert transport.write_arguments[1] == transport.write_arguments[0]
    for previous, current in zip(
        transport.write_arguments[1:], transport.write_arguments[2:]
    ):
        if len(previous) > 3:
            assert current == previous[3:]
    boundary = len(expected_lines[0].encode())
    assert bytes(transport.accepted[:boundary]) == expected_lines[0].encode()
    assert bytes(transport.accepted[boundary:]) == expected_lines[1].encode()


def test_sentinel_resumes_exact_partial_suffix_without_false_reconnect(monkeypatch):
    class PartialSentinelTransport(link_module.Transport):
        name = "/dev/ttyACM-test"

        def __init__(self):
            self.accepted = bytearray()
            self.arguments = []
            self.replied = False

        def write(self, _data):
            raise AssertionError("sentinel must use progress-reporting write_some")

        def write_some(self, data):
            self.arguments.append(bytes(data))
            accepted = min(3, len(data))
            self.accepted.extend(data[:accepted])
            return accepted

        def read(self, _n):
            if self.replied or not self.accepted.endswith(b"}\n"):
                return b""
            lines = [line for line in bytes(self.accepted).splitlines()
                     if line.strip()]
            if not lines:
                return b""
            request = json.loads(lines[-1])
            self.replied = True
            return (json.dumps({
                "type": "ACK", "id": request["id"],
            }) + "\n").encode()

        def close(self):
            pass

    monkeypatch.setattr(link_module, "SYNC_TIMEOUT_SERIAL", 1.0)
    link = UpstreamLink(None, on_line=lambda _line: None)
    transport = PartialSentinelTransport()
    assert link._sentinel_sync(transport)
    assert transport.arguments
    for previous, current in zip(
        transport.arguments, transport.arguments[1:]
    ):
        assert current == previous[3:]
    assert bytes(transport.accepted).startswith(
        b'\n{"type":"PING","id":"__hub_sync_')


def test_sentinel_waiting_for_ack_does_not_spin_on_immediate_empty_reads(
    monkeypatch,
):
    sleeps = []
    link = UpstreamLink(None, on_line=lambda _line: None)

    class NoAckTransport(link_module.Transport):
        name = "/dev/ttyACM-test"

        def __init__(self):
            self.reads = 0

        def write_some(self, data):
            return len(data)

        def read(self, _n):
            self.reads += 1
            if self.reads == 3:
                link._stop.set()
            return b""

        def close(self):
            pass

    monkeypatch.setattr(link_module.time, "sleep", sleeps.append)
    assert not link._sentinel_sync(NoAckTransport())
    assert sleeps == [0.01, 0.01]


def test_blocked_partial_tx_does_not_spin_cpu(monkeypatch):
    sleeps = []

    class BlockedTransport(link_module.Transport):
        name = "/dev/ttyACM-test"

        def __init__(self, owner):
            self.owner = owner
            self.calls = 0

        def write_some(self, _data):
            self.calls += 1
            if self.calls == 3:
                self.owner._stop.set()
            return 0

        @staticmethod
        def read(_n):
            return b""

        def close(self):
            pass

    link = UpstreamLink(None, on_line=lambda _line: None)
    link._connected = True
    transport = BlockedTransport(link)
    link._transport = transport
    assert link.send('{"type":"PING","id":"blocked"}')
    monkeypatch.setattr(link_module.time, "sleep", sleeps.append)

    link._pump()

    assert transport.calls == 3
    assert sleeps == [0.01, 0.01, 0.01]


def test_disconnect_purge_is_atomic_with_concurrent_send():
    class PausingQueue(queue.Queue):
        def __init__(self):
            super().__init__(maxsize=8)
            self.entered = threading.Event()
            self.release = threading.Event()

        def put_nowait(self, item):
            self.entered.set()
            assert self.release.wait(2)
            return super().put_nowait(item)

    link = UpstreamLink(None, on_line=lambda _line: None)
    link._connected = True
    tx = PausingQueue()
    link._tx = tx
    result = []
    sender = threading.Thread(target=lambda: result.append(
        link.send('{"type":"EVENT","event":"old-session"}')
    ))
    sender.start()
    assert tx.entered.wait(1)

    disconnect = threading.Thread(target=lambda: link._set_state(
        False, "closed", discard_tx=True
    ))
    disconnect.start()
    # send() owns the state lock until queue admission is complete, so the
    # disconnect must wait and then purge that old-session command.
    assert disconnect.is_alive()
    tx.release.set()
    sender.join(1)
    disconnect.join(1)

    assert result == [True]
    assert not sender.is_alive() and not disconnect.is_alive()
    assert tx.empty()
    assert not link.connected


def test_stop_suppresses_expected_close_race_exception(monkeypatch, caplog):
    """A driver exception caused by stop() closing the fd is not a link fault."""

    class ClosingTransport:
        name = "/dev/ttyACM-test"

        def __init__(self):
            self.write_entered = threading.Event()
            self.closed = threading.Event()

        def write(self, _data):
            self.write_entered.set()
            assert self.closed.wait(10), "close did not interrupt the write"
            raise TypeError("'NoneType' object cannot be interpreted as an integer")

        @staticmethod
        def read(_size):
            return b""

        def close(self):
            self.closed.set()

    transport = ClosingTransport()
    monkeypatch.setattr(link_module, "discover_candidates", lambda _target: ["test"])
    monkeypatch.setattr(link_module, "_open_transport", lambda _target: transport)
    link = UpstreamLink(None, on_line=lambda _line: None)

    with caplog.at_level(logging.WARNING, logger="bosun_hub.link"):
        link.start()
        assert transport.write_entered.wait(1)
        started = time.monotonic()
        link.stop()

    assert time.monotonic() - started < 0.5
    assert link._thread is None
    assert not any("NoneType" in record.getMessage() for record in caplog.records)
    assert not any("did not stop" in record.getMessage() for record in caplog.records)
