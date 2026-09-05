"""Deterministic regression tests for Hub GET_CONTEXT single-flight routing.

The unit tests drive Hub directly so timeout/link-state edge cases do not
depend on thread scheduling.  The final test keeps the real UpstreamLink and
TCP framing in the loop, with a fake pedal that deliberately holds the
snapshot open while many subscribers enqueue requests.
"""

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


class DeferredContextPedal(FakePedal):
    """Answer every command except GET_CONTEXT, which the test releases."""

    def _reply(self, msg):
        if msg.get("type") == "GET_CONTEXT":
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


def _context_requests(lines: list[str]) -> list[dict]:
    requests = []
    for line in lines:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("type") == "GET_CONTEXT":
            requests.append(msg)
    return requests


def test_context_single_flight_preserves_each_subscriber_request_id_and_pushes():
    async def body():
        hub = Hub(None, context_timeout_s=1.0)
        link = RecordingLink()
        hub.link = link
        first = hub.subscribe()
        second = hub.subscribe()

        first.send('{"type":"GET_CONTEXT","id":"first-1"}')
        first.send('{"type":"GET_CONTEXT"}')
        second.send('{"type":"GET_CONTEXT","id":null}')

        assert len(link.sent) == 1
        upstream = json.loads(link.sent[0])
        assert upstream["type"] == "GET_CONTEXT"
        assert upstream["id"].startswith("__bosun_ctx_")
        assert upstream["id"] not in {"first-1", None}

        # A proactive firmware push must remain a broadcast and, critically,
        # must not accidentally complete the correlated request flight.
        pushed = {"type": "CONTEXT", "context": {"seq": "push"}}
        hub._dispatch(json.dumps(pushed))
        assert await _message(first) == pushed
        assert await _message(second) == pushed
        assert hub._context_flight_id == upstream["id"]

        snapshot = {
            "type": "CONTEXT",
            "id": upstream["id"],
            "context": {"bank": 2, "slot": 3, "kemper_block_X": "on"},
        }
        hub._dispatch(json.dumps(snapshot))

        first_one = await _message(first)
        first_two = await _message(first)
        second_one = await _message(second)
        assert first_one["id"] == "first-1"
        assert "id" not in first_two  # absent stays absent, not id:null/id:""
        assert "id" in second_one and second_one["id"] is None
        for reply in (first_one, first_two, second_one):
            assert reply["context"] == snapshot["context"]
            assert reply.get("id") != upstream["id"]
        assert first._queue.empty() and second._queue.empty()
        assert hub._context_flight_id is None
        hub.stop()

    _run(body())


def test_context_disconnect_and_link_down_fail_only_live_waiters_then_recover():
    async def body():
        hub = Hub(None, context_timeout_s=1.0)
        link = RecordingLink()
        hub.link = link
        gone = hub.subscribe()
        live = hub.subscribe()

        gone.send('{"type":"GET_CONTEXT","id":"gone"}')
        live.send('{"type":"GET_CONTEXT","id":"live"}')
        old_flight = json.loads(link.sent[0])["id"]
        gone.send('{"type":"SWITCH_PATCH","id":"switch","bank":2,"slot":1}')
        gone.send('{"type":"GET_CONTEXT","id":"gone-next"}')
        live.send('{"type":"GET_CONTEXT","id":"live-next"}')
        gone.close()
        assert all(waiter[0] is live for waiter in hub._context_waiters)
        assert all(waiter[0] is live for waiter in hub._context_next_waiters)

        link.connected = False
        hub._dispatch_status(hub._status_line(False))
        failure = await _message(live)
        next_failure = await _message(live)
        assert failure == {
            "type": "ERROR",
            "error": "link_down",
            "of": "GET_CONTEXT",
            "id": "live",
        }
        assert next_failure == {
            "type": "ERROR",
            "error": "link_down",
            "of": "GET_CONTEXT",
            "id": "live-next",
        }
        # Closing removed the other waiter before failure fanout.
        assert await asyncio.wait_for(gone._queue.get(), 0.1) is None

        # A request received while down fails immediately and creates neither
        # an upstream command nor poisoned single-flight state.
        live.send('{"type":"GET_CONTEXT","id":"while-down"}')
        down_failure = await _message(live)
        assert down_failure["id"] == "while-down"
        assert down_failure["error"] == "link_down"
        assert len(link.sent) == 2 and hub._context_flight_id is None

        link.connected = True
        hub._dispatch_status(hub._status_line(True))
        live.send('{"type":"GET_CONTEXT","id":"retry"}')
        assert len(link.sent) == 3
        new_flight = json.loads(link.sent[2])["id"]
        assert new_flight != old_flight

        # A delayed response from the pre-disconnect generation is private
        # hub traffic: do not leak it or let it settle the retry.
        hub._dispatch(json.dumps({
            "type": "CONTEXT", "id": old_flight, "context": {"stale": True}
        }))
        assert live._queue.empty()

        hub._dispatch(json.dumps({
            "type": "CONTEXT", "id": new_flight, "context": {"fresh": True}
        }))
        reply = await _message(live)
        assert reply == {
            "type": "CONTEXT", "id": "retry", "context": {"fresh": True}
        }
        hub.stop()

    _run(body())


def test_context_mutation_barrier_queues_a_fresh_post_mutation_snapshot():
    async def body():
        hub = Hub(None, context_timeout_s=1.0)
        link = RecordingLink()
        hub.link = link
        before = hub.subscribe()
        after = hub.subscribe()

        before.send('{"type":"GET_CONTEXT","id":"before"}')
        old_flight = json.loads(link.sent[0])["id"]
        before.send('{"type":"SWITCH_PATCH","id":"switch","bank":2,"slot":1}')
        after.send('{"type":"GET_CONTEXT","id":"after"}')
        assert len(link.sent) == 2  # context + mutation; no parallel snapshot
        assert len(hub._context_waiters) == 1
        assert len(hub._context_next_waiters) == 1

        hub._dispatch(json.dumps({
            "type": "CONTEXT", "id": old_flight,
            "context": {"bank": 1, "slot": 1},
        }))
        assert (await _message(before))["id"] == "before"
        # Completing the old generation promotes exactly one post-barrier
        # generation, ordered on the wire after SWITCH_PATCH.
        assert len(link.sent) == 3
        assert json.loads(link.sent[1])["type"] == "SWITCH_PATCH"
        fresh_flight = json.loads(link.sent[2])["id"]
        assert fresh_flight != old_flight
        assert after._queue.empty()

        hub._dispatch(json.dumps({
            "type": "CONTEXT", "id": fresh_flight,
            "context": {"bank": 2, "slot": 1},
        }))
        assert await _message(after) == {
            "type": "CONTEXT", "id": "after",
            "context": {"bank": 2, "slot": 1},
        }
        hub.stop()

    _run(body())


def test_context_noncanonical_request_is_private_but_not_coalesced():
    async def body():
        hub = Hub(None, context_timeout_s=1.0)
        link = RecordingLink()
        hub.link = link
        first = hub.subscribe()
        second = hub.subscribe()

        # Unknown future selectors must not be merged with today's global
        # snapshot semantics. It still gets ordinary private correlation and
        # admission so future fields cannot bypass Captain's background cap.
        first.send('{"type":"GET_CONTEXT","id":"future","scope":"x"}')
        upstream = json.loads(link.sent[0])
        assert upstream["type"] == "GET_CONTEXT"
        assert upstream["scope"] == "x"
        assert upstream["id"].startswith("__bosun_req_")
        assert upstream["id"] != "future"
        assert hub._context_flight_id is None
        hub._dispatch(json.dumps({
            "type": "CONTEXT", "id": upstream["id"],
            "context": {"future": True},
        }))
        assert await _message(first) == {
            "type": "CONTEXT", "id": "future",
            "context": {"future": True},
        }
        assert second._queue.empty()

        # The private-looking stem is not a globally reserved client id.  An
        # ordinary RPC response bearing it must still broadcast unchanged.
        ordinary = {"type": "ACK", "id": "__bosun_ctx_client", "fw": "x"}
        hub._dispatch(json.dumps(ordinary))
        assert await _message(first) == ordinary
        assert await _message(second) == ordinary
        hub.stop()

    _run(body())


def test_context_malformed_or_unexpected_private_response_fails_and_recovers():
    async def body():
        hub = Hub(None, context_timeout_s=1.0)
        link = RecordingLink()
        hub.link = link
        sub = hub.subscribe()
        sub.send('{"type":"GET_CONTEXT","id":"bad"}')
        flight = json.loads(link.sent[0])["id"]
        hub._dispatch(json.dumps({"type": "CONTEXT", "id": flight}))
        assert await _message(sub) == {
            "type": "ERROR", "id": "bad", "error": "context_protocol",
            "of": "GET_CONTEXT",
        }
        sub.send('{"type":"GET_CONTEXT","id":"wrong-type"}')
        next_flight = json.loads(link.sent[1])["id"]
        hub._dispatch(json.dumps({"type": "ACK", "id": next_flight}))
        assert await _message(sub) == {
            "type": "ERROR", "id": "wrong-type",
            "error": "context_protocol", "of": "GET_CONTEXT",
        }

        sub.send('{"type":"GET_CONTEXT","id":"good"}')
        good_flight = json.loads(link.sent[2])["id"]
        hub._dispatch(json.dumps({
            "type": "CONTEXT", "id": good_flight, "context": {"ok": True}
        }))
        assert (await _message(sub))["id"] == "good"
        hub.stop()

    _run(body())


def test_context_waiter_cap_is_per_subscriber_and_correlated():
    async def body():
        hub = Hub(None, context_timeout_s=1.0)
        link = RecordingLink()
        hub.link = link
        noisy = hub.subscribe()
        healthy = hub.subscribe()
        for i in range(64):
            noisy.send(json.dumps({"type": "GET_CONTEXT", "id": f"n-{i}"}))
        noisy.send('{"type":"GET_CONTEXT","id":"over-cap"}')
        healthy.send('{"type":"GET_CONTEXT","id":"healthy"}')
        assert len(link.sent) == 1
        assert await _message(noisy) == {
            "type": "ERROR", "id": "over-cap", "error": "context_busy",
            "of": "GET_CONTEXT",
        }
        flight = json.loads(link.sent[0])["id"]
        hub._dispatch(json.dumps({
            "type": "CONTEXT", "id": flight, "context": {"ok": True}
        }))
        noisy_replies = [await _message(noisy) for _ in range(64)]
        assert [msg["id"] for msg in noisy_replies] == [f"n-{i}" for i in range(64)]
        assert (await _message(healthy))["id"] == "healthy"
        hub.stop()

    _run(body())


def test_context_timeout_is_correlated_and_does_not_poison_next_flight():
    async def body():
        hub = Hub(None, context_timeout_s=0.02)
        link = RecordingLink()
        hub.link = link
        sub = hub.subscribe()

        sub.send('{"type":"GET_CONTEXT","id":"slow-1"}')
        sub.send('{"type":"GET_CONTEXT","id":"slow-2"}')
        expired_flight = json.loads(link.sent[0])["id"]
        errors = [await _message(sub), await _message(sub)]
        assert [msg["id"] for msg in errors] == ["slow-1", "slow-2"]
        assert all(msg["type"] == "ERROR" for msg in errors)
        assert all(msg["error"] == "context_timeout" for msg in errors)
        assert hub._context_flight_id is None

        # Late completion is swallowed, then a fresh request starts a new
        # generation and can complete normally.
        hub._dispatch(json.dumps({
            "type": "CONTEXT", "id": expired_flight,
            "context": {"too_late": True},
        }))
        assert sub._queue.empty()

        sub.send('{"type":"GET_CONTEXT","id":"retry"}')
        retry_flight = json.loads(link.sent[1])["id"]
        assert retry_flight != expired_flight
        hub._dispatch(json.dumps({
            "type": "CONTEXT", "id": retry_flight,
            "context": {"recovered": True},
        }))
        assert await _message(sub) == {
            "type": "CONTEXT", "id": "retry",
            "context": {"recovered": True},
        }
        hub.stop()

    _run(body())


def test_context_timeout_promotes_one_sealed_post_mutation_batch():
    async def body():
        hub = Hub(None, context_timeout_s=0.02)
        link = RecordingLink()
        hub.link = link
        before = hub.subscribe()
        after = hub.subscribe()

        before.send('{"type":"GET_CONTEXT","id":"before-timeout"}')
        expired_flight = json.loads(link.sent[0])["id"]
        before.send('{"type":"SWITCH_PATCH","id":"sw","bank":3,"slot":4}')
        after.send('{"type":"GET_CONTEXT","id":"after-timeout"}')

        error = await _message(before)
        assert error["id"] == "before-timeout"
        assert error["error"] == "context_timeout"
        # The deferred batch is promoted exactly once, after the mutation.
        assert len(link.sent) == 3
        promoted_flight = json.loads(link.sent[2])["id"]
        assert promoted_flight != expired_flight

        hub._dispatch(json.dumps({
            "type": "CONTEXT", "id": expired_flight,
            "context": {"stale": True},
        }))
        assert after._queue.empty()
        hub._dispatch(json.dumps({
            "type": "CONTEXT", "id": promoted_flight,
            "context": {"bank": 3, "slot": 4},
        }))
        assert await _message(after) == {
            "type": "CONTEXT", "id": "after-timeout",
            "context": {"bank": 3, "slot": 4},
        }
        hub.stop()

    _run(body())


def test_context_upstream_error_is_recorrelated_for_every_waiter():
    async def body():
        hub = Hub(None, context_timeout_s=1.0)
        link = RecordingLink()
        hub.link = link
        first = hub.subscribe()
        second = hub.subscribe()
        first.send('{"type":"GET_CONTEXT","id":"a"}')
        second.send('{"type":"GET_CONTEXT","id":"b"}')
        flight = json.loads(link.sent[0])["id"]

        hub._dispatch(json.dumps({
            "type": "ERROR", "id": flight, "error": "exception",
            "detail": "snapshot failed", "of": "GET_CONTEXT",
        }))
        a = await _message(first)
        b = await _message(second)
        assert a["id"] == "a" and b["id"] == "b"
        assert a["error"] == b["error"] == "exception"
        assert a["detail"] == b["detail"] == "snapshot failed"
        assert hub._context_flight_id is None
        hub.stop()

    _run(body())


def test_context_single_flight_e2e_stress_uses_one_upstream_snapshot():
    async def body():
        pedal = DeferredContextPedal()
        hub = Hub(f"tcp://127.0.0.1:{pedal.port}", context_timeout_s=2.0)
        hub.start()
        try:
            assert await _wait(lambda: hub.link.connected), "link never synced"
            first = hub.subscribe()
            second = hub.subscribe()
            first_ids = [f"raw-{i}" for i in range(40)]
            second_ids = [f"ws-{i}" for i in range(40)]

            for first_id, second_id in zip(first_ids, second_ids):
                first.send(json.dumps({"type": "GET_CONTEXT", "id": first_id}))
                second.send(json.dumps({"type": "GET_CONTEXT", "id": second_id}))

            assert await _wait(lambda: len(_context_requests(pedal.received)) == 1)
            await asyncio.sleep(0.1)
            requests = _context_requests(pedal.received)
            assert len(requests) == 1, "coalesced requests leaked onto the CDC"
            flight = requests[0]["id"]

            pedal.push({"type": "CONTEXT", "context": {"unsolicited": True}})
            assert (await _message(first))["context"] == {"unsolicited": True}
            assert (await _message(second))["context"] == {"unsolicited": True}

            pedal.push({
                "type": "CONTEXT", "id": flight,
                "context": {"bank": 1, "slot": 2, "kemper_block_C": "on"},
            })
            first_replies = [await _message(first, 2.0) for _ in first_ids]
            second_replies = [await _message(second, 2.0) for _ in second_ids]
            assert [msg["id"] for msg in first_replies] == first_ids
            assert [msg["id"] for msg in second_replies] == second_ids
            assert all(msg["id"] != flight for msg in first_replies + second_replies)
            assert first._queue.empty() and second._queue.empty()
        finally:
            hub.stop()
            pedal.close()

    _run(body())


def test_context_single_flight_across_real_raw_tcp_and_websocket_clients():
    async def body():
        from websockets.asyncio.client import connect

        pedal = DeferredContextPedal()
        hub = Hub(f"tcp://127.0.0.1:{pedal.port}", context_timeout_s=2.0)
        hub.start()
        tcp_server = None
        ws_server = None
        tcp_writer = None
        try:
            assert await _wait(lambda: hub.link.connected), "link never synced"
            tcp_server = await _serve_tcp(hub, "127.0.0.1", 0)
            tcp_port = tcp_server.sockets[0].getsockname()[1]
            ws_server = await _serve_ws(hub, "127.0.0.1", 0)
            ws_port = ws_server.sockets[0].getsockname()[1]

            tcp_reader, tcp_writer = await asyncio.open_connection(
                "127.0.0.1", tcp_port
            )
            async with connect(f"ws://127.0.0.1:{ws_port}") as ws:
                status = json.loads(await asyncio.wait_for(ws.recv(), 1.0))
                assert status == {"type": "HUB", "link": "up"}

                raw_ids = ["raw-a", "same", "same"]
                ws_ids = ["ws-a", "same"]
                for request_id in raw_ids:
                    tcp_writer.write((json.dumps({
                        "type": "GET_CONTEXT", "id": request_id
                    }) + "\n").encode())
                await tcp_writer.drain()
                for request_id in ws_ids:
                    await ws.send(json.dumps({
                        "type": "GET_CONTEXT", "id": request_id
                    }))

                assert await _wait(
                    lambda: len(_context_requests(pedal.received)) == 1
                )
                await asyncio.sleep(0.05)
                requests = _context_requests(pedal.received)
                assert len(requests) == 1
                pedal.push({
                    "type": "CONTEXT", "id": requests[0]["id"],
                    "context": {"bank": 4, "slot": 2},
                })

                raw_replies = []
                for _ in raw_ids:
                    line = await asyncio.wait_for(tcp_reader.readline(), 2.0)
                    assert line
                    raw_replies.append(json.loads(line))
                ws_replies = [
                    json.loads(await asyncio.wait_for(ws.recv(), 2.0))
                    for _ in ws_ids
                ]
                # Duplicate ids are requests, not dictionary keys: each one
                # still gets exactly one response.  Equal ids in two clients
                # never route a response to the wrong socket.
                assert [msg["id"] for msg in raw_replies] == raw_ids
                assert [msg["id"] for msg in ws_replies] == ws_ids
                assert all(msg["type"] == "CONTEXT" for msg in raw_replies)
                assert all(msg["type"] == "CONTEXT" for msg in ws_replies)
                assert all(msg.get("type") != "HUB" for msg in raw_replies)
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
