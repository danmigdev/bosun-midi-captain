"""Regression tests for keyed GET_PATCH single-flight and cache routing."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bosun_hub.hub import Hub  # noqa: E402
from bosun_hub.server import _serve_tcp, _serve_ws  # noqa: E402
from tests.fake_pedal import FakePedal  # noqa: E402


class RecordingLink:
    def __init__(self) -> None:
        self.connected = True
        self.sent: list[str] = []

    def send(self, line: str) -> None:
        self.sent.append(line)

    def stop(self) -> None:
        pass


class RejectingLink(RecordingLink):
    def send(self, line: str) -> bool:
        self.sent.append(line)
        return False


class DeferredPatchPedal(FakePedal):
    def _reply(self, msg):
        if msg.get("type") == "GET_PATCH":
            return None
        return super()._reply(msg)


def _run(coro):
    return asyncio.run(coro)


async def _message(sub, timeout: float = 1.0) -> dict:
    line = await asyncio.wait_for(sub._queue.get(), timeout)
    assert line is not None
    return json.loads(line)


async def _wait(predicate, timeout: float = 6.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def _patch_requests(lines: list[str]) -> list[dict]:
    requests = []
    for line in lines:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("type") == "GET_PATCH":
            requests.append(msg)
    return requests


def _patch_response(request: dict, name: str = "Patch") -> dict:
    return {
        "type": "PATCH",
        "id": request["id"],
        "bank": request["bank"],
        "slot": request["slot"],
        "profile": request.get("profile", ""),
        "patch": {"name": name, "bindings": []},
    }


def test_patch_same_key_coalesces_and_preserves_all_id_forms_then_caches():
    async def body():
        hub = Hub(None, patch_timeout_s=1.0)
        link = RecordingLink()
        hub.link = link
        first = hub.subscribe()
        second = hub.subscribe()

        first.send('{"type":"GET_PATCH","id":"same","bank":2,"slot":3}')
        first.send('{"type":"GET_PATCH","id":"same","bank":2,"slot":3}')
        first.send('{"type":"GET_PATCH","bank":2,"slot":3}')
        second.send('{"type":"GET_PATCH","id":null,"bank":2,"slot":3,"profile":null}')
        second.send('{"type":"GET_PATCH","id":"same","bank":2,"slot":3}')
        requests = _patch_requests(link.sent)
        assert len(requests) == 1
        assert requests[0]["id"].startswith("__bosun_patch_")

        # A foreign/legacy response is still a broadcast, but cannot settle
        # or poison the cache for the private flight.
        foreign = {
            "type": "PATCH", "id": "foreign", "bank": 2, "slot": 3,
            "profile": "", "patch": {"name": "Foreign"},
        }
        hub._dispatch(json.dumps(foreign))
        assert await _message(first) == foreign
        assert await _message(second) == foreign
        assert hub._patch_flights
        assert ("", 2, 3) not in hub._patch_cache

        response = _patch_response(requests[0], "Coalesced")
        hub._dispatch(json.dumps(response))
        first_replies = [await _message(first) for _ in range(3)]
        second_replies = [await _message(second) for _ in range(2)]
        assert [msg.get("id") for msg in first_replies] == ["same", "same", None]
        assert "id" not in first_replies[2]
        assert "id" in second_replies[0] and second_replies[0]["id"] is None
        assert second_replies[1]["id"] == "same"
        assert all(msg["patch"]["name"] == "Coalesced"
                   for msg in first_replies + second_replies)
        assert not hub._patch_flights

        before = len(link.sent)
        second.send('{"type":"GET_PATCH","id":"warm","bank":2,"slot":3}')
        warm = await _message(second)
        assert warm["id"] == "warm" and warm["patch"]["name"] == "Coalesced"
        assert len(link.sent) == before
        hub.stop()

    _run(body())


def test_patch_different_keys_and_profiles_have_independent_flights():
    async def body():
        hub = Hub(None, patch_timeout_s=1.0)
        link = RecordingLink()
        hub.link = link
        first = hub.subscribe()
        second = hub.subscribe()
        third = hub.subscribe()
        first.send('{"type":"GET_PATCH","id":"one","bank":1,"slot":1}')
        second.send('{"type":"GET_PATCH","id":"two","bank":1,"slot":2}')
        third.send('{"type":"GET_PATCH","id":"named","bank":1,"slot":1,"profile":"clean"}')
        requests = _patch_requests(link.sent)
        assert len(requests) == 2
        assert await _message(third) == {
            "type": "ERROR", "id": "named", "error": "patch_busy",
            "of": "GET_PATCH",
        }
        by_key = {
            (msg.get("profile", ""), msg["bank"], msg["slot"]): msg
            for msg in requests
        }

        hub._dispatch(json.dumps(_patch_response(by_key[("", 1, 2)], "Two")))
        assert (await _message(second))["id"] == "two"
        assert first._queue.empty() and third._queue.empty()
        # The released slot admits a retry for the independent profile key.
        third.send('{"type":"GET_PATCH","id":"named-retry","bank":1,"slot":1,"profile":"clean"}')
        named_request = _patch_requests(link.sent)[-1]
        hub._dispatch(json.dumps(_patch_response(named_request, "Named")))
        assert (await _message(third))["patch"]["name"] == "Named"
        assert first._queue.empty()
        hub._dispatch(json.dumps(_patch_response(by_key[("", 1, 1)], "One")))
        assert (await _message(first))["patch"]["name"] == "One"
        assert set(hub._patch_cache) == {
            ("", 1, 1), ("", 1, 2), ("clean", 1, 1),
        }
        hub.stop()

    _run(body())


def test_patch_noncanonical_requests_are_private_and_admission_limited():
    async def body():
        hub = Hub(None, patch_timeout_s=1.0)
        link = RecordingLink()
        hub.link = link
        first = hub.subscribe()
        second = hub.subscribe()
        originals = [
            {"type": "GET_PATCH", "id": "future", "bank": 1, "slot": 1,
             "future_selector": True},
            {"type": "GET_PATCH", "id": "bad-bank", "bank": True, "slot": 1},
            {"type": "GET_PATCH", "id": "bad-profile", "bank": 1, "slot": 1,
             "profile": 7},
        ]
        for request in originals:
            first.send(json.dumps(request))
        forwarded = [json.loads(line) for line in link.sent]
        assert len(forwarded) == 2
        for original, upstream in zip(originals, forwarded):
            assert upstream["type"] == original["type"]
            assert upstream["id"].startswith("__bosun_req_")
            assert upstream["id"] != original["id"]
            assert {k: v for k, v in upstream.items() if k != "id"} == {
                k: v for k, v in original.items() if k != "id"
            }
        assert await _message(first) == {
            "type": "ERROR", "id": "bad-profile", "error": "patch_busy",
            "of": "GET_PATCH",
        }
        assert not hub._patch_flights

        # A future-selector reply remains private and restores the exact
        # caller id even though it bypassed keyed coalescing/cache semantics.
        hub._dispatch(json.dumps({
            "type": "PATCH", "id": forwarded[0]["id"],
            "bank": 1, "slot": 1, "profile": "",
            "patch": {"name": "future"},
        }))
        assert (await _message(first))["id"] == "future"
        assert second._queue.empty()

        ordinary = {
            "type": "PATCH", "id": "__bosun_patch_client",
            "bank": 1, "slot": 1, "profile": "",
            "patch": {"name": "ordinary"},
        }
        hub._dispatch(json.dumps(ordinary))
        assert await _message(first) == ordinary
        assert await _message(second) == ordinary
        assert not hub._patch_cache
        hub.stop()

    _run(body())


def test_patch_legacy_named_profile_reply_is_delivered_but_not_cached():
    async def body():
        hub = Hub(None, patch_timeout_s=1.0)
        link = RecordingLink()
        hub.link = link
        sub = hub.subscribe()
        sub.send('{"type":"GET_PATCH","id":"legacy","bank":2,"slot":1,"profile":"clean"}')
        request = _patch_requests(link.sent)[0]
        legacy = _patch_response(request, "Legacy active")
        legacy.pop("profile")
        hub._dispatch(json.dumps(legacy))
        reply = await _message(sub)
        assert reply["id"] == "legacy"
        assert reply["patch"]["name"] == "Legacy active"
        assert ("clean", 2, 1) not in hub._patch_cache

        # Since provenance was ambiguous, the same named request must reach
        # upstream again instead of receiving a falsely scoped cache hit.
        sub.send('{"type":"GET_PATCH","id":"again","bank":2,"slot":1,"profile":"clean"}')
        assert len(_patch_requests(link.sent)) == 2
        hub.stop()

    _run(body())


def test_patch_switch_barrier_starts_one_fresh_generation_and_skips_stale_cache():
    async def body():
        hub = Hub(None, patch_timeout_s=1.0)
        link = RecordingLink()
        hub.link = link
        before = hub.subscribe()
        after = hub.subscribe()
        before.send('{"type":"GET_PATCH","id":"before","bank":3,"slot":4}')
        old = _patch_requests(link.sent)[0]
        before.send('{"type":"SWITCH_PATCH","id":"sw","bank":3,"slot":4}')
        after.send('{"type":"GET_PATCH","id":"after","bank":3,"slot":4}')
        assert len(_patch_requests(link.sent)) == 1
        flight = hub._patch_flights[("", 3, 4)]
        assert flight.sealed and len(flight.next_waiters) == 1

        hub._dispatch(json.dumps(_patch_response(old, "Before")))
        assert (await _message(before))["id"] == "before"
        assert ("", 3, 4) not in hub._patch_cache
        requests = _patch_requests(link.sent)
        assert len(requests) == 2 and requests[1]["id"] != old["id"]
        assert after._queue.empty()

        hub._dispatch(json.dumps(_patch_response(requests[1], "After")))
        reply = await _message(after)
        assert reply["id"] == "after" and reply["patch"]["name"] == "After"
        assert hub._patch_cache[("", 3, 4)]["patch"]["name"] == "After"
        hub.stop()

    _run(body())


def test_patch_mutation_invalidates_all_profile_aliases_only_at_location():
    async def body():
        hub = Hub(None, patch_timeout_s=1.0)
        link = RecordingLink()
        hub.link = link
        sub = hub.subscribe()
        for profile, bank, slot in (("", 1, 1), ("clean", 1, 1), ("", 2, 1)):
            hub._patch_cache[(profile, bank, slot)] = {
                "type": "PATCH", "profile": profile, "bank": bank, "slot": slot,
                "patch": {"name": profile or "active"},
            }

        sub.send(json.dumps({
            "type": "PUT_PATCH", "id": "put", "bank": 1, "slot": 1,
            "profile": "clean", "patch": {"name": "changed"},
        }))
        assert ("", 1, 1) not in hub._patch_cache
        assert ("clean", 1, 1) not in hub._patch_cache
        assert ("", 2, 1) in hub._patch_cache

        # A profile switch has ambiguous active aliases, so it invalidates all.
        sub.send('{"type":"SWITCH_PROFILE","id":"profile","profile_id":"clean"}')
        assert not hub._patch_cache
        hub.stop()

    _run(body())


def test_patch_mutation_seals_active_and_named_aliases_with_independent_next_batches():
    async def body():
        hub = Hub(None, patch_timeout_s=1.0)
        link = RecordingLink()
        hub.link = link
        active = hub.subscribe()
        named = hub.subscribe()
        active.send('{"type":"GET_PATCH","id":"active-old","bank":3,"slot":2}')
        named.send('{"type":"GET_PATCH","id":"named-old","bank":3,"slot":2,"profile":"clean"}')
        old_requests = _patch_requests(link.sent)
        active_old = next(msg for msg in old_requests if "profile" not in msg)
        named_old = next(msg for msg in old_requests if msg.get("profile") == "clean")

        active.send(json.dumps({
            "type": "PUT_PATCH", "id": "mutation", "bank": 3, "slot": 2,
            "profile": "clean", "patch": {"name": "new"},
        }))
        active.send('{"type":"GET_PATCH","id":"active-new","bank":3,"slot":2}')
        named.send('{"type":"GET_PATCH","id":"named-new","bank":3,"slot":2,"profile":"clean"}')
        assert hub._patch_flights[("", 3, 2)].sealed
        assert hub._patch_flights[("clean", 3, 2)].sealed
        assert len(_patch_requests(link.sent)) == 2

        # Complete the two old generations out of order. Each promotes only
        # its own keyed NEXT batch and neither old payload enters the cache.
        hub._dispatch(json.dumps(_patch_response(named_old, "Named old")))
        assert (await _message(named))["id"] == "named-old"
        assert len(_patch_requests(link.sent)) == 3
        assert ("clean", 3, 2) not in hub._patch_cache
        hub._dispatch(json.dumps(_patch_response(active_old, "Active old")))
        assert (await _message(active))["id"] == "active-old"
        assert len(_patch_requests(link.sent)) == 4
        assert ("", 3, 2) not in hub._patch_cache

        next_requests = _patch_requests(link.sent)[2:]
        active_new = next(msg for msg in next_requests if "profile" not in msg)
        named_new = next(msg for msg in next_requests if msg.get("profile") == "clean")
        hub._dispatch(json.dumps(_patch_response(active_new, "Active new")))
        hub._dispatch(json.dumps(_patch_response(named_new, "Named new")))
        assert (await _message(active))["id"] == "active-new"
        assert (await _message(named))["id"] == "named-new"
        assert hub._patch_cache[("", 3, 2)]["patch"]["name"] == "Active new"
        assert hub._patch_cache[("clean", 3, 2)]["patch"]["name"] == "Named new"
        hub.stop()

    _run(body())


def test_patch_timeout_is_keyed_suppresses_late_reply_and_recovers():
    async def body():
        hub = Hub(None, patch_timeout_s=0.03)
        link = RecordingLink()
        hub.link = link
        slow = hub.subscribe()
        fast = hub.subscribe()
        slow.send('{"type":"GET_PATCH","id":"slow","bank":1,"slot":1}')
        fast.send('{"type":"GET_PATCH","id":"fast","bank":1,"slot":2}')
        requests = _patch_requests(link.sent)
        slow_req = next(msg for msg in requests if msg["slot"] == 1)
        fast_req = next(msg for msg in requests if msg["slot"] == 2)
        hub._dispatch(json.dumps(_patch_response(fast_req, "Fast")))
        assert (await _message(fast))["id"] == "fast"
        timeout = await _message(slow)
        assert timeout["id"] == "slow" and timeout["error"] == "patch_timeout"

        hub._dispatch(json.dumps(_patch_response(slow_req, "Late")))
        assert slow._queue.empty() and ("", 1, 1) not in hub._patch_cache
        slow.send('{"type":"GET_PATCH","id":"retry","bank":1,"slot":1}')
        retry = _patch_requests(link.sent)[-1]
        assert retry["id"] != slow_req["id"]
        hub._dispatch(json.dumps(_patch_response(retry, "Recovered")))
        assert (await _message(slow))["patch"]["name"] == "Recovered"
        hub.stop()

    _run(body())


def test_patch_timeout_promotes_one_post_mutation_batch_without_caching_old():
    async def body():
        hub = Hub(None, patch_timeout_s=0.02)
        link = RecordingLink()
        hub.link = link
        before = hub.subscribe()
        after = hub.subscribe()
        before.send('{"type":"GET_PATCH","id":"before","bank":4,"slot":1}')
        expired = _patch_requests(link.sent)[0]
        before.send('{"type":"SWITCH_PATCH","id":"sw","bank":4,"slot":1}')
        after.send('{"type":"GET_PATCH","id":"after","bank":4,"slot":1}')

        timeout = await _message(before)
        assert timeout["id"] == "before" and timeout["error"] == "patch_timeout"
        requests = _patch_requests(link.sent)
        assert len(requests) == 2 and requests[1]["id"] != expired["id"]
        assert ("", 4, 1) not in hub._patch_cache

        hub._dispatch(json.dumps(_patch_response(expired, "Expired")))
        assert after._queue.empty()
        hub._dispatch(json.dumps(_patch_response(requests[1], "Current")))
        current = await _message(after)
        assert current["id"] == "after" and current["patch"]["name"] == "Current"
        assert hub._patch_cache[("", 4, 1)]["patch"]["name"] == "Current"
        hub.stop()

    _run(body())


def test_patch_disconnect_link_down_next_batch_and_reconnect():
    async def body():
        hub = Hub(None, patch_timeout_s=1.0)
        link = RecordingLink()
        hub.link = link
        gone = hub.subscribe()
        live = hub.subscribe()
        gone.send('{"type":"GET_PATCH","id":"gone","bank":2,"slot":2}')
        live.send('{"type":"GET_PATCH","id":"live","bank":2,"slot":2}')
        old = _patch_requests(link.sent)[0]
        gone.send('{"type":"SWITCH_PATCH","id":"sw","bank":2,"slot":2}')
        gone.send('{"type":"GET_PATCH","id":"gone-next","bank":2,"slot":2}')
        live.send('{"type":"GET_PATCH","id":"live-next","bank":2,"slot":2}')
        gone.close()
        flight = hub._patch_flights[("", 2, 2)]
        assert all(waiter[0] is live for waiter in flight.waiters)
        assert all(waiter[0] is live for waiter in flight.next_waiters)

        link.connected = False
        hub._dispatch_status(hub._status_line(False))
        failures = [await _message(live), await _message(live)]
        assert [msg["id"] for msg in failures] == ["live", "live-next"]
        assert all(msg["error"] == "link_down" for msg in failures)
        assert await asyncio.wait_for(gone._queue.get(), 0.1) is None
        assert not hub._patch_flights

        live.send('{"type":"GET_PATCH","id":"down","bank":2,"slot":2}')
        assert (await _message(live))["error"] == "link_down"
        assert len(_patch_requests(link.sent)) == 1
        link.connected = True
        live.send('{"type":"GET_PATCH","id":"retry","bank":2,"slot":2}')
        retry = _patch_requests(link.sent)[-1]
        hub._dispatch(json.dumps(_patch_response(old, "Stale")))
        assert live._queue.empty()
        hub._dispatch(json.dumps(_patch_response(retry, "Fresh")))
        assert (await _message(live))["patch"]["name"] == "Fresh"
        hub.stop()

    _run(body())


def test_patch_link_flag_wins_over_cache_before_async_down_status_arrives():
    async def body():
        hub = Hub(None, patch_timeout_s=1.0)
        link = RecordingLink()
        hub.link = link
        sub = hub.subscribe()
        hub._patch_cache[("", 7, 1)] = {
            "type": "PATCH", "bank": 7, "slot": 1, "profile": "",
            "patch": {"name": "old-session"},
        }

        # UpstreamLink flips this flag in its thread before its status callback
        # is posted to the asyncio loop. Do not serve the narrow stale-cache
        # race window as a successful read.
        link.connected = False
        sub.send('{"type":"GET_PATCH","id":"race","bank":7,"slot":1}')
        assert await _message(sub) == {
            "type": "ERROR", "id": "race", "error": "link_down",
            "of": "GET_PATCH",
        }
        assert not link.sent and not hub._patch_flights
        hub._dispatch_status(hub._status_line(False))
        assert not hub._patch_cache
        hub.stop()

    _run(body())


def test_patch_orphaned_flight_can_be_rejoined_without_duplicate_upstream_read():
    async def body():
        hub = Hub(None, patch_timeout_s=1.0)
        link = RecordingLink()
        hub.link = link
        gone = hub.subscribe()
        gone.send('{"type":"GET_PATCH","id":"gone","bank":6,"slot":2}')
        request = _patch_requests(link.sent)[0]
        gone.close()
        flight = hub._patch_flights[("", 6, 2)]
        assert flight.waiters == []

        replacement = hub.subscribe()
        replacement.send('{"type":"GET_PATCH","id":"replacement","bank":6,"slot":2}')
        assert len(_patch_requests(link.sent)) == 1
        hub._dispatch(json.dumps(_patch_response(request, "Reused")))
        assert await _message(replacement) == {
            "type": "PATCH", "id": "replacement", "bank": 6, "slot": 2,
            "profile": "", "patch": {"name": "Reused", "bindings": []},
        }
        assert await asyncio.wait_for(gone._queue.get(), 0.1) is None
        hub.stop()

    _run(body())


def test_patch_malformed_unexpected_and_upstream_error_are_recorrelated():
    async def body():
        hub = Hub(None, patch_timeout_s=1.0)
        link = RecordingLink()
        hub.link = link
        sub = hub.subscribe()

        sub.send('{"type":"GET_PATCH","id":"wrong-key","bank":1,"slot":1}')
        request = _patch_requests(link.sent)[-1]
        wrong = _patch_response(request)
        wrong["slot"] = 9
        hub._dispatch(json.dumps(wrong))
        assert (await _message(sub))["error"] == "patch_protocol"

        sub.send('{"type":"GET_PATCH","id":"partial","bank":1,"slot":1}')
        request = _patch_requests(link.sent)[-1]
        partial = _patch_response(request)
        partial["partial"] = True
        hub._dispatch(json.dumps(partial))
        partial_error = await _message(sub)
        assert partial_error["id"] == "partial"
        assert partial_error["error"] == "patch_protocol"

        sub.send('{"type":"GET_PATCH","id":"ack","bank":1,"slot":1}')
        request = _patch_requests(link.sent)[-1]
        hub._dispatch(json.dumps({"type": "ACK", "id": request["id"]}))
        ack_error = await _message(sub)
        assert ack_error["id"] == "ack" and ack_error["error"] == "patch_protocol"

        sub.send('{"type":"GET_PATCH","id":"firmware","bank":1,"slot":1}')
        request = _patch_requests(link.sent)[-1]
        hub._dispatch(json.dumps({
            "type": "ERROR", "id": request["id"], "error": "not_found",
            "bank": 1, "slot": 1,
        }))
        firmware_error = await _message(sub)
        assert firmware_error["id"] == "firmware"
        assert firmware_error["error"] == "not_found"
        hub.stop()

    _run(body())


def test_patch_waiter_and_distinct_flight_caps_are_local_errors():
    async def body():
        hub = Hub(None, patch_timeout_s=1.0)
        link = RecordingLink()
        hub.link = link
        noisy = hub.subscribe()
        healthy = hub.subscribe()
        for i in range(64):
            noisy.send(json.dumps({
                "type": "GET_PATCH", "id": f"n-{i}", "bank": 1, "slot": 1,
            }))
        noisy.send('{"type":"GET_PATCH","id":"over","bank":1,"slot":1}')
        healthy.send('{"type":"GET_PATCH","id":"healthy","bank":1,"slot":1}')
        assert (await _message(noisy))["id"] == "over"
        request = _patch_requests(link.sent)[0]
        hub._dispatch(json.dumps(_patch_response(request)))
        assert [
            (await _message(noisy))["id"] for _ in range(64)
        ] == [f"n-{i}" for i in range(64)]
        assert (await _message(healthy))["id"] == "healthy"
        hub.stop()

        hub = Hub(None, patch_timeout_s=1.0)
        link = RecordingLink()
        hub.link = link
        sub = hub.subscribe()
        for slot in range(1, 3):
            sub.send(json.dumps({
                "type": "GET_PATCH", "id": f"slot-{slot}",
                "bank": 1, "slot": slot,
            }))
        sub.send('{"type":"GET_PATCH","id":"third","bank":2,"slot":1}')
        busy = await _message(sub)
        assert busy["id"] == "third" and busy["error"] == "patch_busy"
        assert len(_patch_requests(link.sent)) == 2
        hub.stop()

        # Eight subscribers x 64 requests across the same two coalesced keys
        # reaches the global 512-waiter bound without exceeding the two
        # physical upstream flights or 64-per-subscriber bound.
        hub = Hub(None, patch_timeout_s=1.0)
        link = RecordingLink()
        hub.link = link
        subscribers = [hub.subscribe() for _ in range(9)]
        for sub_index, current in enumerate(subscribers[:8]):
            for request_index in range(64):
                current.send(json.dumps({
                    "type": "GET_PATCH",
                    "id": f"global-{sub_index}-{request_index}",
                    "bank": 2, "slot": (sub_index % 2) + 1,
                }))
        subscribers[8].send(
            '{"type":"GET_PATCH","id":"global-over","bank":2,"slot":1}'
        )
        global_busy = await _message(subscribers[8])
        assert global_busy["id"] == "global-over"
        assert global_busy["error"] == "patch_busy"
        assert len(_patch_requests(link.sent)) == 2
        hub.stop()

    _run(body())


def test_patch_rejected_upstream_enqueue_fails_read_and_mutation_immediately():
    async def body():
        hub = Hub(None, patch_timeout_s=1.0)
        link = RejectingLink()
        hub.link = link
        sub = hub.subscribe()

        sub.send('{"type":"GET_PATCH","id":"read","bank":1,"slot":1}')
        read_error = await _message(sub)
        assert read_error == {
            "type": "ERROR", "id": "read", "error": "link_busy",
            "of": "GET_PATCH",
        }
        assert not hub._patch_flights and not hub._patch_flight_ids

        hub._patch_cache[("", 1, 1)] = {
            "type": "PATCH", "bank": 1, "slot": 1, "profile": "",
            "patch": {"name": "stale"},
        }
        sub.send(json.dumps({
            "type": "PUT_PATCH", "id": "write", "bank": 1, "slot": 1,
            "patch": {"name": "new"},
        }))
        write_error = await _message(sub)
        assert write_error == {
            "type": "ERROR", "id": "write", "error": "link_busy",
            "of": "PUT_PATCH",
        }
        assert ("", 1, 1) not in hub._patch_cache
        hub.stop()

    _run(body())


def test_patch_single_flight_real_raw_tcp_websocket_and_warm_cache():
    async def body():
        from websockets.asyncio.client import connect

        pedal = DeferredPatchPedal()
        hub = Hub(f"tcp://127.0.0.1:{pedal.port}", patch_timeout_s=2.0)
        hub.start()
        tcp_server = None
        ws_server = None
        tcp_writer = None
        try:
            assert await _wait(lambda: hub.link.connected)
            tcp_server = await _serve_tcp(hub, "127.0.0.1", 0)
            tcp_port = tcp_server.sockets[0].getsockname()[1]
            ws_server = await _serve_ws(hub, "127.0.0.1", 0)
            ws_port = ws_server.sockets[0].getsockname()[1]
            tcp_reader, tcp_writer = await asyncio.open_connection("127.0.0.1", tcp_port)

            async with connect(f"ws://127.0.0.1:{ws_port}") as ws:
                assert json.loads(await ws.recv()) == {"type": "HUB", "link": "up"}
                raw_ids = ["raw", "same", "same"]
                ws_ids = ["ws", "same"]
                for request_id in raw_ids:
                    tcp_writer.write((json.dumps({
                        "type": "GET_PATCH", "id": request_id,
                        "bank": 5, "slot": 2,
                    }) + "\n").encode())
                await tcp_writer.drain()
                for request_id in ws_ids:
                    await ws.send(json.dumps({
                        "type": "GET_PATCH", "id": request_id,
                        "bank": 5, "slot": 2,
                    }))

                assert await _wait(
                    lambda: (
                        (flight := hub._patch_flights.get(("", 5, 2))) is not None
                        and len(flight.waiters) == len(raw_ids) + len(ws_ids)
                    )
                )
                assert await _wait(
                    lambda: len(_patch_requests(pedal.received)) == 1
                )
                request = _patch_requests(pedal.received)[0]
                pedal.push(_patch_response(request, "Network"))
                raw = [
                    json.loads(await asyncio.wait_for(tcp_reader.readline(), 2.0))
                    for _ in raw_ids
                ]
                ws_messages = [
                    json.loads(await asyncio.wait_for(ws.recv(), 2.0))
                    for _ in ws_ids
                ]
                assert [msg["id"] for msg in raw] == raw_ids
                assert [msg["id"] for msg in ws_messages] == ws_ids
                assert all(msg["patch"]["name"] == "Network"
                           for msg in raw + ws_messages)
                assert all(msg.get("type") != "HUB" for msg in raw)

                # Both endpoints now hit the same cache; no CDC request.
                tcp_writer.write(b'{"type":"GET_PATCH","id":"raw-warm","bank":5,"slot":2}\n')
                await tcp_writer.drain()
                await ws.send('{"type":"GET_PATCH","id":"ws-warm","bank":5,"slot":2}')
                raw_warm = json.loads(await asyncio.wait_for(tcp_reader.readline(), 1.0))
                ws_warm = json.loads(await asyncio.wait_for(ws.recv(), 1.0))
                assert raw_warm["id"] == "raw-warm"
                assert ws_warm["id"] == "ws-warm"
                await asyncio.sleep(0.05)
                assert len(_patch_requests(pedal.received)) == 1
        finally:
            if tcp_writer is not None:
                tcp_writer.close()
                await tcp_writer.wait_closed()
            if tcp_server is not None:
                tcp_server.close()
                await tcp_server.wait_closed()
            if ws_server is not None:
                ws_server.close()
                await ws_server.wait_closed()
            hub.stop()
            pedal.close()

    _run(body())


if __name__ == "__main__":
    functions = [
        value for name, value in sorted(globals().items())
        if name.startswith("test_")
    ]
    failures = 0
    for function in functions:
        try:
            function()
            print(f"PASS {function.__name__}")
        except Exception as exc:  # noqa: BLE001 - standalone test runner
            failures += 1
            print(f"FAIL {function.__name__}: {exc!r}")
    raise SystemExit(1 if failures else 0)
