"""
Full-stack smoke test: fake pedal <-> hub server <-> a raw TCP client,
a WebSocket client, and the static HTTP endpoint, all at once.

    python -m pytest -q tests/test_server_smoke.py
    python tests/test_server_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bosun_hub.server import run  # noqa: E402
from tests.fake_pedal import FakePedal  # noqa: E402


async def _tcp_roundtrip(port: int) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b'{"type":"PING","id":"smoke1"}\n')
    await writer.drain()
    deadline = asyncio.get_event_loop().time() + 5
    buf = b""
    while asyncio.get_event_loop().time() < deadline:
        buf += await reader.read(4096)
        if b"smoke1" in buf and b"ACK" in buf:
            break
    assert b"smoke1" in buf and b"ACK" in buf, buf
    writer.close()
    await writer.wait_closed()


async def _ws_roundtrip(port: int) -> None:
    from websockets.asyncio.client import connect

    async with connect(f"ws://127.0.0.1:{port}") as ws:
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert first.get("type") == "HUB" and first.get("link") == "up", first
        await ws.send('{"type":"PING","id":"smokews"}')
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            if msg.get("id") == "smokews" and msg.get("type") == "ACK":
                return


def _http_get(port: int) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
        return r.read().decode("utf-8")


def test_server_smoke(tmp_path_factory=None):
    async def body():
        tmp = tempfile.TemporaryDirectory()
        stage = Path(tmp.name)
        (stage / "index.html").write_text("<h1>stage kiosk</h1>", encoding="utf-8")

        pedal = FakePedal()
        server_task = asyncio.create_task(
            run(
                f"tcp://127.0.0.1:{pedal.port}",
                host="127.0.0.1",
                tcp_port=9899,
                ws_port=8091,
                http_port=8090,
                stage_dir=stage,
            )
        )
        await asyncio.sleep(1.0)  # let the link sync and listeners bind
        try:
            await _tcp_roundtrip(9899)
            await _ws_roundtrip(8091)
            body_html = await asyncio.get_event_loop().run_in_executor(
                None, _http_get, 8090
            )
            assert "stage kiosk" in body_html
        finally:
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass
            pedal.close()
            tmp.cleanup()

    asyncio.run(body())


if __name__ == "__main__":
    try:
        test_server_smoke()
        print("PASS test_server_smoke")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL test_server_smoke: {exc!r}")
        raise
