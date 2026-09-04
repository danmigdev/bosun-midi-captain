"""
Fan-out multiplexer between one :class:`UpstreamLink` (its own thread)
and many asyncio consumers (raw TCP clients, WebSocket clients).

Rules:

  - every upstream protocol line goes to every subscriber's queue. The
    queue is bounded and drops its oldest entry when full, so one stuck
    socket can never back-pressure the pedal reader.
  - any subscriber's line is forwarded to the upstream write queue,
    which the link serialises onto the wire.
  - link up/down transitions are delivered to subscribers that asked
    for status (the Stage kiosk, so it can show a "reconnecting"
    overlay). The raw TCP endpoint does not get these: it stays a
    transparent byte pipe for editor compatibility.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from .link import UpstreamLink

log = logging.getLogger("bosun_hub.hub")

SUBSCRIBER_QUEUE_MAX = 512


class Subscription:
    """One consumer's view of the stream. Async-iterate :meth:`lines` for
    inbound protocol lines; call :meth:`send` to write one upstream."""

    def __init__(self, hub: "Hub", want_status: bool) -> None:
        self._hub = hub
        self._want_status = want_status
        self._queue: asyncio.Queue[Optional[str]] = asyncio.Queue(
            maxsize=SUBSCRIBER_QUEUE_MAX
        )

    def _offer(self, line: str) -> None:
        try:
            self._queue.put_nowait(line)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(line)
            except asyncio.QueueFull:
                pass

    async def lines(self):
        while True:
            line = await self._queue.get()
            if line is None:  # close sentinel
                return
            yield line

    def send(self, line: str) -> None:
        self._hub.link.send(line)

    def close(self) -> None:
        self._hub._remove(self)
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass


class Hub:
    def __init__(self, target: Optional[str]) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subs: set[Subscription] = set()
        self.link = UpstreamLink(
            target=target,
            on_line=self._on_upstream_line,
            on_state=self._on_upstream_state,
        )

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.link.start()

    def stop(self) -> None:
        self.link.stop()
        for sub in list(self._subs):
            sub.close()

    # -- subscribers ----------------------------------------------------

    def subscribe(self, want_status: bool = False) -> Subscription:
        sub = Subscription(self, want_status)
        self._subs.add(sub)
        if want_status:
            sub._offer(self._status_line(self.link.connected))
        return sub

    def _remove(self, sub: Subscription) -> None:
        self._subs.discard(sub)

    # -- upstream callbacks (called from the link thread) --------------

    def _on_upstream_line(self, line: str) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._dispatch, line)

    def _on_upstream_state(self, up: bool, detail: str) -> None:
        log.info("upstream link %s (%s)", "up" if up else "down", detail)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(
                self._dispatch_status, self._status_line(up)
            )

    def _dispatch(self, line: str) -> None:
        for sub in self._subs:
            sub._offer(line)

    def _dispatch_status(self, line: str) -> None:
        for sub in self._subs:
            if sub._want_status:
                sub._offer(line)

    @staticmethod
    def _status_line(up: bool) -> str:
        return json.dumps({"type": "HUB", "link": "up" if up else "down"})
