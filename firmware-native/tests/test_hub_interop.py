"""Real native C application -> production hub -> concurrent TCP and WS clients.

Requires tools/rpi-hub/requirements.txt. Uses ephemeral loopback ports and a
temporary configuration tree; never imports a hardware transport target.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile

from test_host_emulator import Emulator, seed

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/rpi-hub"))
from bosun_hub.hub import Hub
from bosun_hub.server import _serve_tcp, _serve_ws
from websockets.asyncio.client import connect


async def tcp_response(reader, request_id):
    async with asyncio.timeout(15):
        while True:
            line = await reader.readline()
            assert line.endswith(b"\n"), ("TCP closed/truncated", line)
            response = json.loads(line)
            if response.get("id") == request_id:
                return response


async def ws_response(ws, request_id, events):
    async with asyncio.timeout(15):
        while True:
            response = json.loads(await ws.recv())
            if response.get("id") == request_id:
                return response
            events.append(response)


async def exercise(emulator):
    hub = Hub(f"tcp://127.0.0.1:{emulator.port}")
    hub.start()
    tcp_server = ws_server = writer = None
    try:
        async with asyncio.timeout(15):
            while not hub.link.connected:
                await asyncio.sleep(0.01)
        tcp_server = await _serve_tcp(hub, "127.0.0.1", 0)
        ws_server = await _serve_ws(hub, "127.0.0.1", 0)
        tcp_port = tcp_server.sockets[0].getsockname()[1]
        ws_port = ws_server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
        events = []

        async def tcp_request(request_id, kind, expected, **fields):
            writer.write(json.dumps(dict(type=kind, id=request_id, **fields)).encode() + b"\n")
            await writer.drain()
            response = await tcp_response(reader, request_id)
            assert response["type"] == expected, response
            return response

        async with connect(f"ws://127.0.0.1:{ws_port}") as ws:
            first = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert first["type"] == "HUB" and first["link"] == "up", first

            async def ws_request(request_id, kind, expected, **fields):
                await ws.send(json.dumps(dict(type=kind, id=request_id, **fields)))
                response = await ws_response(ws, request_id, events)
                assert response["type"] == expected, response
                return response

            # Independent editor and kiosk may both use id='1'. Each must get
            # its own response after the hub's internal request correlation.
            global_reply, manifest = await asyncio.gather(
                tcp_request("1", "GET_GLOBAL", "GLOBAL"),
                ws_request("1", "GET_MANIFEST", "MANIFEST"))
            assert global_reply["device"]["device_name"] == "Offline Captain"
            assert set(manifest["plugins"]) == {"generic_midi", "kemper_player"}
            device = await ws_request("bootstrap-device", "GET_DEVICE_INFO", "DEVICE_INFO")
            patches = await ws_request("bootstrap-list", "LIST_PATCHES", "PATCH_LIST")
            context = await ws_request("bootstrap-context", "GET_CONTEXT", "CONTEXT")
            assert device["current"] == {"bank": 1, "slot": 1}
            assert len(patches["patches"]) == 2
            assert context["context"]["patch_name"] == "CLEAN"
            first_patch = await ws_request("cache-prime", "GET_PATCH", "PATCH", bank=1, slot=1)
            assert first_patch["patch"]["name"] == "CLEAN"

            changed = {"name": "UPDATED CLEAN", "bindings": [{"switch": "1", "mode": "tap", "label": "NEW BINDING"}]}
            await tcp_request("edit", "PUT_PATCH", "ACK", bank=1, slot=1, patch=changed)
            updated = await ws_request("cache-invalidated", "GET_PATCH", "PATCH", bank=1, slot=1)
            assert updated["patch"] == changed
            # The current location didn't change: the UI needs an explicit
            # patch event to invalidate the old switch labels.
            await ws_request("after-edit-context", "GET_CONTEXT", "CONTEXT")
            async with asyncio.timeout(10):
                while not any(e.get("event") == "patch_switched" and e.get("slot") == 1 for e in events):
                    events.append(json.loads(await ws.recv()))

            await tcp_request("save", "SAVE_NOW", "SAVED")
            await tcp_request("switch", "SWITCH_PATCH", "ACK", bank=1, slot=2)
            switched = await ws_request("after-switch", "GET_CONTEXT", "CONTEXT")
            assert switched["context"]["slot"] == 2 and switched["context"]["patch_name"] == "CRUNCH"

            # Exercise the actual line framing for a near-limit patch while a
            # second client pulls the manifest. Neither stream may interleave.
            large = {"name": "LARGE", "unknown": "x" * 24000, "bindings": []}
            await tcp_request("large-put", "PUT_PATCH", "ACK", bank=1, slot=3, patch=large)
            large_reply, _ = await asyncio.gather(
                tcp_request("large-read", "GET_PATCH", "PATCH", bank=1, slot=3),
                ws_request("manifest-again", "GET_MANIFEST", "MANIFEST"))
            assert large_reply["patch"] == large

            # Repeated colliding ids stress response ownership and context
            # coalescing with the real C endpoint, not a fake pedal responder.
            for cycle in range(100):
                left, right = await asyncio.gather(
                    tcp_request(str(cycle), "GET_CONTEXT", "CONTEXT"),
                    ws_request(str(cycle), "GET_PATCH", "PATCH", bank=1, slot=2))
                assert left["context"]["slot"] == 2
                assert right["patch"]["name"] == "CRUNCH"
            stats = await tcp_request("stats", "STATS", "STATS")
            assert stats["protocol_errors"] == 0, stats
            assert stats["queue_overflows"] == 0, stats

            # Losing the editor must leave Stage connected and keep its own
            # request ids working; reconnecting the editor shares that link.
            writer.close()
            await writer.wait_closed()
            reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
            await asyncio.gather(tcp_request("reconnect", "PING", "ACK"),
                                 ws_request("reconnect", "GET_CONTEXT", "CONTEXT"))
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        for server in (tcp_server, ws_server):
            if server is not None:
                server.close()
        hub.stop()
        await asyncio.gather(*(server.wait_closed() for server in (tcp_server, ws_server) if server is not None))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emulator", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        seed(root)
        emulator = Emulator(args.emulator.resolve(strict=True), root, chunk=64)
        try:
            asyncio.run(exercise(emulator))
            saved = json.loads((root / "config/profiles/test/patches/01/01.json").read_text())
            assert saved["name"] == "UPDATED CLEAN"
            assert (root / "untouched.txt").read_text() == "original sentinel"
        finally:
            emulator.close()
    print("PASS native application + production hub: TCP/WS bootstrap, colliding IDs, UI events, cache invalidation, large JSON, 100 concurrent rounds, save and reconnect")


if __name__ == "__main__":
    main()
