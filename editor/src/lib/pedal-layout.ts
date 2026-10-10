// Schematic pedal switch layout for the PaintAudio MIDI Captain.
//
// The map is drawn as a fixed schematic of the connected model's physical
// rows (lib/hardware.ts). The helpers below read a switch's binding label /
// bound state for rendering.

import { CAPTAIN_10, type HardwareLayout } from "./hardware";
import type { Binding } from "./protocol";

/** A layout is a list of rows; each row is a list of switch names. An empty
 *  string "" is a spacer / empty cell. */
export type PedalLayout = string[][];

/** The 10-switch Captain's switch names, the default when the firmware does
 *  not report its hardware. */
export const ALL_SWITCHES: string[] = CAPTAIN_10.switches;

/** The 10-switch Captain's schematic arrangement. */
export const DEFAULT_LAYOUT: PedalLayout = CAPTAIN_10.rows;

/** The schematic arrangement of a model's switches, top row first. */
export function layoutFor(hardware: HardwareLayout | null | undefined): PedalLayout {
  return (hardware ?? CAPTAIN_10).rows;
}

/** The user label for a switch's binding, or "" when unbound / unlabeled.
 *  Never throws. */
export function labelForSwitch(bindings: Binding[], sw: string): string {
  return bindings.find((b) => b.switch === sw)?.label ?? "";
}

/** Whether a binding exists for the given switch. */
export function isBound(bindings: Binding[], sw: string): boolean {
  return bindings.some((b) => b.switch === sw);
}
