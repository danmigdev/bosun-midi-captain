import { describe, expect, it } from "vitest";
import { filterManifestForProfile } from "./profile-message-types";
import { pluginSectionsToShow } from "./plugin-sections";
import { flattenManifest, type Manifest, type PluginManifestEntry } from "./protocol";

function kemper(label: string, banks: number): PluginManifestEntry {
  return {
    label, version: "1", config_schema: { key: "kemper", label, fields: {} },
    messages: {
      kemper_rig: { label: "Select", params: { bank: { type: "int", min: 1, max: banks } } },
      kemper_morph: { label: "Morph", params: { value: { type: "int", min: 0, max: 127 } } },
    },
  };
}
const manifest: Manifest = {
  core_messages: {},
  plugins: { kemper_player: kemper("Player", 25), kemper_head: kemper("Head", 125) },
};

describe("Kemper plugins sharing their schemas and configuration key", () => {
  it.each(["kemper_player", "kemper_head"])("uses only %s's choices and settings", kind => {
    const device = { kemper: { debug: false } };
    const filtered = filterManifestForProfile(manifest, kind, device);
    expect(Object.keys(filtered.plugins)).toEqual([kind]);
    const messages = flattenManifest(filtered);
    expect(messages.map(m => m.type)).toEqual(["kemper_rig", "kemper_morph"]);
    expect(messages[0].params.bank.max).toBe(kind === "kemper_head" ? 125 : 25);
    expect(pluginSectionsToShow(manifest, kind, device)).toEqual([manifest.plugins[kind].config_schema]);
  });

  it("waits for model metadata instead of guessing from a shared block", () => {
    expect(filterManifestForProfile(manifest, "", { kemper: {} }).plugins).toEqual({});
    expect(pluginSectionsToShow(manifest, "", { kemper: {} })).toEqual([]);
  });

  it("retains device-block inference with older single-plugin firmware", () => {
    const older = { ...manifest, plugins: { kemper_player: manifest.plugins.kemper_player } };
    expect(Object.keys(filterManifestForProfile(older, "", { kemper: {} }).plugins)).toEqual(["kemper_player"]);
  });

  it("uses plugin defaults and selected capabilities without changing stored schemas", () => {
    const profiler = kemper("PROFILER", 125);
    profiler.config_schema!.fields = {
      generation: { type: "enum", values: ["MK1", "MK2"], default: "MK1" },
      mode: { type: "enum", values: ["performance", "browse"], default: "performance" },
    };
    profiler.messages.kemper_rig.requires = { mode: "performance" };
    profiler.messages.kemper_browse_rig = { label: "Browse", params: {}, requires: { mode: "browse" } };
    profiler.messages.kemper_fixed_toggle = { label: "Fixed FX", params: {}, requires: { generation: "MK2" } };
    const full = { core_messages: {}, plugins: { kemper_head: profiler } };
    const choices = (device?: Record<string, unknown>) =>
      Object.keys(filterManifestForProfile(full, "kemper_head", device).plugins.kemper_head.messages);
    expect(choices()).toEqual(["kemper_rig", "kemper_morph"]);
    expect(choices({ kemper: { generation: "MK2", mode: "browse" } })).toEqual([
      "kemper_morph", "kemper_browse_rig", "kemper_fixed_toggle",
    ]);
    expect(Object.keys(profiler.messages)).toHaveLength(4);
  });
});
