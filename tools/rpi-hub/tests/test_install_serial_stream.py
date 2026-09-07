"""Exercise updater request framing against a fragmented, unsolicited CDC stream."""
from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bosun_hub import firmware_install as install


def frame(value):
    return (json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def reply(request, kind, **fields):
    return frame({"type": kind, "id": request["id"], **fields})


class StreamSerial:
    """Only bytes queued by a request handler exist; no real serial is imported."""
    def __init__(self, handler):
        self.handler = handler
        self.incoming = deque()
        self.requests = []
        self.read_count = 0
        self.reset_count = 0
        self.closed = False

    def queue(self, *chunks):
        self.incoming.extend(chunk for chunk in chunks if chunk)

    def write(self, wire):
        assert wire.endswith(b"\n") and wire.count(b"\n") == 1
        request = json.loads(wire)
        assert isinstance(request["id"], str) and request["id"].startswith("update-")
        self.requests.append(request)
        self.handler(request, self)
        return len(wire)

    def read(self, size):
        assert size > 0
        self.read_count += 1
        assert self.incoming, "The client discarded a frame or requested unscripted serial data"
        chunk = self.incoming.popleft()
        if len(chunk) > size:
            self.incoming.appendleft(chunk[size:])
        return chunk[:size]

    def reset_input_buffer(self):
        self.reset_count += 1
        self.incoming.clear()

    def flush(self):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def client_factory(monkeypatch):
    clients = []

    def create(handler):
        stream = StreamSerial(handler)
        def open_serial(port, baudrate, **kwargs):
            assert port == "/dev/simulated-captain" and baudrate == 115200
            assert kwargs["exclusive"] is True
            return stream
        monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=open_serial))
        client = install._ProtocolClient("/dev/simulated-captain")
        clients.append(client)
        return client, stream

    yield create
    for client in clients:
        client.close()


@pytest.mark.parametrize("split", [1, 18, -1])
def test_startup_ack_preserves_partial_unsolicited_frame_for_next_request(client_factory, split):
    event = frame({"type": "STATE", "rig": "Clean", "performance": 3})
    def handle(request, stream):
        if request["type"] == "PING":
            stream.queue(reply(request, "ACK") + event[:split])
        else:
            assert request["type"] == "GET_DEVICE_INFO"
            stream.queue(event[split:] + reply(request, "DEVICE_INFO", fw="0.6.5-native"))
    client, stream = client_factory(handle)
    actual = client.request("GET_DEVICE_INFO", "DEVICE_INFO")
    assert actual["fw"] == "0.6.5-native"
    assert actual["id"] == stream.requests[-1]["id"]
    assert stream.reset_count == 1


def test_reply_preserves_multiple_complete_frames_and_partial_following_frame(client_factory):
    events = [frame({"type": "STATE", "sequence": n, "rig": "Clean"}) for n in range(3)]
    def handle(request, stream):
        command = request["type"]
        if command == "PING":
            stream.queue(reply(request, "ACK"))
        elif command == "GET_GLOBAL":
            stream.queue(reply(request, "GLOBAL", device={"brightness": 81})
                         + events[0] + events[1] + events[2][:17])
        else:
            assert command == "GET_PATCH"
            stream.queue(events[2][17:] + reply(request, "PATCH", patch={"name": "Clean"}))
    client, _ = client_factory(handle)
    assert client.request("GET_GLOBAL", "GLOBAL")["device"] == {"brightness": 81}
    assert client.request("GET_PATCH", "PATCH")["patch"] == {"name": "Clean"}


@pytest.mark.parametrize("after_startup", [False, True])
def test_complete_malformed_frame_queued_after_reply_is_not_discarded(client_factory, after_startup):
    malformed = b"CORRUPT_PENDING_FRAME\n"
    def handle(request, stream):
        command = request["type"]
        if command == "PING":
            stream.queue(reply(request, "ACK") + (b"" if after_startup else malformed))
        elif command == "GET_GLOBAL":
            stream.queue(reply(request, "GLOBAL", device={}) + malformed)
        else:
            stream.queue(reply(request, "DEVICE_INFO", fw="0.6.5-native"))
    client, _ = client_factory(handle)
    if after_startup:
        assert client.request("GET_GLOBAL", "GLOBAL")["device"] == {}
    with pytest.raises(install.FirmwareInstallError, match="Malformed") as raised:
        client.request("GET_DEVICE_INFO", "DEVICE_INFO")
    assert "GET_DEVICE_INFO" in str(raised.value)
    assert "CORRUPT_PENDING_FRAME" in str(raised.value)


def test_boot_junk_is_accepted_only_before_correlated_startup_ack(client_factory):
    def handle(request, stream):
        if request["type"] == "PING":
            stream.queue(b"CircuitPython boot output\r\n\xff invalid boot bytes\n",
                         frame({"type": "ACK", "id": "another-client"}), reply(request, "ACK"))
        else:
            stream.queue(b"CORRUPT_AFTER_STARTUP\n", reply(request, "DEVICE_INFO", fw="0.6.5-native"))
    client, _ = client_factory(handle)
    with pytest.raises(install.FirmwareInstallError, match="Malformed") as raised:
        client.request("GET_DEVICE_INFO", "DEVICE_INFO")
    assert "GET_DEVICE_INFO" in str(raised.value)
    assert "CORRUPT_AFTER_STARTUP" in str(raised.value)


def test_malformed_response_diagnostic_contains_command_and_bounded_prefix(client_factory):
    prefix = "BAD_FRAME_PREFIX:"
    def handle(request, stream):
        stream.queue(reply(request, "ACK") if request["type"] == "PING"
                     else (prefix + "x" * 12000 + "PRIVATE_TAIL_MUST_NOT_APPEAR\n").encode())
    client, _ = client_factory(handle)
    with pytest.raises(install.FirmwareInstallError, match="Malformed") as raised:
        client.request("GET_PATCH", "PATCH", bank=1, slot=2)
    message = str(raised.value)
    assert "GET_PATCH" in message and prefix in message
    assert "PRIVATE_TAIL_MUST_NOT_APPEAR" not in message
    assert len(message) < 512


@pytest.mark.parametrize("terminated", [False, True])
def test_oversized_single_frame_is_rejected_before_json_processing(client_factory, terminated):
    oversized = b'{"type":"STATE","payload":"' + b"x" * 65537
    if terminated:
        oversized += b'"}\n'
    def handle(request, stream):
        stream.queue(reply(request, "ACK") if request["type"] == "PING" else oversized)
    client, stream = client_factory(handle)
    with pytest.raises(install.FirmwareInstallError, match="limits|limit|large|long"):
        client.request("GET_DEVICE_INFO", "DEVICE_INFO")
    assert stream.read_count <= 20


def test_large_unsolicited_utf8_frame_survives_split_across_requests_and_reads(client_factory):
    event = frame({"type": "STATE", "title": "Ritorné", "details": "abcd" * 7000})
    split = event.index("é".encode()) + 1  # The reply boundary splits a UTF-8 character.
    def handle(request, stream):
        if request["type"] == "PING":
            stream.queue(reply(request, "ACK") + event[:split])
        else:
            for offset in range(split, len(event), 997):
                stream.queue(event[offset:offset + 997])
            stream.queue(reply(request, "DEVICE_INFO", fw="0.6.5-native"))
    client, stream = client_factory(handle)
    assert client.request("GET_DEVICE_INFO", "DEVICE_INFO")["fw"] == "0.6.5-native"
    assert stream.read_count > 20


def test_large_matching_response_and_following_frame_are_both_preserved(client_factory):
    patch = {"name": "Large patch", "future_fields": "p" * 50000}
    following = frame({"type": "STATE", "rig": "Lead"})
    def handle(request, stream):
        if request["type"] == "PING":
            stream.queue(reply(request, "ACK"))
        elif request["type"] == "GET_PATCH":
            stream.queue(reply(request, "PATCH", patch=patch) + following[:11])
        else:
            assert request["type"] == "GET_DIRTY"
            stream.queue(following[11:] + reply(request, "DIRTY", patches=[]))
    client, _ = client_factory(handle)
    assert client.request("GET_PATCH", "PATCH", bank=1, slot=1)["patch"] == patch
    assert client.request("GET_DIRTY", "DIRTY")["patches"] == []


def test_complete_unsolicited_lines_do_not_accumulate_toward_single_line_limit(client_factory):
    event = frame({"type": "STATE", "payload": "s" * 3900})
    def handle(request, stream):
        if request["type"] == "PING":
            stream.queue(reply(request, "ACK"))
        else:
            stream.queue(*(event for _ in range(25)), reply(request, "DIRTY", patches=[]))
    client, _ = client_factory(handle)
    assert client.request("GET_DIRTY", "DIRTY")["patches"] == []
