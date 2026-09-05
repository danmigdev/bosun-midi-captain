import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render } from "@testing-library/svelte";
import PatchEditor from "../../src/components/PatchEditor.svelte";
import ExpressionPedals from "../../src/components/ExpressionPedals.svelte";
import { fallbackManifest, type ExpressionConfig, type Manifest, type Patch } from "../../src/lib/protocol";
import { reactiveFixture } from "../reactive-fixture.svelte";

const commands = vi.hoisted(() => ({
  putBinding: vi.fn(), putPatch: vi.fn(), getPatch: vi.fn(async () => ({})), getStats: vi.fn(),
}));
vi.mock("../../src/lib/protocol", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../src/lib/protocol")>(), cmd: commands,
}));
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn(async () => () => {}) }));

const plugins = [
  ["kemper_player", "kemper", "Kemper", "kemper_fx"],
  ["ampero_ii_stage", "ampero", "Ampero", "ampero_scene"],
  ["headrush_core", "headrush", "Headrush", "headrush_rig"],
] as const;

function manifest(): Manifest {
  const m = fallbackManifest();
  for (const [kind, key, label, type] of plugins) {
    m.plugins[kind] = {
      label, version: "1.0", config_schema: { key, label, fields: {} },
      messages: {
        [type]: { label: `${label} command`, params: {
          channel: { type: "int", min: 1, max: 16, default: 1 },
          target: { type: "int", min: 0, max: 127, default: 3 },
        } },
        [`${key}_volume`]: { label: `${label} volume`, params: {
          channel: { type: "int", min: 1, max: 16, default: 2 },
          value: { type: "int", min: 0, max: 127, default: 0 },
        } },
      },
    };
  }
  return m;
}

function patch(type = "cc"): Patch {
  return reactiveFixture<Patch>({
    name: "Saved patch", tft_color: "#ffffff", bindings: [{ switch: "1", mode: "tap", actions: {
      press: { messages: [{ type, channel: 5, target: 9, cc: 7, value: 64 }] },
    } }],
    on_enter: { messages: [{ type, channel: 6, target: 10 }] },
    on_exit: { messages: [{ type, channel: 7, target: 11 }] },
  });
}

function choices(select: HTMLSelectElement): string[] {
  return [...select.options].filter(o => !o.disabled).map(o => o.value).sort();
}

async function expandSwitch(container: HTMLElement) {
  await fireEvent.click(container.querySelector("#swrow-1 .bindinghead")!);
  return container.querySelector("#add-1-press") as HTMLSelectElement;
}

beforeEach(() => vi.clearAllMocks());
afterEach(cleanup);

describe("profile-scoped patch commands", () => {
  it.each(plugins)("offers only core + %s commands in switch and patch macros", async (kind) => {
    const m = manifest();
    const view = render(PatchEditor, { bank: 1, slot: 1, patch: patch(), manifest: m, activeKind: kind });
    const add = await expandSwitch(view.container);
    const expected = [...Object.keys(m.core_messages), ...Object.keys(m.plugins[kind].messages)].sort();
    expect(choices(add)).toEqual(expected);
    expect(choices(view.container.querySelector("#swrow-1 .action .msg select")!)).toEqual(expected);
    for (const title of ["On enter", "On exit"]) {
      await fireEvent.click(view.getByRole("button", { name: new RegExp(title) }));
    }
    for (const select of view.container.querySelectorAll<HTMLSelectElement>(
      ".onbody .msg > select, #oe-add, #ox-add")) expect(choices(select)).toEqual(expected);
    expect(commands.putBinding).not.toHaveBeenCalled();
    expect(commands.putPatch).not.toHaveBeenCalled();
  });

  it("starts with core only and reacts when the profile kind arrives or changes", async () => {
    const props = { bank: 1, slot: 1, patch: patch(), manifest: manifest(), activeKind: "" };
    const view = render(PatchEditor, props);
    const add = await expandSwitch(view.container);
    expect(choices(add)).toEqual(Object.keys(props.manifest.core_messages).sort());
    for (const [kind, , , type] of plugins) {
      await view.rerender({ ...props, activeKind: kind });
      expect(choices(add)).toContain(type);
      for (const [other, , , otherType] of plugins) {
        if (other !== kind) expect(choices(add)).not.toContain(otherType);
      }
    }
    expect(commands.putBinding).not.toHaveBeenCalled();
  });

  it.each([
    ["", null, []],
    ["", { kemper: {} }, ["kemper_fx", "kemper_volume"]],
    ["", { kemper: {}, ampero: {} }, []],
    ["other", { kemper: {} }, []],
    ["unknown_plugin", { kemper: {} }, []],
    ["headrush_core", { kemper: {} }, ["headrush_rig", "headrush_volume"]],
  ])("uses saved profile metadata without guessing from USB identity: %s / %j", async (activeKind, device, expected) => {
    const m = manifest();
    const view = render(PatchEditor, { bank: 1, slot: 1, patch: patch(), manifest: m, activeKind, device });
    const add = await expandSwitch(view.container);
    expect(choices(add)).toEqual([...Object.keys(m.core_messages), ...expected].sort());
  });

  it.each(["ampero_scene", "legacy_unavailable_command"])(
    "preserves foreign/unknown saved %s as its selected disabled option", async type => {
      const saved = patch(type);
      const before = JSON.parse(JSON.stringify(saved));
      const view = render(PatchEditor, {
        bank: 1, slot: 1, patch: saved, manifest: manifest(), activeKind: "kemper_player",
      });
      const add = await expandSwitch(view.container);
      const select = view.container.querySelector<HTMLSelectElement>("#swrow-1 .action .msg select")!;
      expect(select.value).toBe(type);
      expect(select.selectedOptions[0]).toBeDisabled();
      expect(select.selectedOptions[0]).toHaveTextContent("(saved)");
      expect(choices(add)).not.toContain(type);
      for (const title of ["On enter", "On exit"]) {
        await fireEvent.click(view.getByRole("button", { name: new RegExp(title) }));
      }
      for (const current of view.container.querySelectorAll<HTMLSelectElement>(".onbody .msg > select")) {
        expect(current.value).toBe(type);
        expect(current.selectedOptions[0]).toBeDisabled();
      }
      expect(saved).toEqual(before);
      expect(commands.putBinding).not.toHaveBeenCalled();
      expect(commands.putPatch).not.toHaveBeenCalled();
      await fireEvent.change(select, { target: { value: "kemper_fx" } });
      expect(commands.putBinding).toHaveBeenCalledWith(1, 1, expect.objectContaining({
        actions: { press: { messages: [{ type: "kemper_fx", channel: 1, target: 3 }] } },
      }));
      expect(saved).toEqual(before);
    },
  );

  it("filters per-patch expression targets while retaining an imported target", async () => {
    const saved = reactiveFixture({ ...patch(), expression: [{ jack: 1, message: { type: "ampero_volume", channel: 8, value: 0 } }] });
    const view = render(PatchEditor, {
      bank: 1, slot: 1, patch: saved, manifest: manifest(), activeKind: "kemper_player",
    });
    await fireEvent.click(view.getByRole("button", { name: /Expression/ }));
    const select = view.container.querySelector<HTMLSelectElement>(".expjack .msg > select")!;
    expect(select.value).toBe("ampero_volume");
    expect(select.selectedOptions[0]).toBeDisabled();
    expect(choices(select)).toContain("cc");
    expect(choices(select)).toContain("kemper_volume");
    expect(choices(select)).not.toContain("headrush_volume");
    expect(choices(select)).not.toContain("ampero_volume");
    expect(commands.putPatch).not.toHaveBeenCalled();
  });
});

describe("profile-scoped device expression commands", () => {
  function expression(type = "cc"): ExpressionConfig[] {
    return reactiveFixture<ExpressionConfig[]>([{ jack: 1, enabled: true, invert: false, curve: "linear",
      calibration: { min: 0, max: 65535 }, message: { type, channel: 9, cc: 11, value: 0 } }]);
  }

  it("follows the selected profile while offline and keeps the saved foreign value", async () => {
    const saved = expression("ampero_volume");
    const before = JSON.parse(JSON.stringify(saved));
    const props = { expression: saved, manifest: manifest(), activeKind: "kemper_player", connected: false };
    const view = render(ExpressionPedals, props);
    const select = view.getByLabelText("Sends") as HTMLSelectElement;
    expect(select.value).toBe("ampero_volume");
    expect(select.selectedOptions[0]).toBeDisabled();
    expect(choices(select)).toContain("kemper_volume");
    expect(choices(select)).toContain("cc");
    expect(choices(select)).not.toContain("headrush_volume");
    expect(choices(select)).not.toContain("ampero_volume");
    await view.rerender({ ...props, activeKind: "ampero_ii_stage" });
    expect(select.value).toBe("ampero_volume");
    expect(select.selectedOptions[0]).not.toBeDisabled();
    expect(choices(select)).toContain("ampero_volume");
    expect(choices(select)).not.toContain("kemper_volume");
    expect(saved).toEqual(before);
    expect(commands.getStats).not.toHaveBeenCalled();
    expect(commands.putPatch).not.toHaveBeenCalled();
  });

  it("uses core-only choices until a saved config identifies one plugin", async () => {
    const props = { expression: expression(), manifest: manifest(), connected: false };
    const view = render(ExpressionPedals, props);
    const select = view.getByLabelText("Sends") as HTMLSelectElement;
    expect(choices(select)).not.toContain("kemper_volume");
    expect(choices(select)).toContain("cc");
    await view.rerender({ ...props, device: { kemper: {} } });
    expect(choices(select)).toContain("kemper_volume");
    expect(choices(select)).not.toContain("ampero_volume");
  });
});
