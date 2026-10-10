// Cross-checks the editor's model table against the firmware source.
// Lives under tests/ (outside the type-checked src tree) because it reads a
// repository file with Node APIs.
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { KNOWN_HARDWARE, knownHardware } from "../src/lib/hardware";

describe("firmware parity", () => {
  it("matches the firmware's model table", () => {
    // The installer, onboarding and fallbacks use this table before the
    // pedal reports its own descriptor, so it must never drift from it.
    // Vitest runs from editor/ (jsdom gives this module no file URL).
    const source = readFileSync(resolve(process.cwd(), "../firmware/src/hardware.c"), "utf8");
    const names = new Map([...source.matchAll(/static const char \*const (\w+)_names\[\] = \{([^}]*)\};/g)]
      .map((m) => [m[1], [...m[2].matchAll(/"([^"]+)"/g)].map((n) => n[1])]));
    const models = [...source.matchAll(
      /const bosun_hardware_t bosun_hardware_\w+ = \{\s*"([^"]+)", "([^"]+)", (\w+)_names, \w+_bits, (\d+), (\d+), (\d+), (\d+)\s*\};/g)]
      .map((m) => {
        const switches = names.get(m[3])!;
        const rowLength = Number(m[5]);
        return {
          model: m[1], name: m[2], switches, switchCount: Number(m[4]),
          rows: Array.from({ length: switches.length / rowLength }, (_, r) => switches.slice(r * rowLength, (r + 1) * rowLength)),
          led_count: Number(m[6]), expression_jacks: Number(m[7]),
        };
      });
    expect(models.map((m) => m.model)).toEqual(KNOWN_HARDWARE.map((h) => h.model));
    for (const firmware of models) {
      const editor = knownHardware(firmware.model)!;
      expect(firmware.switchCount).toBe(firmware.switches.length);
      expect({ name: editor.name, switches: editor.switches, rows: editor.rows, led_count: editor.led_count, expression_jacks: editor.expression_jacks })
        .toEqual({ name: firmware.name, switches: firmware.switches, rows: firmware.rows, led_count: firmware.led_count, expression_jacks: firmware.expression_jacks });
    }
  });
});
