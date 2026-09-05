"""Small, local-network discovery replies, independent of the Captain link."""

from __future__ import annotations

import asyncio
import json
import logging
import socket

log = logging.getLogger("bosun_hub.discovery")

DISCOVERY_PORT = 9877
MAX_DATAGRAM_BYTES = 512
MAX_NONCE_LENGTH = 64


class DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, tcp_port: int) -> None:
        self._tcp_port = tcp_port
        self._name = socket.gethostname()
        self._transport: asyncio.DatagramTransport | None = None
        self._closed = asyncio.get_running_loop().create_future()

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self._transport = transport

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        if not data or len(data) > MAX_DATAGRAM_BYTES:
            return
        try:
            request = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(request, dict):
            return
        nonce = request.get("nonce")
        if (request.get("type") != "BOSUN_DISCOVER"
                or type(request.get("version")) is not int
                or request["version"] != 1
                or not isinstance(nonce, str)
                or not 1 <= len(nonce) <= MAX_NONCE_LENGTH):
            return
        if self._transport is None or self._transport.is_closing():
            return
        response = {
            "type": "BOSUN_HUB", "version": 1, "nonce": nonce,
            "name": self._name, "tcp_port": self._tcp_port,
        }
        # Reply only to the sender. This module never subscribes to Hub or
        # probes the Captain, so an unplugged pedal remains discoverable.
        self._transport.sendto(
            json.dumps(response, separators=(",", ":")).encode("utf-8"), addr,
        )

    def error_received(self, exc: Exception) -> None:
        log.warning("LAN discovery UDP error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        if exc is not None:
            log.warning("LAN discovery socket closed: %s", exc)
        if not self._closed.done():
            self._closed.set_result(None)

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()

    async def wait_closed(self) -> None:
        await self._closed


async def serve_discovery(
    host: str, tcp_port: int, *, port: int = DISCOVERY_PORT,
) -> DiscoveryProtocol:
    """Bind discovery to the server's interface; ``port`` supports local tests."""
    _, protocol = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: DiscoveryProtocol(tcp_port), local_addr=(host, port),
    )
    log.info("LAN discovery on %s:%d (TCP %d)", host, port, tcp_port)
    return protocol
