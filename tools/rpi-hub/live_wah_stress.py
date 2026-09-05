#!/usr/bin/env python3
"""RPi real fixed-Wah test: independent Kemper MIDI readback + Captain context.

Changes Wah through ALSA NRPN, without querying it during each assertion.
Only the shipping Captain polling may discover those silent state changes.
Restores and independently confirms the exact initial fixed-Wah value.
"""
import argparse
import json
import re
import socket
import subprocess
import threading
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=5)
    args = parser.parse_args()
    if args.cycles < 1:
        parser.error("cycles must be positive")
    listing = subprocess.check_output(["aplaymidi", "-l"], text=True)
    ports = re.findall(r"^\s*(\d+:\d+)\s+(?:Kemper|Profiler)\b", listing, re.M | re.I)
    if len(ports) != 1:
        raise RuntimeError("expected exactly one Kemper MIDI destination: " + repr(ports))
    capture = subprocess.Popen(["stdbuf", "-oL", "aseqdump", "-p", ports[0]],
                               stdout=subprocess.PIPE, text=True)
    observed = []

    def reader():
        for line in capture.stdout:
            match = re.search(r"F0 .* F7", line)
            if match:
                data = bytes.fromhex(match[0])
                if (len(data) == 13 and data[1:4] == bytes((0, 32, 51))
                        and data[6:10] == bytes((1, 0, 5, 21))):
                    observed.append((time.monotonic(), (data[10] << 7) | data[11], match[0]))
    thread = threading.Thread(target=reader)
    thread.start()

    def midi(track):
        track += b"\x00\xff\x2f\x00"
        payload = (b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x00\x60"
                   + b"MTrk" + len(track).to_bytes(4, "big") + track)
        subprocess.run(["aplaymidi", "-d", "0", "-p", ports[0], "-"],
                       input=payload, check=True, timeout=5)

    def set_wah(value):
        midi(bytes((0, 176, 99, 5, 0, 176, 98, 21, 0, 176, 6, 0, 0, 176, 38, value)))

    def raw_query():
        start = time.monotonic()
        midi(bytes.fromhex("00 F0 0A 00 20 33 02 7F 41 00 05 15 F7"))
        while time.monotonic() - start < 3:
            found = [item for item in observed if item[0] >= start]
            if found:
                return found[-1][1]
            time.sleep(0.02)
        raise TimeoutError("independent fixed-Wah response missing")

    initial = None
    primary_error = None
    try:
        time.sleep(0.3)
        initial = raw_query()
        if initial not in (0, 1):
            raise RuntimeError("unknown initial fixed-Wah state")
        print("INITIAL RAW WAH", initial, flush=True)
        with socket.create_connection(("127.0.0.1", 9876), 5) as sock:
            sock.settimeout(3)
            stream = sock.makefile("rb")
            def context():
                ident = "wah-" + str(time.monotonic_ns())
                sock.sendall((json.dumps({"type": "GET_CONTEXT", "id": ident}) + "\n").encode())
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    line = stream.readline()
                    if not line:
                        raise RuntimeError("hub disconnected")
                    message = json.loads(line)
                    if message.get("id") == ident:
                        return message["context"]
                raise TimeoutError("Captain context missing")

            for value in (1 - initial, initial) * args.cycles:
                started = time.monotonic()
                set_wah(value)
                expected = "WAH" if value else "VOL"
                last, raw = {}, []
                while time.monotonic() - started < 2.5:
                    last = context()
                    raw = [item for item in observed if item[0] >= started and item[1] == value]
                    if last.get("expression_mode") == expected and raw:
                        break
                    time.sleep(0.1)
                assert raw and last.get("expression_mode") == expected, (expected, last, raw)
                print("PASS", expected, round((time.monotonic() - started) * 1000),
                      "ms", raw[-1][2], flush=True)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            if initial in (0, 1):
                set_wah(initial)
                actual = raw_query()
                if actual != initial:
                    raise RuntimeError("restored value differs: " + repr(actual))
                print("RESTORED WAH", initial, flush=True)
        except Exception as exc:
            raise RuntimeError("Wah restoration failed; state unknown; primary=%r; restore=%r" %
                               (primary_error, exc)) from exc
        finally:
            capture.terminate()
            thread.join(3)
            capture.wait(timeout=3)


if __name__ == "__main__":
    main()
