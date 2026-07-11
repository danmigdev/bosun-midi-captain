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

/** Short, generic help for the common message parameter names, keyed by the
 * param name as it appears in the manifest schema. */
export const PARAM_HELP: Record<string, string> = {
  channel: "MIDI channel 1-16.",
  cc: "Control Change number (0-127).",
  value: "Control Change value (0-127).",
  program: "Program Change number (0-127).",
  delta: "How many steps to move, positive or negative.",
  scope: "Preview by single patch or whole bank.",
  bank: "Target bank number.",
  slot: "Target slot within the bank.",
  ms: "Duration in milliseconds.",
  note: "MIDI note number (0-127).",
  velocity: "How hard the note is struck (0-127).",
  bpm: "Tempo in beats per minute.",
  direction: "Which way to move: up or down.",
  state: "On or off.",
};

/** One-sentence description of a common message type, or undefined if the
 * type has no dedicated help entry. */
export function helpForMessageType(type: string): string | undefined {
  const HELP: Record<string, string> = {
    cc: "Send a Control Change to set a parameter on the target device.",
    pc: "Send a Program Change to select a preset on the target device.",
    note_on: "Send a MIDI Note On to start a note.",
    note_off: "Send a MIDI Note Off to stop a note.",
    delay: "Pause for a set time before the next message in the chain.",
    captain_patch: "Load a specific Captain patch by bank and slot.",
    captain_bank_step: "Jump to the same slot in another bank.",
    captain_preview_step:
      "Scroll the on-screen patch preview without loading anything - commit to jump.",
    captain_preview_commit: "Load the patch currently shown in the preview.",
    captain_preview_cancel: "Dismiss the preview and stay on the current patch.",
  };
  return HELP[type];
}
