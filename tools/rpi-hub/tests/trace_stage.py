"""
Continuous diagnostic: connect to the hub's WebSocket exactly like the
kiosk does and log, with timestamps, every message that carries switch/
effect-relevant state - so an effect toggle on the pedal can be timed
end to end without needing a browser console.

    python3 trace_stage.py            # logs to stdout
Run it via run-demo.sh-style nohup and tail /tmp/stage-trace.log.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

URL = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8081/"


async def main() -> None:
    from websockets.asyncio.client import connect

    async with connect(URL, max_size=None) as ws:
        t0 = time.time()

        def log(*a: object) -> None:
            print(f"{time.time() - t0:8.2f}", *a, flush=True)

        log("connected to", URL)
        # Deliberately NOT requesting GET_MANIFEST: it streams for seconds
        # on the firmware and starves CONTEXT pushes - which is exactly
        # what we are here to observe.
        for cmd in ("GET_DEVICE_INFO", "GET_CONTEXT"):
            await ws.send(json.dumps({"type": cmd, "id": cmd}))

        last_blocks: dict[str, str] = {}
        last_rig = None
        msg_count = 0
        parse_fail = 0

        while True:
            raw = await ws.recv()
            msg_count += 1
            try:
                m = json.loads(raw)
            except Exception as e:  # noqa: BLE001
                parse_fail += 1
                log(f"PARSE FAIL #{parse_fail} len={len(raw)} err={e} head={raw[:80]!r} tail={raw[-80:]!r}")
                continue

            t = m.get("type")
            if t == "CONTEXT":
                ctx = m.get("context", {})
                blocks = {k[len("kemper_block_"):]: v for k, v in ctx.items() if k.startswith("kemper_block_")}
                rig = ctx.get("kemper_rig_name")
                changed = {k: v for k, v in blocks.items() if last_blocks.get(k) != v}
                solicited = m.get("id") == "GET_CONTEXT"
                tag = "CONTEXT(poll)" if solicited else "CONTEXT(push)"
                if changed:
                    log(f"{tag} blocks changed -> {changed}   full={blocks}")
                elif rig != last_rig:
                    log(f"{tag} rig -> {rig}")
                last_blocks = blocks or last_blocks
                last_rig = rig
            elif t == "EVENT":
                log(f"EVENT {m.get('event')} sw={m.get('switch')} action={m.get('action')} bank={m.get('bank')} slot={m.get('slot')}")
            elif t == "PATCH":
                p = m.get("patch", {})
                log(f"PATCH {m.get('bank')}/{m.get('slot')} name={p.get('name')} bindings={len(p.get('bindings', []))}")
            elif t in ("HUB", "DEVICE_INFO", "MANIFEST"):
                extra = f" link={m.get('link')}" if t == "HUB" else ""
                log(f"{t}{extra} (len={len(raw)})")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
