#!/usr/bin/env python3
"""Capture what the Kemper Player broadcasts on a RIG CHANGE while the
bidirectional beacon is active. Specifically: does it re-send effect-block
on/off states (pages 0x32-0x3D addr 0x03, 0x4A/0x4B addr 0x02) every time a
rig is loaded, or only at the initial handshake?

Usage: python tools/kemper_rigchange_probe.py
"""
import time
import mido

BEACON_INIT = [0x00, 0x20, 0x33, 0x02, 0x7F, 0x7E, 0x00, 0x40, 0x02, 0x23, 0x05]
BEACON_KA   = [0x00, 0x20, 0x33, 0x02, 0x7F, 0x7E, 0x00, 0x40, 0x02, 0x22, 0x05]

# (page, addr) -> block name, for the on/off states we care about.
BLOCK_ONOFF = {
    (0x32, 0x03): "A", (0x33, 0x03): "B", (0x34, 0x03): "C", (0x35, 0x03): "D",
    (0x38, 0x03): "X", (0x3A, 0x03): "Mod",
    (0x3C, 0x03): "Delay@3C", (0x3D, 0x03): "Reverb@3D",
    (0x4A, 0x02): "Delay@4A", (0x4B, 0x02): "Reverb@4B",
}

inp = mido.open_input(next(n for n in mido.get_input_names() if "Profiler" in n))
out = mido.open_output(next(n for n in mido.get_output_names() if "Profiler" in n))

_last_ka = [time.time()]

def keepalive():
    if time.time() - _last_ka[0] > 1.8:
        out.send(mido.Message("sysex", data=BEACON_KA))
        _last_ka[0] = time.time()

def drain(label, secs):
    print("=== %s ===" % label)
    end = time.time() + secs
    blocks_seen = []
    while time.time() < end:
        keepalive()
        for m in inp.iter_pending():
            if m.type == "sysex":
                b = list(m.bytes())          # includes F0 ... F7
                # payload (without F0/F7): b[1:-1]
                p = b[1:-1]
                if len(p) >= 11 and p[5] == 0x01:   # single param response
                    page, addr = p[7], p[8]
                    val = (p[9] << 7) | (p[10] & 0x7F)
                    tag = BLOCK_ONOFF.get((page, addr))
                    if tag:
                        blocks_seen.append("%s=%d" % (tag, val))
                        print("   BLOCK %-10s page=%02x addr=%02x val=%d" % (tag, page, addr, val))
                        continue
                if len(p) >= 9 and p[5] == 0x03:    # string response (rig name)
                    name = "".join(chr(c) for c in p[9:] if 0x20 <= c < 0x7F)
                    print("   RIGNAME '%s'" % name)
                    continue
                print("   SX " + " ".join("%02x" % x for x in b))
            elif m.type not in ("clock", "active_sensing"):
                print("   " + str(m))
        time.sleep(0.005)
    print("   -> blocks this window: %s" % (blocks_seen or "(none)"))

def select_rig(bank, rig):
    out.send(mido.Message("control_change", channel=0, control=0,  value=0))
    out.send(mido.Message("control_change", channel=0, control=32, value=bank - 1))
    time.sleep(0.01)
    out.send(mido.Message("program_change", channel=0, program=rig - 1))

out.send(mido.Message("sysex", data=BEACON_INIT))
_last_ka[0] = time.time()
drain("baseline (initial full dump expected here)", 3.0)

for (bk, rg) in [(1, 2), (1, 3), (1, 1), (1, 4), (1, 5)]:
    select_rig(bk, rg)
    drain("after select rig %d-%d" % (bk, rg), 2.0)

inp.close(); out.close()
print("done")
