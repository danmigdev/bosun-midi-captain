import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import StartupLogo from "../src/components/StartupLogo.svelte";

const mocks = vi.hoisted(() => ({
  getDeviceInfo: vi.fn(async () => {}),
  prepareLogo: vi.fn(), uploadPedalLogo: vi.fn(), deletePedalLogo: vi.fn(),
  hubLogoInfo: vi.fn(), uploadHubLogo: vi.fn(), resetHubLogo: vi.fn(),
}));
vi.mock("../src/lib/protocol", () => ({ cmd: { getDeviceInfo: mocks.getDeviceInfo } }));
vi.mock("../src/lib/boot-logo", async (importOriginal) => ({
  ...await importOriginal<typeof import("../src/lib/boot-logo")>(),
  prepareLogo: mocks.prepareLogo, uploadPedalLogo: mocks.uploadPedalLogo, deletePedalLogo: mocks.deletePedalLogo,
  hubLogoInfo: mocks.hubLogoInfo, uploadHubLogo: mocks.uploadHubLogo, resetHubLogo: mocks.resetHubLogo,
}));

const prepared = {
  tft: new Uint8Array(96000), piPicture: new Uint8Array(266416), piPng: new Uint8Array([1, 2, 3]),
  previewUrl: "blob:logo",
};

beforeEach(() => {
  for (const mock of Object.values(mocks)) mock.mockReset();
  mocks.getDeviceInfo.mockResolvedValue(undefined);
  mocks.prepareLogo.mockResolvedValue(prepared);
  mocks.hubLogoInfo.mockResolvedValue({ supported: true, custom: false });
  URL.revokeObjectURL = vi.fn();
});
afterEach(() => cleanup());

async function pickFile(container: HTMLElement) {
  const input = container.querySelector('input[type="file"]') as HTMLInputElement;
  Object.defineProperty(input, "files", { value: [new File(["x"], "logo.png", { type: "image/png" })], configurable: true });
  await fireEvent.change(input);
  return screen.findByRole("button", { name: "Use this logo" });
}

describe("StartupLogo", () => {
  it("asks for a firmware update when the pedal has no startup logo", () => {
    render(StartupLogo, { connected: true, pedalLogo: null, networkSession: false });
    expect(screen.getByText(/Update the pedal firmware/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Choose image/ })).toBeNull();
  });

  it("uploads a picked logo to the pedal over USB without contacting a hub", async () => {
    const { container } = render(StartupLogo, { connected: true, pedalLogo: "default", networkSession: false, version: "0.8.5" });
    expect((screen.getByRole("button", { name: "Use Bosun logo" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole("img", { name: /Raspberry Pi/ })).toBeNull();
    expect(screen.getByText("v0.8.5")).toBeTruthy();
    await fireEvent.click(await pickFile(container));
    await waitFor(() => expect(screen.getByText(/Startup logo saved on the pedal\./)).toBeTruthy());
    expect(mocks.uploadPedalLogo).toHaveBeenCalledWith(prepared.tft, expect.any(Function));
    expect(mocks.getDeviceInfo).toHaveBeenCalled();
    expect(mocks.hubLogoInfo).not.toHaveBeenCalled();
    expect(mocks.uploadHubLogo).not.toHaveBeenCalled();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:logo");
  });

  it("sends the logo to the pedal and the Raspberry Pi on a network session", async () => {
    mocks.uploadHubLogo.mockResolvedValue({ supported: true, custom: true, early: "applied" });
    const { container } = render(StartupLogo, { connected: true, pedalLogo: "default", networkSession: true });
    await screen.findByRole("img", { name: /Raspberry Pi/ });
    mocks.hubLogoInfo.mockResolvedValue({ supported: true, custom: true, early: "applied", png: "AAAA" });
    await fireEvent.click(await pickFile(container));
    await waitFor(() => expect(screen.getByText(/saved on the pedal and Raspberry Pi/)).toBeTruthy());
    expect(mocks.uploadHubLogo).toHaveBeenCalledWith(prepared.piPng, prepared.piPicture, expect.any(Function));
    expect(screen.getByText(/Shown from early in the Raspberry Pi startup/)).toBeTruthy();
  });

  it("restores the Bosun logo on both targets", async () => {
    mocks.hubLogoInfo.mockResolvedValue({ supported: true, custom: true, early: "unavailable", png: "AAAA" });
    mocks.resetHubLogo.mockResolvedValue({ supported: true, custom: false, early: "applied" });
    render(StartupLogo, { connected: true, pedalLogo: "custom", networkSession: true });
    await screen.findByText(/no early startup picture installed/);
    mocks.hubLogoInfo.mockResolvedValue({ supported: true, custom: false, early: "applied" });
    await fireEvent.click(screen.getByRole("button", { name: "Use Bosun logo" }));
    await waitFor(() => expect(screen.getByText(/Bosun logo restored on the pedal and Raspberry Pi/)).toBeTruthy());
    expect(mocks.deletePedalLogo).toHaveBeenCalled();
    expect(mocks.resetHubLogo).toHaveBeenCalled();
  });

  it("keeps the pedal result when the Raspberry Pi step fails", async () => {
    mocks.uploadHubLogo.mockRejectedValue(new Error("error: Insufficient disk space"));
    const { container } = render(StartupLogo, { connected: true, pedalLogo: "default", networkSession: true });
    await screen.findByRole("img", { name: /Raspberry Pi/ });
    await fireEvent.click(await pickFile(container));
    await waitFor(() => expect(screen.getByText(/Saved on the pedal, but the next step failed/)).toBeTruthy());
  });

  it("explains an older Raspberry Pi and still offers the pedal", async () => {
    mocks.hubLogoInfo.mockResolvedValue({ supported: false, custom: false });
    render(StartupLogo, { connected: true, pedalLogo: "default", networkSession: true });
    await screen.findByText(/Update the Raspberry Pi software/);
    expect(screen.getByRole("button", { name: /Choose image/ })).toBeTruthy();
  });
});
