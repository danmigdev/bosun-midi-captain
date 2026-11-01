"""The Captain's screensaver reaches Stage through the hub's HUB frames.

The Captain announces ``{"type":"SCREENSAVER","active":...,"effect":...}``
whenever its screensaver starts, stops or changes effect. The hub keeps the
latest state for Stage clients, asks ``GET_SCREENSAVER`` on every Captain
(re)connect and treats both an ERROR reply (older firmware) and a lost link
as off. Editor commands for the screensaver are ordinary forwarded requests.
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


def _run(coro):
    return asyncio.run(coro)


def _isolated_hub():
    hub = Hub(None)
    sent: list[dict] = []

    def capture(line: str):
        sent.append(json.loads(line))
        return True

    hub.link.send = capture
    hub.link._connected = True
    return hub, sent


def _link(hub, up: bool) -> None:
    """A Captain link transition as the link thread reports it."""
    hub.link._connected = up
    hub._link_state(up)


def _queued(sub) -> list[dict]:
    """Every line queued for a subscriber, oldest first."""
    lines = []
    while not sub._queue.empty():
        lines.append(json.loads(sub._queue.get_nowait()))
    return lines


def _hub_frames(sub) -> list[dict]:
    return [line for line in _queued(sub) if line.get("type") == "HUB"]


def _event(active: bool, effect: str | None = None) -> str:
    event = {"type": "SCREENSAVER", "id": None, "active": active}
    if effect is not None:
        event["effect"] = effect
    return json.dumps(event)


def _queries(sent: list[dict]) -> list[dict]:
    return [request for request in sent if request["type"] == "GET_SCREENSAVER"]


async def _wait(predicate, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


UP = {"type": "HUB", "link": "up"}
DOWN = {"type": "HUB", "link": "down"}


def test_events_reach_stage_frames_and_every_client_gets_the_raw_line():
    async def body():
        hub, _ = _isolated_hub()
        stage = hub.subscribe(want_status=True)
        raw = hub.subscribe()
        try:
            assert _queued(stage) == [UP]

            hub._dispatch(_event(True, "bounce"))
            assert _queued(stage) == [
                {**UP, "screensaver": {"effect": "bounce"}},
                {"type": "SCREENSAVER", "id": None, "active": True, "effect": "bounce"},
            ]
            # The raw TCP view never gets HUB frames, only the Captain's line.
            assert _queued(raw) == [
                {"type": "SCREENSAVER", "id": None, "active": True, "effect": "bounce"},
            ]

            # Mix rotates: each new effect is one new frame, a repeat is none.
            hub._dispatch(_event(True, "drift"))
            assert _hub_frames(stage) == [{**UP, "screensaver": {"effect": "drift"}}]
            hub._dispatch(_event(True, "drift"))
            assert _hub_frames(stage) == []

            hub._dispatch(_event(False))
            assert _hub_frames(stage) == [UP]
            hub._dispatch(_event(False))
            assert _hub_frames(stage) == []
            assert len(_queued(raw)) == 4
        finally:
            hub.stop()

    _run(body())


def test_malformed_events_keep_the_last_state_and_an_unknown_effect_still_covers_stage():
    async def body():
        hub, _ = _isolated_hub()
        stage = hub.subscribe(want_status=True)
        try:
            _queued(stage)
            hub._dispatch(_event(True, "breathe"))
            assert _hub_frames(stage) == [{**UP, "screensaver": {"effect": "breathe"}}]
            hub._dispatch(json.dumps({"type": "SCREENSAVER", "active": "yes"}))
            hub._dispatch(json.dumps({"type": "SCREENSAVER"}))
            assert _hub_frames(stage) == []
            assert hub._screensaver == {"effect": "breathe"}

            # Stage chooses how to show an effect it does not know.
            hub._dispatch(_event(True, "starfield"))
            assert _hub_frames(stage) == [{**UP, "screensaver": {"effect": "starfield"}}]
            hub._dispatch(_event(True))
            assert _hub_frames(stage) == [{**UP, "screensaver": {}}]
        finally:
            hub.stop()

    _run(body())


def test_a_stage_that_connects_later_gets_the_remembered_screensaver():
    async def body():
        hub, _ = _isolated_hub()
        try:
            hub._dispatch(_event(True, "breathe"))
            late = hub.subscribe(want_status=True)
            assert _queued(late) == [{**UP, "screensaver": {"effect": "breathe"}}]
            raw = hub.subscribe()
            assert _queued(raw) == []

            hub._dispatch(_event(False))
            later = hub.subscribe(want_status=True)
            assert _queued(later) == [UP]
        finally:
            hub.stop()

    _run(body())


def test_a_stage_whose_queue_overflows_resyncs_with_the_screensaver():
    async def body():
        hub, _ = _isolated_hub()
        stage = hub.subscribe(want_status=True)
        try:
            hub._dispatch(_event(True, "bounce"))
            for index in range(stage._queue.maxsize + 1):
                hub._dispatch(json.dumps({"type": "CONTEXT", "context": {"n": index}}))
            frames = _hub_frames(stage)
            assert frames[-2:] == [DOWN, {**UP, "screensaver": {"effect": "bounce"}}]
        finally:
            hub.stop()

    _run(body())


def test_link_up_asks_get_screensaver_and_applies_the_reply_privately():
    async def body():
        hub, sent = _isolated_hub()
        stage = hub.subscribe(want_status=True)
        raw = hub.subscribe()
        try:
            _queued(stage)
            _link(hub, False)
            assert _queued(stage) == [DOWN]
            assert _queries(sent) == []

            _link(hub, True)
            assert _queued(stage) == [UP]
            [query] = _queries(sent)
            assert set(query) == {"type", "id"}
            assert query["id"].startswith("__bosun_ss_")
            # Not the link's own keepalive namespace, which it swallows.
            assert not query["id"].startswith("__hub_")

            hub._dispatch(json.dumps({
                "type": "SCREENSAVER", "id": query["id"],
                "active": True, "effect": "drift",
            }))
            assert _queued(stage) == [{**UP, "screensaver": {"effect": "drift"}}]
            assert _queued(raw) == []  # the hub's own reply is never relayed

            # A duplicate (or a late copy) of an answered reply is dropped.
            hub._dispatch(json.dumps({
                "type": "SCREENSAVER", "id": query["id"], "active": False,
            }))
            assert _queued(stage) == []
            assert _queued(raw) == []
            assert hub._screensaver == {"effect": "drift"}
        finally:
            hub.stop()

    _run(body())


def test_an_error_reply_from_older_firmware_means_off():
    async def body():
        hub, sent = _isolated_hub()
        stage = hub.subscribe(want_status=True)
        raw = hub.subscribe()
        try:
            _link(hub, False)
            _link(hub, True)
            _queued(stage)
            [query] = _queries(sent)
            hub._dispatch(json.dumps({
                "type": "ERROR", "id": query["id"],
                "error": "unknown_type", "of": "GET_SCREENSAVER",
            }))
            assert _queued(stage) == []
            assert _queued(raw) == []
            assert hub._screensaver is None

            # So does any other unexpected answer, e.g. a stub that ACKs all.
            hub._dispatch(_event(True, "bounce"))
            _queued(stage)
            _queued(raw)
            _link(hub, False)
            _link(hub, True)
            assert _queued(stage) == [DOWN, UP]
            query = _queries(sent)[-1]
            hub._dispatch(json.dumps({"type": "ACK", "id": query["id"]}))
            assert _queued(stage) == []
            assert _queued(raw) == []
        finally:
            hub.stop()

    _run(body())


def test_a_lost_link_turns_the_screensaver_off_and_drops_old_session_replies():
    async def body():
        hub, sent = _isolated_hub()
        stage = hub.subscribe(want_status=True)
        raw = hub.subscribe()
        try:
            _link(hub, False)
            _link(hub, True)
            first = _queries(sent)[-1]
            hub._dispatch(_event(True, "bounce"))
            _queued(stage)
            _queued(raw)

            _link(hub, False)
            assert _queued(stage) == [DOWN]
            assert hub._screensaver is None
            assert _queued(hub.subscribe(want_status=True)) == [DOWN]

            _link(hub, True)
            second = _queries(sent)[-1]
            assert second["id"] != first["id"]
            assert _queued(stage) == [UP]

            # The previous session's answer arrives late: not applied, not relayed.
            hub._dispatch(json.dumps({
                "type": "SCREENSAVER", "id": first["id"],
                "active": True, "effect": "bounce",
            }))
            assert _queued(stage) == []
            assert _queued(raw) == []
            hub._dispatch(json.dumps({
                "type": "SCREENSAVER", "id": second["id"], "active": False,
            }))
            assert _queued(stage) == []
            assert hub._screensaver is None
        finally:
            hub.stop()

    _run(body())


def test_a_query_the_link_refuses_leaves_the_screensaver_off():
    async def body():
        hub = Hub(None)
        hub.link.send = lambda line: False
        hub.link._connected = True
        stage = hub.subscribe(want_status=True)
        try:
            _link(hub, True)
            assert hub._screensaver_query is None
            assert _hub_frames(stage) == [UP, UP]
        finally:
            hub.stop()

    _run(body())


def test_editor_screensaver_commands_are_forwarded_and_their_replies_routed():
    async def body():
        hub, sent = _isolated_hub()
        stage = hub.subscribe(want_status=True)
        editor = hub.subscribe()
        other = hub.subscribe()
        try:
            _queued(stage)
            editor.send('{"type":"SCREENSAVER_START","id":7,"effect":"drift"}')
            [start] = [request for request in sent if request["type"] == "SCREENSAVER_START"]
            assert start["effect"] == "drift"
            assert start["id"] != 7
            hub._dispatch(json.dumps({"type": "ACK", "id": start["id"]}))
            assert _queued(editor) == [{"type": "ACK", "id": 7}]
            assert _queued(other) == []

            # The Captain then announces it like any start.
            hub._dispatch(_event(True, "drift"))
            assert _hub_frames(stage) == [{**UP, "screensaver": {"effect": "drift"}}]
            _queued(editor)
            _queued(other)

            editor.send('{"type":"GET_SCREENSAVER","id":"q"}')
            [query] = _queries(sent)
            assert query["id"] != "q" and not query["id"].startswith("__bosun_ss_")
            hub._dispatch(json.dumps({
                "type": "SCREENSAVER", "id": query["id"],
                "active": True, "effect": "breathe",
            }))
            assert _queued(editor) == [{
                "type": "SCREENSAVER", "id": "q", "active": True, "effect": "breathe",
            }]
            assert _queued(other) == []
            # The editor's answer is the Captain's current state too.
            assert _hub_frames(stage) == [{**UP, "screensaver": {"effect": "breathe"}}]
        finally:
            hub.stop()

    _run(body())


def test_editor_commands_are_refused_while_the_link_is_down():
    async def body():
        hub, sent = _isolated_hub()
        hub.link._connected = False
        editor = hub.subscribe()
        try:
            editor.send('{"type":"SCREENSAVER_START","id":1}')
            editor.send('{"type":"GET_SCREENSAVER","id":2}')
            assert sent == []
            assert _queued(editor) == [
                {"type": "ERROR", "error": "link_down", "of": "SCREENSAVER_START", "id": 1},
                {"type": "ERROR", "error": "link_down", "of": "GET_SCREENSAVER", "id": 2},
            ]
        finally:
            hub.stop()

    _run(body())


class ScreensaverPedal(FakePedal):
    """A Captain whose screensaver is on when the hub connects."""

    def __init__(self) -> None:
        self.screensaver: dict = {"active": True, "effect": "bounce"}
        super().__init__()

    def _reply(self, msg):
        if msg.get("type") == "GET_SCREENSAVER":
            return {"type": "SCREENSAVER", "id": msg.get("id", ""), **self.screensaver}
        return super()._reply(msg)


def test_end_to_end_over_tcp_and_websocket_with_a_reconnecting_captain():
    async def body():
        from websockets.asyncio.client import connect

        pedal = ScreensaverPedal()
        hub = Hub(f"tcp://127.0.0.1:{pedal.port}")
        hub.start()
        tcp_server = None
        ws_server = None
        tcp_writer = None

        def queries():
            return [line for line in pedal.received if '"GET_SCREENSAVER"' in line]

        async def frame(ws) -> dict:
            while True:
                message = json.loads(await asyncio.wait_for(ws.recv(), 3))
                if message.get("type") == "HUB":
                    return message

        try:
            assert await _wait(lambda: hub.link.connected), "hub link never synced"
            assert await _wait(lambda: hub._screensaver is not None)
            assert len(queries()) == 1
            tcp_server = await _serve_tcp(hub, "127.0.0.1", 0)
            ws_server = await _serve_ws(hub, "127.0.0.1", 0)
            tcp_port = tcp_server.sockets[0].getsockname()[1]
            ws_port = ws_server.sockets[0].getsockname()[1]
            tcp_reader, tcp_writer = await asyncio.open_connection("127.0.0.1", tcp_port)

            async with connect(f"ws://127.0.0.1:{ws_port}") as ws:
                # A Stage opened while the screensaver shows covers itself at once.
                assert await frame(ws) == {**UP, "screensaver": {"effect": "bounce"}}

                tcp_writer.write(b'{"type":"SCREENSAVER_START","id":"s","effect":"breathe"}\n')
                await tcp_writer.drain()
                assert json.loads(await asyncio.wait_for(tcp_reader.readline(), 3)) == {
                    "type": "ACK", "id": "s",
                }
                assert any('"SCREENSAVER_START"' in line and '"breathe"' in line
                           for line in pedal.received)

                pedal.push({"type": "SCREENSAVER", "id": None, "active": True, "effect": "breathe"})
                assert await frame(ws) == {**UP, "screensaver": {"effect": "breathe"}}
                assert json.loads(await asyncio.wait_for(tcp_reader.readline(), 3))["effect"] == "breathe"

                # The Captain goes away and comes back with its screensaver off.
                pedal.screensaver = {"active": False}
                pedal.drop_clients()
                assert await frame(ws) == DOWN
                assert await frame(ws) == UP
                assert await _wait(lambda: len(queries()) == 2)
                await asyncio.sleep(0.1)
                assert hub._screensaver is None

                pedal.push({"type": "SCREENSAVER", "id": None, "active": True, "effect": "drift"})
                assert await frame(ws) == {**UP, "screensaver": {"effect": "drift"}}
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
