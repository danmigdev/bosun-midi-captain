// Pure TypeScript port of the firmware switch FSM (SwitchFsm) from
// firmware/lib/captain/bindings.py. This is the offline simulator variant:
// it drives CLEAN edges (no debounce, no pins, no digitalio) and ports ONLY
// the mode logic, with an injected clock. Keep the transitions in sync with
// the firmware SwitchFsm and with tools/fsm_test.py.

export type SwitchMode =
  | "tap"
  | "latched"
  | "momentary"
  | "long_press_alt"
  | "double_tap";

export type ActionKey =
  | "press"
  | "release"
  | "toggle_on"
  | "toggle_off"
  | "long_press"
  | "double_tap";

export interface SwitchFsmOptions {
  longPressMs?: number;
  doubleTapWindowMs?: number;
  autoMomentaryOnHold?: boolean;
  autoMomentaryMs?: number;
}

export class SwitchFsm {
  longPressMs: number;
  doubleTapWindowMs: number;
  autoMomentaryOnHold: boolean;
  autoMomentaryMs: number;

  // Public latched state (mirrors firmware `latched_on`).
  latchedOn = false;

  // Mode-specific scratch (mirrors the firmware private fields).
  private pressStartMs = 0;
  private firedLongPress = false;
  private latchedPrePress = false;
  private tapPendingUntilMs = 0;
  // Mirrors `not self._stable` (switch currently held). Set on press,
  // cleared on release; gates the long_press tick just like the firmware.
  private pressed = false;

  constructor(options: SwitchFsmOptions = {}) {
    this.longPressMs = options.longPressMs ?? 600;
    this.doubleTapWindowMs = options.doubleTapWindowMs ?? 250;
    this.autoMomentaryOnHold = options.autoMomentaryOnHold ?? true;
    this.autoMomentaryMs = options.autoMomentaryMs ?? 500;
  }

  /** Reset scratch state and latchedOn. Mirrors firmware reset() minus the
   * pin-held special case (the simulator has no hardware pin to inspect). */
  reset(): void {
    this.pressStartMs = 0;
    this.firedLongPress = false;
    this.latchedOn = false;
    this.latchedPrePress = false;
    this.tapPendingUntilMs = 0;
    this.pressed = false;
  }

  /** Replicates _on_press. */
  press(nowMs: number, mode: string): ActionKey[] {
    this.pressed = true;
    if (mode === "tap") {
      return ["press"];
    }
    if (mode === "latched") {
      this.latchedPrePress = this.latchedOn;
      this.pressStartMs = nowMs;
      this.latchedOn = !this.latchedOn;
      return [this.latchedOn ? "toggle_on" : "toggle_off"];
    }
    if (mode === "momentary") {
      return ["press"];
    }
    if (mode === "long_press_alt") {
      this.pressStartMs = nowMs;
      this.firedLongPress = false;
      return [];
    }
    if (mode === "double_tap") {
      if (this.tapPendingUntilMs && nowMs <= this.tapPendingUntilMs) {
        this.tapPendingUntilMs = 0;
        return ["double_tap"];
      }
      this.tapPendingUntilMs = nowMs + this.doubleTapWindowMs;
      return [];
    }
    return [];
  }

  /** Replicates _on_release. */
  release(nowMs: number, mode: string): ActionKey[] {
    this.pressed = false;
    if (mode === "momentary") {
      return ["release"];
    }
    if (mode === "long_press_alt") {
      if (!this.firedLongPress) {
        const held = nowMs - this.pressStartMs;
        if (held < this.longPressMs) {
          return ["press"];
        }
      }
      return [];
    }
    if (mode === "latched" && this.autoMomentaryOnHold) {
      const held = nowMs - this.pressStartMs;
      if (held >= this.autoMomentaryMs && this.latchedOn !== this.latchedPrePress) {
        this.latchedOn = this.latchedPrePress;
        return [this.latchedOn ? "toggle_on" : "toggle_off"];
      }
    }
    return [];
  }

  /** Replicates _on_tick. In the firmware the pin-held check is `not self._stable`;
   * in the simulator we track held state via the double_tap window / long-press
   * timer set on press. */
  tick(nowMs: number, mode: string): ActionKey[] {
    const triggers: ActionKey[] = [];
    if (mode === "long_press_alt" && this.pressed && !this.firedLongPress) {
      if (nowMs - this.pressStartMs >= this.longPressMs) {
        this.firedLongPress = true;
        triggers.push("long_press");
      }
    }
    if (mode === "double_tap" && this.tapPendingUntilMs) {
      if (nowMs > this.tapPendingUntilMs) {
        this.tapPendingUntilMs = 0;
        triggers.push("press");
      }
    }
    return triggers;
  }
}
