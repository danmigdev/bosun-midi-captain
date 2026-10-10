import { describe, expect, it, vi } from "vitest";
import { backupHardwareNotice, validateBackup, type ConfigBackup } from "../src/lib/config-backup";
import { CAPTAIN_10, MINI_6 } from "../src/lib/hardware";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn(async () => () => {}) }));

function backup(hardware?: ConfigBackup["hardware"]): ConfigBackup {
  return validateBackup({
    format: "bosun-config-backup", version: 2, generated_at: "2026-10-10T12:00:00Z",
    device: {}, patches: [{ bank: 1, slot: 1, patch: { name: "Clean", bindings: [{ switch: "up", mode: "tap", actions: {} }] } }],
    ...(hardware ? { hardware } : {}),
  });
}

describe("backups across pedal models", () => {
  it("accepts backups with and without the source model", () => {
    expect(backup().hardware).toBeUndefined();
    expect(backup({ model: "mini6", name: "MIDI Captain Mini 6" }).hardware?.model).toBe("mini6");
  });

  it("says nothing when the backup matches the connected model", () => {
    expect(backupHardwareNotice(backup(), CAPTAIN_10)).toBe("");
    expect(backupHardwareNotice(backup({ model: "mini6", name: "MIDI Captain Mini 6" }), MINI_6)).toBe("");
  });

  it("treats backups from earlier editors as 10-switch backups", () => {
    const notice = backupHardwareNotice(backup(), MINI_6);
    expect(notice).toContain("comes from a MIDI Captain.");
    expect(notice).toContain("the MIDI Captain Mini 6 does not have");
  });

  it("warns when restoring a Mini 6 backup on the 10-switch Captain", () => {
    expect(backupHardwareNotice(backup({ model: "mini6", name: "MIDI Captain Mini 6" }), CAPTAIN_10))
      .toContain("comes from a MIDI Captain Mini 6");
  });
});
