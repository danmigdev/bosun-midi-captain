"""Ordinary request caps and tracking of reads still queued on the Captain.

A read stays tracked until the Captain answers it or the session ends, even
after its client timed out or closed: a firmware update waits for the
tracked reads to drain before it takes the serial port.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bosun_hub.hub import (  # noqa: E402
    Hub,
    REQUESTS_MAX,
    REQUESTS_PER_SUB_MAX,
)


class RecordingLink:
    def __init__(self) -> None:
        self.connected = True
        self.sent: list[str] = []

    def send(self, line: str) -> bool:
        self.sent.append(line)
        return True

    def stop(self) -> None:
        pass


def _run(coro):
    return asyncio.run(coro)


def _hub(**kwargs) -> tuple[Hub, RecordingLink]:
    hub = Hub(None, **kwargs)
    link = RecordingLink()
    hub.link = link
    return hub, link


async def _message(sub, timeout: float = 0.5) -> dict:
    raw = await asyncio.wait_for(sub._queue.get(), timeout)
    assert raw is not None
    return json.loads(raw)


def _sent(link: RecordingLink) -> list[dict]:
    return [json.loads(line) for line in link.sent]


def test_idless_read_is_tracked_and_reply_keeps_broadcast_semantics():
    async def body():
        hub, link = _hub(request_timeout_s=60)
        requester = hub.subscribe()
        observer = hub.subscribe()
        try:
            requester.send('{"type":"LED_DUMP"}')
            upstream = _sent(link)[0]
            assert upstream["id"].startswith(hub._request_id_prefix)
            assert hub._upstream_reads == {upstream["id"]}

            hub._dispatch(json.dumps({
                "type": "LED_DUMP", "id": upstream["id"], "pixels": [],
            }))
            expected = {"type": "LED_DUMP", "pixels": []}
            assert await _message(requester) == expected
            assert await _message(observer) == expected
            assert not hub._upstream_reads

            # Without an admission window every idless read goes upstream.
            for kind in ("GET_MANIFEST", "GET_GLOBAL", "STATS", "LED_DUMP"):
                requester.send(json.dumps({"type": kind}))
            assert len(link.sent) == 5
            assert len(hub._upstream_reads) == 4
        finally:
            hub.stop()

    _run(body())


def test_idless_read_outlives_requester_and_still_broadcasts_reply():
    async def body():
        hub, link = _hub(request_timeout_s=60)
        requester = hub.subscribe()
        observer = hub.subscribe()
        try:
            requester.send('{"type":"LED_DUMP"}')
            upstream = _sent(link)[0]
            private_id = upstream["id"]

            requester.close()
            assert requester not in hub._request_ids_by_sub
            assert hub._request_pending[private_id].sub is None
            assert hub._upstream_reads == {private_id}

            hub._dispatch(json.dumps({
                "type": "LED_DUMP", "id": private_id, "pixels": [],
            }))
            assert await _message(observer) == {
                "type": "LED_DUMP", "pixels": [],
            }
            assert not hub._request_pending
            assert not hub._upstream_reads
        finally:
            hub.stop()

    _run(body())


def test_idless_read_timeout_is_broadcast_and_late_reply_ends_tracking():
    async def body():
        hub, link = _hub(request_timeout_s=0.02)
        requester = hub.subscribe()
        observer = hub.subscribe()
        try:
            requester.send('{"type":"LED_DUMP"}')
            private_id = _sent(link)[0]["id"]
            expected = {
                "type": "ERROR", "error": "request_timeout",
                "of": "LED_DUMP",
            }
            assert await _message(requester) == expected
            assert await _message(observer) == expected
            assert hub._upstream_reads == {private_id}

            hub._dispatch(json.dumps({
                "type": "LED_DUMP", "id": private_id, "pixels": [],
            }))
            assert requester._queue.empty() and observer._queue.empty()
            assert not hub._upstream_reads
        finally:
            hub.stop()

    _run(body())


def test_timeout_and_subscriber_close_keep_reads_tracked_until_reply_or_down():
    async def body():
        hub, link = _hub(request_timeout_s=0.02)
        first = hub.subscribe()
        second = hub.subscribe()
        try:
            first.send('{"type":"LED_DUMP","id":"slow"}')
            slow_id = _sent(link)[0]["id"]
            assert (await _message(first))["error"] == "request_timeout"
            assert hub._upstream_reads == {slow_id}

            second.send('{"type":"LED_DUMP","id":"retry"}')
            retry_id = _sent(link)[1]["id"]
            assert hub._upstream_reads == {slow_id, retry_id}
            second.close()
            assert retry_id in hub._upstream_reads
            assert retry_id not in hub._request_pending

            hub._dispatch(json.dumps({
                "type": "LED_DUMP", "id": slow_id, "pixels": [],
            }))
            assert slow_id not in hub._upstream_reads
            # Its downstream correlation already expired, so no stale reply.
            assert first._queue.empty()

            hub.link.connected = False
            hub._dispatch_status(hub._status_line(False))
            assert not hub._upstream_reads
        finally:
            hub.stop()

    _run(body())


def test_ordinary_per_subscriber_cap_preserves_other_client_and_reports_mutation():
    async def body():
        hub, link = _hub(request_timeout_s=60)
        noisy = hub.subscribe()
        healthy = hub.subscribe()
        try:
            for index in range(REQUESTS_PER_SUB_MAX):
                noisy.send(json.dumps({"type": "PING", "id": f"n-{index}"}))
            assert len(link.sent) == REQUESTS_PER_SUB_MAX

            # SWITCH_PATCH is intentionally not forwarded after the caller's
            # own fairness budget is exhausted, but it is never silent: the
            # exact original id receives an immediate correlated failure.
            noisy.send(
                '{"type":"SWITCH_PATCH","id":"important","bank":2,"slot":1}'
            )
            assert await _message(noisy) == {
                "type": "ERROR", "error": "request_busy",
                "of": "SWITCH_PATCH", "id": "important",
            }
            assert len(link.sent) == REQUESTS_PER_SUB_MAX

            healthy.send('{"type":"PING","id":"healthy"}')
            assert len(link.sent) == REQUESTS_PER_SUB_MAX + 1
            assert _sent(link)[-1]["type"] == "PING"
        finally:
            hub.stop()

    _run(body())


def test_ordinary_global_cap_is_exact_across_clients_and_never_silent():
    async def body():
        hub, link = _hub(request_timeout_s=60)
        first = hub.subscribe()
        second = hub.subscribe()
        third = hub.subscribe()
        try:
            assert REQUESTS_MAX == 2 * REQUESTS_PER_SUB_MAX
            for sub, prefix in ((first, "a"), (second, "b")):
                for index in range(REQUESTS_PER_SUB_MAX):
                    sub.send(json.dumps({
                        "type": "PING", "id": f"{prefix}-{index}",
                    }))
            assert len(link.sent) == REQUESTS_MAX
            assert len(hub._request_pending) == REQUESTS_MAX

            third.send(
                '{"type":"PUT_BINDING","id":"write","bank":1,"slot":1,'
                '"binding":{"switch":"1"}}'
            )
            assert await _message(third) == {
                "type": "ERROR", "error": "request_busy",
                "of": "PUT_BINDING", "id": "write",
            }
            assert len(link.sent) == REQUESTS_MAX
            assert len(hub._request_pending) == REQUESTS_MAX
        finally:
            hub.stop()

    _run(body())
