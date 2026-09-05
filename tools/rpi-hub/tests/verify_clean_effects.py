"""Stress ACOUSTIC -> CLEAN and verify fresh CLEAN effect state each cycle."""
import asyncio
import json
import time

from websockets.asyncio.client import connect


async def wait_for(ws, predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = json.loads(await asyncio.wait_for(ws.recv(), deadline - time.monotonic()))
        if predicate(message):
            return message
    raise TimeoutError


async def switch(ws, slot, ident):
    await ws.send(json.dumps({"type": "SWITCH_PATCH", "id": ident,
                              "bank": 1, "slot": slot}))
    await wait_for(ws, lambda m: m.get("id") == ident)


async def main():
    async with connect("ws://127.0.0.1:8081/", max_size=None) as ws:
        await wait_for(ws, lambda m: m.get("type") == "HUB" and m.get("link") == "up")
        for cycle in range(1, 6):
            await switch(ws, 1, "a%d" % cycle)
            await asyncio.sleep(1.2)
            started = time.monotonic()
            await switch(ws, 2, "c%d" % cycle)
            context = await wait_for(
                ws,
                lambda m: m.get("type") == "CONTEXT"
                and m.get("context", {}).get("bank") == 1
                and m.get("context", {}).get("slot") == 2
                and "kemper_block_X" in m.get("context", {}),
            )
            ctx = context["context"]
            assert ctx["kemper_block_X"] == "on", ctx
            assert ctx.get("kemper_block_Reverb") != "on", ctx
            print("cycle %d PASS clean_ms=%.1f X=on Reverb!=on" %
                  (cycle, (time.monotonic() - started) * 1000), flush=True)
            await asyncio.sleep(0.4)


asyncio.run(main())
