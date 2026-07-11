#!/usr/bin/env python3
"""Offline tests for the preset-preview navigation math.

Runs the pure helpers from firmware/lib/captain/navigation.py - no hardware,
no CircuitPython. Covers patch-scope and bank-scope stepping, wrap-around,
slot fallback when a target bank lacks the current slot, and snapping when the
start cursor isn't itself a real patch.

Usage
-----
    python tools/nav_preview_test.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "firmware" / "lib"))

from captain import navigation  # noqa: E402


_failures = []


def check(name, got, want):
    if got == want:
        print("ok   -", name)
    else:
        print("FAIL -", name, "-> got", got, "want", want)
        _failures.append(name)


def patches(*pairs):
    return [{"bank": b, "slot": s} for (b, s) in pairs]


def main():
    # --- patch_order ---
    check("patch_order sorts",
          navigation.patch_order(patches((2, 1), (1, 3), (1, 1))),
          [(1, 1), (1, 3), (2, 1)])
    check("patch_order empty", navigation.patch_order([]), [])

    order = navigation.patch_order(patches((1, 1), (1, 2), (2, 1), (2, 5), (3, 3)))

    # --- patch scope ---
    check("patch +1", navigation.step_index(order, (1, 1), 1, "patch"), (1, 2))
    check("patch +1 across bank", navigation.step_index(order, (1, 2), 1, "patch"), (2, 1))
    check("patch -1", navigation.step_index(order, (2, 1), -1, "patch"), (1, 2))
    check("patch wrap forward", navigation.step_index(order, (3, 3), 1, "patch"), (1, 1))
    check("patch wrap backward", navigation.step_index(order, (1, 1), -1, "patch"), (3, 3))
    check("patch +2", navigation.step_index(order, (1, 1), 2, "patch"), (2, 1))
    check("patch delta 0 no move", navigation.step_index(order, (2, 1), 0, "patch"), (2, 1))

    # start not present -> snaps to first >= start, then steps
    check("patch snap missing start", navigation.step_index(order, (1, 4), 1, "patch"), (2, 5))

    # --- bank scope ---
    check("bank +1 keeps slot", navigation.step_index(order, (1, 1), 1, "bank"), (2, 1))
    check("bank +1 slot fallback lower",
          navigation.step_index(order, (1, 2), 1, "bank"), (2, 1))   # bank 2 has no slot 2 -> nearest lower = 1
    check("bank +2 first slot",
          navigation.step_index(order, (1, 2), 2, "bank"), (3, 3))   # bank 3 only has slot 3
    # bank 1 slots are {1,2}; wrap from bank 3, target slot 3 -> nearest lower = 2
    check("bank wrap slot fallback",
          navigation.step_index(order, (3, 3), 1, "bank"), (1, 2))
    check("bank -1", navigation.step_index(order, (2, 5), -1, "bank"), (1, 2))  # bank1 no slot5 -> lower=2

    # --- degenerate ---
    check("empty order", navigation.step_index([], (1, 1), 1, "patch"), (1, 1))
    single = navigation.patch_order(patches((4, 2)))
    check("single patch scope", navigation.step_index(single, (4, 2), 1, "patch"), (4, 2))
    check("single bank scope", navigation.step_index(single, (4, 2), 1, "bank"), (4, 2))

    print()
    if _failures:
        print("{} FAILURE(S): {}".format(len(_failures), ", ".join(_failures)))
        return 1
    print("all navigation tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
