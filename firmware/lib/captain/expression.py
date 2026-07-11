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


def _clampf(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


class _Jack:
    """One expression jack: owns its AnalogIn and turns a raw read into a
    calibrated, smoothed 0..127 value."""

    def __init__(self, cfg):
        cfg = cfg or {}
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
        self.configure(expression_cfg)

    def configure(self, expression_cfg):
        """(Re)build the jacks from a device.json `expression` list. Releases
        any previously-claimed ADC pins first so a PUT_GLOBAL that flips a
        jack on/off takes effect without a reboot."""
        for j in self._jacks:
            j.deinit()
        self._jacks = [_Jack(c) for c in (expression_cfg or [])]

    def any_active(self):
        return any(j.active() for j in self._jacks)

    def poll(self, now_ms):
        """Return a list of (message_template, value127) for jacks whose value
        moved past the deadband since last poll. Throttled to _POLL_INTERVAL_MS
        so a slow sweep doesn't spam MIDI faster than a real pedal would."""
        if now_ms - self._last_poll_ms < _POLL_INTERVAL_MS:
            return []
        self._last_poll_ms = now_ms
        events = []
        for j in self._jacks:
            v = j.sample()
            if v is None:
                continue
            if j.value < 0 or abs(v - j.value) >= _DEADBAND:
                j.value = v
                if j.message:
                    events.append((j.message, v))
        return events

    def stats(self):
        """Per-jack {jack, raw, value} for the editor's live calibration bar."""
        return [{"jack": j.jack, "raw": j.raw,
                 "value": j.value if j.value >= 0 else 0}
                for j in self._jacks]
