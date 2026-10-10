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
