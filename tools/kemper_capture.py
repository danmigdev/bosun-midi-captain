#!/usr/bin/env python3
"""Diagnostic: talk to a Kemper Player over USB-MIDI directly from the PC.

Sends the bidirectional beacon and captures everything the Player broadcasts
(sensing, effect-block on/off, tuner, rig name) as raw bytes, so the real
protocol can be compared against the firmware's parser. Temporary tool.

Usage:
    python tools/kemper_capture.py [seconds]
"""
import sys
import time

import mido

# Beacon / keep-alive payloads (SysEx data WITHOUT F0/F7), matching the
# firmware: 00 20 33 | product=02 dev=7F | fn=7E | inst=00 | page=40 set=02 |
# flags | lease/2=05.  flags 0x23 = init+sysex+tunemode, 0x22 = keepalive.
BEACON_INIT = [0x00, 0x20, 0x33, 0x02, 0x7F, 0x7E, 0x00, 0x40, 0x02, 0x23, 0x05]
BEACON_KA   = [0x00, 0x20, 0x33, 0x02, 0x7F, 0x7E, 0x00, 0x40, 0x02, 0x22, 0x05]

DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 20

ins = [n for n in mido.get_input_names() if "Profiler" in n]
outs = [n for n in mido.get_output_names() if "Profiler" in n]
print("inputs :", ins)
print("outputs:", outs)
if not ins or not outs:
    print("No Profiler ports found - is the Player connected via USB?")
    sys.exit(1)

def _try_open(opener, names, kind):
    out = []
    for n in names:
        try:
            out.append((n, opener(n)))
        except Exception as e:                                   # noqa: BLE001
            print("  (skip %s %r: %s)" % (kind, n, e))
    return out

inports = [p for _n, p in _try_open(mido.open_input, ins, "input")]
outports = _try_open(mido.open_output, outs, "output")
print("opened %d input(s), %d output(s)" % (len(inports), len(outports)))
if not inports or not outports:
    print("Could not open a Profiler input+output pair (ports busy?). "
          "Close any MIDI routing app holding them and retry.")
    sys.exit(1)


def send(data, label):
    for n, op in outports:
        try:
            op.send(mido.Message("sysex", data=data))
        except Exception as e:                                   # noqa: BLE001
            print("send err on %s: %s" % (n, e))
    print(">>> sent %s on %d port(s)" % (label, len(outports)))


send(BEACON_INIT, "BEACON_INIT")
last_ka = time.time()
deadline = time.time() + DURATION
sysex_count = 0
other_count = 0
print("capturing %ds - toggle effects / tuner on the Player now...\n" % DURATION)

while time.time() < deadline:
    if time.time() - last_ka > 2.0:
        send(BEACON_KA, "keepalive")
        last_ka = time.time()
    for ip in inports:
        for msg in ip.iter_pending():
            if msg.type == "sysex":
                b = list(msg.bytes())  # includes F0 ... F7
                sysex_count += 1
                print("SX " + " ".join("%02x" % x for x in b))
            elif msg.type in ("clock", "active_sensing"):
                continue
            else:
                other_count += 1
                print("   " + str(msg))
    time.sleep(0.02)

print("\n--- done: %d sysex, %d other messages ---" % (sysex_count, other_count))
for ip in inports:
    ip.close()
for _n, op in outports:
    op.close()
