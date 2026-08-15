#!/usr/bin/env python3
"""Offline regression tests for firmware/boot.py.

boot.py has two historical failure modes, both regressions fixed in the
2026-08-12 Android connectivity work:

1. USB drive mode trap: the GP1 footswitch debounce check decided between
   performance mode (USB MSC off) and editing mode (USB MSC on).  A stuck
   or phantom GP1 reading kept the pedal permanently in drive mode and the
   editor could never see the data CDC.  The fix forces performance mode
   unconditionally - storage.disable_usb_drive() must be a top-level call,
   NOT nested inside an if/else.

2. Console CDC disabled (usb_cdc.enable(console=False, ...)): print()
   calls then write into an internal buffer that nothing drains; once it
   fills, print() blocks and boot slows to a crawl.  The console must stay
   ENABLED (console=True) so print() output has somewhere to go.  The
   Android editor avoids the console port by sorting port indices.

These tests parse boot.py with ast and assert the contracts above so
neither regression can silently return.

Usage
-----
    python tools/boot_py_test.py
"""

import ast
import sys
from pathlib import Path

BOOT_PY = Path(__file__).resolve().parent.parent / "firmware" / "boot.py"

_FAILURES = []


def _fail(msg):
    _FAILURES.append(msg)
    print("FAIL: " + msg)


def _top_level_calls(tree):
    """Return the list of ast.Call nodes at module top level (not nested
    in functions or conditionals)."""
    return [n.value for n in tree.body if isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Call)]


def main():
    src = BOOT_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)

    # ---- 1) performance mode is forced unconditionally ----
    top_calls = _top_level_calls(tree)
    names = set()
    for call in top_calls:
        if isinstance(call.func, ast.Attribute):
            names.add(call.func.attr)

    if "disable_usb_drive" not in names:
        _fail("storage.disable_usb_drive() is not a top-level call; "
              "the GP1 check can trap the pedal in drive mode again")

    # ---- 2) console CDC stays enabled ----
    cdc_calls = [
        c for c in ast.walk(tree)
        if isinstance(c, ast.Call)
        and isinstance(c.func, ast.Attribute)
        and isinstance(c.func.value, ast.Name)
        and c.func.value.id == "usb_cdc"
        and c.func.attr == "enable"
    ]
    if not cdc_calls:
        _fail("usb_cdc.enable(...) call not found in boot.py")

    for call in cdc_calls:
        # console= must be the first keyword
        if (call.keywords
                and call.keywords[0].arg == "console"
                and call.keywords[0].value is not None):
            console_val = ast.literal_eval(call.keywords[0].value)
            if console_val is False:
                _fail("usb_cdc.enable(console=False) found; print() will "
                      "block once the console buffer fills and boot slows")
        else:
            _fail("usb_cdc.enable(...) lacks an explicit console= keyword")
        # data= must be True (the protocol link)
        for kw in call.keywords:
            if kw.arg == "data" and kw.value is not None:
                if ast.literal_eval(kw.value) is not True:
                    _fail("usb_cdc.enable(...) has data != True; "
                          "the data CDC is the Bosun protocol link")

    if _FAILURES:
        print("\n%d boot.py contract violation(s)" % len(_FAILURES))
        sys.exit(1)
    print("boot.py OK: performance mode forced, console CDC enabled")


if __name__ == "__main__":
    main()
