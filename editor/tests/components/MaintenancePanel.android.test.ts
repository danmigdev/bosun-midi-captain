// The Android app offers no Reboot action and no "Open folder" link: neither
// can do anything there. Desktop keeps both.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/svelte";

const platform = vi.hoisted(() => ({ IS_ANDROID: false }));
vi.mock("../../src/lib/platform", () => platform);
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("../../src/lib/protocol", () => ({
  cmd: { getStats: vi.fn(() => new Promise(() => {})), reboot: vi.fn(), setHardware: vi.fn() },
  waitForReboot: vi.fn(async () => true),
  sendAndAwait: vi.fn(),
}));

import MaintenancePanel from "../../src/components/MaintenancePanel.svelte";

const firmwareInfo = { fw: "0.8.1-native" } as never;

describe("MaintenancePanel platform actions", () => {
  beforeEach(() => { platform.IS_ANDROID = false; });

  it("shows Reboot on desktop", () => {
    render(MaintenancePanel, { props: { connected: true, firmwareInfo } });
    expect(screen.getByRole("button", { name: "Reboot pedal" })).toBeInTheDocument();
  });

  it("has no Reboot section on Android", () => {
    platform.IS_ANDROID = true;
    render(MaintenancePanel, { props: { connected: true, firmwareInfo } });
    expect(screen.queryByRole("button", { name: "Reboot pedal" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Reboot" })).not.toBeInTheDocument();
  });
});
