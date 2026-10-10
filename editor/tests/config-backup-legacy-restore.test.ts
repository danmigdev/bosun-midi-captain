// Backups written before MIDI Learn was retired carry a `midi_learn` section.
// Restoring one must still write the device and patches, and must not send
// the MIDI Learn command the firmware no longer understands.
import { describe, it, expect, vi, beforeEach } from "vitest";

const mocks = vi.hoisted(() => ({
  sendAndAwait: vi.fn(),
  createProfile: vi.fn(),
}));

vi.mock("../src/lib/protocol", () => ({
  sendAndAwait: mocks.sendAndAwait,
  cmd: { createProfile: mocks.createProfile },
}));

import { importConfig, validateBackup } from "../src/lib/config-backup";

const legacyBackup = {
  format: "bosun-config-backup",
  version: 2,
  generated_at: "2026-06-16T18:35:56.000Z",
  kind: "kemper_player",
  device: { device_name: "MIDI Captain" },
  patches: [{ bank: 1, slot: 1, patch: { name: "Clean", bindings: [] } }],
  midi_learn: { pc_to_patch: [{ channel: 1, bank_msb: 0, pc: 0, captain_patch: "01/01" }] },
};

describe("restoring a backup that still has a midi_learn section", () => {
  beforeEach(() => {
    mocks.sendAndAwait.mockReset();
    mocks.sendAndAwait.mockImplementation(async (message: { type: string }) =>
      message.type === "LIST_PATCHES" ? { type: "PATCH_LIST", patches: [] } : { type: "ACK" });
  });

  it("writes the device and patches and skips the retired table", async () => {
    await importConfig(validateBackup(JSON.parse(JSON.stringify(legacyBackup))));
    const sent = mocks.sendAndAwait.mock.calls.map(([message]) => message.type);
    expect(sent).toEqual(["PUT_GLOBAL", "LIST_PATCHES", "PUT_PATCH"]);
  });

  it("does the same when importing as a new profile", async () => {
    await importConfig(validateBackup(legacyBackup),
      undefined, { asNewProfile: { profile_id: "restored", name: "Restored", kind: "kemper_player" } });
    const sent = mocks.sendAndAwait.mock.calls.map(([message]) => message.type);
    expect(mocks.createProfile).toHaveBeenCalledWith("restored", "Restored", "kemper_player");
    expect(sent).toEqual(["PUT_GLOBAL", "PUT_PATCH"]);
  });
});
