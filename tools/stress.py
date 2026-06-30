#!/usr/bin/env python3
"""Stress / smoke runner for the MIDI Captain firmware.

Talks to the firmware over the secondary USB CDC port (where the JSON protocol
lives - NOT the REPL console). Pick that COM port with --port.

Modes
-----
smoke     Sanity-pass every protocol command. Prints what the firmware reports.
hammer    PUT_BINDING in a tight loop. Reports min/p50/p95/max ACK latency.
spam      Fire-and-forget: send N PINGs without waiting; drain replies after.
          Tests the firmware's CDC buffer + main-loop catch-up behavior.
soak      PING + STATS every N seconds for a duration. Logs CSV to stdout.
          Cheap way to spot memory leaks or runaway counters.
midi-rt   MIDI round-trip latency. Requires DIN-out wired to DIN-in (loopback).
          Starts a learn capture, sends a PUT_BINDING that fires a CC, measures
          time from press to the matching midi_in_captured event.

Examples
--------
    python tools/stress.py --port COM7 smoke
    python tools/stress.py --port COM7 hammer --count 500
    python tools/stress.py --port COM7 spam --count 1000
    python tools/stress.py --port COM7 soak --duration 3600 --interval 30 > soak.csv
"""

import argparse
import json
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("pyserial missing: pip install pyserial")


class CaptainClient:
    def __init__(self, port: str, baud: int = 115200):
        self.ser = serial.Serial(port, baud, timeout=0.05)
        self.events: list[dict] = []
        self.responses: dict[str, dict] = {}
        self._next_id = 1
        self._rx_buf = bytearray()

    def _send(self, obj: dict) -> None:
        self.ser.write((json.dumps(obj) + "\n").encode())

    def call(self, type_: str, **kwargs) -> str:
        mid = str(self._next_id); self._next_id += 1
        self._send({"type": type_, "id": mid, **kwargs})
        return mid

    def drain(self, ms: float = 50) -> None:
        deadline = time.monotonic() + ms / 1000
        while time.monotonic() < deadline:
            chunk = self.ser.read(256)
            if not chunk:
                continue
            self._rx_buf.extend(chunk)
            while b"\n" in self._rx_buf:
                line, _, rest = self._rx_buf.partition(b"\n")
                self._rx_buf = bytearray(rest)
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "EVENT":
                    self.events.append(obj)
                elif "id" in obj:
                    self.responses[obj["id"]] = obj

    def call_sync(self, type_: str, timeout: float = 2.0, **kwargs) -> dict:
        mid = self.call(type_, **kwargs)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.drain(20)
            if mid in self.responses:
                return self.responses.pop(mid)
        raise TimeoutError(f"no response to {type_}#{mid} after {timeout}s")

    def close(self):
        self.ser.close()


# ---------------- smoke ----------------

def smoke(c: CaptainClient) -> None:
    print("# SMOKE")
    print("PING ->", c.call_sync("PING", timeout=1))
    info = c.call_sync("GET_DEVICE_INFO", timeout=1)
    print("DEVICE_INFO ->", info)
    manifest = c.call_sync("GET_MANIFEST", timeout=3)
    n_core = len(manifest.get("core_messages", {}))
    plugins = manifest.get("plugins", {})
    print(f"MANIFEST: {n_core} core message types, {len(plugins)} plugins")
    for name, plug in plugins.items():
        print(f"  plugin {name} v{plug['version']}: {len(plug['messages'])} message types")
    patches = c.call_sync("LIST_PATCHES", timeout=3)["patches"]
    print(f"LIST_PATCHES: {len(patches)} patches")
    dirty = c.call_sync("GET_DIRTY", timeout=1)["patches"]
    print(f"GET_DIRTY: {len(dirty)} dirty")
    learn = c.call_sync("GET_MIDI_LEARN", timeout=1).get("table", {})
    print(f"GET_MIDI_LEARN: {len(learn.get('pc_to_patch', []))} entries")
    stats = c.call_sync("STATS", timeout=1)
    print(f"STATS: uptime={stats['uptime_ms']/1000:.0f}s mem_free={stats['mem_free']} loop_iters={stats['loop_iters']}")


# ---------------- hammer ----------------

def hammer(c: CaptainClient, count: int, interval: float) -> None:
    info = c.call_sync("GET_DEVICE_INFO")["current"]
    bank, slot = info["bank"], info["slot"]
    patch = c.call_sync("GET_PATCH", bank=bank, slot=slot)["patch"]
    if not patch["bindings"]:
        sys.exit("active patch has no bindings to hammer")
    binding = patch["bindings"][0]

    print(f"# HAMMER {count} PUT_BINDING on {bank:02d}/{slot:02d} sw={binding['switch']} interval={interval*1000:.0f}ms")
    latencies = []
    errors = 0
    t_start = time.monotonic()
    for i in range(count):
        binding["label"] = f"stress {i}"
        t0 = time.monotonic()
        try:
            resp = c.call_sync("PUT_BINDING", bank=bank, slot=slot, binding=binding, timeout=2)
            latencies.append((time.monotonic() - t0) * 1000)
            if resp.get("type") != "ACK":
                errors += 1
        except TimeoutError:
            errors += 1
            latencies.append(2000)
        if interval > 0:
            time.sleep(interval)
    wall = time.monotonic() - t_start

    latencies.sort()
    def pct(p):
        idx = max(0, min(len(latencies) - 1, int(len(latencies) * p)))
        return latencies[idx]
    print(f"  wall: {wall:.2f}s   throughput: {count/wall:.0f} req/s")
    print(f"  latency ms - min/p50/p95/p99/max: "
          f"{latencies[0]:.1f}/{pct(0.50):.1f}/{pct(0.95):.1f}/{pct(0.99):.1f}/{latencies[-1]:.1f}")
    print(f"  errors: {errors}")


# ---------------- spam ----------------

def spam(c: CaptainClient, count: int) -> None:
    print(f"# SPAM {count} PINGs fire-and-forget")
    ids = []
    t_start = time.monotonic()
    for _ in range(count):
        ids.append(c.call("PING"))
    t_sent = time.monotonic()
    # Drain responses
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and len(c.responses) < count:
        c.drain(100)
    t_drained = time.monotonic()
    print(f"  send: {(t_sent - t_start)*1000:.0f}ms ({count/(t_sent-t_start):.0f}/s)")
    print(f"  drain: {(t_drained - t_sent)*1000:.0f}ms")
    print(f"  received: {len(c.responses)}/{count} ACKs")
    missing = sum(1 for i in ids if i not in c.responses)
    if missing:
        print(f"  MISSING: {missing} responses - firmware dropped or buffer overflow")


# ---------------- soak ----------------

def soak(c: CaptainClient, duration_s: float, interval_s: float) -> None:
    print("uptime_s,mem_free,mem_alloc,loop_iters,iters_per_s,midi_rx,midi_tx,cmd_count")
    last_iters = 0
    last_uptime = 0
    t_start = time.monotonic()
    while time.monotonic() - t_start < duration_s:
        try:
            s = c.call_sync("STATS", timeout=2)
            d_iters = s["loop_iters"] - last_iters
            d_uptime = (s["uptime_ms"] - last_uptime) / 1000
            iters_per_s = d_iters / d_uptime if d_uptime > 0 else 0
            print(f"{s['uptime_ms']/1000:.0f},{s['mem_free']},{s['mem_alloc']},"
                  f"{s['loop_iters']},{iters_per_s:.0f},{s['midi_rx_count']},"
                  f"{s.get('midi_tx_count',0)},{s['protocol_cmd_count']}")
            sys.stdout.flush()
            last_iters = s["loop_iters"]
            last_uptime = s["uptime_ms"]
        except TimeoutError:
            print(f"{(time.monotonic()-t_start):.0f},timeout", flush=True)
        time.sleep(interval_s)


# ---------------- midi round-trip (DIN loopback) ----------------

def midi_round_trip(c: CaptainClient, presses: int = 20) -> None:
    """Requires DIN-OUT wired into DIN-IN (TRS loopback cable).

    Strategy: start learn mode so inbound MIDI gets forwarded as events. We
    can't physically press a switch from the host, so instead we configure
    a binding that fires a unique CC, then change the patch via SWITCH_PATCH
    which fires its on_enter macro. The captain TX goes out on DIN, loops
    back, gets parsed, and emits a midi_in_captured event."""

    print("# MIDI ROUND TRIP (DIN loopback required)")
    c.call_sync("START_MIDI_LEARN", timeout=1)
    info = c.call_sync("GET_DEVICE_INFO")["current"]
    bank, slot = info["bank"], info["slot"]
    latencies = []
    for i in range(presses):
        # Distinctive PC each iteration so we can disambiguate echoes
        pc = i % 128
        c.events.clear()
        t0 = time.monotonic()
        c.call_sync("PUT_PATCH", bank=bank, slot=slot, timeout=1, patch={
            "name": f"rt {i}",
            "on_enter": {"messages": [{"type": "pc", "channel": 16, "program": pc}]},
            "bindings": [],
        })
        c.call_sync("SWITCH_PATCH", bank=bank, slot=slot, timeout=1)
        # Wait for the captured PC event
        deadline = time.monotonic() + 0.5
        captured = False
        while time.monotonic() < deadline:
            c.drain(10)
            for ev in c.events:
                if (ev.get("event") == "midi_in_captured"
                        and ev.get("kind") == "pc"
                        and (ev.get("data") or [-1])[0] == pc):
                    latencies.append((time.monotonic() - t0) * 1000)
                    captured = True
                    break
            if captured:
                break
        if not captured:
            print(f"  iter {i}: no loopback echo (cable wired?)")
    c.call_sync("STOP_MIDI_LEARN", timeout=1)

    if not latencies:
        print("  no round trips completed.")
        return
    latencies.sort()
    print(f"  round trips: {len(latencies)}/{presses}")
    print(f"  latency ms - min/p50/p95/max: "
          f"{latencies[0]:.1f}/{latencies[len(latencies)//2]:.1f}/"
          f"{latencies[int(len(latencies)*0.95)]:.1f}/{latencies[-1]:.1f}")


# ---------------- entry ----------------

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", required=True, help="COM/tty path for the Captain data CDC")
    p.add_argument("--baud", type=int, default=115200)
    sub = p.add_subparsers(dest="mode", required=True)
    sub.add_parser("smoke")
    sh = sub.add_parser("hammer")
    sh.add_argument("--count", type=int, default=200)
    sh.add_argument("--interval", type=float, default=0.0, help="seconds between sends")
    ss = sub.add_parser("spam")
    ss.add_argument("--count", type=int, default=500)
    so = sub.add_parser("soak")
    so.add_argument("--duration", type=float, default=600)
    so.add_argument("--interval", type=float, default=5)
    sm = sub.add_parser("midi-rt")
    sm.add_argument("--presses", type=int, default=20)
    args = p.parse_args()

    client = CaptainClient(args.port, args.baud)
    time.sleep(0.5)  # let port settle

    try:
        if args.mode == "smoke":
            smoke(client)
        elif args.mode == "hammer":
            hammer(client, args.count, args.interval)
        elif args.mode == "spam":
            spam(client, args.count)
        elif args.mode == "soak":
            soak(client, args.duration, args.interval)
        elif args.mode == "midi-rt":
            midi_round_trip(client, args.presses)
    finally:
        client.close()


if __name__ == "__main__":
    main()
