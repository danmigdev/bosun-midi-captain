import { describe, it, expect } from "vitest";
import { pluginSectionsToShow } from "./plugin-sections";
import type { Manifest, PluginManifestEntry, PluginConfigSchema } from "./protocol";

function cfg(key: string, label: string): PluginConfigSchema {
  return { key, label, fields: {} };
}

function plugin(config_schema: PluginConfigSchema | null): PluginManifestEntry {
  return { label: "x", version: "1", messages: {}, config_schema };
}

function manifest(plugins: Record<string, PluginManifestEntry>): Manifest {
  return { core_messages: {}, plugins };
}

const M = manifest({
  kemper_player: plugin(cfg("kemper", "Kemper Player")),
  ampero_ii_stage: plugin(cfg("ampero", "Ampero II Stage")),
  other: plugin(null), // plugin with no config schema
});

describe("pluginSectionsToShow", () => {
  it("shows the active profile's plugin section (fresh profile, no block yet)", () => {
    const out = pluginSectionsToShow(M, "kemper_player", {});
    expect(out.map(c => c.key)).toEqual(["kemper"]);
  });

  it("REPRO: Kemper profile imported, activeKind not yet resolved - still shows via the device block", () => {
    // The bug: gating on activeKind alone hid the Kemper section whenever
    // activeKind was "" (async LIST_PROFILES hadn't landed). The device.json
    // already carries a `kemper` block, so the section must show.
    const device = { kemper: { enabled: true, midi_channel: 1 }, tft: {} };
    const out = pluginSectionsToShow(M, "", device);
    expect(out.map(c => c.key)).toEqual(["kemper"]);
  });

  it("no profile (no activeKind, no plugin blocks) shows nothing", () => {
    expect(pluginSectionsToShow(M, "", {})).toEqual([]);
    expect(pluginSectionsToShow(M, "", { leds: { brightness: 64 } })).toEqual([]);
  });

  it("shows every plugin whose block is present", () => {
    const device = { kemper: {}, ampero: {} };
    const out = pluginSectionsToShow(M, "", device);
    expect(out.map(c => c.key).sort()).toEqual(["ampero", "kemper"]);
  });

  it("active kind + a different plugin's block shows both", () => {
    const out = pluginSectionsToShow(M, "kemper_player", { ampero: {} });
    expect(out.map(c => c.key).sort()).toEqual(["ampero", "kemper"]);
  });

  it("a plugin without a config schema is never shown", () => {
    // 'other' has config_schema null - even if some block named 'other'
    // existed it has no schema to render.
    const out = pluginSectionsToShow(M, "other", { other: {} });
    expect(out).toEqual([]);
  });

  it("null manifest or null device are safe", () => {
    expect(pluginSectionsToShow(null, "kemper_player", { kemper: {} })).toEqual([]);
    expect(pluginSectionsToShow(M, "kemper_player", null).map(c => c.key)).toEqual(["kemper"]);
  });
});
