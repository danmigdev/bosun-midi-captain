"""Measure a real rig switch as observed by the Stage WebSocket client."""
import asyncio
import json
import sys
import time

from websockets.asyncio.client import connect


async def receive_until(ws, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    seen = []
    while time.monotonic() < deadline:
        raw = await asyncio.wait_for(ws.recv(), deadline - time.monotonic())
        msg = json.loads(raw)
        seen.append((time.monotonic(), msg))
        if predicate(msg):
            return seen
    raise TimeoutError("expected Stage update not received")


async def switch(ws, bank, slot):
    ident = "switch-%s-%s" % (bank, slot)
    started = time.monotonic()
    await ws.send(json.dumps({"type": "SWITCH_PATCH", "id": ident,
                              "bank": bank, "slot": slot}))
    seen = await receive_until(ws, lambda m: m.get("id") == ident)
    ack_ms = (seen[-1][0] - started) * 1000
    await ws.send(json.dumps({"type": "GET_PATCH", "id": ident + "-patch",
                              "bank": bank, "slot": slot}))
    seen += await receive_until(
        ws, lambda m: m.get("type") == "PATCH"
        and m.get("id") == ident + "-patch")
    print("switch %s/%s ack_ms=%.1f patch_ms=%.1f messages=%d" %
          (bank, slot, ack_ms, (seen[-1][0] - started) * 1000, len(seen)))
    return started, seen


async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8081/"
    async with connect(url, max_size=None) as ws:
        await receive_until(ws, lambda m: m.get("type") == "HUB" and m.get("link") == "up")
        await switch(ws, 1, 2)
        await asyncio.sleep(1.5)
        started, seen = await switch(ws, 1, 1)
        for received, msg in seen:
            if (msg.get("type") == "CONTEXT"
                    and msg.get("context", {}).get("kemper_block_C") == "on"):
                print("ACOUSTIC HARM(C)=on after_ms=%.1f partial=%s" %
                      ((received - started) * 1000, msg.get("partial")))
                return
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            seen.append((time.monotonic(), msg))
            if msg.get("type") == "CONTEXT" and msg.get("context", {}).get("kemper_block_C") == "on":
                print("ACOUSTIC HARM(C)=on after_ms=%.1f partial=%s" %
                      ((seen[-1][0] - started) * 1000, msg.get("partial")))
                return
        raise SystemExit("ACOUSTIC never published kemper_block_C=on")


asyncio.run(main())
