"""Capture every Stage-relevant message during a real patch transition."""
import asyncio
import json
import time

from websockets.asyncio.client import connect


async def main():
    async with connect("ws://127.0.0.1:8081/", max_size=None) as ws:
        started = time.monotonic()

        async def send(message):
            await ws.send(json.dumps(message))

        async def collect(seconds):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                try:
                    message = json.loads(await asyncio.wait_for(ws.recv(), deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    return
                if message.get("type") in ("CONTEXT", "EVENT", "PATCH", "DEVICE_INFO"):
                    print(f"{(time.monotonic() - started) * 1000:7.1f} {json.dumps(message, separators=(',', ':'))}", flush=True)

        await send({"type": "SWITCH_PATCH", "id": "to-acoustic", "bank": 1, "slot": 1})
        await collect(2.0)
        await send({"type": "GET_CONTEXT", "id": "acoustic-context"})
        await collect(1.5)
        print("--- CLEAN ---", flush=True)
        await send({"type": "SWITCH_PATCH", "id": "to-clean", "bank": 1, "slot": 2})
        await send({"type": "GET_PATCH", "id": "clean-patch", "bank": 1, "slot": 2})
        await collect(3.0)


asyncio.run(main())
