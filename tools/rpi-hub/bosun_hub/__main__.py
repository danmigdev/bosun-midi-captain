"""
Entry point: ``python -m bosun_hub``.

Typical Pi usage (from the systemd unit):

    python -m bosun_hub --stage-dir /opt/bosun-hub/stage

Local development against the firmware emulator (no pedal, no Pi):

    python tools/tcp_firmware_emulator.py                 # terminal 1
    python -m bosun_hub --target tcp://127.0.0.1:9876 \\   # terminal 2
        --tcp-port 9899 --ws-port 8081 --http-port 8080
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from . import __version__
from .server import run


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="bosun-hub", description=__doc__)
    p.add_argument(
        "--target",
        default=None,
        help="pedal data port: a device path, a tcp://host:port address, "
        "or omitted to auto-detect /dev/ttyACM*",
    )
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--tcp-port", type=int, default=9876, help="raw protocol (editor)")
    p.add_argument("--ws-port", type=int, default=8081, help="WebSocket (kiosk)")
    p.add_argument("--http-port", type=int, default=8080, help="static Stage bundle")
    p.add_argument(
        "--stage-dir",
        type=Path,
        default=None,
        help="directory of the built Stage kiosk bundle to serve over HTTP",
    )
    p.add_argument("-v", "--verbose", action="count", default=0)
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)

    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        pass


async def _amain(args: argparse.Namespace) -> None:
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(
        run(
            args.target,
            host=args.host,
            tcp_port=args.tcp_port,
            ws_port=args.ws_port,
            http_port=args.http_port,
            stage_dir=args.stage_dir,
        )
    )
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, task.cancel)
        except NotImplementedError:  # Windows
            pass
    try:
        await task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    main()
