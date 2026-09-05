"""Generic per-subscriber request correlation regression tests.

These tests exercise ordinary request/reply traffic.  GET_CONTEXT and
canonical GET_PATCH keep their dedicated single-flight implementations and
are covered by their own test modules.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bosun_hub import hub as hub_module  # noqa: E402
from bosun_hub.hub import Hub  # noqa: E402
from bosun_hub.server import _serve_tcp, _serve_ws  # noqa: E402
from tests.fake_pedal import FakePedal  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


async def _message(sub, timeout: float = 0.5) -> dict:
    line = await asyncio.wait_for(sub._queue.get(), timeout=timeout)
    assert line is not None
    return json.loads(line)


async def _wait(predicate, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


async def _expect_timeout(awaitable, timeout: float = 0.05) -> None:
    try:
        await asyncio.wait_for(awaitable, timeout)
    except asyncio.TimeoutError:
        return
    raise AssertionError("unexpected message leaked to the other client")


def _isolated_hub(**kwargs):
    hub = Hub(None, **kwargs)
    sent: list[dict] = []

    def capture(line: str):
        sent.append(json.loads(line))
        return True

    hub.link.send = capture
    hub.link._connected = True
    return hub, sent


def test_same_client_id_is_unique_upstream_and_replies_are_private():
    async def body():
        hub, sent = _isolated_hub()
        first = hub.subscribe()
        second = hub.subscribe()
        try:
            first.send('{"type":"PING","id":"same"}')
            second.send('{"type":"PING","id":"same"}')

            assert len(sent) == 2
            upstream_ids = [request["id"] for request in sent]
            assert upstream_ids[0] != upstream_ids[1]
            assert "same" not in upstream_ids

            hub._dispatch(json.dumps({
                "type": "ACK", "id": upstream_ids[0], "source": "first",
            }))
            assert await _message(first) == {
                "type": "ACK", "id": "same", "source": "first",
            }
            assert second._queue.empty(), "reply leaked to the other subscriber"

            hub._dispatch(json.dumps({
                "type": "ERROR", "id": upstream_ids[1],
                "error": "rejected", "source": "second",
            }))
            assert await _message(second) == {
                "type": "ERROR", "id": "same",
                "error": "rejected", "source": "second",
            }
            assert first._queue.empty(), "ERROR leaked to the other subscriber"
        finally:
            hub.stop()

    _run(body())


def test_same_id_over_raw_tcp_and_websocket_routes_to_the_right_socket():
    class DeferredRigInfoPedal(FakePedal):
        def _reply(self, msg):
            if msg.get("type") == "GET_RIG_INFO":
                return None
            return super()._reply(msg)

    async def body():
        from websockets.asyncio.client import connect

        pedal = DeferredRigInfoPedal()
        hub = Hub(f"tcp://127.0.0.1:{pedal.port}")
        hub.start()
        tcp_server = None
        ws_server = None
        tcp_writer = None
        try:
            assert await _wait(lambda: hub.link.connected), "hub link never synced"
            tcp_server = await _serve_tcp(hub, "127.0.0.1", 0)
            ws_server = await _serve_ws(hub, "127.0.0.1", 0)
            tcp_port = tcp_server.sockets[0].getsockname()[1]
            ws_port = ws_server.sockets[0].getsockname()[1]

            tcp_reader, tcp_writer = await asyncio.open_connection(
                "127.0.0.1", tcp_port,
            )
            async with connect(f"ws://127.0.0.1:{ws_port}") as ws:
                status = json.loads(await asyncio.wait_for(ws.recv(), 1))
                assert status == {"type": "HUB", "link": "up"}

                tcp_writer.write(b'{"type":"GET_RIG_INFO","id":"same"}\n')
                await tcp_writer.drain()
                assert await _wait(lambda: sum(
                    json.loads(line).get("type") == "GET_RIG_INFO"
                    for line in pedal.received
                ) == 1)

                await ws.send('{"type":"GET_RIG_INFO","id":"same"}')
                assert await _wait(lambda: sum(
                    json.loads(line).get("type") == "GET_RIG_INFO"
                    for line in pedal.received
                ) == 2)
                requests = [
                    json.loads(line) for line in pedal.received
                    if json.loads(line).get("type") == "GET_RIG_INFO"
                ]
                assert requests[0]["id"] != requests[1]["id"]

                pedal.push({
                    "type": "RIG_INFO", "id": requests[0]["id"],
                    "name": "raw",
                })
                raw_reply = json.loads(await asyncio.wait_for(
                    tcp_reader.readline(), 1,
                ))
                assert raw_reply == {
                    "type": "RIG_INFO", "id": "same", "name": "raw",
                }
                await _expect_timeout(ws.recv())

                pedal.push({
                    "type": "RIG_INFO", "id": requests[1]["id"],
                    "name": "ws",
                })
                ws_reply = json.loads(await asyncio.wait_for(ws.recv(), 1))
                assert ws_reply == {
                    "type": "RIG_INFO", "id": "same", "name": "ws",
                }
                await _expect_timeout(tcp_reader.readline())
        finally:
            if tcp_writer is not None:
                tcp_writer.close()
                await tcp_writer.wait_closed()
            for server in (tcp_server, ws_server):
                if server is not None:
                    server.close()
                    await server.wait_closed()
            hub.stop()
            pedal.close()

    _run(body())


def test_mutation_ack_is_private_but_unsolicited_event_and_context_broadcast():
    async def body():
        hub, sent = _isolated_hub()
        first = hub.subscribe()
        second = hub.subscribe()
        try:
            first.send(json.dumps({
                "type": "SWITCH_PATCH", "id": "mut", "bank": 1, "slot": 2,
            }))
            upstream_id = sent[0]["id"]
            assert upstream_id != "mut"

            event_line = json.dumps({
                "type": "EVENT", "event": "patch_switched",
                "id": "unsolicited-event", "bank": 1, "slot": 2,
            }, separators=(",", ":"))
            context_line = json.dumps({
                "type": "CONTEXT", "id": "unsolicited-context",
                "partial": True,
                "context": {"kemper_block_X": "on"},
            }, separators=(",", ":"))
            hub._dispatch(event_line)
            hub._dispatch(context_line)

            for sub in (first, second):
                assert await asyncio.wait_for(sub._queue.get(), 0.5) == event_line
                assert await asyncio.wait_for(sub._queue.get(), 0.5) == context_line

            assert upstream_id in hub._request_pending
            hub._dispatch(json.dumps({"type": "ACK", "id": upstream_id}))
            assert await _message(first) == {"type": "ACK", "id": "mut"}
            assert second._queue.empty()
        finally:
            hub.stop()

    _run(body())


def test_timeout_restores_original_id_and_late_private_reply_is_dropped():
    async def body():
        hub, sent = _isolated_hub(request_timeout_s=0.02)
        requester = hub.subscribe()
        observer = hub.subscribe()
        try:
            requester.send('{"type":"GET_DEVICE_INFO","id":"slow"}')
            upstream_id = sent[0]["id"]

            assert await _message(requester) == {
                "type": "ERROR", "error": "request_timeout",
                "of": "GET_DEVICE_INFO", "id": "slow",
            }
            assert observer._queue.empty()
            assert not hub._request_pending

            hub._dispatch(json.dumps({
                "type": "DEVICE_INFO", "id": upstream_id, "fw": "late",
            }))
            await asyncio.sleep(0)
            assert requester._queue.empty()
            assert observer._queue.empty()
        finally:
            hub.stop()

    _run(body())


def test_link_queue_rejection_releases_pending_and_reports_link_busy():
    async def body():
        hub = Hub(None, request_timeout_s=60)
        sent: list[dict] = []

        def reject(line: str):
            sent.append(json.loads(line))
            return False

        hub.link.send = reject
        hub.link._connected = True
        requester = hub.subscribe()
        observer = hub.subscribe()
        try:
            requester.send('{"type":"PING","id":"busy"}')
            assert len(sent) == 1 and sent[0]["id"] != "busy"
            assert await _message(requester) == {
                "type": "ERROR", "error": "link_busy", "of": "PING",
                "id": "busy",
            }
            assert observer._queue.empty()
            assert not hub._request_pending
            assert not hub._request_ids_by_sub
        finally:
            hub.stop()

    _run(body())


def test_original_id_value_is_restored_exactly_even_when_not_a_string():
    async def body():
        hub, sent = _isolated_hub()
        sub = hub.subscribe()
        try:
            ids = [None, 17, ["compound", 3]]
            for index, request_id in enumerate(ids):
                sub.send(json.dumps({
                    "type": "PING", "id": request_id, "seq": index,
                }))
            assert len({request["id"] for request in sent}) == len(ids)

            for request, expected_id in zip(sent, ids):
                hub._dispatch(json.dumps({
                    "type": "ACK", "id": request["id"],
                }))
                assert (await _message(sub))["id"] == expected_id
            assert not hub._request_pending
        finally:
            hub.stop()

    _run(body())


def test_profile_management_legacy_id_target_survives_private_correlation():
    async def body():
        hub, sent = _isolated_hub()
        sub = hub.subscribe()
        try:
            kinds = (
                "CREATE_PROFILE", "SWITCH_PROFILE",
                "DELETE_PROFILE", "RENAME_PROFILE",
            )
            for kind in kinds:
                sub.send(json.dumps({
                    "type": kind, "id": "legacy-profile", "name": "Legacy",
                }))

            assert len(sent) == len(kinds)
            for upstream, kind in zip(sent, kinds):
                assert upstream["type"] == kind
                assert upstream["profile_id"] == "legacy-profile"
                assert upstream["id"].startswith(hub._request_id_prefix)
                assert upstream["id"] != "legacy-profile"
                hub._dispatch(json.dumps({
                    "type": "ACK", "id": upstream["id"],
                    "profile_id": upstream["profile_id"],
                }))
                assert await _message(sub) == {
                    "type": "ACK", "id": "legacy-profile",
                    "profile_id": "legacy-profile",
                }
        finally:
            hub.stop()

    _run(body())


def test_falsey_legacy_profile_id_is_forwarded_without_semantic_rewrite():
    async def body():
        hub, sent = _isolated_hub()
        sub = hub.subscribe()
        try:
            request = {"type": "SWITCH_PROFILE", "id": None}
            sub.send(json.dumps(request))
            assert sent == [request]
            assert not hub._request_pending
            assert not hub._request_ids_by_sub
        finally:
            hub.stop()

    _run(body())


def test_link_disconnect_fails_all_pending_privately_and_cleans_timers():
    async def body():
        hub, sent = _isolated_hub(request_timeout_s=60)
        first = hub.subscribe()
        second = hub.subscribe()
        try:
            first.send('{"type":"PING","id":"one"}')
            second.send('{"type":"GET_DEVICE_INFO","id":"two"}')
            private_ids = [request["id"] for request in sent]

            hub._dispatch_status(hub._status_line(False))
            assert await _message(first) == {
                "type": "ERROR", "error": "link_down", "of": "PING",
                "id": "one",
            }
            assert await _message(second) == {
                "type": "ERROR", "error": "link_down",
                "of": "GET_DEVICE_INFO", "id": "two",
            }
            assert not hub._request_pending

            for private_id in private_ids:
                hub._dispatch(json.dumps({"type": "ACK", "id": private_id}))
            assert first._queue.empty()
            assert second._queue.empty()
        finally:
            hub.stop()

    _run(body())


def test_subscriber_close_releases_only_its_pending_requests():
    async def body():
        hub, sent = _isolated_hub(request_timeout_s=60)
        closed = hub.subscribe()
        live = hub.subscribe()
        try:
            closed.send('{"type":"PING","id":"closed"}')
            live.send('{"type":"PING","id":"live"}')
            closed_id, live_id = [request["id"] for request in sent]

            closed.close()
            assert closed not in hub._request_ids_by_sub
            assert live in hub._request_ids_by_sub

            hub._dispatch(json.dumps({"type": "ACK", "id": closed_id}))
            assert live._queue.empty()
            hub._dispatch(json.dumps({"type": "ACK", "id": live_id}))
            assert await _message(live) == {"type": "ACK", "id": "live"}
            assert not hub._request_pending
        finally:
            hub.stop()

    _run(body())


def test_pending_limits_fail_closed_without_forwarding():
    async def body():
        hub, sent = _isolated_hub(request_timeout_s=60)
        first = hub.subscribe()
        second = hub.subscribe()
        third = hub.subscribe()
        try:
            first.send('{"type":"PING","id":"first"}')
            first.send('{"type":"PING","id":"first-overflow"}')
            assert await _message(first) == {
                "type": "ERROR", "error": "request_busy", "of": "PING",
                "id": "first-overflow",
            }

            second.send('{"type":"PING","id":"second"}')
            third.send('{"type":"PING","id":"global-overflow"}')
            assert await _message(third) == {
                "type": "ERROR", "error": "request_busy", "of": "PING",
                "id": "global-overflow",
            }
            assert len(sent) == 2
            assert len(hub._request_pending) == 2
        finally:
            hub.stop()

    with (
        patch.object(hub_module, "REQUESTS_MAX", 2),
        patch.object(hub_module, "REQUESTS_PER_SUB_MAX", 1),
    ):
        _run(body())


def test_request_without_id_keeps_transparent_broadcast_semantics():
    async def body():
        hub, sent = _isolated_hub()
        first = hub.subscribe()
        second = hub.subscribe()
        try:
            original = '{"type":"PING"}'
            first.send(original)
            assert sent == [{"type": "PING"}]
            reply = '{"type":"ACK"}'
            hub._dispatch(reply)
            assert await asyncio.wait_for(first._queue.get(), 0.5) == reply
            assert await asyncio.wait_for(second._queue.get(), 0.5) == reply
        finally:
            hub.stop()

    _run(body())


def test_repeated_context_then_led_dump_keeps_ids_private_and_never_loses_reply():
    """Exercise the exact diagnostic sequence used by the browser harness.

    Stage and the test-only control socket deliberately reuse the same local
    context id here.  Each CONTEXT must still return to its own subscriber,
    and the following ordinary LED_DUMP must neither collide with the context
    private namespace nor leak to Stage.  Repetition catches pending/timer
    state which is not released between requests.
    """

    async def body():
        hub, sent = _isolated_hub(request_timeout_s=60)
        stage = hub.subscribe(want_status=True)
        control = hub.subscribe(want_status=True)
        try:
            # Discard the synthetic initial link status queued for WS clients.
            assert (await _message(stage))["type"] == "HUB"
            assert (await _message(control))["type"] == "HUB"

            for cycle in range(100):
                local_context_id = f"cycle-{cycle}-context"
                stage.send(json.dumps({
                    "type": "GET_CONTEXT", "id": local_context_id,
                }))
                control.send(json.dumps({
                    "type": "GET_CONTEXT", "id": local_context_id,
                }))
                assert sent[-1]["type"] == "GET_CONTEXT"
                context_private_id = sent[-1]["id"]
                assert context_private_id.startswith(hub._context_id_prefix)
                hub._dispatch(json.dumps({
                    "type": "CONTEXT", "id": context_private_id,
                    "context": {"cycle": cycle},
                }))
                for sub in (stage, control):
                    assert await _message(sub) == {
                        "type": "CONTEXT", "id": local_context_id,
                        "context": {"cycle": cycle},
                    }

                local_led_id = f"cycle-{cycle}-leds"
                control.send(json.dumps({
                    "type": "LED_DUMP", "id": local_led_id,
                }))
                assert sent[-1]["type"] == "LED_DUMP"
                led_private_id = sent[-1]["id"]
                assert led_private_id.startswith(hub._request_id_prefix)
                assert led_private_id != context_private_id
                hub._dispatch(json.dumps({
                    "type": "LED_DUMP", "id": led_private_id,
                    "pixels": [[cycle, 0, 0]],
                }))
                assert await _message(control) == {
                    "type": "LED_DUMP", "id": local_led_id,
                    "pixels": [[cycle, 0, 0]],
                }
                assert stage._queue.empty(), "LED_DUMP leaked to Stage"

            assert len(sent) == 200
            assert not hub._request_pending
            assert not hub._request_ids_by_sub
            assert hub._context_flight_id is None
        finally:
            hub.stop()

    _run(body())


def test_context_success_then_missing_led_reply_times_out_only_led_request():
    """Reproduce the observed GET_CONTEXT-success/LED_DUMP-silence shape.

    If the upstream device emits no complete LED_DUMP line, the hub cannot
    manufacture one: it keeps exactly that ordinary request pending, then
    reports a correlated timeout and remains usable.  This distinguishes an
    upstream non-response from a private-id routing collision.
    """

    async def body():
        hub, sent = _isolated_hub(request_timeout_s=0.02)
        control = hub.subscribe()
        observer = hub.subscribe()
        try:
            control.send('{"type":"GET_CONTEXT","id":"ctx"}')
            context_private_id = sent[-1]["id"]
            hub._dispatch(json.dumps({
                "type": "CONTEXT", "id": context_private_id,
                "context": {"kemper_block_X": "on"},
            }))
            assert await _message(control) == {
                "type": "CONTEXT", "id": "ctx",
                "context": {"kemper_block_X": "on"},
            }

            control.send('{"type":"LED_DUMP","id":"led"}')
            led_private_id = sent[-1]["id"]
            assert led_private_id in hub._request_pending
            assert await _message(control) == {
                "type": "ERROR", "error": "request_timeout",
                "of": "LED_DUMP", "id": "led",
            }
            assert not hub._request_pending
            assert observer._queue.empty()

            # A later request still routes normally; the missing LED reply did
            # not wedge the hub's per-subscriber correlation state.
            control.send('{"type":"PING","id":"after"}')
            ping_private_id = sent[-1]["id"]
            hub._dispatch(json.dumps({
                "type": "ACK", "id": ping_private_id,
            }))
            assert await _message(control) == {
                "type": "ACK", "id": "after",
            }

            # A late LED reply is quarantined and cannot leak a hub-private id.
            hub._dispatch(json.dumps({
                "type": "LED_DUMP", "id": led_private_id, "pixels": [],
            }))
            assert control._queue.empty()
            assert observer._queue.empty()
        finally:
            hub.stop()

    _run(body())


def test_request_timeout_must_be_positive():
    for timeout in (0, -0.1):
        try:
            Hub(None, request_timeout_s=timeout)
        except ValueError as exc:
            assert str(exc) == "request timeout must be positive"
        else:
            raise AssertionError(f"request timeout {timeout} was accepted")


if __name__ == "__main__":
    functions = [
        value for name, value in sorted(globals().items())
        if name.startswith("test_")
    ]
    failed = 0
    for function in functions:
        try:
            function()
            print(f"PASS {function.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {function.__name__}: {exc!r}")
    raise SystemExit(1 if failed else 0)
