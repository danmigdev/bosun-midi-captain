import { describe, it, expect } from "vitest";
import type { Manifest } from "./protocol";
import { RECIPES, type Recipe, type RecipeCtx } from "./recipes";

// --------------------- fakes ---------------------

function kemperManifest(): Manifest {
  return {
    core_messages: {},
    plugins: {
      kemper_player: {
        label: "Kemper Player",
        version: "1.0.0",
        messages: {
          kemper_tuner: {
            label: "Tuner",
            params: {
              state: { type: "enum", values: ["on", "off"], default: "on", label: "State" },
              channel: { type: "int", min: 1, max: 16, default: 1, label: "Channel" },
            },
          },
          kemper_tap_tempo: {
            label: "Tap Tempo",
            params: {
              channel: { type: "int", min: 1, max: 16, default: 1, label: "Channel" },
            },
          },
        },
      },
    },
  };
}

function kemperCtx(): RecipeCtx {
  return { activeKind: "kemper_player", manifest: kemperManifest() };
}

function emptyPluginCtx(): RecipeCtx {
  return {
    activeKind: "generic",
    manifest: {
      core_messages: {},
      plugins: {
        generic: { label: "Generic", version: "1.0.0", messages: {} },
      },
    },
  };
}

function recipe(id: string): Recipe {
  const r = RECIPES.find((x) => x.id === id);
  if (!r) throw new Error(`recipe ${id} not found`);
  return r;
}

// --------------------- preview ---------------------

describe("preview recipe", () => {
  it("builds 3 tap bindings with correct types and deltas", () => {
    const ctx = kemperCtx();
    const bindings = recipe("preview").build(
      { up: "up", down: "down", commit: "A" },
      ctx,
    );
    expect(bindings).toHaveLength(3);
    for (const b of bindings) expect(b.mode).toBe("tap");

    const up = bindings.find((b) => b.switch === "up")!;
    expect(up.actions.press.messages[0]).toMatchObject({
      type: "captain_preview_step",
      delta: 1,
      scope: "patch",
    });

    const down = bindings.find((b) => b.switch === "down")!;
    expect(down.actions.press.messages[0]).toMatchObject({
      type: "captain_preview_step",
      delta: -1,
      scope: "patch",
    });

    const commit = bindings.find((b) => b.switch === "A")!;
    expect(commit.actions.press.messages[0]).toMatchObject({
      type: "captain_preview_commit",
    });
  });

  it("adds a cancel binding when a cancel switch is assigned", () => {
    const bindings = recipe("preview").build(
      { up: "up", down: "down", commit: "A", cancel: "B" },
      kemperCtx(),
    );
    expect(bindings).toHaveLength(4);
    const cancel = bindings.find((b) => b.switch === "B")!;
    expect(cancel.actions.press.messages[0]).toMatchObject({
      type: "captain_preview_cancel",
    });
  });

  it("is always available", () => {
    expect(recipe("preview").available(kemperCtx())).toBe(true);
    expect(recipe("preview").available(emptyPluginCtx())).toBe(true);
  });
});

// --------------------- bank_nav ---------------------

describe("bank_nav recipe", () => {
  it("builds two captain_bank_step bindings with delta +1/-1", () => {
    const bindings = recipe("bank_nav").build(
      { bankUp: "up", bankDown: "down" },
      kemperCtx(),
    );
    expect(bindings).toHaveLength(2);

    const up = bindings.find((b) => b.switch === "up")!;
    expect(up.mode).toBe("tap");
    expect(up.actions.press.messages[0]).toMatchObject({
      type: "captain_bank_step",
      delta: 1,
    });

    const down = bindings.find((b) => b.switch === "down")!;
    expect(down.actions.press.messages[0]).toMatchObject({
      type: "captain_bank_step",
      delta: -1,
    });
  });

  it("is always available", () => {
    expect(recipe("bank_nav").available(emptyPluginCtx())).toBe(true);
  });
});

// --------------------- tuner ---------------------

describe("tuner recipe", () => {
  it("is available for a kemper ctx and unavailable for an empty-plugin ctx", () => {
    expect(recipe("tuner").available(kemperCtx())).toBe(true);
    expect(recipe("tuner").available(emptyPluginCtx())).toBe(false);
  });

  it("builds a latched binding with toggle_on/off state and the kemper_tuner type", () => {
    const bindings = recipe("tuner").build({ switch: "1" }, kemperCtx());
    expect(bindings).toHaveLength(1);
    const b = bindings[0];
    expect(b.switch).toBe("1");
    expect(b.mode).toBe("latched");

    const on = b.actions.toggle_on.messages[0];
    expect(on).toMatchObject({ type: "kemper_tuner", state: "on" });
    // Other params seeded from schema defaults.
    expect(on.channel).toBe(1);

    const off = b.actions.toggle_off.messages[0];
    expect(off).toMatchObject({ type: "kemper_tuner", state: "off" });
  });
});

// --------------------- tap_tempo ---------------------

describe("tap_tempo recipe", () => {
  it("is available for kemper and unavailable for an empty plugin", () => {
    expect(recipe("tap_tempo").available(kemperCtx())).toBe(true);
    expect(recipe("tap_tempo").available(emptyPluginCtx())).toBe(false);
  });

  it("builds a tap binding firing kemper_tap_tempo", () => {
    const bindings = recipe("tap_tempo").build({ switch: "C" }, kemperCtx());
    expect(bindings).toHaveLength(1);
    const b = bindings[0];
    expect(b.switch).toBe("C");
    expect(b.mode).toBe("tap");
    expect(b.actions.press.messages[0]).toMatchObject({
      type: "kemper_tap_tempo",
      channel: 1,
    });
  });
});
