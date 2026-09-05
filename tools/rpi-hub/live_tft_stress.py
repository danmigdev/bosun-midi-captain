#!/usr/bin/env python3
"""RPi hardware regression: rig changes must not exhaust the Captain TFT heap.

Runs on the RPi with the production hub active. Captures the console passively
(no Ctrl-C/reset), checks correlated rig context, and restores the initial rig.
The final hardware screen still needs visual confirmation: a console and a
CONTEXT reply do not measure emitted TFT pixels.
"""
import argparse
import json
import socket
import sys
import threading
import time

import serial

CONSOLE_ERROR_TOKENS = (
    "memory", "display:", "display refresh", "loop error", "traceback", "_send exc")


class StressFailure(RuntimeError):
    def __init__(self, primary, restoration):
        self.primary = primary
        self.restoration = restoration
        super().__init__("test: %s: %s; restoration: %s: %s" % (
            type(primary).__name__, primary, type(restoration).__name__, restoration))


def request_on(sock, stream, kind, **fields):
    ident = "tft-" + str(time.monotonic_ns())
    started = time.monotonic()
    deadline = started + 5
    print("TRACE SEND", started, kind, ident, fields, flush=True)
    try:
        sock.settimeout(5)
        sock.sendall((json.dumps(dict(type=kind, id=ident, **fields)) + "\n").encode())
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(kind + " " + ident)
            sock.settimeout(remaining)
            line = stream.readline()
            if not line:
                raise RuntimeError("hub disconnected during " + kind)
            reply = json.loads(line)
            if isinstance(reply, dict) and reply.get("id") == ident:
                now = time.monotonic()
                print("TRACE RECV", now, kind, ident, reply.get("type"),
                      "elapsed_ms=%.3f" % ((now - started) * 1000), flush=True)
                if reply.get("type") == "ERROR":
                    raise RuntimeError(str(reply))
                return reply
    except BaseException as exc:
        print("TRACE FAIL", time.monotonic(), kind, ident,
              type(exc).__name__, str(exc), flush=True)
        raise


def restore_rig(bank, slot, primary=None):
    """A timed-out socket.makefile cannot resume reads; restore independently."""
    try:
        with socket.create_connection(("127.0.0.1", 9876), 5) as sock:
            with sock.makefile("rb") as stream:
                assert request_on(sock, stream, "SWITCH_PATCH", bank=bank, slot=slot)["type"] == "ACK"
                time.sleep(1.8)
                restored = request_on(sock, stream, "GET_CONTEXT")["context"]
                assert (restored["bank"], restored["slot"]) == (bank, slot), restored
                print("RESTORED", "B%d R%d" % (bank, slot), flush=True)
    except BaseException as exc:
        if primary is not None:
            raise StressFailure(primary, exc) from primary
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=25)
    parser.add_argument("--console", default="/dev/ttyACM0")
    args = parser.parse_args()
    if args.cycles < 1:
        parser.error("cycles must be positive")
    stop = threading.Event()
    console_lines = []
    reader_errors = []
    with serial.Serial(args.console, 115200, timeout=0.2) as console:
        def monitor():
            pending = b""
            try:
                while not stop.is_set():
                    pending += console.read(2048)
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        console_lines.append(line.decode("utf8", "replace"))
            except Exception as exc:
                reader_errors.append(str(exc))
            finally:
                if pending:
                    console_lines.append(pending.decode("utf8", "replace"))
        thread = threading.Thread(target=monitor)
        thread.start()
        try:
            with socket.create_connection(("127.0.0.1", 9876), 5) as sock:
                sock.settimeout(5)
                stream = sock.makefile("rb")
                def request(kind, **fields):
                    return request_on(sock, stream, kind, **fields)

                initial = request("GET_CONTEXT")["context"]
                bank, slot = initial["bank"], initial["slot"]
                baseline = request("STATS")
                changed = False
                try:
                    for index in range(args.cycles):
                        target = (3, 2, 1, 4, 5)[index % 5]
                        changed = True
                        assert request("SWITCH_PATCH", bank=1, slot=target)["type"] == "ACK"
                        time.sleep(1.8)
                        ctx = request("GET_CONTEXT")["context"]
                        assert (ctx["bank"], ctx["slot"], ctx["kemper_rig_in_bank"]) == (1, target, target), ctx
                        assert ctx.get("expression_mode") in ("VOL", "WAH"), ctx
                        print("PASS", index + 1, ctx["patch_name"], "B1", "R" + str(target), ctx["expression_mode"], flush=True)
                finally:
                    if changed:
                        restore_rig(bank, slot, primary=sys.exc_info()[1])
                stats = request("STATS")
                assert stats["uptime_ms"] >= baseline["uptime_ms"], "Captain rebooted"
                assert stats["usb_tx_dropped"] == baseline["usb_tx_dropped"], "MIDI TX drops increased"
                print("STATS", json.dumps(stats), flush=True)
        finally:
            stop.set()
            thread.join(2)
            if (sys.exc_info()[1] is not None or reader_errors
                    or any(token in line.lower() for line in console_lines
                           for token in CONSOLE_ERROR_TOKENS)):
                print("CONSOLE CAPTURE (%d lines)" % len(console_lines), flush=True)
                for line in console_lines:
                    print("CONSOLE", line, flush=True)
                for error in reader_errors:
                    print("CONSOLE READER ERROR", error, flush=True)
    errors = [line for line in console_lines if any(
        token in line.lower() for token in CONSOLE_ERROR_TOKENS)]
    assert not reader_errors, reader_errors
    assert not errors, "Captain console errors:\n" + "\n".join(errors[:30])
    print("PASS console: no display/memory/send errors; original rig restored", flush=True)


if __name__ == "__main__":
    main()
