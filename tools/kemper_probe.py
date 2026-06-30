#!/usr/bin/env python3
"""Drive a Kemper Player over USB-MIDI and capture its responses, labelled
by the action, to reverse-engineer the real broadcast addresses for effect
blocks and tuner. Temporary diagnostic.

Usage: python tools/kemper_probe.py
"""
import time
import mido

BEACON_INIT = [0x00, 0x20, 0x33, 0x02, 0x7F, 0x7E, 0x00, 0x40, 0x02, 0x23, 0x05]
BEACON_KA   = [0x00, 0x20, 0x33, 0x02, 0x7F, 0x7E, 0x00, 0x40, 0x02, 0x22, 0x05]

inp = mido.open_input(next(n for n in mido.get_input_names() if "Profiler" in n))
out = mido.open_output(next(n for n in mido.get_output_names() if "Profiler" in n))

_last_ka = [time.time()]

def keepalive():
    if time.time() - _last_ka[0] > 1.8:
        out.send(mido.Message("sysex", data=BEACON_KA))
        _last_ka[0] = time.time()

def drain(label, secs):
    print("=== " + label + " ===")
    end = time.time() + secs
    while time.time() < end:
        keepalive()
        for m in inp.iter_pending():
            if m.type == "sysex":
                b = list(m.bytes())
                print("SX " + " ".join("%02x" % x for x in b))
            elif m.type not in ("clock", "active_sensing"):
                print("   " + str(m))
        time.sleep(0.01)

def cc(control, value):
    out.send(mido.Message("control_change", channel=0, control=control, value=value))

out.send(mido.Message("sysex", data=BEACON_INIT))
_last_ka[0] = time.time()
drain("baseline", 2.5)
# Map every block's on/off: send the CC, capture the frame that flips.
for name, c in [("A", 17), ("B", 18), ("C", 19), ("D", 20),
                ("X", 22), ("Mod", 24), ("Delay", 27), ("Reverb", 29)]:
    cc(c, 127); drain("%s ON  (CC %d=127)" % (name, c), 1.0)
    cc(c, 0);   drain("%s OFF (CC %d=0)" % (name, c), 1.0)
cc(31, 127); drain("Tuner ON  (CC 31=127)", 2.0)
cc(31, 0);   drain("Tuner OFF (CC 31=0)", 2.0)
inp.close(); out.close()
print("done")
