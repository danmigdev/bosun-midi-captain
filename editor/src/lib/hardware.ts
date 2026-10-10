// Hardware layouts of the supported PaintAudio MIDI Captain models.
//
// Firmware 0.8 and later reports its model in DEVICE_INFO.hardware.
// Every earlier release only ran on the 10-switch Captain, so a missing
// descriptor means CAPTAIN_10. Switch order is the NeoPixel chain order;
// rows are the physical rows, top row first.

export interface HardwareLayout {
  /** Stable model id, e.g. "captain10" or "mini6". */
  model: string;
  /** Product name shown to the user. */
  name: string;
  /** Switch ids in chain order (row-major, top row first). */
  switches: string[];
  /** Physical rows, top row first. */
  rows: string[][];
  led_count: number;
  expression_jacks: number;
  /** A persisted hardware record selected this model; false means the
   * firmware fell back to its default (or predates hardware reporting). */
  configured: boolean;
  /** Models SET_HARDWARE accepts; empty when the firmware cannot switch. */
  models: string[];
}

/** Deep-frozen so a shared table can never be edited through a layout. */
function define(rows: string[][], fields: Omit<HardwareLayout, "switches" | "rows">): HardwareLayout {
  const frozenRows = Object.freeze(rows.map((row) => Object.freeze([...row])));
  return Object.freeze({
    ...fields,
    switches: Object.freeze(rows.flat()),
    rows: frozenRows,
    models: Object.freeze([...fields.models]),
  }) as unknown as HardwareLayout;
}

export const CAPTAIN_10: HardwareLayout = define(
  [["1", "2", "3", "4", "up"], ["A", "B", "C", "D", "down"]],
  { model: "captain10", name: "MIDI Captain", led_count: 30, expression_jacks: 2, configured: false, models: [] },
);

/** Pin map from PySwitch (pa_midicaptain_mini_6.py); not verified on hardware. */
export const MINI_6: HardwareLayout = define(
  [["1", "2", "3"], ["A", "B", "C"]],
  { model: "mini6", name: "MIDI Captain Mini 6", led_count: 18, expression_jacks: 0, configured: false, models: [] },
);

/** Models this editor knows how to describe, in selection order. */
export const KNOWN_HARDWARE: readonly HardwareLayout[] = Object.freeze([CAPTAIN_10, MINI_6]);

export function knownHardware(model: string | null | undefined): HardwareLayout | null {
  return KNOWN_HARDWARE.find((hardware) => hardware.model === model) ?? null;
}

/** "MIDI Captain (10 switches)" style label for pickers and notices. */
export function hardwareLabel(hardware: Pick<HardwareLayout, "name" | "switches">): string {
  return `${hardware.name} (${hardware.switches.length} switches)`;
}

function stringList(value: unknown): string[] | null {
  if (!Array.isArray(value) || !value.every((item) => typeof item === "string" && item !== "")) return null;
  return value as string[];
}

function sameList(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((item, index) => item === b[index]);
}

/** Validate DEVICE_INFO.hardware. Absent or unusable descriptors fall back to
 * the 10-switch Captain (or to the editor's table for a recognised model id),
 * so a malformed reply can never produce an empty or duplicated layout. */
export function parseHardware(raw: unknown): HardwareLayout {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return CAPTAIN_10;
  const value = raw as Record<string, unknown>;
  const model = typeof value.model === "string" && value.model ? value.model : null;
  const known = knownHardware(model);
  const configured = value.configured === true;
  const models = (Array.isArray(value.models) ? value.models : [])
    .filter((id, index, all): id is string => typeof id === "string" && id !== "" && all.indexOf(id) === index);
  const switches = stringList(value.switches);
  const usableSwitches = !!switches && switches.length > 0 && switches.length <= 10 &&
    new Set(switches).size === switches.length;
  if (!model || !usableSwitches) {
    return known ? { ...known, configured, models } : CAPTAIN_10;
  }
  const rawRows = Array.isArray(value.rows) ? value.rows.map(stringList) : null;
  const rows = rawRows && rawRows.every((row): row is string[] => !!row && row.length > 0) &&
    sameList(rawRows.flat() as string[], switches!)
    ? (rawRows as string[][])
    : known && sameList(known.switches, switches!) ? known.rows : [switches!];
  const jacks = typeof value.expression_jacks === "number" && Number.isInteger(value.expression_jacks)
    ? Math.min(2, Math.max(0, value.expression_jacks))
    : known?.expression_jacks ?? 0;
  const leds = typeof value.led_count === "number" && Number.isInteger(value.led_count) && value.led_count >= 0
    ? value.led_count
    : switches!.length * 3;
  return {
    model,
    name: typeof value.name === "string" && value.name.trim() ? value.name : known?.name ?? model,
    switches: [...switches!],
    rows: rows.map((row) => [...row]),
    led_count: leds,
    expression_jacks: jacks,
    configured,
    models,
  };
}

export function hasSwitch(hardware: HardwareLayout, id: string): boolean {
  return hardware.switches.includes(id);
}

/** Ids from `ids` that this pedal does not have (kept in their first-seen
 * order). Configurations copied from another model can contain these. */
export function missingSwitches(hardware: HardwareLayout, ids: Iterable<string>): string[] {
  const missing: string[] = [];
  for (const id of ids) {
    if (!hardware.switches.includes(id) && !missing.includes(id)) missing.push(id);
  }
  return missing;
}

/** Expression jack numbers (1-based) present on this pedal. */
export function expressionJacks(hardware: HardwareLayout): number[] {
  return Array.from({ length: hardware.expression_jacks }, (_, index) => index + 1);
}
