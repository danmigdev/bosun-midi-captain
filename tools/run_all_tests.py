#!/usr/bin/env python3
"""Run the full offline firmware test battery (no hardware, no CircuitPython).

Each suite is a standalone script that exits non-zero on failure; this
runner invokes them all and aggregates the result. Editor (TypeScript)
tests run separately via `npm test` in editor/.

Usage:
    python tools/run_all_tests.py
"""

import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent

SUITES = [
    "fsm_test.py",                  # switch FSM (debounce / long-press / double-tap)
    "nav_preview_test.py",          # preset-preview navigation cursor math
    "preview_tuner_test.py",        # app-level preset preview + tuner exit-on-press
    "setlist_test.py",              # app-level setlist navigation (device-aware setlist)
    "store_test.py",                # bounded PatchStore metadata/existence reads
    "midi_parser_test.py",          # MIDI stream parser (running status, realtime, SYSEX)
    "midi_tx_test.py",              # MIDI outbound TX retry loop + switch poll_hook interleave
    "protocol_test.py",             # USB CDC protocol handlers + partial-write streaming
    "build_manifest_tail_test.py",  # deterministic static MANIFEST tail + drift guard
    "build_firmware_mpy_test.py",   # pinned CircuitPython compiler + reproducible .mpy build
    "sync_firmware_resources_test.py",  # canonical, cache-free Tauri resource mirrors
    "provision_adafruit_bundle_test.py",  # pinned CP9 vendor bundle + exact 9-file inventory
    "verify_firmware_package_test.py",  # Android staging/APK inventory + SHA-256
    "push_firmware_test.py",        # deploy transport selection (COM + pyserial URL)
    "verify_captain_runtime_test.py",  # bounded post-reboot hub readiness
    "write_via_repl_test.py",       # raw-REPL recovery readback + host-side SHA-256
    "remove_via_repl_test.py",      # guarded inspect-first Captain staging cleanup
    "stress_test.py",                # real-hardware smoke timeouts + response contract
    "expression_test.py",           # expression-pedal calibration / curves / deadband
    "display_test.py",              # retained TFT rows under allocation pressure + pedal badge
    "plugins_test.py",              # plugin dispatch + cross-plugin manifest consistency
    "kemper_plugin_test.py",        # Kemper bidirectional inbound handling
    "kemper_bank_change_test.py",   # Kemper bank-select dispatch (always-send, self-heal)
    "bilateral_test.py",            # Kemper bilateral protocol + MIDI parser + plugin registry
    "firmware_stability_test.py",   # protocol + main-loop resilience + MIDI parser fuzz
    "soak_test.py",                 # hours-of-use endurance: no leaks, no crashes
]


def main():
    failed = []
    for suite in SUITES:
        print("=" * 64)
        print("RUN  " + suite)
        print("=" * 64)
        result = subprocess.run([sys.executable, str(TOOLS / suite)])
        if result.returncode != 0:
            failed.append(suite)
        print("")

    print("=" * 64)
    if failed:
        print("BATTERY FAILED: " + ", ".join(failed))
        sys.exit(1)
    print("BATTERY PASSED (%d suites)" % len(SUITES))


if __name__ == "__main__":
    main()
