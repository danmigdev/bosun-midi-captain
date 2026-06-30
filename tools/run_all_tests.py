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
    "firmware_stability_test.py",   # protocol + main-loop resilience + MIDI parser fuzz
    "kemper_plugin_test.py",        # Kemper bidirectional inbound handling
    "kemper_bank_change_test.py",   # Kemper bank-select dispatch (always-send, self-heal)
    "bilateral_test.py",            # Kemper bilateral protocol + MIDI parser + plugin registry
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
