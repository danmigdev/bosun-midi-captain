#!/usr/bin/env python3
"""Offline unit tests for captain.expression - no hardware, no CircuitPython.

Mocks `board` and `analogio` so the real ExpressionArray / _Jack can be
imported and exercised on a host. Covers calibration scaling, clamping,
inversion, taper curves, the change deadband, the poll throttle, live
reconfiguration (pin release), inert/disabled jacks, and read-error
robustness (a jack that raises on read must never take the loop down).

Usage:
    python tools/expression_test.py
"""

import sys
import types
from pathlib import Path


# ---------------- mock the CircuitPython surface expression.py needs ----------------

def _mod(name):
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m


board = _mod("board")
for _i in range(30):
    setattr(board, "GP%d" % _i, "GP%d" % _i)

analogio = _mod("analogio")


class FakeAnalogIn:
    """Records constructed pins so a test can drive .value by pin, and tracks
    deinit() so we can prove ExpressionArray.configure() releases pins."""
    registry = {}

    def __init__(self, pin):
        self.pin = pin
        self._value = 0
        self.deinited = False
        FakeAnalogIn.registry[pin] = self

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, v):
        self._value = v

    def deinit(self):
        self.deinited = True


class RaisingAnalogIn(FakeAnalogIn):
    @property
    def value(self):
        raise RuntimeError("ADC read blew up")


analogio.AnalogIn = FakeAnalogIn


# ---------------- import the real module under test ----------------

_LIB = Path(__file__).resolve().parent.parent / "firmware" / "lib"
sys.path.insert(0, str(_LIB))

from captain.expression import ExpressionArray, _Jack, _POLL_INTERVAL_MS  # noqa: E402


# ---------------- tiny harness ----------------

PASS = 0
FAIL = 0
FAILURES = []


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  ok   %s" % name)
    else:
        FAIL += 1
        FAILURES.append(name + ((" - " + detail) if detail else ""))
        print("  FAIL %s %s" % (name, detail))


def _reset_registry():
    FakeAnalogIn.registry = {}
    analogio.AnalogIn = FakeAnalogIn


def _cfg(jack=1, enabled=True, invert=False, cal_min=1000, cal_max=5000,
         curve="linear", cc=11):
    return {"jack": jack, "enabled": enabled, "invert": invert,
            "calibration": {"min": cal_min, "max": cal_max}, "curve": curve,
            "message": {"type": "cc", "channel": 1, "cc": cc, "value": 0}}


def _first_value(jack, raw):
    """Set a jack's raw and take the FIRST sample (EMA seeds to raw, so the
    first read is exact - deterministic for calibration assertions)."""
    FakeAnalogIn.registry[jack._adc.pin]._value = raw
    return jack.sample()


# ---------------- calibration + curves ----------------

def test_linear_calibration():
    _reset_registry()
    j = _Jack(_cfg(cal_min=1000, cal_max=5000))
    check("linear: raw at min -> 0", _first_value(j, 1000) == 0)
    j2 = _Jack(_cfg(cal_min=1000, cal_max=5000))
    check("linear: raw at max -> 127", _first_value(j2, 5000) == 127)
    j3 = _Jack(_cfg(cal_min=1000, cal_max=5000))
    mid = _first_value(j3, 3000)
    check("linear: raw at midpoint -> ~63/64", mid in (63, 64), "got %r" % mid)


def test_clamping():
    _reset_registry()
    j = _Jack(_cfg(cal_min=1000, cal_max=5000))
    check("clamp: below min -> 0", _first_value(j, 0) == 0)
    j2 = _Jack(_cfg(cal_min=1000, cal_max=5000))
    check("clamp: above max -> 127", _first_value(j2, 60000) == 127)


def test_invert():
    _reset_registry()
    j = _Jack(_cfg(invert=True, cal_min=1000, cal_max=5000))
    check("invert: raw at min -> 127", _first_value(j, 1000) == 127)
    j2 = _Jack(_cfg(invert=True, cal_min=1000, cal_max=5000))
    check("invert: raw at max -> 0", _first_value(j2, 5000) == 0)


def test_curves():
    _reset_registry()
    # frac 0.5: exp -> 0.25 (~32), log -> ~0.707 (~90), linear -> ~64.
    lin = _first_value(_Jack(_cfg(curve="linear", cal_min=0, cal_max=1000)), 500)
    exp = _first_value(_Jack(_cfg(curve="exp", cal_min=0, cal_max=1000)), 500)
    log = _first_value(_Jack(_cfg(curve="log", cal_min=0, cal_max=1000)), 500)
    check("curve linear at 0.5 -> ~64", 63 <= lin <= 64, "got %r" % lin)
    check("curve exp at 0.5 -> ~32 (finer at heel)", 30 <= exp <= 34, "got %r" % exp)
    check("curve log at 0.5 -> ~90 (finer at toe)", 88 <= log <= 92, "got %r" % log)


def test_degenerate_calibration():
    _reset_registry()
    # max <= min must not divide by zero; treated as a 1-count span.
    j = _Jack(_cfg(cal_min=5000, cal_max=5000))
    v = _first_value(j, 5000)
    check("degenerate cal (min==max) does not raise", isinstance(v, int))


# ---------------- deadband + throttle via ExpressionArray.poll ----------------

def test_arms_then_emits_on_change():
    _reset_registry()
    arr = ExpressionArray([_cfg(jack=1, cal_min=0, cal_max=65535, cc=11)])
    adc = FakeAnalogIn.registry["GP27"]
    adc._value = 0
    # A jack that hasn't moved is disarmed and stays silent (no power-on blast).
    e0 = arr.poll(0)
    check("unmoved jack emits nothing (disarmed)", e0 == [], "got %r" % e0)
    e1 = arr.poll(_POLL_INTERVAL_MS + 1)
    check("still-unmoved jack emits nothing", e1 == [], "got %r" % e1)
    # Move the pedal to the toe: it arms and emits. The EMA smoother means a big
    # jump converges over several polls (by design, so noise/jumps don't snap
    # the value); keep polling and assert it climbs monotonically to the top.
    adc._value = 65535
    t = 2 * (_POLL_INTERVAL_MS + 1)
    seq = []
    for _ in range(80):
        e = arr.poll(t)
        t += _POLL_INTERVAL_MS + 1
        if e:
            seq.append(e[0][1])
    monotonic = all(b >= a for a, b in zip(seq, seq[1:]))
    check("moving the pedal arms it and emits a rising stream", len(seq) > 1 and monotonic,
          "seq=%r" % seq[:10])
    check("value converges to the toe (127)", seq and seq[-1] == 127, "last=%r" % (seq[-1:] or None))
    # Once settled, holding still emits nothing more.
    e_hold = arr.poll(t)
    check("holding still after convergence emits nothing", e_hold == [], "got %r" % e_hold)


def test_floating_jack_never_emits():
    # The morph-fantasma bug: an enabled jack with NO pedal plugged reads a
    # floating ADC that only jitters a count or two. It must never arm, so it
    # emits nothing - no stray CC 4 (Kemper morph) or any other phantom.
    _reset_registry()
    arr = ExpressionArray([_cfg(jack=1, cal_min=0, cal_max=65535, cc=4)])
    adc = FakeAnalogIn.registry["GP27"]
    emitted = 0
    t = 0
    for i in range(300):
        # ~1-count wobble near the top, mirroring the real hardware capture
        # where an unplugged jack sat at value 126..127.
        adc._value = 65535 if (i % 2) else 65020
        emitted += len(arr.poll(t))
        t += _POLL_INTERVAL_MS + 1
    check("floating/unplugged jack never emits (no phantom CC)", emitted == 0,
          "emitted %d" % emitted)


def test_throttle():
    _reset_registry()
    arr = ExpressionArray([_cfg(jack=1, cal_min=0, cal_max=65535)])
    adc = FakeAnalogIn.registry["GP27"]
    adc._value = 0
    arr.poll(0)
    adc._value = 65535
    # Second poll within the throttle window returns nothing even though the
    # value changed - protects MIDI from a flood faster than a real pedal.
    e = arr.poll(_POLL_INTERVAL_MS - 1)
    check("poll throttled within interval returns []", e == [], "got %r" % e)


def test_message_template_carried():
    _reset_registry()
    arr = ExpressionArray([_cfg(jack=1, cc=7, cal_min=0, cal_max=65535)])
    adc = FakeAnalogIn.registry["GP27"]
    adc._value = 0
    arr.poll(0)                                  # baseline (disarmed)
    adc._value = 65535                           # sweep to arm + reach the toe
    events = []
    t = _POLL_INTERVAL_MS + 1
    for _ in range(80):
        events += arr.poll(t)
        t += _POLL_INTERVAL_MS + 1
    msg, value = events[-1]
    check("emitted event carries the message template", msg.get("cc") == 7 and msg.get("type") == "cc")
    check("emitted event reaches the 0..127 value", value == 127)


# ---------------- inert / disabled / reconfigure ----------------

def test_disabled_jack_is_inert():
    _reset_registry()
    arr = ExpressionArray([_cfg(jack=1, enabled=False)])
    check("disabled jack constructs no ADC", not arr.any_active())
    check("disabled jack polls empty", arr.poll(0) == [])


def test_missing_message_is_inert():
    _reset_registry()
    cfg = _cfg(jack=1)
    cfg["message"] = None
    arr = ExpressionArray([cfg])
    check("enabled jack with no message is inert", not arr.any_active())


def test_two_jacks():
    _reset_registry()
    arr = ExpressionArray([_cfg(jack=1, cc=11, cal_min=0, cal_max=65535),
                           _cfg(jack=2, cc=7, cal_min=0, cal_max=65535)])
    r1 = FakeAnalogIn.registry["GP27"]; r2 = FakeAnalogIn.registry["GP28"]
    r1._value = 0; r2._value = 0
    arr.poll(0)                                  # baselines (both disarmed)
    r1._value = 65535; r2._value = 65535         # move both -> arm both
    ccs = set()
    t = _POLL_INTERVAL_MS + 1
    for _ in range(80):
        for m, _v in arr.poll(t):
            ccs.add(m.get("cc"))
        t += _POLL_INTERVAL_MS + 1
    check("both jacks arm and emit independently", ccs == {7, 11}, "got %r" % sorted(ccs))


def test_configure_releases_pins():
    _reset_registry()
    arr = ExpressionArray([_cfg(jack=1)])
    old = FakeAnalogIn.registry["GP27"]
    check("jack active before reconfigure", arr.any_active())
    arr.configure([])                       # user disabled everything
    check("old ADC deinited on reconfigure", old.deinited is True)
    check("no active jacks after empty reconfigure", not arr.any_active())


def _arm_and_settle(arr, pin, raw, start_ms=0):
    """Sweep `pin` from 0 up to `raw` so the jack arms (crosses _ARM_DELTA) and
    the EMA-smoothed value converges. Returns the next free timestamp."""
    reg = FakeAnalogIn.registry[pin]
    reg._value = 0
    t = start_ms
    arr.poll(t); t += _POLL_INTERVAL_MS + 1              # baseline at 0
    reg._value = raw
    for _ in range(120):
        arr.poll(t)
        t += _POLL_INTERVAL_MS + 1
    return t


def test_unchanged_jack_not_reemitted_on_reconfigure():
    # Regression: a PUT_GLOBAL that leaves a jack's config untouched must not
    # re-emit its live position. Real-world symptom: the morph pedal (CC 4)
    # jumped every time an unrelated global setting was saved, because
    # configure() reset value=-1 and the next poll fired an "initial" value.
    _reset_registry()
    cfg = _cfg(jack=1, cc=4, cal_min=0, cal_max=65535)   # CC 4 = Kemper morph
    arr = ExpressionArray([cfg])
    t = _arm_and_settle(arr, "GP27", 32768)              # armed, settled at mid
    # An unrelated settings save: same expression config pushed again. The new
    # ADC starts at 0, so mirror the physical position the pedal is still at.
    arr.configure([dict(cfg)])
    FakeAnalogIn.registry["GP27"]._value = 32768
    e = arr.poll(t)
    check("unchanged armed jack across reconfigure emits nothing", e == [], "got %r" % e)


def test_edited_jack_rearms_and_stays_silent():
    # The flip side: a jack whose config actually changed (here: recalibrated)
    # re-arms - so it does NOT blast the target on save either; it stays silent
    # until the pedal is physically moved again, then resumes normally.
    _reset_registry()
    arr = ExpressionArray([_cfg(jack=1, cc=4, cal_min=0, cal_max=65535)])
    t = _arm_and_settle(arr, "GP27", 32768)
    arr.configure([_cfg(jack=1, cc=4, cal_min=0, cal_max=40000)])  # recalibrated
    FakeAnalogIn.registry["GP27"]._value = 32768         # same position, no move
    e_still = []
    for _ in range(10):
        e_still += arr.poll(t)
        t += _POLL_INTERVAL_MS + 1
    check("edited jack stays silent until moved", e_still == [], "got %r" % e_still)
    FakeAnalogIn.registry["GP27"]._value = 5000          # now move it
    seq = []
    for _ in range(80):
        seq += [v for _m, v in arr.poll(t)]
        t += _POLL_INTERVAL_MS + 1
    check("edited jack emits again once moved", len(seq) > 0, "seq=%r" % seq[:5])


def test_stats_shape():
    _reset_registry()
    arr = ExpressionArray([_cfg(jack=1, cal_min=0, cal_max=65535)])
    FakeAnalogIn.registry["GP27"]._value = 32768
    arr.poll(0)
    s = arr.stats()
    ok = (isinstance(s, list) and len(s) == 1
          and set(s[0].keys()) == {"jack", "raw", "value", "armed"}
          and s[0]["jack"] == 1 and s[0]["raw"] == 32768 and s[0]["armed"] is False)
    check("stats() reports {jack, raw, value, armed} per jack", ok, "got %r" % s)


# ---------------- robustness ----------------

def test_read_error_does_not_throw():
    _reset_registry()
    analogio.AnalogIn = RaisingAnalogIn
    arr = ExpressionArray([_cfg(jack=1)])
    raised = None
    try:
        for t in range(0, 500, _POLL_INTERVAL_MS + 1):
            arr.poll(t)
    except Exception as e:                   # noqa: BLE001
        raised = e
    check("a jack that raises on read never propagates", raised is None, "raised %r" % raised)
    analogio.AnalogIn = FakeAnalogIn


def test_no_analogio_is_inert():
    _reset_registry()
    # Simulate a build without analogio: expression.py caches the module ref at
    # import, so patch its module-level `analogio` to None for this check.
    import captain.expression as expr
    saved = expr.analogio
    expr.analogio = None
    try:
        arr = ExpressionArray([_cfg(jack=1)])
        check("no analogio -> jacks inert, no crash", not arr.any_active())
    finally:
        expr.analogio = saved


def main():
    print("Expression pedal tests (offline, mocked analogio)\n")
    print("A. calibration + curves")
    test_linear_calibration()
    test_clamping()
    test_invert()
    test_curves()
    test_degenerate_calibration()
    print("B. arming + deadband + throttle")
    test_arms_then_emits_on_change()
    test_floating_jack_never_emits()
    test_throttle()
    test_message_template_carried()
    print("C. inert / disabled / reconfigure")
    test_disabled_jack_is_inert()
    test_missing_message_is_inert()
    test_two_jacks()
    test_configure_releases_pins()
    test_unchanged_jack_not_reemitted_on_reconfigure()
    test_edited_jack_rearms_and_stays_silent()
    test_stats_shape()
    print("D. robustness")
    test_read_error_does_not_throw()
    test_no_analogio_is_inert()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    if FAILURES:
        print("\nFailures:")
        for f in FAILURES:
            print("  - " + f)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
