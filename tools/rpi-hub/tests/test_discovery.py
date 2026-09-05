"""Real loopback UDP discovery, including disconnected and failed-bind lifecycle."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bosun_hub import discovery, server  # noqa: E402
from tests.test_server_smoke import test_server_smoke as _server_smoke  # noqa: E402


class Client(asyncio.DatagramProtocol):
    def __init__(self):
        self.messages = asyncio.Queue()

    def datagram_received(self, data, addr):
        self.messages.put_nowait((json.loads(data), addr))


async def _client():
    return await asyncio.get_running_loop().create_datagram_endpoint(
        Client, local_addr=("127.0.0.1", 0),
    )


def _request(nonce="test-123", **overrides):
    return json.dumps({
        "type": "BOSUN_DISCOVER", "version": 1, "nonce": nonce, **overrides,
    }).encode("utf-8")


def test_loopback_discovery_returns_configured_tcp_port_and_same_nonce(monkeypatch):
    monkeypatch.setattr(discovery.socket, "gethostname", lambda: "stage-pi")

    async def body():
        responder = await discovery.serve_discovery("127.0.0.1", 19876, port=0)
        transport, client = await _client()
        other_transport, other = await _client()
        address = responder._transport.get_extra_info("sockname")
        try:
            for nonce in ("test-123", "n" * 64, "pedale-è"):
                transport.sendto(_request(nonce), address)
                reply, source = await asyncio.wait_for(client.messages.get(), 1)
                assert source == address
                assert reply == {
                    "type": "BOSUN_HUB", "version": 1, "nonce": nonce,
                    "name": "stage-pi", "tcp_port": 19876,
                }
            # Discovery replies belong only to the requesting client.
            assert other.messages.empty()
        finally:
            transport.close()
            other_transport.close()
            responder.close()
            await responder.wait_closed()
        # Cleanup releases the listener, including on Windows exclusive binds.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(address)

    asyncio.run(body())


def test_malformed_or_oversize_requests_are_ignored_and_responder_stays_usable():
    async def body():
        responder = await discovery.serve_discovery("127.0.0.1", 9876, port=0)
        transport, client = await _client()
        address = responder._transport.get_extra_info("sockname")
        malformed = [
            b"", b"\xff", b"not-json", b"{", b"[]", b"null", b"true", b"{}",
            _request(type="PING"), _request(version=True), _request(version=1.0),
            _request(version="1"), _request(version=2), _request(nonce=None),
            _request(nonce=123), _request(nonce=""), _request(nonce="n" * 65),
            b'{"type":"BOSUN_DISCOVER","version":1}',
            _request() + b" " * discovery.MAX_DATAGRAM_BYTES,
        ]
        try:
            for data in malformed:
                transport.sendto(data, address)
            # A valid request behind the rejected packets must still work.
            transport.sendto(_request("after-malformed"), address)
            reply, _ = await asyncio.wait_for(client.messages.get(), 1)
            assert reply["nonce"] == "after-malformed"
            assert client.messages.empty()
        finally:
            transport.close()
            responder.close()
            await responder.wait_closed()

    asyncio.run(body())


class DisconnectedHub:
    """No Captain connection or protocol methods: discovery must not need them."""

    def __init__(self, target):
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def test_run_discovers_without_captain_and_closes_udp_on_shutdown(monkeypatch):
    hubs = []

    def create_hub(target):
        hub = DisconnectedHub(target)
        hubs.append(hub)
        return hub

    monkeypatch.setattr(server, "Hub", create_hub)

    async def body():
        task = asyncio.create_task(server.run(None, host="127.0.0.1", tcp_port=0, ws_port=0))
        transport, client = await _client()
        address = ("127.0.0.1", discovery.DISCOVERY_PORT)
        try:
            for _ in range(50):
                if task.done():
                    await task
                    pytest.fail("Hub exited during discovery startup")
                transport.sendto(_request("disconnected"), address)
                try:
                    reply, _ = await asyncio.wait_for(client.messages.get(), .05)
                    break
                except asyncio.TimeoutError:
                    continue
            else:
                pytest.fail("Discovery did not start")
            assert reply["nonce"] == "disconnected"
            assert 0 < reply["tcp_port"] <= 65535
            assert hubs[0].started
        finally:
            transport.close()
            task.cancel()
            await asyncio.wait_for(task, 2)
        assert hubs[0].stopped
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(address)

    asyncio.run(body())


def test_discovery_bind_failure_keeps_tcp_ws_and_stage_running(caplog):
    # Occupy the real fixed UDP port; exercise a real bind failure, while the
    # existing full-stack test checks TCP, WebSocket and Stage against its fake.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as blocker:
        blocker.bind(("127.0.0.1", discovery.DISCOVERY_PORT))
        _server_smoke()
    assert "LAN discovery unavailable" in caplog.text
    assert "manual TCP connection remains available" in caplog.text


def test_startup_failure_also_releases_discovery(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "Hub", DisconnectedHub)

    async def body():
        with pytest.raises(FileNotFoundError):
            await server.run(
                None, host="127.0.0.1", tcp_port=0, ws_port=0,
                stage_dir=tmp_path / "missing-stage",
            )
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", discovery.DISCOVERY_PORT))

    asyncio.run(body())
