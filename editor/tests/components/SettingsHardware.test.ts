import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { tick } from "svelte";
import Settings from "../../src/components/Settings.svelte";
import ExpressionPedals from "../../src/components/ExpressionPedals.svelte";
import { CAPTAIN_10, MINI_6 } from "../../src/lib/hardware";
import type { ExpressionConfig } from "../../src/lib/protocol";
import { reactiveFixture } from "../reactive-fixture.svelte";

const commands = vi.hoisted(() => ({
  putGlobal: vi.fn(async (_device: Record<string, unknown>) => ({})),
  getGlobal: vi.fn(async () => ({})),
  getStats: vi.fn(async () => ({ expression: [] })),
}));
vi.mock("../../src/lib/protocol", async (original) => ({
  ...await original<typeof import("../../src/lib/protocol")>(), cmd: commands,
}));
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn(async () => () => {}) }));
beforeEach(() => vi.clearAllMocks());

const BANK_SELECTS = ["Bank up: hold this switch", "Bank down: hold this switch"];
const bankUp = { type: "captain_bank_step", delta: 1 };
const bankDown = { type: "captain_bank_step", delta: -1 };
const holdCc = { type: "cc", channel: 1, cc: 97, value: 127 };

function jack(n: number, extra: Record<string, unknown> = {}): ExpressionConfig {
  return { jack: n, enabled: true, invert: false, calibration: { min: 300, max: 65200 }, curve: "linear",
    message: { type: "cc", channel: 1, cc: 10 + n, value: 0 }, ...extra };
}

/** A device.json written on a 10-switch Captain, then restored on a Mini 6. */
function tenSwitchConfig() {
  return {
    device_name: "From a 10-switch",
    long_press_actions: { up: [bankUp], A: [bankDown], down: [holdCc] },
    preset_navigation: { switches: { A: 1, D: 4 }, bank_colors: { "1": "#3a8eff" } },
    expression: [jack(1), jack(2, { future_option: 7 })],
  };
}

function switchOptions(label: string): string[] {
  return [...(screen.getByLabelText(label) as HTMLSelectElement).options].map(o => o.value).filter(v => v !== "");
}
function navSwitches(): string[] {
  return screen.getAllByPlaceholderText("unmapped").map(input => input.closest("label")!.textContent!.trim());
}
function jackLabels(): string[] {
  return screen.queryAllByText(/^EXP \d$/).map(node => node.textContent ?? "");
}
function group(name: string) {
  return within(screen.getByRole("group", { name }));
}
async function save(): Promise<Record<string, any>> {
  await fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
  await waitFor(() => expect(commands.putGlobal).toHaveBeenCalledOnce());
  return commands.putGlobal.mock.calls[0][0];
}

describe("Settings switch pickers per model", () => {
  it("lists exactly the Mini 6 switches in the bank selects and preset-navigation inputs", () => {
    render(Settings, { device: {}, hardware: MINI_6 });
    for (const label of BANK_SELECTS) expect(switchOptions(label)).toEqual(["1", "2", "3", "A", "B", "C"]);
    expect(navSwitches()).toEqual(["1", "2", "3", "A", "B", "C"]);
    expect(screen.queryByText(/not on this pedal/i)).not.toBeInTheDocument();
  });

  it("keeps all ten switches and no orphan groups for a 10-switch config without hardware info", async () => {
    render(Settings, { device: {
      long_press_actions: { up: [bankUp], down: [bankDown] },
      preset_navigation: { switches: { A: 1, D: 4, down: 5 } },
    } });
    for (const label of BANK_SELECTS) expect(switchOptions(label)).toEqual(CAPTAIN_10.switches);
    expect(navSwitches()).toEqual(["1", "2", "3", "4", "UP", "A", "B", "C", "D", "DOWN"]);
    await waitFor(() => expect(screen.getByLabelText(BANK_SELECTS[0])).toHaveValue("up"));
    expect(screen.getByLabelText(BANK_SELECTS[1])).toHaveValue("down");
    expect(screen.getByLabelText("DOWN")).toHaveValue(5);
    expect(screen.queryByText(/not on this pedal/i)).not.toBeInTheDocument();
  });
});

describe("Settings expression jacks per model", () => {
  it.each([
    ["is missing", {}],
    ["is empty", { expression: [] }],
  ])("seeds and saves no expression entry on a Mini 6 when expression %s", async (_case, stored) => {
    render(Settings, { device: { ...stored, custom: { keep: 42 } }, hardware: MINI_6 });
    expect(screen.queryByRole("heading", { name: "Expression pedals" })).not.toBeInTheDocument();
    expect(screen.queryByText(/expression jack/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Sends")).not.toBeInTheDocument();
    const saved = await save();
    expect(saved.expression ?? []).toEqual([]);
    if (!("expression" in stored)) expect(saved).not.toHaveProperty("expression");
    expect(saved).toMatchObject({ custom: { keep: 42 } });
    expect(commands.getStats).not.toHaveBeenCalled();
  });

  it("still seeds and saves both default jacks on the 10-switch model", async () => {
    render(Settings, { device: {} });
    expect(screen.getByRole("heading", { name: "Expression pedals" })).toBeInTheDocument();
    expect(screen.getByText(/Two expression jacks\./)).toBeInTheDocument();
    expect(jackLabels()).toEqual(["EXP 1", "EXP 2"]);
    const saved = await save();
    expect(saved.expression).toEqual([
      expect.objectContaining({ jack: 1, enabled: false, message: { type: "cc", channel: 1, cc: 11, value: 0 } }),
      expect.objectContaining({ jack: 2, enabled: false, message: { type: "cc", channel: 1, cc: 4, value: 0 } }),
    ]);
  });

  it("words and seeds a one-jack model without mentioning two jacks", async () => {
    render(Settings, { device: {}, hardware: { ...CAPTAIN_10, model: "one_jack", expression_jacks: 1 } });
    expect(screen.getByText(/One expression jack\./)).toBeInTheDocument();
    expect(screen.queryByText(/Two expression jacks/)).not.toBeInTheDocument();
    expect(jackLabels()).toEqual(["EXP 1"]);
    const saved = await save();
    expect(saved.expression).toEqual([expect.objectContaining({ jack: 1, enabled: false })]);
  });

  it("reseeds the jacks when the reported model changes after the config", async () => {
    const view = render(Settings, { device: { custom: { keep: 42 } } });
    expect(jackLabels()).toEqual(["EXP 1", "EXP 2"]);
    await view.rerender({ hardware: MINI_6 });
    expect(screen.queryByRole("heading", { name: "Expression pedals" })).not.toBeInTheDocument();
    // The editor's seeded defaults are not user data, so they never linger as orphans.
    expect(screen.queryByText(/not on this pedal/i)).not.toBeInTheDocument();
    await view.rerender({ hardware: CAPTAIN_10 });
    await waitFor(() => expect(jackLabels()).toEqual(["EXP 1", "EXP 2"]));
    await view.rerender({ hardware: MINI_6 });
    expect(screen.queryByRole("heading", { name: "Expression pedals" })).not.toBeInTheDocument();
    const saved = await save();
    expect(saved).not.toHaveProperty("expression");
  });

  it("backfills a partial jack entry kept verbatim on a Mini 6 once the model has that jack", async () => {
    const partial = { jack: 1, calibration: { min: 0, max: 1023 } };
    const view = render(Settings, { device: { expression: [partial] }, hardware: MINI_6 });
    expect(group("Expression pedals not on this pedal").getByText("EXP 1")).toBeInTheDocument();
    await view.rerender({ hardware: CAPTAIN_10 });
    expect(jackLabels()).toEqual(["EXP 1"]);
    expect(screen.getByLabelText("Sends")).toHaveValue("cc");
    expect(screen.queryByText(/not on this pedal/i)).not.toBeInTheDocument();
    const saved = await save();
    expect(saved.expression).toEqual([{ jack: 1, enabled: false, invert: false, calibration: { min: 0, max: 1023 },
      curve: "linear", message: { type: "cc", channel: 1, cc: 11, value: 0 } }]);
  });
});

describe("Settings keys for switches and jacks the pedal lacks", () => {
  it("lists them as not on this pedal and saves them untouched", async () => {
    const device = tenSwitchConfig();
    const before = structuredClone(device);
    render(Settings, { device, hardware: MINI_6 });

    const up = screen.getByLabelText(BANK_SELECTS[0]) as HTMLSelectElement;
    await waitFor(() => expect(up).toHaveValue("up"));
    expect(up.selectedOptions[0]).toHaveTextContent("up (not on this pedal)");
    expect(screen.getByLabelText(BANK_SELECTS[1])).toHaveValue("A");

    const longPress = group("Long-press actions not on this pedal");
    expect(longPress.getAllByRole("listitem")).toHaveLength(2);
    expect(longPress.getByText("Bank up")).toBeInTheDocument();
    expect(longPress.getByText("cc")).toBeInTheDocument();
    expect(longPress.getByRole("button", { name: "Remove up" })).toBeInTheDocument();
    expect(longPress.getByRole("button", { name: "Remove down" })).toBeInTheDocument();

    const nav = group("Preset navigation not on this pedal");
    expect(nav.getAllByRole("listitem")).toHaveLength(1);
    expect(nav.getByText("slot 4")).toBeInTheDocument();
    expect(nav.getByRole("button", { name: "Remove D" })).toBeInTheDocument();

    // The jacks are listed for removal, but nothing offers to calibrate them.
    expect(screen.getByRole("heading", { name: "Expression pedals" })).toBeInTheDocument();
    const exp = group("Expression pedals not on this pedal");
    expect(exp.getByText("EXP 1")).toBeInTheDocument();
    expect(exp.getByText("enabled, CC 11")).toBeInTheDocument();
    expect(exp.getByText("EXP 2")).toBeInTheDocument();
    expect(screen.queryByText(/expression jacks?\./)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Sends")).not.toBeInTheDocument();

    const saved = await save();
    expect(saved.long_press_actions).toEqual(before.long_press_actions);
    expect(saved.preset_navigation).toEqual(before.preset_navigation);
    expect(saved.expression).toEqual(before.expression);
    expect(device).toEqual(before);
    expect(commands.getStats).not.toHaveBeenCalled();
  });

  it("removes each one only when asked", async () => {
    render(Settings, { device: tenSwitchConfig(), hardware: MINI_6 });

    await fireEvent.click(group("Long-press actions not on this pedal").getByRole("button", { name: "Remove up" }));
    expect(screen.getByLabelText(BANK_SELECTS[0])).toHaveValue("");
    expect(switchOptions(BANK_SELECTS[0])).toEqual(["1", "2", "3", "A", "B", "C"]);
    expect(group("Long-press actions not on this pedal").getAllByRole("listitem")).toHaveLength(1);

    await fireEvent.click(group("Preset navigation not on this pedal").getByRole("button", { name: "Remove D" }));
    expect(screen.queryByRole("group", { name: "Preset navigation not on this pedal" })).not.toBeInTheDocument();

    await fireEvent.click(group("Expression pedals not on this pedal").getByRole("button", { name: "Remove EXP 2" }));
    expect(group("Expression pedals not on this pedal").getAllByRole("listitem")).toHaveLength(1);

    const saved = await save();
    expect(saved.long_press_actions).toEqual({ A: [bankDown], down: [holdCc] });
    expect(saved.preset_navigation).toEqual({ switches: { A: 1 }, bank_colors: { "1": "#3a8eff" } });
    expect(saved.expression).toEqual([jack(1)]);
  });

  it("hides the Expression section once the last jack entry is removed", async () => {
    render(Settings, { device: { expression: [jack(1)] }, hardware: MINI_6 });
    await fireEvent.click(group("Expression pedals not on this pedal").getByRole("button", { name: "Remove EXP 1" }));
    expect(screen.queryByRole("heading", { name: "Expression pedals" })).not.toBeInTheDocument();
    expect((await save()).expression).toEqual([]);
  });

  it("shows the bank switch that fires when a missing switch carries the same action", async () => {
    const device = { long_press_actions: { up: [bankUp], B: [bankUp] } };
    const view = render(Settings, { device, hardware: MINI_6 });
    await waitFor(() => expect(screen.getByLabelText(BANK_SELECTS[0])).toHaveValue("B"));
    expect(switchOptions(BANK_SELECTS[0])).toEqual(["1", "2", "3", "A", "B", "C"]);
    expect(group("Long-press actions not on this pedal").getByText("Bank up")).toBeInTheDocument();
    // The 10-switch model has both switches, so the first one still wins.
    await view.rerender({ hardware: CAPTAIN_10 });
    await waitFor(() => expect(screen.getByLabelText(BANK_SELECTS[0])).toHaveValue("up"));
    expect(screen.queryByText(/not on this pedal/i)).not.toBeInTheDocument();
  });

  it("moves an orphaned bank-up action to a switch the pedal has", async () => {
    render(Settings, { device: tenSwitchConfig(), hardware: MINI_6 });
    const up = screen.getByLabelText(BANK_SELECTS[0]);
    await fireEvent.change(up, { target: { value: "B" } });
    expect(up).toHaveValue("B");
    expect(switchOptions(BANK_SELECTS[0])).toEqual(["1", "2", "3", "A", "B", "C"]);
    expect(screen.queryByRole("button", { name: "Remove up" })).not.toBeInTheDocument();
    const saved = await save();
    expect(saved.long_press_actions).toEqual({ A: [bankDown], down: [holdCc], B: [bankUp] });
  });
});

describe("ExpressionPedals jacks", () => {
  it("renders only the jacks the pedal has and leaves the other entries untouched", () => {
    const expression = reactiveFixture<ExpressionConfig[]>([jack(1), jack(2)]);
    const before = JSON.parse(JSON.stringify(expression));
    render(ExpressionPedals, { expression, manifest: null, jacks: [2], connected: false });
    expect(jackLabels()).toEqual(["EXP 2"]);
    expect(screen.getAllByLabelText("Sends")).toHaveLength(1);
    expect(JSON.parse(JSON.stringify(expression))).toEqual(before);
  });

  it("does not poll STATS for a pedal without jacks", async () => {
    render(ExpressionPedals, { expression: reactiveFixture<ExpressionConfig[]>([jack(1)]), manifest: null, jacks: [], connected: true });
    await tick();
    expect(jackLabels()).toEqual([]);
    expect(commands.getStats).not.toHaveBeenCalled();
  });

  it("defaults to the 10-switch jacks and polls while connected", async () => {
    render(ExpressionPedals, { expression: reactiveFixture<ExpressionConfig[]>([jack(1), jack(2), jack(3)]), manifest: null, connected: true });
    expect(jackLabels()).toEqual(["EXP 1", "EXP 2"]);
    await waitFor(() => expect(commands.getStats).toHaveBeenCalled());
  });
});
