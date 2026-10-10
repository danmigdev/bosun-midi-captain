import type { BindingMode } from "./protocol";

/** One-sentence explanation of each switch binding mode, accurate to the
 * firmware's action-key semantics (see ACTION_KEYS_BY_MODE in protocol.ts). */
export const MODE_HELP: Record<BindingMode, string> = {
  tap: "Fires its action once on each press.",
  latched:
    "Toggles on/off between two actions (toggle_on / toggle_off) with each press; the LED reflects the state.",
  momentary:
    "Fires \"press\" on press and \"release\" on release, so the action is active only while the switch is held.",
  long_press_alt:
    "A short press fires \"press\"; holding past the long-press threshold fires \"long_press\" instead.",
  double_tap:
    "Two quick presses within the double-tap window fire \"double_tap\"; a single press still fires \"press\".",
};
