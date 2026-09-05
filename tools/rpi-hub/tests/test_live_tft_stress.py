"""Offline regression tests for TFT diagnostic timeout cleanup."""
import json
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import live_tft_stress as stress


class FakeSocket:
    def __init__(self, replies):
        self.replies = list(replies)
        self.commands = []
        self.timeouts = []
        self.poisoned = False
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def makefile(self, _mode):
        return self

    def settimeout(self, value):
        self.timeouts.append(value)

    def sendall(self, data):
        self.commands.append(json.loads(data))

    def readline(self):
        if self.poisoned:
            raise OSError("cannot read from timed out object")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            self.poisoned = True
            raise reply
        return (json.dumps(dict(reply, id=self.commands[-1]["id"])) + "\n").encode()


def test_timeout_restores_on_fresh_connection_and_prints_console(monkeypatch, capsys):
    timeout = TimeoutError("primary GET_CONTEXT timed out")
    original = FakeSocket([
        {"type": "CONTEXT", "context": {"bank": 1, "slot": 2}},
        {"type": "STATS", "uptime_ms": 1000, "usb_tx_dropped": 7},
        {"type": "ACK"}, timeout,
    ])
    fresh = FakeSocket([
        {"type": "ACK"},
        {"type": "CONTEXT", "context": {"bank": 1, "slot": 2}},
    ])
    captured = threading.Event()

    class Console:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, _size):
            if not captured.is_set():
                captured.set()
                return b"display: MemoryError retained evidence\n"
            threading.Event().wait(0.002)
            return b""

    connections = []

    def connect(address, timeout):
        assert captured.wait(1)
        assert address == ("127.0.0.1", 9876) and timeout == 5
        result = (original, fresh)[len(connections)]
        connections.append(result)
        return result

    monkeypatch.setattr(stress.serial, "Serial", lambda *_a, **_k: Console())
    monkeypatch.setattr(stress.socket, "create_connection", connect)
    monkeypatch.setattr(stress.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(sys, "argv", ["live_tft_stress.py", "--cycles", "1"])
    with pytest.raises(TimeoutError) as caught:
        stress.main()
    assert caught.value is timeout
    assert connections == [original, fresh]
    assert original.poisoned
    assert [command["type"] for command in fresh.commands] == ["SWITCH_PATCH", "GET_CONTEXT"]
    assert (fresh.commands[0]["bank"], fresh.commands[0]["slot"]) == (1, 2)
    assert fresh.closed
    output = capsys.readouterr().out
    assert "RESTORED B1 R2" in output
    assert "CONSOLE display: MemoryError retained evidence" in output
    assert "TRACE FAIL" in output and "GET_CONTEXT" in output


def test_restore_failure_preserves_primary_and_secondary_errors(monkeypatch):
    primary = TimeoutError("primary context timeout")
    restoration = OSError("restore connection unavailable")

    def connect(*_args):
        raise restoration

    monkeypatch.setattr(stress.socket, "create_connection", connect)
    with pytest.raises(stress.StressFailure) as caught:
        stress.restore_rig(1, 2, primary=primary)
    assert caught.value.primary is primary
    assert caught.value.restoration is restoration
    assert caught.value.__cause__ is primary
    assert "primary context timeout" in str(caught.value)
    assert "restore connection unavailable" in str(caught.value)


def test_request_trace_correlates_send_and_reply_and_bounds_waits(capsys):
    sock = FakeSocket([{"type": "ACK"}])
    reply = stress.request_on(sock, sock, "PING")
    output = capsys.readouterr().out
    ident = sock.commands[0]["id"]
    assert reply["id"] == ident
    assert output.count(ident) == 2
    assert "TRACE SEND" in output and "TRACE RECV" in output
    assert "elapsed_ms=" in output
    assert all(0 < timeout <= 5 for timeout in sock.timeouts)
