#!/usr/bin/env python3
"""Push the local firmware/ tree to the bosun pedal over USB CDC.

Mirrors what the editor's `FirmwarePushOverlay` does (firmware-push.ts):
opens the data port, sends each file via the PUT_FILE_BEGIN / CHUNK /
END protocol, then REBOOT. No bootloader / mass-storage drive needed -
the pedal stays in performance mode the entire time.

Use when:
- The editor's "Re-flash firmware" button isn't accessible (UI bug,
  port held by another process, dev iteration without the editor open)
- You want a reproducible CLI flash from scripts / CI

Refuses to run if the editor is holding the port - bosun-editor.exe must
be closed first so this script can grab COM4. Reconnect by reopening
the editor after the script reports "rebooted".

Usage
-----
    python tools/flash_firmware.py            # auto-detects bosun port
    python tools/flash_firmware.py --port COM4
    python tools/flash_firmware.py --no-reboot
"""
import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial required: pip install pyserial")


FIRMWARE_ROOT = Path(__file__).resolve().parent.parent / "firmware"
CHUNK_B64 = 512
DEFAULT_TIMEOUT_S = 6.0
PROBE_TIMEOUT_S = 1.5


# ---------------- IO helpers ----------------

def _send_command(port, obj):
    line = json.dumps(obj).encode() + b"\n"
    port.write(line)
    port.flush()


def _read_line(port, timeout_s):
    deadline = time.monotonic() + timeout_s
    buf = bytearray()
    while time.monotonic() < deadline:
        chunk = port.read(256)
        if chunk:
            buf.extend(chunk)
            nl = buf.find(b"\n")
            if nl >= 0:
                line = bytes(buf[:nl])
                # Anything after the newline is the start of the next
                # line - the editor's parser is line-based, we mimic.
                rest = bytes(buf[nl + 1:])
                if rest:
                    port._leftover = rest
                return line
    return None


def _read_json_with_id(port, want_id, timeout_s):
    """Read JSON lines from port until one with matching id arrives.
    Returns the parsed dict, or None on timeout. Logs unrelated lines."""
    leftover = getattr(port, "_leftover", b"")
    port._leftover = b""
    if leftover:
        # If a previous _read_line stashed leftover bytes, prepend them.
        # cheap: we re-issue read() in the loop. For simplicity, drop
        # leftover - the firmware's responses are small enough that we
        # almost never have leftover in practice and the worst case is
        # one extra timeout.
        pass
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        remaining = max(0.05, deadline - time.monotonic())
        line = _read_line(port, remaining)
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            print("  noise: %r" % line[:80])
            continue
        if msg.get("id") == want_id:
            return msg
        # Ignore other messages (EVENTs, unrelated ACKs from a busy host).
        print("  skip:", msg.get("type"), msg.get("id"))
    return None


# ---------------- protocol ops ----------------

class Pedal:
    def __init__(self, port_name):
        self.port_name = port_name
        self.port = None
        self._next_id = 1

    def _id(self):
        self._next_id += 1
        return "flash-%d" % self._next_id

    def open(self):
        # Same baud the editor uses; serial2 / pyserial both honor it
        # the same on Windows CDC.
        self.port = serial.Serial(
            self.port_name, 115200,
            timeout=0.1, write_timeout=2.0,
            rtscts=False, dsrdtr=False,
        )
        # Drain any boot chatter (REPL leftovers, sensing frames, etc.).
        time.sleep(0.3)
        end = time.monotonic() + 0.5
        while time.monotonic() < end:
            data = self.port.read(4096)
            if not data:
                break

    def close(self):
        if self.port and self.port.is_open:
            self.port.close()

    def ping(self):
        mid = self._id()
        _send_command(self.port, {"type": "PING", "id": mid})
        return _read_json_with_id(self.port, mid, PROBE_TIMEOUT_S)

    def request(self, msg_type, timeout_s=DEFAULT_TIMEOUT_S, **fields):
        mid = self._id()
        _send_command(self.port, {"type": msg_type, "id": mid, **fields})
        resp = _read_json_with_id(self.port, mid, timeout_s)
        if resp is None:
            raise TimeoutError(f"{msg_type}: no response within {timeout_s}s")
        if resp.get("type") == "ERROR":
            raise RuntimeError("%s -> %s (%s)" % (
                msg_type, resp.get("error", "?"), resp.get("detail", "")))
        return resp

    def push_file(self, abs_path, dst_path):
        with open(abs_path, "rb") as f:
            raw = f.read()
        b64 = base64.b64encode(raw).decode("ascii")
        self.request("PUT_FILE_BEGIN", path=dst_path)
        for i in range(0, len(b64), CHUNK_B64):
            self.request("PUT_FILE_CHUNK", path=dst_path,
                         data_b64=b64[i:i + CHUNK_B64])
        self.request("PUT_FILE_END", path=dst_path)
        return len(raw)


# ---------------- directory walk ----------------

def list_firmware_files(root):
    """Mirror of installer.rs:walk_collect. Returns sorted list of
    (abs_path, dst_path, size) tuples.

    /config is skipped at the top level because the pedal's user data
    (profiles, patches, midi_learn) lives there - we never want to
    overwrite that during a firmware push. A virgin pedal boots into
    the editor's Onboarding wizard with no profiles."""
    SKIP_DIRS = ("__pycache__",)
    SKIP_FILE_SUFFIX = (".pyc", ".tmp")
    SKIP_FILE_EXACT = (".DS_Store", "Thumbs.db")
    SKIP_TOP_LEVEL_DIRS = ("config",)
    root_path = Path(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root_path)
        if str(rel_dir) == ".":
            dirnames[:] = [d for d in dirnames
                           if d not in SKIP_DIRS
                           and not d.startswith(".")
                           and d not in SKIP_TOP_LEVEL_DIRS]
        else:
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name in SKIP_FILE_EXACT:
                continue
            if any(name.endswith(s) for s in SKIP_FILE_SUFFIX):
                continue
            abs_path = Path(dirpath) / name
            rel = abs_path.relative_to(root).as_posix()
            dst = "/" + rel
            out.append((abs_path, dst, abs_path.stat().st_size))
    out.sort(key=lambda t: t[1])
    return out


# ---------------- port autodetect ----------------

def find_bosun_port(preferred):
    """If preferred is given, try only that. Otherwise probe every
    serial port with a PING and return the one that ACKs first."""
    if preferred:
        candidates = [preferred]
    else:
        candidates = [p.device for p in list_ports.comports()]
    print("Probing ports:", ", ".join(candidates))
    for name in candidates:
        try:
            pedal = Pedal(name)
            pedal.open()
            try:
                ack = pedal.ping()
            except Exception as e:
                print(f"  {name}: ping failed ({e})")
                pedal.close()
                continue
            pedal.close()
            if ack and ack.get("type") == "ACK":
                print(f"  {name}: ACK (fw {ack.get('fw', '?')})")
                return name
            print(f"  {name}: no ACK")
        except serial.SerialException as e:
            print(f"  {name}: {e}")
    return None


# ---------------- main flow ----------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", help="Force a specific COM port (skip autodetect)")
    parser.add_argument("--no-reboot", action="store_true",
                        help="Skip the final REBOOT - files land but firmware keeps running the old code")
    args = parser.parse_args()

    if not FIRMWARE_ROOT.is_dir():
        sys.exit(f"firmware/ not found at {FIRMWARE_ROOT}")

    files = list_firmware_files(FIRMWARE_ROOT)
    total_bytes = sum(s for _, _, s in files)
    print(f"\nFirmware tree at {FIRMWARE_ROOT}")
    print(f"{len(files)} files, {total_bytes / 1024:.1f} KB total")

    port_name = find_bosun_port(args.port)
    if not port_name:
        sys.exit("\nNo bosun pedal found. Close the editor first (it holds the data port) and retry.")

    print(f"\nFlashing via {port_name}...")
    pedal = Pedal(port_name)
    pedal.open()
    try:
        t0 = time.monotonic()
        for idx, (abs_path, dst, size) in enumerate(files, 1):
            bar = f"[{idx:2d}/{len(files)}]"
            print(f"  {bar} {dst}  ({size} B)", end="", flush=True)
            try:
                pedal.push_file(abs_path, dst)
                print("  OK")
            except Exception as e:
                print(f"\n  FAILED: {e}")
                raise

        elapsed = time.monotonic() - t0
        print(f"\nAll {len(files)} files pushed in {elapsed:.1f}s "
              f"({total_bytes / elapsed / 1024:.1f} KB/s).")

        if args.no_reboot:
            print("Skipping REBOOT (--no-reboot)")
        else:
            print("Sending REBOOT...")
            try:
                pedal.request("REBOOT", timeout_s=2.0)
            except TimeoutError:
                # ACK may not come back - the firmware reboots almost
                # immediately. Treat absence as success.
                print("  no ACK (firmware likely already reset)")
            except Exception as e:
                print(f"  REBOOT request failed: {e}")
    finally:
        pedal.close()

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
