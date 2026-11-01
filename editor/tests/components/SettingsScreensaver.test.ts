import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import Settings from "../../src/components/Settings.svelte";
import { FirmwareCommandTimeoutError } from "../../src/lib/protocol";

const commands = vi.hoisted(() => ({
  putGlobal: vi.fn(async (_device: Record<string, unknown>) => ({})),
  getGlobal: vi.fn(async () => ({})),
  getStats: vi.fn(async () => ({ expression: [] })),
  startScreensaver: vi.fn(async (_effect?: string) => {}),
}));
vi.mock("../../src/lib/protocol", async (original) => ({
  ...await original<typeof import("../../src/lib/protocol")>(), cmd: commands,
}));
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn(async () => () => {}) }));
beforeEach(() => {
  vi.clearAllMocks();
  commands.startScreensaver.mockImplementation(async () => {});
});

const MINUTES = "Screensaver after (minutes)";
const EFFECT = "Screensaver effect";

function minutes() { return screen.getByLabelText(MINUTES) as HTMLInputElement; }
function effect() { return screen.getByLabelText(EFFECT) as HTMLSelectElement; }
function showNow() { return screen.getByRole("button", { name: "Show now" }) as HTMLButtonElement; }
async function save(): Promise<Record<string, any>> {
  await fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
  await waitFor(() => expect(commands.putGlobal).toHaveBeenCalledOnce());
  return commands.putGlobal.mock.calls[0][0];
}

describe("Settings screensaver fields", () => {
  it("are hidden and never saved when the firmware does not report a screensaver", async () => {
    render(Settings, { device: { tft: { brightness: 50, rotation: 0 } } });
    expect(screen.queryByLabelText(MINUTES)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(EFFECT)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show now" })).not.toBeInTheDocument();
    const saved = await save();
    expect(saved.tft).toEqual({ brightness: 50, rotation: 0 });
  });

  it("default to off and Mix when the keys are missing", async () => {
    render(Settings, { device: {}, screensaver: true });
    expect(minutes()).toHaveValue(0);
    expect(minutes()).toHaveAttribute("min", "0");
    expect(minutes()).toHaveAttribute("max", "120");
    expect(effect()).toHaveValue("mix");
    expect([...effect().options].map(o => [o.value, o.textContent])).toEqual([
      ["bounce", "Bounce"], ["breathe", "Breathe"], ["drift", "Drift"], ["mix", "Mix"],
    ]);
    expect(screen.getByText(/0 turns it off\./)).toBeInTheDocument();
    const saved = await save();
    expect(saved.tft).toMatchObject({ screensaver_minutes: 0, screensaver_effect: "mix" });
  });

  it("show the stored values and read an unknown effect as Mix", async () => {
    const view = render(Settings, { device: { tft: { screensaver_minutes: 15, screensaver_effect: "drift" } }, screensaver: true });
    expect(minutes()).toHaveValue(15);
    expect(effect()).toHaveValue("drift");
    // device.json is not validated: a future or hand-edited value can arrive.
    const stored: Record<string, any> = { tft: { screensaver_minutes: 5, screensaver_effect: "sparkle" } };
    await view.rerender({ device: stored });
    expect(minutes()).toHaveValue(5);
    expect(effect()).toHaveValue("mix");
  });

  it.each([
    ["above the range", "500", 120],
    ["below the range", "-3", 0],
    ["a fraction", "7.6", 8],
    ["a cleared field", "", 0],
  ])("clamp %s on save", async (_case, typed, expected) => {
    render(Settings, { device: {}, screensaver: true });
    await fireEvent.input(minutes(), { target: { value: typed } });
    const saved = await save();
    expect(saved.tft.screensaver_minutes).toBe(expected);
  });

  it("save the chosen effect and minutes", async () => {
    render(Settings, { device: { tft: { brightness: 70, rotation: 180 } }, screensaver: true });
    await fireEvent.input(minutes(), { target: { value: "30" } });
    await fireEvent.change(effect(), { target: { value: "bounce" } });
    const saved = await save();
    expect(saved.tft).toEqual({ brightness: 70, rotation: 180, screensaver_minutes: 30, screensaver_effect: "bounce" });
  });
});

describe("Settings screensaver fingerprint", () => {
  it("reseeds the defaults when the firmware reports the screensaver after the config", async () => {
    const view = render(Settings, { device: { tft: { brightness: 50 } } });
    expect(screen.queryByLabelText(MINUTES)).not.toBeInTheDocument();
    await view.rerender({ screensaver: true });
    expect(minutes()).toHaveValue(0);
    expect(effect()).toHaveValue("mix");
    await view.rerender({ screensaver: false });
    expect(screen.queryByLabelText(MINUTES)).not.toBeInTheDocument();
    const saved = await save();
    expect(saved.tft).toEqual({ brightness: 50 });
  });

  it("keeps unsaved edits until the stored config itself changes", async () => {
    const device = { tft: { screensaver_minutes: 10, screensaver_effect: "breathe" as const } };
    const view = render(Settings, { device, screensaver: true });
    await fireEvent.input(minutes(), { target: { value: "45" } });
    await fireEvent.change(effect(), { target: { value: "drift" } });
    // The same config again (a Reload or another prop update) is not a change.
    await view.rerender({ device: structuredClone(device), connected: false });
    expect(minutes()).toHaveValue(45);
    expect(effect()).toHaveValue("drift");
    // A different stored config (saved elsewhere) replaces the working copy.
    await view.rerender({ device: { tft: { screensaver_minutes: 20, screensaver_effect: "bounce" } } });
    expect(minutes()).toHaveValue(20);
    expect(effect()).toHaveValue("bounce");
  });
});

describe("Settings screensaver Show now", () => {
  it("sends the selected effect, even unsaved and with the screensaver off", async () => {
    render(Settings, { device: {}, screensaver: true });
    await fireEvent.change(effect(), { target: { value: "breathe" } });
    await fireEvent.click(showNow());
    expect(commands.startScreensaver).toHaveBeenCalledExactlyOnceWith("breathe");
    expect(commands.putGlobal).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("previews Mix by default", async () => {
    render(Settings, { device: {}, screensaver: true });
    await fireEvent.click(showNow());
    expect(commands.startScreensaver).toHaveBeenCalledExactlyOnceWith("mix");
  });

  it("is disabled while disconnected", () => {
    render(Settings, { device: {}, screensaver: true, connected: false });
    expect(showNow()).toBeDisabled();
  });

  it("is disabled while a request is in flight", async () => {
    let finish!: () => void;
    commands.startScreensaver.mockImplementation(() => new Promise<void>(resolve => { finish = resolve; }));
    render(Settings, { device: {}, screensaver: true });
    await fireEvent.click(showNow());
    expect(showNow()).toBeDisabled();
    finish();
    await waitFor(() => expect(showNow()).toBeEnabled());
  });

  it("explains a refusal while an update runs", async () => {
    commands.startScreensaver.mockRejectedValue(new Error("error: update_lock"));
    render(Settings, { device: {}, screensaver: true });
    await fireEvent.click(showNow());
    expect(await screen.findByRole("alert")).toHaveTextContent("An update is running. Try again when it has finished.");
  });

  it.each([
    ["an ERROR", new Error("error: storage_error")],
    ["a timeout", new FirmwareCommandTimeoutError("SCREENSAVER_START", "7")],
  ])("reports %s as a generic failure and clears it on the next success", async (_case, failure) => {
    commands.startScreensaver.mockRejectedValueOnce(failure);
    render(Settings, { device: {}, screensaver: true });
    await fireEvent.click(showNow());
    expect(await screen.findByRole("alert")).toHaveTextContent("The pedal could not start the screensaver.");
    await fireEvent.click(showNow());
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(commands.startScreensaver).toHaveBeenCalledTimes(2);
  });
});
