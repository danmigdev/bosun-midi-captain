"""Expression-pedal input: reads the two analog jacks and turns pedal
travel into a continuous MIDI value.

Each jack is configured in device.json under `expression`:

    {"jack": 1, "enabled": false, "invert": false,
     "calibration": {"min": 300, "max": 65200}, "curve": "linear",
     "message": {"type": "cc", "channel": 1, "cc": 11, "value": 0}}

`message` is a template; the live 0..127 value is substituted into its
`value` field before the app dispatches it - as a plain CC or a plugin
continuous-control message (kemper_wah / kemper_volume / kemper_morph, ...).

The RP2040 ADC reads 0..65535 through analogio.AnalogIn. Calibration min/max
are raw counts (captured in the editor by sweeping the pedal heel-to-toe); the
reading is lightly smoothed, scaled into 0..127 with an optional taper curve
and inversion. A jack with enabled=false, no message, or an unconstructable
pin is simply skipped, so a board with nothing plugged in costs nothing.
"""

try:
    import analogio
except ImportError:                       # not present under the test harness
    analogio = None

from .board import EXP1_ADC, EXP2_ADC


_ADC_PINS = {1: EXP1_ADC, 2: EXP2_ADC}
_ADC_FULL = 65535
_POLL_INTERVAL_MS = 10                     # cap sampling at ~100 Hz
_DEADBAND = 1                              # min 0..127 delta to emit a message
_ARM_DELTA = 8                             # 0..127 travel before a jack "arms"
                                           # and starts emitting (see poll)

# Pedal-presence detection (charge-and-decay impedance probe). The board has no
# plug-detect pin, so we drive the ADC tip to each rail and read where it
# settles: a floating (unplugged) jack holds its charge -> big hi/lo gap; a
# pedal is a low-impedance pot that snaps back to its wiper -> small gap,
# whatever its position. Measured on hardware: unplugged ~64700, pedal ~2500.
_PRESENCE_INTERVAL_MS = 1500               # how often to probe one jack
_PROBE_CHARGE_S = 0.002                    # drive the tip to the rail
_PROBE_SETTLE_S = 0.010                    # let a pot snap back before reading
_PRESENCE_GAP = 30000                      # gap above this (of 65535) = no pedal
_PRESENCE_STREAK = 3                       # consecutive "absent" probes to mute


def _clampf(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


class _Jack:
    """One expression jack: owns its AnalogIn and turns a raw read into a
    calibrated, smoothed 0..127 value."""

    def __init__(self, cfg):
        cfg = cfg or {}
        self._cfg = cfg                    # kept verbatim so configure() can
                                           # tell an unchanged jack from an edit
        self.jack = int(cfg.get("jack", 0))
        self.enabled = bool(cfg.get("enabled", False))
        self.invert = bool(cfg.get("invert", False))
        cal = cfg.get("calibration") or {}
        self.cal_min = int(cal.get("min", 300))
        self.cal_max = int(cal.get("max", _ADC_FULL))
        self.curve = cfg.get("curve", "linear")
        self.message = cfg.get("message")
        self.raw = 0                       # last raw read, for calibration UI
        self.value = -1                    # last emitted 0..127 (-1 = never)
        self.armed = False                 # has the input actually moved yet?
        self.present = True                 # a pedal is plugged (until proven not)
        self._absent_streak = 0            # consecutive "no pedal" probe results
        self._baseline = None              # first value seen, to measure travel
        self._smooth = None                # EMA state on the raw signal
        self._adc = None
        if self.enabled and self.message and analogio is not None:
            pin = _ADC_PINS.get(self.jack)
            if pin is not None:
                try:
                    self._adc = analogio.AnalogIn(pin)
                except Exception as e:
                    print("expression jack", self.jack, "init failed:", e)
                    self._adc = None

    def active(self):
        return self._adc is not None

    def _presence_probe(self):
        """Charge-and-decay impedance test. Returns (gap, valid). `gap` is the
        hi-minus-lo ADC reading after driving the tip to each rail: large for a
        floating (unplugged) jack that holds its charge (~64000 on hardware),
        small for a pedal that snaps back to its wiper (~2500). Even a pedal
        swept fast reads only a few thousand (the wiper can't traverse the
        range in the ~12 ms probe), so it stays well under _PRESENCE_GAP - no
        motion guard needed. `valid` is False only on a read error. Releases
        and reclaims the ADC around the probe."""
        import digitalio
        import time as _t
        pin = _ADC_PINS.get(self.jack)
        if pin is None or self._adc is None:
            return (0, False)
        self._adc.deinit()
        self._adc = None
        gap, valid = 0, False
        try:
            def charge_read(level):
                d = digitalio.DigitalInOut(pin)
                d.switch_to_output(value=level)
                _t.sleep(_PROBE_CHARGE_S)             # drive node to the rail
                d.deinit()
                a = analogio.AnalogIn(pin)
                _t.sleep(_PROBE_SETTLE_S)             # let a pot snap back
                v = a.value
                a.deinit()
                return v
            gap = charge_read(True) - charge_read(False)
            valid = True
        except Exception as e:
            print("expression jack", self.jack, "probe failed:", e)
        finally:
            try:
                self._adc = analogio.AnalogIn(pin)
            except Exception:
                self._adc = None
        return (gap, valid)

    def deinit(self):
        if self._adc is not None:
            try:
                self._adc.deinit()
            except Exception:
                pass
            self._adc = None

    def sample(self):
        """Read the ADC and return the calibrated 0..127 value, or None when
        the jack isn't active. Always refreshes self.raw so the editor's
        calibration bar keeps moving even before a value crosses a step."""
        if self._adc is None:
            return None
        try:
            self.raw = self._adc.value
        except Exception:
            return None
        # Light EMA (alpha 1/4) to keep ADC noise from chattering the value
        # across a step boundary while the pedal is held still.
        if self._smooth is None:
            self._smooth = self.raw
        else:
            self._smooth += (self.raw - self._smooth) // 4
        lo, hi = self.cal_min, self.cal_max
        if hi <= lo:
            hi = lo + 1
        frac = _clampf((self._smooth - lo) / (hi - lo), 0.0, 1.0)
        if self.curve == "exp":
            frac = frac * frac                 # finer control near the heel
        elif self.curve == "log":
            frac = frac ** 0.5                 # finer control near the toe
        if self.invert:
            frac = 1.0 - frac
        return int(frac * 127 + 0.5)


class ExpressionArray:
    """Owns both jacks. `poll()` samples them (throttled) and returns the
    messages to dispatch for jacks whose value changed."""

    def __init__(self, expression_cfg):
        self._jacks = []
        self._last_poll_ms = -100000
        self._presence_next_ms = 0
        self._presence_rr = -1
        self.configure(expression_cfg)

    def configure(self, expression_cfg):
        """(Re)build the jacks from a device.json `expression` list. Releases
        any previously-claimed ADC pins first so a PUT_GLOBAL that flips a
        jack on/off takes effect without a reboot.

        Runtime state (last emitted value, smoothing, and the armed/baseline
        movement gate) is carried over for any jack whose config is
        byte-for-byte unchanged, so a PUT_GLOBAL that doesn't touch this jack
        neither re-emits its current position nor makes it re-arm. Without the
        value carry-over, every settings save re-seeded value=-1 and the next
        poll blasted the live pedal value at its target - e.g. jamming the
        Kemper morph (CC 4) to wherever the pedal physically sits. A jack that
        was actually added or edited starts fresh and must re-arm (move) before
        it emits again."""
        # Snapshot old per-jack state keyed by jack number BEFORE releasing the
        # pins (deinit() drops the ADC, but these are all plain values).
        prev = {j.jack: (j._cfg, j.value, j._smooth, j.armed, j._baseline,
                         j.present, j._absent_streak)
                for j in self._jacks}
        for j in self._jacks:
            j.deinit()
        new_jacks = []
        for c in (expression_cfg or []):
            j = _Jack(c)
            saved = prev.get(j.jack)
            if saved is not None and saved[0] == j._cfg:
                (j.value, j._smooth, j.armed, j._baseline,
                 j.present, j._absent_streak) = saved[1:]
            new_jacks.append(j)
        self._jacks = new_jacks

    def any_active(self):
        return any(j.active() for j in self._jacks)

    def _probe_presence(self, now_ms):
        """Every _PRESENCE_INTERVAL_MS, run the charge-and-decay probe on one
        active jack (round-robin) and update its `present` flag. Debounced: it
        takes _PRESENCE_STREAK consecutive "absent" reads to mute a jack, so a
        pedal sweep (which can read absent mid-motion) is never mistaken for an
        unplugged jack; one "present" read restores it immediately."""
        if now_ms - self._presence_next_ms < 0:
            return
        self._presence_next_ms = now_ms + _PRESENCE_INTERVAL_MS
        active = [j for j in self._jacks if j.active()]
        if not active:
            return
        self._presence_rr = (self._presence_rr + 1) % len(active)
        j = active[self._presence_rr]
        gap, valid = j._presence_probe()
        if not valid:
            return
        if gap > _PRESENCE_GAP:
            j._absent_streak += 1
            if j._absent_streak >= _PRESENCE_STREAK:
                j.present = False
        else:
            j._absent_streak = 0
            j.present = True

    def poll(self, now_ms):
        """Return a list of (message_template, value127) for jacks whose value
        moved past the deadband since last poll. Throttled to _POLL_INTERVAL_MS
        so a slow sweep doesn't spam MIDI faster than a real pedal would.

        Two gates keep a bare jack silent. `present`: a periodic impedance probe
        detects whether a pedal is actually plugged (see _probe_presence); an
        unplugged jack is muted outright. `armed`: even a present jack only
        starts emitting once its value has travelled _ARM_DELTA from the first
        reading, so no jack blasts its power-on position and residual float
        jitter never leaks out. `value` still tracks every read for the editor's
        live calibration bar."""
        self._probe_presence(now_ms)
        if now_ms - self._last_poll_ms < _POLL_INTERVAL_MS:
            return []
        self._last_poll_ms = now_ms
        events = []
        for j in self._jacks:
            v = j.sample()
            if v is None:
                continue
            if not (j.value < 0 or abs(v - j.value) >= _DEADBAND):
                continue
            if not j.armed:
                if j._baseline is None:
                    j._baseline = v
                elif abs(v - j._baseline) >= _ARM_DELTA:
                    j.armed = True
            j.value = v                    # track for the calibration bar even
                                           # while disarmed / unplugged
            if j.armed and j.present and j.message:
                events.append((j.message, v))
        return events

    def stats(self):
        """Per-jack {jack, raw, value, armed, present} for the editor's live
        calibration bar. `present` is False when the probe finds no pedal
        plugged, so the editor can show a "no pedal detected" hint."""
        return [{"jack": j.jack, "raw": j.raw, "armed": j.armed,
                 "present": j.present,
                 "value": j.value if j.value >= 0 else 0}
                for j in self._jacks]
