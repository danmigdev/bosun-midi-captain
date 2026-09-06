"""Regression tests for outbound wakeups without serial polling or stale replay.

Long blocking waits plus explicit events make the tests independent of the
50 ms serial timeout. Short join deadlines only bound a broken implementation;
these tests do not assert a platform-specific sub-millisecond latency.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bosun_hub import link as link_module  # noqa: E402
from bosun_hub.link import Transport, UpstreamLink  # noqa: E402


def _worker(function):
    values, errors = [], []
    finished = threading.Event()

    def run():
        try:
            values.append(function())
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, finished, values, errors


class BlockingTransport(Transport):
    """Fake physical reader: only incoming bytes, wake, or close end its wait."""

    name = "/dev/ttyACM-wakeup-test"

    def __init__(self):
        self.condition = threading.Condition()
        self.read_entered = threading.Event()
        self.written = threading.Event()
        self.before_wait_gate = None
        self.rx = bytearray()
        self.accepted = bytearray()
        self.pending_wakes = 0
        self.wake_calls = 0
        self.read_calls = 0
        self.closed = False

    def wake_read(self):
        with self.condition:
            self.pending_wakes += 1
            self.wake_calls += 1
            self.condition.notify_all()

    def inject(self, payload):
        with self.condition:
            self.rx.extend(payload)
            self.condition.notify_all()

    def read_available(self, limit):
        with self.condition:
            chunk = bytes(self.rx[:limit])
            del self.rx[:len(chunk)]
            return chunk

    def read(self, limit):
        self.read_calls += 1
        self.read_entered.set()
        gate, self.before_wait_gate = self.before_wait_gate, None
        if gate is not None:
            assert gate.wait(3), "test did not release the pre-wait gate"
        with self.condition:
            assert self.condition.wait_for(
                lambda: self.closed or self.pending_wakes or self.rx,
                timeout=10,
            ), "read was never woken"
            self.pending_wakes = 0
            chunk = bytes(self.rx[:limit])
            del self.rx[:len(chunk)]
            return chunk

    def write_some(self, data):
        with self.condition:
            assert not self.closed
            self.accepted.extend(data)
            self.written.set()
        return len(data)

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()


def _pump(link, transport):
    link._transport = transport
    link._connected = True
    thread, finished, values, errors = _worker(link._pump)
    link._thread = thread
    return finished, errors


@pytest.mark.parametrize("wake_before_wait", [False, True])
def test_send_wakes_blocked_reader_or_preserves_early_notification(wake_before_wait):
    transport = BlockingTransport()
    gate = threading.Event() if wake_before_wait else None
    transport.before_wait_gate = gate
    link = UpstreamLink(None, lambda _line: None)
    finished, errors = _pump(link, transport)
    try:
        assert transport.read_entered.wait(1)
        assert link.send('{"type":"PING","id":"wake-request"}')
        if gate:
            gate.set()
        assert transport.written.wait(1), "command waited for the blocking read's timeout"
        assert bytes(transport.accepted) == b'{"type":"PING","id":"wake-request"}\n'
        assert transport.wake_calls == 1
    finally:
        if gate:
            gate.set()
        link.stop()
    assert finished.wait(1)
    assert not errors


def test_simultaneous_receive_and_send_wakeup_preserve_complete_incoming_line():
    transport = BlockingTransport()
    gate = threading.Event()
    transport.before_wait_gate = gate
    received = []
    delivered = threading.Event()

    def on_line(line):
        received.append(line)
        delivered.set()

    link = UpstreamLink(None, on_line)
    finished, errors = _pump(link, transport)
    try:
        assert transport.read_entered.wait(1)
        transport.inject(b'{"type":"CONTEXT","context":{"bank":1,"slot":2}}\n')
        assert link.send('{"type":"PING","id":"simultaneous"}')
        gate.set()
        assert delivered.wait(1)
        assert transport.written.wait(1)
        assert received == ['{"type":"CONTEXT","context":{"bank":1,"slot":2}}']
    finally:
        gate.set()
        link.stop()
    assert finished.wait(1)
    assert not errors


def test_woken_partial_writes_keep_fifo_and_drain_fragmented_rx():
    commands = (b'{"type":"PING","id":"one"}\n', b'{"type":"GET_CONTEXT","id":"two"}\n')
    expected = b"".join(commands)
    received = []
    complete = threading.Event()

    class PartialTransport(BlockingTransport):
        def __init__(self):
            super().__init__()
            self.arguments = []
            self.incoming = []

        def read_available(self, limit):
            return super().read_available(min(limit, 5))

        def read(self, limit):
            return super().read(min(limit, 5))

        def write_some(self, data):
            self.arguments.append(bytes(data))
            # First write reports backpressure with no accepted bytes. Every
            # attempt also produces input, making RX drain necessary to finish.
            count = 0 if len(self.arguments) == 1 else min(3, len(data))
            self.accepted.extend(data[:count])
            last = bytes(self.accepted) == expected
            frame = json.dumps({"type": "EVENT", "n": len(self.arguments), "last": last})
            self.incoming.append(frame)
            self.inject(frame.encode() + b"\n")
            return count

    def on_line(line):
        received.append(line)
        if json.loads(line).get("last"):
            complete.set()

    transport = PartialTransport()
    gate = threading.Event()
    transport.before_wait_gate = gate
    link = UpstreamLink(None, on_line)
    finished, errors = _pump(link, transport)
    try:
        assert transport.read_entered.wait(1)
        for command in commands:
            assert link.send(command.decode())
        gate.set()
        assert complete.wait(2)
        assert bytes(transport.accepted) == expected
        assert received == transport.incoming
        assert transport.arguments[1] == transport.arguments[0]
        for previous, current in zip(transport.arguments[1:], transport.arguments[2:]):
            if len(previous) > 3:
                assert current == previous[3:]
    finally:
        gate.set()
        link.stop()
    assert finished.wait(1)
    assert not errors


def test_send_notifies_old_session_outside_state_lock_and_never_replays_after_purge():
    wake_entered, release_wake = threading.Event(), threading.Event()

    class PausingWakeTransport(BlockingTransport):
        def wake_read(self):
            wake_entered.set()
            assert release_wake.wait(3), "test did not release the old-session wake"
            super().wake_read()

    old, new = PausingWakeTransport(), BlockingTransport()
    link = UpstreamLink(None, lambda _line: None)
    link._connected = True
    link._transport = old
    sender, sent, values, errors = _worker(lambda: link.send('{"type":"PING","id":"old-session"}'))
    finished = None
    try:
        assert wake_entered.wait(1)
        teardown, down, _, teardown_errors = _worker(
            lambda: link._set_state(False, "test teardown", discard_tx=True)
        )
        assert down.wait(1), "wake_read held the admission lock and blocked teardown"
        teardown.join(1)
        assert not teardown_errors
        assert link._tx.empty()
        with link._transport_lock:
            link._transport = new
        old.close()
        release_wake.set()
        assert sent.wait(1)
        sender.join(1)
        assert values == [True] and not errors
        assert old.wake_calls == 1 and new.wake_calls == 0
        assert not link.send('{"type":"PING","id":"disconnected"}')
        link._set_state(True, "new session")
        assert link.send('{"type":"PING","id":"fresh-session"}')
        finished, pump_errors = _pump(link, new)
        assert new.written.wait(1)
        assert bytes(new.accepted) == b'{"type":"PING","id":"fresh-session"}\n'
        assert not old.accepted
    finally:
        release_wake.set()
        sender.join(1)
        link.stop()
    if finished is not None:
        assert finished.wait(1)
        assert not pump_errors


def test_stop_interrupts_an_idle_wake_capable_reader():
    transport = BlockingTransport()
    link = UpstreamLink(None, lambda _line: None)
    finished, errors = _pump(link, transport)
    assert transport.read_entered.wait(1)
    link.stop()
    assert finished.wait(1)
    assert not errors
    assert transport.closed
    assert transport.read_calls == 1


@pytest.mark.parametrize("notify_first", [False, True])
def test_real_wakeup_socketpair_interrupts_wait_without_fake_receive_bytes(notify_first):
    wake = link_module._ReadWakeup()
    reader, peer = socket.socketpair()
    entering = threading.Event()

    def wait():
        entering.set()
        return wake.wait(reader, 10)

    if notify_first:
        wake.notify()
    thread, finished, values, errors = _worker(wait)
    try:
        assert entering.wait(1)
        if not notify_first:
            wake.notify()
        assert finished.wait(1), "notification did not interrupt readiness wait"
        assert values == [False] and not errors
        reader.setblocking(False)
        with pytest.raises(BlockingIOError):
            reader.recv(1)
    finally:
        wake.close()
        reader.close()
        peer.close()
        thread.join(1)


def test_wakeup_drains_notifications_then_blocks_until_real_rx():
    wake = link_module._ReadWakeup()
    reader, peer = socket.socketpair()
    try:
        # A storm must be coalesced/bounded rather than blocking senders or
        # leaving thousands of stale tokens to spin future idle iterations.
        notifier, notified, _, notify_errors = _worker(lambda: [wake.notify() for _ in range(20000)])
        assert notified.wait(2), "notification writer blocked on its own full pipe"
        notifier.join(1)
        assert not notify_errors
        assert wake.wait(reader, 0) is False
        entering = threading.Event()

        def wait():
            entering.set()
            return wake.wait(reader, 10)

        thread, finished, values, errors = _worker(wait)
        assert entering.wait(1)
        assert not finished.wait(.05), "consumed wake tokens kept the idle reader spinning"
        peer.sendall(b"received")
        assert finished.wait(1)
        assert values == [True] and not errors
        assert reader.recv(8) == b"received"
        thread.join(1)
    finally:
        wake.close()
        wake.close()
        wake.notify()  # A delayed notification may outlive its old session.
        reader.close()
        peer.close()


def test_real_wakeup_preserves_simultaneous_rx_and_consumes_only_its_notification():
    wake = link_module._ReadWakeup()
    reader, peer = socket.socketpair()
    try:
        peer.sendall(b'{"type":"CONTEXT"}\n')
        wake.notify()
        assert wake.wait(reader, 0) is True
        assert reader.recv(4096) == b'{"type":"CONTEXT"}\n'
        assert wake.wait(reader, 0) is False
    finally:
        wake.close()
        reader.close()
        peer.close()


def test_real_wakeup_close_interrupts_long_wait_and_late_notification_is_harmless():
    wake = link_module._ReadWakeup()
    reader, peer = socket.socketpair()
    entering = threading.Event()

    def wait():
        entering.set()
        return wake.wait(reader, 10)

    thread, finished, values, errors = _worker(wait)
    try:
        assert entering.wait(1)
        wake.close()
        wake.close()
        wake.notify()
        assert finished.wait(1), "closing a pending readiness wait did not interrupt it"
        # Concurrent fd closure may report an ordinary select error; the
        # owner's shutdown path already suppresses that expected exception.
        assert values == [False] or (not values and len(errors) == 1
                                     and isinstance(errors[0], (OSError, ValueError)))
    finally:
        wake.close()
        reader.close()
        peer.close()
        thread.join(1)


def test_tcp_transport_wakeup_and_available_reads_preserve_stream_and_eof(monkeypatch):
    reader, peer = socket.socketpair()
    monkeypatch.setattr(link_module.socket, "create_connection", lambda *_args, **_kwargs: reader)
    transport = link_module.TcpTransport("unused", 9876)
    try:
        assert transport.read_available(4096) == b""
        transport.wake_read()
        assert transport.read(4096) == b""
        # Wake and incoming bytes being ready together must not consume or
        # replace protocol bytes with the notification socket's byte.
        peer.sendall(b'{"type":"CON')
        transport.wake_read()
        assert transport.read(4096) == b'{"type":"CON'
        peer.sendall(b'TEXT"}\n')
        assert transport.read_available(4096) == b'TEXT"}\n'
        assert transport.read_available(4096) == b""
        transport.write(b'{"type":"PING"}\n')
        assert peer.recv(4096) == b'{"type":"PING"}\n'
        peer.close()
        with pytest.raises(ConnectionError, match="peer closed"):
            transport.read(4096)
    finally:
        transport.close()
        transport.close()
        transport.wake_read()
        peer.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX pseudoterminal exercises the real pyserial fd path")
def test_real_serial_pty_wake_does_not_drop_fragmented_rx_or_change_data_port():
    pytest.importorskip("serial")
    import pty

    master, slave = pty.openpty()
    transport = link_module.SerialTransport(os.ttyname(slave))
    try:
        assert transport.read_available(4096) == b""
        transport.wake_read()
        thread, finished, values, errors = _worker(lambda: transport.read(4096))
        assert finished.wait(1)
        assert values == [b""] and not errors
        thread.join(1)
        os.write(master, b'{"type":"CON')
        received = bytearray()
        deadline = time.monotonic() + 2
        while len(received) < len(b'{"type":"CON') and time.monotonic() < deadline:
            received.extend(transport.read(4096))
        assert bytes(received) == b'{"type":"CON'
        transport.wake_read()
        os.write(master, b'TEXT"}\n')
        while not received.endswith(b"\n") and time.monotonic() < deadline:
            received.extend(transport.read(4096))
        assert bytes(received) == b'{"type":"CONTEXT"}\n'
        assert transport._s.is_open
        assert transport._s.dtr is True
        assert transport.write_some(b'{"type":"PING"}\n') == len(b'{"type":"PING"}\n')
        assert os.read(master, 4096) == b'{"type":"PING"}\n'
    finally:
        transport.close()
        os.close(master)
        os.close(slave)
