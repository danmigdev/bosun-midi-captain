"""Inspect real Stage bootstrap responses from the RPi/Captain link."""
import asyncio
import json
import time

from websockets.asyncio.client import connect


async def main():
    started = time.monotonic()
    async with connect("ws://192.168.1.91:8081/", max_size=None) as ws:
        for kind in ("LIST_PATCHES", "GET_GLOBAL"):
            await ws.send(json.dumps({"type": kind, "id": "inspect-" + kind}))
        pending = {"inspect-LIST_PATCHES", "inspect-GET_GLOBAL"}
        async with asyncio.timeout(20):
            while pending:
                raw = await ws.recv()
                elapsed = (time.monotonic() - started) * 1000
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError as exc:
                    print(f"{elapsed:.0f}ms MALFORMED {exc}: {raw[:160]!r}")
                    continue
                ident = msg.get("id")
                if ident in pending:
                    pending.remove(ident)
                    if msg.get("type") == "PATCH_LIST":
                        print(f"{elapsed:.0f}ms PATCH_LIST {len(msg.get('patches', []))}: "
                              f"{[p.get('name') for p in msg.get('patches', [])]}")
                    elif msg.get("type") == "GLOBAL":
                        nav = (msg.get("device") or {}).get("preset_navigation")
                        print(f"{elapsed:.0f}ms GLOBAL preset_navigation={nav}")
                    else:
                        print(f"{elapsed:.0f}ms {msg}")


asyncio.run(main())
