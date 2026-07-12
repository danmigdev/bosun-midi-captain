// Schematic pedal switch layout for the PaintAudio MIDI Captain (10 switches).
//
// The map is drawn as a fixed schematic: two rows of five switches. The helpers
// below read a switch's binding label / bound state for rendering.

import type { Binding } from "./protocol";

/** A layout is a list of rows; each row is a list of switch names. An empty
 *  string "" is a spacer / empty cell. */
export type PedalLayout = string[][];

/** The complete set of switch names the firmware uses. */
export const ALL_SWITCHES: string[] = ["1", "2", "3", "4", "up", "A", "B", "C", "D", "down"];

/** The fixed schematic arrangement of the ten switches on the map. */
export const DEFAULT_LAYOUT: PedalLayout = [
  ["1", "2", "3", "4", "up"],
  ["A", "B", "C", "D", "down"],
];

/** The user label for a switch's binding, or "" when unbound / unlabeled.
 *  Never throws. */
export function labelForSwitch(bindings: Binding[], sw: string): string {
  return bindings.find((b) => b.switch === sw)?.label ?? "";
}

/** Whether a binding exists for the given switch. */
export function isBound(bindings: Binding[], sw: string): boolean {
  return bindings.some((b) => b.switch === sw);
}
