#!/usr/bin/env python3
"""Compatibility CLI for flashing a Captain with the canonical OTA client.

This command keeps the convenient serial-port autodetection and post-reboot
probe of the original ``flash_firmware.py`` script. All wire framing, upload
chunking, capability negotiation and transaction retries are deliberately
provided by :mod:`push_firmware`; there must be only one OTA implementation.

Examples::

    python tools/flash_firmware.py
    python tools/flash_firmware.py --port COM4
    python tools/flash_firmware.py --port socket://192.168.1.91:9876
    python tools/flash_firmware.py --no-reboot
"""

import argparse
import sys
import time
from pathlib import Path

try:
    import push_firmware as ota
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial required: pip install pyserial")


FIRMWARE_ROOT = Path(__file__).resolve().parent.parent / "firmware"
PROBE_TIMEOUT_S = 1.5


def _close(transport) -> None:
    """Close a transport when possible, including test/dummy transports."""
    close = getattr(transport, "close", None)
    if callable(close):
        close()


def _open_and_drain(port_name: str):
    """Open through the canonical transport factory and discard old chatter."""
    transport = ota.open_transport(port_name)
    try:
        # Preserve the old CLI's bounded local-serial write timeout. URL
        # transports need not expose this pyserial property, so it is optional.
        if hasattr(transport, "write_timeout"):
            transport.write_timeout = 2.0

        time.sleep(0.3)
        end = time.monotonic() + 0.5
        while time.monotonic() < end:
            data = transport.read(4096)
            if not data:
                break

        # A reused transport must not retain frames from an earlier transaction.
        rx = getattr(transport, ota._RX_BUFFER_ATTR, None)
        if isinstance(rx, bytearray):
            rx.clear()
        return transport
    except Exception:
        _close(transport)
        raise


def list_firmware_files(root):
    """Return ``(source, destination, size)`` using canonical selection.

    ``push_firmware.collect_files`` owns all filtering rules, including
    preserving ``config/`` and preferring a compiled ``.mpy`` sibling over
    ``.py``. This adapter preserves the legacy function's return shape
    without cloning those rules.
    """
    pairs = ota.collect_files(Path(root), None, include_config=False)
    return [(source, destination, source.stat().st_size)
            for source, destination in pairs]


def _ping(transport, request_id: str = "flash-probe"):
    return ota.call(
        transport,
        {"type": "PING", "id": request_id},
        timeout=PROBE_TIMEOUT_S,
    )


def find_bosun_port(preferred):
    """Return the requested port, or the first serial port that ACKs PING."""
    candidates = [preferred] if preferred else [p.device for p in list_ports.comports()]
    print("Probing ports:", ", ".join(candidates))
    for name in candidates:
        transport = None
        try:
            transport = _open_and_drain(name)
            ack = _ping(transport)
            if ack.get("type") == "ACK":
                print(f"  {name}: ACK (fw {ack.get('fw', '?')})")
                return name
            print(f"  {name}: no ACK")
        except Exception as exc:
            print(f"  {name}: ping failed ({exc})")
        finally:
            if transport is not None:
                _close(transport)
    return None


def push_files(transport, files) -> tuple[int, float]:
    """Upload ``files`` exclusively through the canonical safe transaction."""
    ids = [0]
    total_bytes = 0
    started = time.monotonic()
    for source, destination, size in files:
        ota.push_file_with_retries(transport, source, destination, ids)
        total_bytes += size
    return total_bytes, time.monotonic() - started


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--port",
        help="Force a data CDC port or pyserial URL (skip autodetect)",
    )
    parser.add_argument(
        "--no-reboot",
        action="store_true",
        help="Skip the final REBOOT; new code starts on the next reset",
    )
    args = parser.parse_args()

    if not FIRMWARE_ROOT.is_dir():
        sys.exit(f"firmware/ not found at {FIRMWARE_ROOT}")

    files = list_firmware_files(FIRMWARE_ROOT)
    total_bytes = sum(size for _, _, size in files)
    print(f"\nFirmware tree at {FIRMWARE_ROOT}")
    print(f"{len(files)} files, {total_bytes / 1024:.1f} KB total")

    port_name = find_bosun_port(args.port)
    if not port_name:
        sys.exit(
            "\nNo bosun pedal found. Close the editor first "
            "(it holds the data port) and retry."
        )

    print(f"\nFlashing via {port_name}...")
    transport = _open_and_drain(port_name)
    try:
        uploaded, elapsed = push_files(transport, files)
        rate = uploaded / elapsed / 1024 if elapsed > 0 else 0.0
        print(
            f"\nAll {len(files)} files pushed in {elapsed:.1f}s "
            f"({rate:.1f} KB/s)."
        )

        if args.no_reboot:
            print("Skipping REBOOT (--no-reboot)")
        else:
            print("Sending REBOOT...")
            response = ota.request_reboot(
                transport, request_id="flash-reboot", timeout=2.0,
            )
            if response is None:
                # USB normally disappears before its ACK can be observed,
                # after the canonical helper has written the complete frame.
                print("  no ACK (firmware likely already reset)")
    finally:
        _close(transport)

    if not args.no_reboot:
        print("Waiting for pedal to come back online...")
        time.sleep(3.5)
        back = find_bosun_port(port_name)
        if back:
            print(f"Pedal is back on {back}")
        else:
            print("Pedal not back yet - may need a few more seconds")

    print("\nDone.")


if __name__ == "__main__":
    main()
