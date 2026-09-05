"""
The three network front-ends, all just views of the same :class:`Hub`
stream:

  - ``tcp``   line-JSON endpoint on :9876, wire-compatible with the editor's
              ``tcp_connect`` (editor/src-tauri/src/tcp_serial.rs). It has no
              status injection; the hub may fulfil/cache/coalesce protocol
              reads locally while preserving each caller's correlation id.
  - ``ws``    the same protocol lines as WebSocket text messages (one
              line per message) for the Stage kiosk browser, plus
              ``{"type":"HUB","link":...}`` status frames.
  - ``http``  static file server for the built Stage kiosk bundle.

UDP discovery on :9877 advertises the TCP endpoint without contacting the
Captain or subscribing to its protocol stream.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from .hub import Hub
from .discovery import DISCOVERY_PORT, DiscoveryProtocol, serve_discovery

log = logging.getLogger("bosun_hub.server")
MAX_CLIENT_LINE = 64 * 1024


# --------------------------------------------------------------------------
# raw TCP  (:9876)
# --------------------------------------------------------------------------


async def _serve_tcp(hub: Hub, host: str, port: int) -> asyncio.AbstractServer:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        log.info("tcp client %s connected", peer)
        sub = hub.subscribe(want_status=False)

        async def pump_down() -> None:
            async for line in sub.lines():
                writer.write(line.encode("utf-8") + b"\n")
                await writer.drain()

        down = asyncio.create_task(pump_down())
        buf = b""
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                buf += data
                if len(buf) > MAX_CLIENT_LINE and b"\n" not in buf:
                    log.warning("tcp client %s exceeded line limit", peer)
                    break
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    if len(raw) > MAX_CLIENT_LINE:
                        raise ConnectionError("protocol line too large")
                    text = raw.decode("utf-8", "replace").strip("\r")
                    if text:
                        sub.send(text)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            sub.close()
            down.cancel()
            await asyncio.gather(down, return_exceptions=True)
            writer.close()
            await writer.wait_closed()
            log.info("tcp client %s disconnected", peer)

    server = await asyncio.start_server(handle, host, port)
    log.info("raw TCP protocol on %s:%d", host, port)
    return server


# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------


async def _serve_ws(hub: Hub, host: str, port: int):
    from websockets.asyncio.server import serve

    async def handle(ws) -> None:
        log.info("ws client %s connected", ws.remote_address)
        sub = hub.subscribe(want_status=True)

        async def pump_down() -> None:
            async for line in sub.lines():
                await ws.send(line)

        down = asyncio.create_task(pump_down())
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    message = message.decode("utf-8", "replace")
                for part in message.splitlines():
                    part = part.strip()
                    if part:
                        sub.send(part)
        except Exception as exc:  # noqa: BLE001 - normal close paths raise here
            log.debug("ws %s recv loop ended: %r", ws.remote_address, exc)
        finally:
            sub.close()
            down.cancel()
            reason = getattr(ws, "close_reason", None)
            code = getattr(ws, "close_code", None)
            log.info(
                "ws client %s disconnected (code=%s reason=%r)",
                ws.remote_address, code, reason,
            )

    # Keep long-idle connections alive: the Stage feed can be silent for
    # minutes and some paths (browser throttling, a busy Pi) delay the
    # pong past the default 20 s. Be generous.
    server = await serve(
        handle, host, port, ping_interval=30, ping_timeout=90,
        max_size=MAX_CLIENT_LINE, max_queue=16,
    )
    log.info("WebSocket protocol on ws://%s:%d", host, port)
    return server


# --------------------------------------------------------------------------
# static HTTP  (Stage kiosk bundle)
# --------------------------------------------------------------------------


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.debug("http %s - %s", self.address_string(), fmt % args)

    def end_headers(self) -> None:
        # The kiosk is a single local client; make sure it never serves a
        # stale bundle after an update.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def _start_http(stage_dir: Path, host: str, port: int) -> ThreadingHTTPServer:
    handler = functools.partial(_QuietHandler, directory=str(stage_dir))
    httpd = ThreadingHTTPServer((host, port), handler)
    threading.Thread(
        target=httpd.serve_forever, name="bosun-hub-http", daemon=True
    ).start()
    log.info("static Stage bundle from %s on http://%s:%d", stage_dir, host, port)
    return httpd


# --------------------------------------------------------------------------
# run everything
# --------------------------------------------------------------------------


async def run(
    target: Optional[str],
    *,
    host: str = "0.0.0.0",
    tcp_port: int = 9876,
    ws_port: int = 8081,
    http_port: int = 8080,
    stage_dir: Optional[Path] = None,
) -> None:
    hub = Hub(target)
    hub.start()
    servers = []
    discovery: DiscoveryProtocol | None = None
    httpd: Optional[ThreadingHTTPServer] = None
    try:
        tcp_server = await _serve_tcp(hub, host, tcp_port)
        servers.append(tcp_server)
        servers.append(await _serve_ws(hub, host, ws_port))
        try:
            # Resolve port=0 too, useful when running a local test instance.
            bound_tcp_port = tcp_server.sockets[0].getsockname()[1]
            discovery = await serve_discovery(host, bound_tcp_port)
        except Exception as exc:  # discovery must not take down the endpoints
            log.warning(
                "LAN discovery unavailable on %s:%d: %s; manual TCP connection remains available",
                host, DISCOVERY_PORT, exc,
            )
        if stage_dir is not None and stage_dir.is_dir():
            httpd = _start_http(stage_dir, host, http_port)
        elif stage_dir is not None:
            raise FileNotFoundError(f"stage dir {stage_dir} missing")
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        if discovery is not None:
            discovery.close()
        for s in servers:
            s.close()
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        hub.stop()
        await asyncio.gather(*(s.wait_closed() for s in servers), return_exceptions=True)
        if discovery is not None:
            await discovery.wait_closed()
