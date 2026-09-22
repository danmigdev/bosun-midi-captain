import { beforeEach, describe, expect, it, vi } from "vitest";
import { enterBootloader } from "../src/lib/bootloader";
import { supportsBootloader } from "../src/lib/firmware-capabilities";
import { sendAndAwait } from "../src/lib/protocol";

vi.mock("../src/lib/protocol", () => ({ sendAndAwait: vi.fn() }));
const send = vi.mocked(sendAndAwait);
beforeEach(() => send.mockReset());

describe("manual bootloader entry", () => {
  it("requires an explicit native bootloader capability", () => {
    expect(supportsBootloader(null)).toBe(false);
    expect(supportsBootloader({ fw: "0.6.10-native" })).toBe(false);
    expect(supportsBootloader({ fw: "legacy", reboot_modes: ["bootloader"] })).toBe(false);
    expect(supportsBootloader({ fw: "0.6.10-native", reboot_modes: ["normal", "bootloader"] })).toBe(true);
  });
  it("checks drafts before requesting the ROM bootloader", async () => {
    send.mockResolvedValueOnce({ type: "DIRTY", patches: [] }).mockResolvedValueOnce({ type: "ACK" });
    await enterBootloader();
    expect(send.mock.calls).toEqual([[{ type: "GET_DIRTY" }], [{ type: "REBOOT", mode: "bootloader" }]]);
  });
  it("leaves unsaved patches untouched", async () => {
    send.mockResolvedValueOnce({ type: "DIRTY", patches: [{ bank: 0, slot: 1 }] });
    await expect(enterBootloader()).rejects.toThrow("Save or discard");
    expect(send).toHaveBeenCalledTimes(1);
  });
  it("refuses to reboot if the draft check is unexpected", async () => {
    send.mockResolvedValueOnce({ type: "ACK" });
    await expect(enterBootloader()).rejects.toThrow("Could not check");
    expect(send).toHaveBeenCalledTimes(1);
  });
  it("does not retry when the reboot acknowledgment is lost", async () => {
    send.mockResolvedValueOnce({ type: "DIRTY", patches: [] }).mockRejectedValueOnce(new Error("Disconnected"));
    await expect(enterBootloader()).rejects.toThrow("Check for the RPI-RP2 drive");
    expect(send).toHaveBeenCalledTimes(2);
  });
});
