"""
End-to-end tests: fake pedal <-> UpstreamLink <-> Hub <-> subscribers.

Dependency-free (no pytest-asyncio): each test wraps an async body in
asyncio.run. Run either way:

    python -m pytest -q            # from tools/rpi-hub/
    python tests/test_hub_e2e.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bosun_hub.hub import Hub  # noqa: E402
from bosun_hub.link import UpstreamLink  # noqa: E402
from tests.fake_pedal import FakePedal  # noqa: E402


async def _wait(predicate, timeout=6.0, interval=0.02) -> bool:
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


def _run(coro):
    return asyncio.run(coro)


# -- link -----------------------------------------------------------------


def test_link_syncs_and_forwards_pushed_lines():
    async def body():
        pedal = FakePedal()
        lines: list[str] = []
        link = UpstreamLink(f"tcp://127.0.0.1:{pedal.port}", on_line=lines.append)
        link.start()
        try:
            assert await _wait(lambda: link.connected), "link never synced"
            pedal.push({"type": "CONTEXT", "context": {"kemper_bpm": 120}})
            assert await _wait(lambda: any("kemper_bpm" in l for l in lines))
        finally:
            link.stop()
            pedal.close()

    _run(body())


def test_link_drops_pre_sync_backlog():
    async def body():
        pedal = FakePedal(backlog_lines=50)
        lines: list[str] = []
        link = UpstreamLink(f"tcp://127.0.0.1:{pedal.port}", on_line=lines.append)
        link.start()
        try:
            assert await _wait(lambda: link.connected)
            pedal.push({"type": "CONTEXT", "context": {"fresh": True}})
            assert await _wait(lambda: any('"fresh"' in l for l in lines))
            # nothing from before the sentinel ACK should have surfaced
            assert not any('"stale"' in l for l in lines), lines
        finally:
            link.stop()
            pedal.close()

    _run(body())


def test_link_swallows_own_keepalive_acks():
    async def body():
        pedal = FakePedal()
        lines: list[str] = []
        link = UpstreamLink(f"tcp://127.0.0.1:{pedal.port}", on_line=lines.append)
        link.start()
        try:
            assert await _wait(lambda: link.connected)
            # KEEPALIVE_S is 6s; wait past it so a keepalive PING/ACK cycles.
            await asyncio.sleep(7.5)
            assert not any("__hub_" in l for l in lines), lines
            assert any('"PING"' in r and "__hub_keepalive" in r for r in pedal.received)
        finally:
            link.stop()
            pedal.close()

    _run(body())


def test_link_reconnects_after_drop():
    async def body():
        pedal = FakePedal()
        states: list[bool] = []
        link = UpstreamLink(
            f"tcp://127.0.0.1:{pedal.port}",
            on_line=lambda l: None,
            on_state=lambda up, detail: states.append(up),
        )
        link.start()
        try:
            assert await _wait(lambda: link.connected)
            pedal.drop_clients()
            assert await _wait(lambda: link.connected is False)
            assert await _wait(lambda: link.connected is True, timeout=12)
            assert states[0] is True and False in states and states[-1] is True
        finally:
            link.stop()
            pedal.close()

    _run(body())


# -- hub ----------------------------------------------------------------


def test_hub_fans_out_to_two_subscribers():
    async def body():
        pedal = FakePedal()
        hub = Hub(f"tcp://127.0.0.1:{pedal.port}")
        hub.start()
        try:
            assert await _wait(lambda: hub.link.connected)
            a = hub.subscribe()
            b = hub.subscribe(want_status=True)
            got_a: list[str] = []
            got_b: list[str] = []

            async def drain(sub, sink):
                async for line in sub.lines():
                    sink.append(line)

            ta = asyncio.create_task(drain(a, got_a))
            tb = asyncio.create_task(drain(b, got_b))

            assert await _wait(lambda: any('"HUB"' in l for l in got_b))

            pedal.push({"type": "CONTEXT", "context": {"kemper_rig_name": "Lead"}})
            assert await _wait(lambda: any("Lead" in l for l in got_a))
            assert await _wait(lambda: any("Lead" in l for l in got_b))

            a.send('{"type":"GET_DEVICE_INFO","id":"x1"}')
            assert await _wait(
                lambda: any('"x1"' in r for r in pedal.received)
            )
            assert await _wait(lambda: any('"DEVICE_INFO"' in l for l in got_a))

            ta.cancel()
            tb.cancel()
        finally:
            hub.stop()
            pedal.close()

    _run(body())


def test_hub_slow_subscriber_drops_not_blocks():
    async def body():
        pedal = FakePedal()
        hub = Hub(f"tcp://127.0.0.1:{pedal.port}")
        hub.start()
        try:
            assert await _wait(lambda: hub.link.connected)
            hub.subscribe()  # never drained
            fast = hub.subscribe()
            got_fast: list[str] = []

            async def drain_fast():
                async for line in fast.lines():
                    got_fast.append(line)

            tf = asyncio.create_task(drain_fast())
            for i in range(2000):
                pedal.push({"type": "CONTEXT", "context": {"n": i}})
            assert await _wait(
                lambda: any('"n": 1999' in l or '"n":1999' in l for l in got_fast),
                timeout=10,
            )
            tf.cancel()
        finally:
            hub.stop()
            pedal.close()

    _run(body())


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
    sys.exit(1 if failed else 0)
