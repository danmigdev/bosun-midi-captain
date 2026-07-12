import type { Binding } from "../lib/protocol";

// Port of firmware/lib/captain/leds.py:_color_for (and parse_hex) so the editor
// can preview exactly what colour the pedal will light for a given binding and
// latched state. Keep this in lockstep with the firmware.

// Off (dimmed) latched LED brightness on the same 0-255 scale as the overall LED
// brightness (device.leds.brightness). Scales the on colour: 255 = as bright as
// on, 0 = off. 64 == the old fixed divide-by-4 (25%). Config: device.leds.dim.
export const DEFAULT_LATCHED_OFF_DIM = 64;

/**
 * Parse '#rrggbb' into an [r, g, b] tuple of ints. Anything malformed
 * (not a string, wrong length, missing leading '#', non-hex digits) -> [0,0,0].
 * Mirrors Python parse_hex.
 */
export function parseHex(color: string): [number, number, number] {
  if (typeof color !== "string" || color.length !== 7 || color[0] !== "#") {
    return [0, 0, 0];
  }
  const r = parseInt(color.slice(1, 3), 16);
  const g = parseInt(color.slice(3, 5), 16);
  const b = parseInt(color.slice(5, 7), 16);
  if (Number.isNaN(r) || Number.isNaN(g) || Number.isNaN(b)) {
    return [0, 0, 0];
  }
  return [r, g, b];
}

/** Convert an [r, g, b] tuple back to a normalized lowercase '#rrggbb' hex. */
export function rgbToHex([r, g, b]: [number, number, number]): string {
  const hex = (n: number) => n.toString(16).padStart(2, "0");
  return `#${hex(r)}${hex(g)}${hex(b)}`;
}

/**
 * Return the '#rrggbb' colour the pedal will render for this binding, given
 * whether the switch is currently latched on. Replicates the firmware
 * _color_for exactly, including the "dim the on colour when latched-off and
 * off is absent or black" behaviour.
 */
export function ledColorFor(
  binding: Binding,
  latchedOn: boolean,
  dim: number = DEFAULT_LATCHED_OFF_DIM,
): string {
  const led = binding.led ?? {};
  const mode = binding.mode ?? "tap";
  const onRgb = parseHex(led.on ?? "#000000");
  if (mode === "latched") {
    if (latchedOn) {
      return rgbToHex(onRgb);
    }
    // Latched-off state. A stompbox-style switch should stay VISIBLE when the
    // effect is off (dim) rather than go black, so you can still see which
    // switch does what. Honour led.off only if it is a real, non-black colour;
    // if it's absent OR black (#000000), dim the on colour instead.
    const offRgb = led.off !== null && led.off !== undefined ? parseHex(led.off) : null;
    if (offRgb !== null && !(offRgb[0] === 0 && offRgb[1] === 0 && offRgb[2] === 0)) {
      return rgbToHex(offRgb);
    }
    const d = dim;
    return rgbToHex([
      Math.floor((onRgb[0] * d) / 255),
      Math.floor((onRgb[1] * d) / 255),
      Math.floor((onRgb[2] * d) / 255),
    ]);
  }
  return rgbToHex(onRgb);
}
