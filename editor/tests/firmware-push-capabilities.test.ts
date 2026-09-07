import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  invoke: vi.fn(),
  info: vi.fn(),
  backup: vi.fn(),
  begin: vi.fn(),
  chunk: vi.fn(),
  end: vi.fn(),
  reboot: vi.fn(),
  waitForReboot: vi.fn(),
}));

vi.mock("@tauri-apps/api/core", () => ({ invoke: mocks.invoke }));
vi.mock("../src/lib/config-backup", () => ({ backupAllProfiles: mocks.backup }));
vi.mock("../src/lib/protocol", async importOriginal => ({
  ...await importOriginal<typeof import("../src/lib/protocol")>(),
  sendAndAwait: mocks.info,
  waitForReboot: mocks.waitForReboot,
  cmd: {
    putFileBegin: mocks.begin,
    putFileChunk: mocks.chunk,
    putFileEnd: mocks.end,
    reboot: mocks.reboot,
  },
}));

import { pushFirmware, pushFirmwareFile, type FirmwarePushState } from "../src/lib/firmware-push";
import { FirmwareOtaUnavailableError } from "../src/lib/firmware-capabilities";

const legacy = {
  type: "DEVICE_INFO",
  fw: "0.6.3",
  device: "MIDI Captain",
  current: { bank: 1, slot: 1 },
};
const file = { rel: "code.py", dst: "/code.py", size: 3 };

beforeEach(() => {
  vi.resetAllMocks();
  mocks.info.mockResolvedValue({ ...legacy });
  mocks.backup.mockResolvedValue("C:/backups/pre-update");
  mocks.begin.mockResolvedValue({ type: "ACK" });
  mocks.chunk.mockResolvedValue({ type: "ACK" });
  mocks.end.mockResolvedValue({ type: "ACK" });
  mocks.reboot.mockResolvedValue(undefined);
  mocks.waitForReboot.mockResolvedValue(true);
  mocks.invoke.mockImplementation(async (command: string) => {
    if (command === "list_firmware_files" || command === "list_firmware_files_at") return [file];
    if (command === "read_firmware_file_b64" || command === "read_firmware_file_at_b64") return "YWJj";
    throw new Error(`Unexpected invoke: ${command}`);
  });
});

function expectNoDeviceWrites(): void {
  expect(mocks.begin).not.toHaveBeenCalled();
  expect(mocks.chunk).not.toHaveBeenCalled();
  expect(mocks.end).not.toHaveBeenCalled();
  expect(mocks.reboot).not.toHaveBeenCalled();
}

describe("firmware update capability preflight", () => {
  it.each([
    ["bundled", undefined, { fw: "0.1.0-native", native_experimental: true, firmware_ota: false }],
    ["folder", "C:/firmware-folder", { fw: "0.1.0-native", native_experimental: true, firmware_ota: false }],
    ["extracted ZIP", "C:/temp/extracted-firmware", { fw: "0.1.0-native", native_experimental: true, firmware_ota: false }],
    ["native marker", undefined, { fw: "1.0.0", native_experimental: true, firmware_ota: true }],
    ["native version without flags", undefined, { fw: "0.1.0-native" }],
    ["explicitly unsupported OTA", undefined, { fw: "0.6.3", firmware_ota: false }],
  ])("blocks %s before backup, local file access or upload", async (_name, source, identity) => {
    mocks.info.mockResolvedValue({ ...legacy, ...identity });
    const states: FirmwarePushState[] = [];

    await expect(pushFirmware(state => states.push(state), { source }))
      .rejects.toBeInstanceOf(FirmwareOtaUnavailableError);

    expect(mocks.info).toHaveBeenCalledWith({ type: "GET_DEVICE_INFO" }, 8000);
    expect(mocks.backup).not.toHaveBeenCalled();
    expect(mocks.invoke).not.toHaveBeenCalled();
    expectNoDeviceWrites();
    expect(states.at(-1)?.phase).toBe("error");
  });

  it("rejects an unexpected correlated response instead of treating it as legacy firmware", async () => {
    mocks.info.mockResolvedValue({ type: "ACK", fw: "0.6.3" });

    await expect(pushFirmware(() => {})).rejects.toBeInstanceOf(FirmwareOtaUnavailableError);

    expect(mocks.backup).not.toHaveBeenCalled();
    expect(mocks.invoke).not.toHaveBeenCalled();
    expectNoDeviceWrites();
  });

  it.each([undefined, "C:/firmware-folder"])("preserves legacy CircuitPython updates from %s", async source => {
    const states: FirmwarePushState[] = [];

    await pushFirmware(state => states.push(state), { source });

    expect(mocks.info).toHaveBeenCalledTimes(1);
    expect(mocks.backup).toHaveBeenCalledTimes(1);
    expect(mocks.info.mock.invocationCallOrder[0]).toBeLessThan(mocks.backup.mock.invocationCallOrder[0]);
    expect(mocks.invoke).toHaveBeenCalledWith(
      ...(source ? ["list_firmware_files_at", { root: source }] : ["list_firmware_files"]),
    );
    expect(mocks.begin).toHaveBeenCalledWith("/code.py", 3);
    expect(mocks.chunk).toHaveBeenCalledWith("/code.py", "YWJj", 0);
    expect(mocks.end).toHaveBeenCalledWith("/code.py");
    expect(mocks.reboot).toHaveBeenCalledTimes(1);
    expect(mocks.waitForReboot).toHaveBeenCalledWith(20000);
    expect(states.at(-1)?.phase).toBe("done");
  });
});

describe("firmware update session revocation", () => {
  it("does not start preflight for a disconnected or superseded session", async () => {
    await expect(pushFirmware(() => {}, { isUpdateAllowed: () => false }))
      .rejects.toBeInstanceOf(FirmwareOtaUnavailableError);

    expect(mocks.info).not.toHaveBeenCalled();
    expect(mocks.backup).not.toHaveBeenCalled();
    expect(mocks.invoke).not.toHaveBeenCalled();
    expectNoDeviceWrites();
  });

  it("rechecks the session after an in-flight capability response", async () => {
    let allowed = true;
    mocks.info.mockImplementation(async () => { allowed = false; return { ...legacy }; });

    await expect(pushFirmware(() => {}, { isUpdateAllowed: () => allowed }))
      .rejects.toBeInstanceOf(FirmwareOtaUnavailableError);

    expect(mocks.backup).not.toHaveBeenCalled();
    expect(mocks.invoke).not.toHaveBeenCalled();
    expectNoDeviceWrites();
  });

  it.each([false, true])("stops after a backup on a changed session (backup fails: %s)", async failBackup => {
    let allowed = true;
    mocks.backup.mockImplementation(async () => {
      allowed = false;
      if (failBackup) throw new Error("disconnected during backup");
      return "C:/backups/pre-update";
    });

    await expect(pushFirmware(() => {}, { isUpdateAllowed: () => allowed }))
      .rejects.toBeInstanceOf(FirmwareOtaUnavailableError);

    expect(mocks.invoke).not.toHaveBeenCalled();
    expectNoDeviceWrites();
  });

  it.each(["listing", "reading"])("stops when the session changes during local file %s", async step => {
    let allowed = true;
    mocks.invoke.mockImplementation(async (command: string) => {
      if (command === "list_firmware_files") {
        if (step === "listing") allowed = false;
        return [file];
      }
      allowed = false;
      return "YWJj";
    });

    await expect(pushFirmware(() => {}, { isUpdateAllowed: () => allowed }))
      .rejects.toBeInstanceOf(FirmwareOtaUnavailableError);

    expect(mocks.invoke).toHaveBeenCalledTimes(step === "listing" ? 1 : 2);
    expectNoDeviceWrites();
  });

  it.each(["begin", "chunk", "end"] as const)("never resumes or retries after revocation during %s", async step => {
    let allowed = true;
    mocks[step].mockImplementation(async () => { allowed = false; return { type: "ACK" }; });

    await expect(pushFirmware(() => {}, { isUpdateAllowed: () => allowed }))
      .rejects.toBeInstanceOf(FirmwareOtaUnavailableError);

    expect(mocks.begin).toHaveBeenCalledTimes(1);
    expect(mocks.chunk).toHaveBeenCalledTimes(step === "begin" ? 0 : 1);
    expect(mocks.end).toHaveBeenCalledTimes(step === "end" ? 1 : 0);
    expect(mocks.reboot).not.toHaveBeenCalled();
  });

  it("does not send the next chunk after the session changes", async () => {
    let allowed = true;
    mocks.chunk.mockImplementation(async () => { allowed = false; return { type: "ACK" }; });

    await expect(pushFirmwareFile("/code.py", btoa("a".repeat(200)), {
      isUpdateAllowed: () => allowed,
      retryDelayMs: 0,
    })).rejects.toBeInstanceOf(FirmwareOtaUnavailableError);

    expect(mocks.begin).toHaveBeenCalledTimes(1);
    expect(mocks.chunk).toHaveBeenCalledTimes(1);
    expect(mocks.end).not.toHaveBeenCalled();
  });

  it("does not restart a transaction if permission is revoked during retry scheduling", async () => {
    let allowed = true;
    mocks.begin.mockRejectedValue(new Error("temporary upload error"));

    await expect(pushFirmwareFile("/code.py", "YWJj", {
      isUpdateAllowed: () => allowed,
      onWarning: () => { allowed = false; },
      retryDelayMs: 0,
    })).rejects.toBeInstanceOf(FirmwareOtaUnavailableError);

    expect(mocks.begin).toHaveBeenCalledTimes(1);
    expect(mocks.chunk).not.toHaveBeenCalled();
    expect(mocks.end).not.toHaveBeenCalled();
  });

  it("checks permission again before rebooting after the last completed file", async () => {
    let allowed = true;

    await expect(pushFirmware(state => {
      if (state.phase === "rebooting") allowed = false;
    }, { isUpdateAllowed: () => allowed })).rejects.toBeInstanceOf(FirmwareOtaUnavailableError);

    expect(mocks.end).toHaveBeenCalledTimes(1);
    expect(mocks.reboot).not.toHaveBeenCalled();
    expect(mocks.waitForReboot).not.toHaveBeenCalled();
  });
});
