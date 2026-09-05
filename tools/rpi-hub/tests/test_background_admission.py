"""Response-coupled admission tests for Captain background generators.

The RP2040 firmware has room for one active generator plus eight queued
generators.  These tests make sure neither TCP/WebSocket fan-in nor unusual
request shapes can make the hub exceed the deliberately smaller safe window.
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bosun_hub.hub import (  # noqa: E402
    BACKGROUND_INFLIGHT_MAX,
    Hub,
    REQUESTS_MAX,
    REQUESTS_PER_SUB_MAX,
    _BACKGROUND_CLASS_BY_TYPE,
    _BACKGROUND_CLASS_LIMITS,
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


def test_manifest_plus_32_led_dumps_forwards_only_bulk_window_and_correlates_all():
    async def body():
        hub, link = _hub(request_timeout_s=60)
        sub = hub.subscribe()
        try:
            sub.send('{"type":"GET_MANIFEST","id":"manifest"}')
            for index in range(32):
                sub.send(json.dumps({
                    "type": "LED_DUMP", "id": f"led-{index:02d}",
                }))

            forwarded = _sent(link)
            assert [msg["type"] for msg in forwarded] == [
                "GET_MANIFEST", "LED_DUMP", "LED_DUMP",
            ]
            assert len(hub._background_tokens) == 3
            assert all(
                request["id"].startswith(hub._request_id_prefix)
                for request in forwarded
            )

            rejected = [await _message(sub) for _ in range(30)]
            assert [msg["id"] for msg in rejected] == [
                f"led-{index:02d}" for index in range(2, 32)
            ]
            assert all(msg == {
                "type": "ERROR", "error": "background_busy",
                "of": "LED_DUMP", "id": msg["id"],
            } for msg in rejected)

            for request in forwarded:
                response_type = (
                    "MANIFEST" if request["type"] == "GET_MANIFEST"
                    else "LED_DUMP"
                )
                hub._dispatch(json.dumps({
                    "type": response_type, "id": request["id"],
                }))
            replies = [await _message(sub) for _ in range(3)]
            assert [msg["id"] for msg in replies] == [
                "manifest", "led-00", "led-01",
            ]
            assert not hub._background_tokens
        finally:
            hub.stop()

    _run(body())


def test_class_reservations_admit_full_stage_batch_during_bulk_pressure():
    async def body():
        hub, link = _hub(request_timeout_s=60)
        sub = hub.subscribe()
        try:
            # Fill the expensive editor/diagnostic allowance first.
            for kind, ident in (
                ("GET_MANIFEST", "manifest"),
                ("GET_GLOBAL", "global"),
                ("STATS", "stats"),
            ):
                sub.send(json.dumps({"type": kind, "id": ident}))

            # Stage's live bootstrap and current/post-switch patch generations
            # all still fit.  They use the same subscriber here deliberately:
            # desktop Stage reaches the hub over raw TCP, not status WebSocket.
            sub.send('{"type":"GET_DEVICE_INFO","id":"device"}')
            sub.send('{"type":"LIST_PATCHES","id":"patch-list"}')
            sub.send('{"type":"GET_CONTEXT","id":"context"}')
            sub.send('{"type":"GET_PATCH","id":"patch-1","bank":1,"slot":1}')
            sub.send('{"type":"GET_PATCH","id":"patch-2","bank":1,"slot":2}')

            assert len(link.sent) == BACKGROUND_INFLIGHT_MAX
            assert len(hub._background_tokens) == BACKGROUND_INFLIGHT_MAX
            assert Counter(hub._background_tokens.values()) == Counter(
                _BACKGROUND_CLASS_LIMITS
            )

            # Coalescing does not spend a ninth token or reject another Stage
            # waiter for the same snapshot.
            sub.send('{"type":"GET_CONTEXT","id":"context-joined"}')
            sub.send('{"type":"GET_PATCH","id":"patch-joined","bank":1,"slot":1}')
            assert len(link.sent) == BACKGROUND_INFLIGHT_MAX
            assert sub._queue.empty()

            # Each class fails closed under its original id.
            checks = (
                ({"type": "LED_DUMP", "id": "bulk-over"}, "background_busy"),
                ({"type": "GET_PATCH", "id": "patch-over",
                  "bank": 1, "slot": 3}, "patch_busy"),
                ({"type": "GET_DEVICE_INFO", "id": "device-over"},
                 "background_busy"),
                ({"type": "LIST_PATCHES", "id": "list-over"},
                 "background_busy"),
            )
            for request, expected_error in checks:
                sub.send(json.dumps(request))
                reply = await _message(sub)
                assert reply == {
                    "type": "ERROR", "error": expected_error,
                    "of": request["type"], "id": request["id"],
                }
            assert len(link.sent) == BACKGROUND_INFLIGHT_MAX
        finally:
            hub.stop()

    _run(body())


def test_mutation_and_immediate_rpc_are_not_starved_by_background_saturation():
    async def body():
        hub, link = _hub(request_timeout_s=60)
        sub = hub.subscribe()
        try:
            requests = [
                {"type": "GET_MANIFEST", "id": "m"},
                {"type": "GET_GLOBAL", "id": "g"},
                {"type": "LED_DUMP", "id": "l"},
                {"type": "GET_DEVICE_INFO", "id": "d"},
                {"type": "LIST_PATCHES", "id": "pl"},
                {"type": "GET_CONTEXT", "id": "c"},
                {"type": "GET_PATCH", "id": "p1", "bank": 1, "slot": 1},
                {"type": "GET_PATCH", "id": "p2", "bank": 1, "slot": 2},
            ]
            for request in requests:
                sub.send(json.dumps(request))
            assert len(hub._background_tokens) == 8

            sub.send('{"type":"SWITCH_PATCH","id":"switch","bank":2,"slot":1}')
            sub.send('{"type":"PING","id":"ping"}')
            sent = _sent(link)
            assert [msg["type"] for msg in sent[-2:]] == ["SWITCH_PATCH", "PING"]
            assert len(hub._background_tokens) == 8

            for request, response_type in zip(sent[-2:], ("ACK", "ACK")):
                hub._dispatch(json.dumps({
                    "type": response_type, "id": request["id"],
                }))
            assert (await _message(sub))["id"] == "switch"
            assert (await _message(sub))["id"] == "ping"
        finally:
            hub.stop()

    _run(body())


def test_idless_background_is_counted_and_reply_keeps_broadcast_semantics():
    async def body():
        hub, link = _hub(request_timeout_s=60)
        requester = hub.subscribe()
        observer = hub.subscribe()
        try:
            requester.send('{"type":"LED_DUMP"}')
            upstream = _sent(link)[0]
            assert upstream["id"].startswith(hub._request_id_prefix)
            assert hub._background_tokens == {upstream["id"]: "bulk"}

            hub._dispatch(json.dumps({
                "type": "LED_DUMP", "id": upstream["id"], "pixels": [],
            }))
            expected = {"type": "LED_DUMP", "pixels": []}
            assert await _message(requester) == expected
            assert await _message(observer) == expected
            assert not hub._background_tokens

            # Fill bulk, then prove an idless fourth request cannot bypass it.
            for kind in ("GET_MANIFEST", "GET_GLOBAL", "STATS"):
                requester.send(json.dumps({"type": kind}))
            before = len(link.sent)
            requester.send('{"type":"LED_DUMP"}')
            assert len(link.sent) == before
            assert await _message(requester) == {
                "type": "ERROR", "error": "background_busy",
                "of": "LED_DUMP",
            }
            assert observer._queue.empty()
        finally:
            hub.stop()

    _run(body())


def test_idless_background_outlives_requester_and_still_broadcasts_reply():
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
            assert hub._background_tokens == {private_id: "bulk"}

            hub._dispatch(json.dumps({
                "type": "LED_DUMP", "id": private_id, "pixels": [],
            }))
            assert await _message(observer) == {
                "type": "LED_DUMP", "pixels": [],
            }
            assert not hub._request_pending
            assert not hub._background_tokens
        finally:
            hub.stop()

    _run(body())


def test_idless_background_timeout_is_broadcast_and_late_reply_only_releases_token():
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
            assert hub._background_tokens == {private_id: "orphan"}

            hub._dispatch(json.dumps({
                "type": "LED_DUMP", "id": private_id, "pixels": [],
            }))
            assert requester._queue.empty() and observer._queue.empty()
            assert not hub._background_tokens
        finally:
            hub.stop()

    _run(body())


def test_timeout_and_subscriber_close_keep_orphan_tokens_until_reply_or_down():
    async def body():
        hub, link = _hub(request_timeout_s=0.02)
        first = hub.subscribe()
        second = hub.subscribe()
        try:
            first.send('{"type":"LED_DUMP","id":"slow"}')
            slow_id = _sent(link)[0]["id"]
            assert (await _message(first))["error"] == "request_timeout"
            assert hub._background_tokens == {slow_id: "orphan"}

            # Orphans still occupy the physical/global window, but no longer
            # consume the live bulk reservation, so recovery can proceed.
            second.send('{"type":"LED_DUMP","id":"retry"}')
            retry_id = _sent(link)[1]["id"]
            assert Counter(hub._background_tokens.values()) == Counter({
                "orphan": 1, "bulk": 1,
            })
            second.close()
            assert retry_id in hub._background_tokens
            assert retry_id not in hub._request_pending

            hub._dispatch(json.dumps({
                "type": "LED_DUMP", "id": slow_id, "pixels": [],
            }))
            assert slow_id not in hub._background_tokens
            # Its downstream correlation already expired, so no stale reply.
            assert first._queue.empty()

            hub.link.connected = False
            hub._dispatch_status(hub._status_line(False))
            assert not hub._background_tokens
        finally:
            hub.stop()

    _run(body())


def test_noncanonical_context_and_patch_cannot_bypass_background_classes():
    async def body():
        hub, link = _hub(request_timeout_s=60)
        sub = hub.subscribe()
        try:
            sub.send('{"type":"GET_CONTEXT","id":"future","scope":"x"}')
            sub.send('{"type":"GET_PATCH","bank":true,"slot":1}')
            sent = _sent(link)
            assert sent[0]["id"].startswith(hub._request_id_prefix)
            assert sent[1]["id"].startswith(hub._request_id_prefix)
            assert Counter(hub._background_tokens.values()) == Counter({
                "context": 1, "patch": 1,
            })

            # The context class is occupied even though canonical coalescing
            # deliberately rejected the unknown selector shape.
            sub.send('{"type":"GET_CONTEXT","id":"canonical"}')
            assert await _message(sub) == {
                "type": "ERROR", "error": "context_busy",
                "of": "GET_CONTEXT", "id": "canonical",
            }
            assert len(link.sent) == 2
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


def test_hub_background_type_inventory_matches_firmware_dispatch():
    """Fail CI when firmware adds a generator without an admission class."""

    source_path = (
        Path(__file__).resolve().parents[3]
        / "firmware" / "lib" / "captain" / "protocol.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    methods = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Protocol"
        for node in node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    def self_calls(node: ast.AST) -> set[str]:
        return {
            call.func.attr
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "self"
        }

    reaches_background = {
        name for name, node in methods.items()
        if "_start_background" in self_calls(node)
    }
    changed = True
    while changed:
        before = len(reaches_background)
        reaches_background.update(
            name for name, node in methods.items()
            if self_calls(node) & reaches_background
        )
        changed = len(reaches_background) != before

    handle = methods["handle"]
    discovered: set[str] = set()
    for branch in ast.walk(handle):
        if not isinstance(branch, ast.If):
            continue
        compare = branch.test
        if not (
            isinstance(compare, ast.Compare)
            and isinstance(compare.left, ast.Name)
            and compare.left.id == "t"
            and len(compare.ops) == len(compare.comparators) == 1
            and isinstance(compare.ops[0], ast.Eq)
            and isinstance(compare.comparators[0], ast.Constant)
            and isinstance(compare.comparators[0].value, str)
        ):
            continue
        calls = set().union(*(self_calls(statement) for statement in branch.body))
        if "_start_background" in calls or calls & reaches_background:
            discovered.add(compare.comparators[0].value)

    assert discovered == set(_BACKGROUND_CLASS_BY_TYPE)
