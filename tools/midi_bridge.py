#!/usr/bin/env python3
"""Bidirectional USB-MIDI bridge between the bosun pedal and the Kemper Player.

The Kemper Player is USB-MIDI only and the MIDI Captain pedal is a USB device
(not a host), so the two cannot be wired together directly. The PC has to relay
MIDI both ways. This script is that relay: it forwards every MIDI message
(including SYSEX, which carries the Kemper bidirectional protocol) between the
Player and the pedal.

Run this whenever you want the pedal and the Player to talk:
  python tools/midi_bridge.py

Without it, rig changes, effect-block on/off, tuner, tempo and the whole
bidirectional beacon never cross between the two devices.

It does NOT touch the editor's data CDC (a separate serial port), so you can
run the editor at the same time.
"""
import sys
import time

import mido


def find(names, *needles):
    for n in names:
        if all(s.lower() in n.lower() for s in needles):
            return n
    return None


def main():
    ins = mido.get_input_names()
    outs = mido.get_output_names()

    player_in  = find(ins,  "profiler")          # what the Player sends
    player_out = find(outs, "profiler")          # what we send to the Player
    pedal_in   = find(ins,  "circuitpython")     # what the pedal sends
    pedal_out  = find(outs, "circuitpython")     # what we send to the pedal

    missing = []
    if not player_in or not player_out: missing.append("Kemper Player (Profiler)")
    if not pedal_in or not pedal_out:   missing.append("bosun pedal (CircuitPython)")
    if missing:
        print("ERROR: MIDI port(s) not found:", ", ".join(missing))
        print("  inputs :", ins)
        print("  outputs:", outs)
        print("Plug both devices into USB and try again.")
        return 1

    print("Bridging (Ctrl+C to stop):")
    print("  Player  -> pedal :  %s  ->  %s" % (player_in, pedal_out))
    print("  pedal   -> Player:  %s  ->  %s" % (pedal_in, player_out))

    p_in  = mido.open_input(player_in)
    p_out = mido.open_output(player_out)
    b_in  = mido.open_input(pedal_in)
    b_out = mido.open_output(pedal_out)

    # Clock floods the link and the Kemper bidirectional protocol does not need
    # it; drop it (and MIDI active-sensing) to keep the relay quiet. Everything
    # else - notably SYSEX - is forwarded verbatim.
    DROP = ("clock", "active_sensing")

    n_pb = 0   # Player -> bosun
    n_bp = 0   # bosun  -> Player
    last_report = time.time()

    try:
        while True:
            for m in p_in.iter_pending():
                if m.type in DROP:
                    continue
                b_out.send(m)
                n_pb += 1
            for m in b_in.iter_pending():
                if m.type in DROP:
                    continue
                p_out.send(m)
                n_bp += 1
            now = time.time()
            if now - last_report >= 5.0:
                print("  [bridge] Player->pedal %d msgs, pedal->Player %d msgs" % (n_pb, n_bp))
                last_report = now
            time.sleep(0.002)
    except KeyboardInterrupt:
        print("\nbridge stopped")
    finally:
        for port in (p_in, p_out, b_in, b_out):
            try: port.close()
            except Exception: pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
