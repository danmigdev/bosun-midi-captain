"""
The three network front-ends, all just views of the same :class:`Hub`
stream:

  - ``tcp``   raw byte pipe on :9876, wire-compatible with the editor's
              ``tcp_connect`` (editor/src-tauri/src/tcp_serial.rs). No
              framing, no status injection: whatever the pedal sends goes
              out verbatim and vice versa.
  - ``ws``    the same protocol lines as WebSocket text messages (one
              line per message) for the Stage kiosk browser, plus
              ``{"type":"HUB","link":...}`` status frames.
  - ``http``  static file server for the built Stage kiosk bundle.
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

log = logging.getLogger("bosun_hub.server")


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
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    text = raw.decode("utf-8", "replace").strip("\r")
                    if text:
                        sub.send(text)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            sub.close()
            down.cancel()
            writer.close()
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
        except Exception:  # noqa: BLE001 - normal close paths raise here
            pass
        finally:
            sub.close()
            down.cancel()
            log.info("ws client %s disconnected", ws.remote_address)

    server = await serve(handle, host, port)
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
    try:
        servers.append(await _serve_tcp(hub, host, tcp_port))
    except OSError as exc:
        log.warning("raw TCP listener not started on :%d: %s", tcp_port, exc)
    try:
        servers.append(await _serve_ws(hub, host, ws_port))
    except Exception as exc:  # noqa: BLE001
        log.warning("WebSocket listener not started: %s", exc)

    httpd: Optional[ThreadingHTTPServer] = None
    if stage_dir is not None and stage_dir.is_dir():
        httpd = _start_http(stage_dir, host, http_port)
    elif stage_dir is not None:
        log.warning("stage dir %s missing, HTTP not started", stage_dir)

    stop = asyncio.Event()
    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    finally:
        for s in servers:
            s.close()
        if httpd is not None:
            httpd.shutdown()
        hub.stop()
