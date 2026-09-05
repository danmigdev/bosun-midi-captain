"""
End-to-end tests: fake pedal <-> UpstreamLink <-> Hub <-> subscribers.

Dependency-free (no pytest-asyncio): each test wraps an async body in
asyncio.run. Run either way:

    python -m pytest -q            # from tools/rpi-hub/
    python tests/test_hub_e2e.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bosun_hub.hub import Hub  # noqa: E402
from bosun_hub.link import UpstreamLink  # noqa: E402
from bosun_hub import link as link_module  # noqa: E402
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


def test_link_drops_commands_sent_while_disconnected():
    async def body():
        pedal = FakePedal()
        link = UpstreamLink(f"tcp://127.0.0.1:{pedal.port}", on_line=lambda line: None)
        link.send('{"type":"EVENT","event":"stale"}')
        link.start()
        try:
            assert await _wait(lambda: link.connected)
            await asyncio.sleep(0.1)
            assert not any('"stale"' in line for line in pedal.received)
        finally:
            link.stop()
            pedal.close()

    _run(body())


def test_link_allows_bootstrap_burst_before_declaring_write_only_stall():
    class SlowBootstrapPedal(FakePedal):
        def _reply(self, msg):
            if msg.get("type") == "PING":
                return super()._reply(msg)
            return None

    async def body():
        pedal = SlowBootstrapPedal()
        link = UpstreamLink(f"tcp://127.0.0.1:{pedal.port}",
                            on_line=lambda line: None)
        link.start()
        try:
            assert await _wait(lambda: link.connected)
            for i in range(link_module.WRITE_ONLY_STALL + 2):
                link.send(json.dumps({"type": "GET_PATCH", "id": str(i),
                                      "bank": 1, "slot": 1}))
            await asyncio.sleep(0.5)
            assert link.connected, "a fast bootstrap burst reset the healthy link"
        finally:
            link.stop()
            pedal.close()

    _run(body())


def test_link_full_tx_queue_rejects_newest_without_evicting_accepted_order():
    link = UpstreamLink(None, on_line=lambda line: None)
    link._connected = True
    accepted = [f'{{"type":"PING","id":"{i}"}}' for i in range(link._tx.maxsize)]
    for line in accepted:
        assert link.send(line) is True
    assert link.send('{"type":"PING","id":"rejected"}') is False

    queued = [link._tx.get_nowait().strip() for _ in accepted]
    assert queued == accepted
    assert link._tx.empty()


def test_link_stop_interrupts_a_blocked_transport(monkeypatch):
    """SIGTERM must not consume systemd's TimeoutStopSec when CDC wedges."""

    class BlockedTransport:
        name = "/dev/ttyACM-test"

        def __init__(self):
            self.entered_read = threading.Event()
            self.closed = threading.Event()

        def write(self, data):
            pass

        def read(self, n):
            self.entered_read.set()
            assert self.closed.wait(10), "test transport was never closed"
            raise OSError("USB disconnected")

        def close(self):
            self.closed.set()

    transport = BlockedTransport()
    monkeypatch.setattr(link_module, "discover_candidates", lambda target: ["test"])
    monkeypatch.setattr(link_module, "_open_transport", lambda target: transport)
    link = UpstreamLink(None, on_line=lambda line: None)
    link.start()
    assert transport.entered_read.wait(1), "link did not enter blocked USB read"

    started = time.monotonic()
    link.stop()
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, f"shutdown took {elapsed:.3f}s"
    assert link._thread is None


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
                lambda: any(
                    json.loads(r).get("type") == "GET_DEVICE_INFO"
                    for r in pedal.received
                )
            )
            device_request = next(
                json.loads(r) for r in pedal.received
                if json.loads(r).get("type") == "GET_DEVICE_INFO"
            )
            assert device_request["id"] != "x1"
            assert device_request["id"].startswith("__bosun_req_")
            assert await _wait(lambda: any('"DEVICE_INFO"' in l for l in got_a))
            await asyncio.sleep(0.05)
            assert not any('"DEVICE_INFO"' in line for line in got_b)

            ta.cancel()
            tb.cancel()
        finally:
            hub.stop()
            pedal.close()

    _run(body())


def test_hub_forwards_pushed_lines_with_low_latency():
    """A CONTEXT/EVENT the pedal pushes must reach every subscriber
    within a few milliseconds - the Stage view's whole point is live
    feedback."""

    async def body():
        pedal = FakePedal()
        hub = Hub(f"tcp://127.0.0.1:{pedal.port}")
        hub.start()
        try:
            assert await _wait(lambda: hub.link.connected)
            sub = hub.subscribe()
            got: "list[tuple[float, str]]" = []

            async def drain():
                async for line in sub.lines():
                    got.append((asyncio.get_event_loop().time(), line))

            task = asyncio.create_task(drain())
            await asyncio.sleep(0.05)  # settle

            samples = []
            for i in range(20):
                sent_at = asyncio.get_event_loop().time()
                pedal.push({"type": "CONTEXT", "context": {"seq": i}})
                assert await _wait(
                    lambda: any(f'"seq": {i}' in l or f'"seq":{i}' in l for _, l in got),
                    timeout=2,
                )
                recv_at = next(t for t, l in got if f'"seq": {i}' in l or f'"seq":{i}' in l)
                samples.append(recv_at - sent_at)
                await asyncio.sleep(0.02)

            worst = max(samples)
            avg = sum(samples) / len(samples)
            # Generous bounds - CI hosts are noisy - but well under the
            # "feels laggy" threshold. Regression net, not a benchmark.
            assert worst < 0.20, f"worst forward latency {worst * 1000:.0f} ms"
            assert avg < 0.05, f"avg forward latency {avg * 1000:.0f} ms"
            task.cancel()
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


def test_get_patch_cache_replies_locally_with_callers_id():
    class DeferredPatchPedal(FakePedal):
        def _reply(self, msg):
            if msg.get("type") == "GET_PATCH":
                return None
            return super()._reply(msg)

    async def body():
        pedal = DeferredPatchPedal()
        hub = Hub(f"tcp://127.0.0.1:{pedal.port}")
        hub.start()
        try:
            assert await _wait(lambda: hub.link.connected)
            sub = hub.subscribe()
            got: list[dict] = []

            async def drain():
                async for line in sub.lines():
                    got.append(json.loads(line))

            task = asyncio.create_task(drain())
            sub.send(json.dumps({"type": "GET_PATCH", "id": "cold", "bank": 2, "slot": 4}))
            def patch_requests():
                return [json.loads(line) for line in pedal.received
                        if json.loads(line).get("type") == "GET_PATCH"]

            assert await _wait(lambda: len(patch_requests()) == 1)
            pedal.push({"type": "PATCH", "id": patch_requests()[0]["id"],
                        "bank": 2, "slot": 4,
                        "profile": "", "patch": {"name": "Acoustic", "bindings": []}})
            assert await _wait(lambda: any(m.get("id") == "cold" for m in got))

            before = len(pedal.received)
            sub.send(json.dumps({"type": "GET_PATCH", "id": "warm", "bank": 2, "slot": 4}))
            assert await _wait(lambda: any(m.get("id") == "warm" for m in got))
            warm = next(m for m in got if m.get("id") == "warm")
            assert warm["patch"]["name"] == "Acoustic"
            await asyncio.sleep(0.1)
            assert len(pedal.received) == before, "cache hit leaked onto CDC"
            task.cancel()
        finally:
            hub.stop()
            pedal.close()

    _run(body())


def test_patch_mutations_and_disconnect_invalidate_cache():
    async def body():
        pedal = FakePedal()
        hub = Hub(f"tcp://127.0.0.1:{pedal.port}")
        hub.start()
        try:
            assert await _wait(lambda: hub.link.connected)
            sub = hub.subscribe()

            def seed(name="Old"):
                hub._patch_cache[("", 1, 3)] = {
                    "type": "PATCH", "bank": 1, "slot": 3,
                    "profile": "", "patch": {"name": name},
                }

            def has_patch_request():
                return any(
                    json.loads(line).get("type") == "GET_PATCH"
                    for line in pedal.received
                )

            for mutation in (
                {"type": "PUT_PATCH", "patch": {"name": "New"}},
                {"type": "PUT_BINDING", "binding": {"switch": "1"}},
                {"type": "DELETE_PATCH"},
                {"type": "DISCARD"},
            ):
                seed()
                mutation.update({"id": "mut", "bank": 1, "slot": 3})
                sub.send(json.dumps(mutation))
                sub.send(json.dumps({"type": "GET_PATCH", "id": "probe", "bank": 1, "slot": 3}))
                assert await _wait(has_patch_request)
                pedal.received.clear()

            seed()
            hub._dispatch_status(hub._status_line(False))
            sub.send(json.dumps({"type": "GET_PATCH", "id": "after-down", "bank": 1, "slot": 3}))
            assert await _wait(has_patch_request)
        finally:
            hub.stop()
            pedal.close()

    _run(body())


def test_patch_cache_is_profile_scoped_and_profile_switch_clears_it():
    async def body():
        hub = Hub(None)
        sent: list[str] = []
        hub.link.send = sent.append
        hub.link._connected = True
        sub = hub.subscribe()
        hub._patch_cache[("clean", 1, 1)] = {
            "type": "PATCH", "bank": 1, "slot": 1, "profile": "clean",
            "patch": {"name": "Clean"},
        }

        sub.send(json.dumps({"type": "GET_PATCH", "id": "wrong", "bank": 1, "slot": 1,
                             "profile": "heavy"}))
        assert any(json.loads(line).get("profile") == "heavy" for line in sent)
        sent.clear()

        # Correct profile is warm and must stay off the upstream link.
        sub.send(json.dumps({"type": "GET_PATCH", "id": "right", "bank": 1, "slot": 1,
                             "profile": "clean"}))
        reply = json.loads(await sub._queue.get())
        assert reply["id"] == "right" and reply["patch"]["name"] == "Clean"
        assert sent == []

        sub.send(json.dumps({"type": "SWITCH_PROFILE", "id": "sw", "profile_id": "clean"}))
        sent.clear()
        sub.send(json.dumps({"type": "GET_PATCH", "id": "after", "bank": 1, "slot": 1,
                             "profile": "clean"}))
        assert any(json.loads(line).get("profile") == "clean" for line in sent)
        sub.close()

    _run(body())


def test_slow_stage_subscriber_gets_forced_resync_after_overflow():
    async def body():
        hub = Hub(None)
        sub = hub.subscribe(want_status=True)
        # Consume the initial status, then overflow without a reader.
        await sub._queue.get()
        for i in range(513):
            sub._offer(json.dumps({"type": "CONTEXT", "context": {"n": i}}))
        first = json.loads(await sub._queue.get())
        second = json.loads(await sub._queue.get())
        newest = json.loads(await sub._queue.get())
        assert first == {"type": "HUB", "link": "down"}
        assert second == {"type": "HUB", "link": "up"}
        assert newest["context"]["n"] == 512
        sub.close()

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
