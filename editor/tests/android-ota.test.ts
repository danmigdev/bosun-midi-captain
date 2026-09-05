import { render, screen } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";

vi.mock("../src/lib/platform", () => ({
  IS_ANDROID: true,
  IS_TAURI: true,
}));

import MaintenancePanel from "../src/components/MaintenancePanel.svelte";
import { cmd } from "../src/lib/protocol";

describe("Android OTA safety", () => {
  it("hides the unsupported firmware updater from Maintenance", () => {
    render(MaintenancePanel, { connected: false, activeProfile: null });

    expect(screen.queryByRole("heading", { name: "Update firmware (OTA)" }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Update from bundled" }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "From folder…" }))
      .not.toBeInTheDocument();
  });

  it("rejects every PUT_FILE command instead of returning a fake ACK", async () => {
    const unsupported = /OTA is not supported by the Android serial backend/;
    await expect(cmd.putFileBegin("/code.py", 3)).rejects.toThrow(unsupported);
    await expect(cmd.putFileChunk("/code.py", "YWJj", 0)).rejects.toThrow(unsupported);
    await expect(cmd.putFileEnd("/code.py")).rejects.toThrow(unsupported);
  });
});
