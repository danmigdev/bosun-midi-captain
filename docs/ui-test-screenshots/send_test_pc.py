#!/usr/bin/env python3
"""Inject a Bank Select MSB + Program Change into the pedal's USB-MIDI IN, to
exercise the MIDI Learn capture pipeline without touching the Kemper.

Sends to the PC->pedal output port ("CircuitPython Audio ..."). Run with the
in-editor bridge OFF (it holds that port exclusively on Windows)."""
import sys, time
import mido

def find(names, needle):
    for n in names:
        if needle.lower() in n.lower():
            return n
    return None

def main():
    ch = int(sys.argv[1]) if len(sys.argv) > 1 else 1   # 1-based
    pc = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    bank = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    out_name = find(mido.get_output_names(), "circuitpython")
    if not out_name:
        print("ERROR: pedal MIDI output not found:", mido.get_output_names())
        return 1
    print("sending to", out_name, "ch", ch, "bankMSB", bank, "PC", pc)
    with mido.open_output(out_name) as out:
        out.send(mido.Message("control_change", channel=ch-1, control=0, value=bank))
        time.sleep(0.05)
        out.send(mido.Message("program_change", channel=ch-1, program=pc))
        time.sleep(0.1)
    print("sent")
    return 0

if __name__ == "__main__":
    sys.exit(main())
