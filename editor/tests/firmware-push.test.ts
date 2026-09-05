import { describe, expect, it, vi } from "vitest";

import {
  decodedBase64Size,
  pushFirmwareFile,
  type FirmwareUploadCommands,
} from "../src/lib/firmware-push";
import {
  FirmwareCommandTimeoutError,
  type FirmwareMessage,
} from "../src/lib/protocol";

const ACK: FirmwareMessage = { type: "ACK" };
const sizeAck = (size: number): FirmwareMessage => ({
  type: "ACK",
  size_check: true,
  size,
});

function bytes(count: number): Uint8Array {
  return Uint8Array.from({ length: count }, (_, i) => (i * 37 + 11) & 0xff);
}

function encode(data: Uint8Array): string {
  let binary = "";
  for (const value of data) binary += String.fromCharCode(value);
  return btoa(binary);
}

function decode(dataB64: string): number[] {
  return Array.from(atob(dataB64), char => char.charCodeAt(0));
}

function commands(overrides: Partial<FirmwareUploadCommands> = {}): FirmwareUploadCommands {
  return {
    putFileBegin: async () => ACK,
    putFileChunk: async () => ACK,
    putFileEnd: async () => ACK,
    ...overrides,
  };
}

describe("decodedBase64Size", () => {
  it("computes exact decoded sizes without allocating a decoded firmware copy", () => {
    expect(decodedBase64Size("")).toBe(0);
    expect(decodedBase64Size("TQ==")).toBe(1);
    expect(decodedBase64Size("TWE=")).toBe(2);
    expect(decodedBase64Size("TWFu")).toBe(3);
    for (const size of [0, 1, 2, 3, 95, 96, 97, 193]) {
      expect(decodedBase64Size(encode(bytes(size)))).toBe(size);
    }
  });

  it("rejects malformed and non-canonical payloads before opening a device file", async () => {
    for (const invalid of ["A", "AA=A", "AA?=", "====", "TR==", "TWF="]) {
      expect(() => decodedBase64Size(invalid)).toThrow(/base64/);
    }

    const begin = vi.fn(async () => ACK);
    await expect(pushFirmwareFile("/bad.mpy", "AA?=", {
      commands: commands({ putFileBegin: begin }),
      retryDelayMs: 0,
    })).rejects.toThrow(/base64/);
    expect(begin).not.toHaveBeenCalled();
  });
});

describe("pushFirmwareFile transactional OTA", () => {
  it("uses post-BOM-normalisation bytes and supports an empty file", async () => {
    const begins: number[] = [];
    const chunks: string[] = [];
    let ends = 0;
    const fake = commands({
      putFileBegin: async (_path, size) => { begins.push(size); return ACK; },
      putFileChunk: async (_path, chunk) => { chunks.push(chunk); return ACK; },
      putFileEnd: async () => { ends += 1; return ACK; },
    });

    // Tauri lists the raw BOM-prefixed file as six bytes, then deliberately
    // strips the three-byte BOM before returning this base64 payload.  The
    // transaction API accepts only the payload, so BEGIN must advertise 3.
    await pushFirmwareFile("/bom.py", "YWJj", { commands: fake, retryDelayMs: 0 });
    await pushFirmwareFile("/empty.py", "", { commands: fake, retryDelayMs: 0 });

    expect(begins).toEqual([3, 0]);
    expect(chunks).toEqual(["YWJj"]);
    expect(ends).toBe(2);
  });

  it("advertises the decoded size and monotonically increasing raw-byte offsets", async () => {
    const payload = bytes(193);
    const dataB64 = encode(payload);
    const begins: Array<{ path: string; size: number }> = [];
    const chunks: Array<{ offset: number; size: number }> = [];

    const result = await pushFirmwareFile("/lib/captain/app.mpy", dataB64, {
      commands: commands({
        putFileBegin: async (path, size) => {
          begins.push({ path, size });
          return ACK;
        },
        putFileChunk: async (_path, chunk, offset) => {
          chunks.push({ offset, size: decode(chunk).length });
          return ACK;
        },
      }),
      retryDelayMs: 0,
    });

    expect(begins).toEqual([{ path: "/lib/captain/app.mpy", size: 193 }]);
    expect(chunks).toEqual([
      { offset: 0, size: 96 },
      { offset: 96, size: 96 },
      { offset: 192, size: 1 },
    ]);
    expect(result).toEqual({ size: 193, attempts: 1, uncertainOffsets: [] });
  });

  it("does not duplicate a chunk when bytes arrived but its ACK was lost", async () => {
    const payload = bytes(193);
    const dataB64 = encode(payload);
    const received: number[] = [];
    const offsets: number[] = [];
    let lostAck = false;
    let endCalls = 0;

    const result = await pushFirmwareFile("/lib/plugins/kemper.mpy", dataB64, {
      commands: commands({
        putFileBegin: async (_path, size) => {
          expect(size).toBe(payload.length);
          received.length = 0;
          return sizeAck(size);
        },
        putFileChunk: async (_path, chunk, offset) => {
          offsets.push(offset);
          received.push(...decode(chunk));
          if (offset === 96 && !lostAck) {
            lostAck = true;
            throw new FirmwareCommandTimeoutError("PUT_FILE_CHUNK", "lost-ack");
          }
          return ACK;
        },
        putFileEnd: async () => {
          endCalls += 1;
          expect(received).toEqual(Array.from(payload));
          return ACK;
        },
      }),
      retryDelayMs: 0,
    });

    expect(offsets).toEqual([0, 96, 192]);
    expect(offsets.filter(offset => offset === 96)).toHaveLength(1);
    expect(endCalls).toBe(1);
    expect(result).toEqual({ size: 193, attempts: 1, uncertainOffsets: [96] });
  });

  it("restarts the whole transaction when an uncertain chunk was truly lost", async () => {
    const payload = bytes(193);
    const dataB64 = encode(payload);
    const received: number[] = [];
    const offsets: number[] = [];
    let attempt = 0;
    let endCalls = 0;

    const result = await pushFirmwareFile("/lib/captain/protocol.mpy", dataB64, {
      commands: commands({
        putFileBegin: async (_path, size) => {
          attempt += 1;
          expect(size).toBe(payload.length);
          received.length = 0; // firmware BEGIN truncates path.tmp
          return sizeAck(size);
        },
        putFileChunk: async (_path, chunk, offset) => {
          offsets.push(offset);
          if (attempt === 1 && offset === 96) {
            // The command itself never reached firmware.  From the host this
            // is indistinguishable from an appended chunk with a lost ACK.
            throw new FirmwareCommandTimeoutError("PUT_FILE_CHUNK", "lost-command");
          }
          received.push(...decode(chunk));
          return ACK;
        },
        putFileEnd: async () => {
          endCalls += 1;
          if (received.length !== payload.length) {
            throw new Error(
              `error: size_mismatch expected=${payload.length} actual=${received.length}`,
            );
          }
          expect(received).toEqual(Array.from(payload));
          return ACK;
        },
      }),
      retries: 3,
      retryDelayMs: 0,
    });

    expect(attempt).toBe(2);
    expect(endCalls).toBe(2);
    expect(offsets).toEqual([0, 96, 192, 0, 96, 192]);
    expect(received).toEqual(Array.from(payload));
    expect(result).toEqual({ size: 193, attempts: 2, uncertainOffsets: [] });
  });

  it("never trusts a legacy END after an uncertain append", async () => {
    const payload = bytes(97);
    const dataB64 = encode(payload);
    const received: number[] = [];
    let attempt = 0;
    let endCalls = 0;
    const chunkCalls: Array<{ attempt: number; offset: number }> = [];

    const result = await pushFirmwareFile("/legacy.py", dataB64, {
      commands: commands({
        // A legacy Captain returns a generic ACK even though it ignored size.
        putFileBegin: async () => {
          attempt += 1;
          received.length = 0;
          return ACK;
        },
        putFileChunk: async (_path, chunk, offset) => {
          chunkCalls.push({ attempt, offset });
          received.push(...decode(chunk));
          if (attempt === 1 && offset === 0) {
            throw new FirmwareCommandTimeoutError("PUT_FILE_CHUNK", "legacy-lost-ack");
          }
          return ACK;
        },
        putFileEnd: async () => {
          endCalls += 1;
          return ACK; // legacy would ACK regardless of actual size
        },
      }),
      retries: 3,
      retryDelayMs: 0,
    });

    expect(attempt).toBe(2);
    expect(endCalls).toBe(1); // never committed the ambiguous first attempt
    expect(chunkCalls).toEqual([
      { attempt: 1, offset: 0 },
      { attempt: 2, offset: 0 },
      { attempt: 2, offset: 96 },
    ]);
    expect(received).toEqual(Array.from(payload));
    expect(result.attempts).toBe(2);
  });

  it("fails closed when firmware claims size checking but echoes a different size", async () => {
    let begins = 0;
    const chunk = vi.fn(async () => ACK);
    const end = vi.fn(async () => ACK);

    await expect(pushFirmwareFile("/mismatch.mpy", encode(bytes(97)), {
      commands: commands({
        putFileBegin: async () => {
          begins += 1;
          return { type: "ACK", size_check: true, size: 96 };
        },
        putFileChunk: chunk,
        putFileEnd: end,
      }),
      retries: 3,
      retryDelayMs: 0,
    })).rejects.toThrow(/size capability mismatch.*expected 97, echoed 96/);

    expect(begins).toBe(3);
    expect(chunk).not.toHaveBeenCalled();
    expect(end).not.toHaveBeenCalled();
  });

  it("retries safely when END committed the file but its ACK arrived too late", async () => {
    const payload = bytes(97);
    const dataB64 = encode(payload);
    const temporary: number[] = [];
    let live: number[] = [];
    let attempt = 0;

    const result = await pushFirmwareFile("/code.py", dataB64, {
      commands: commands({
        putFileBegin: async () => {
          attempt += 1;
          temporary.length = 0;
          return ACK;
        },
        putFileChunk: async (_path, chunk, offset) => {
          expect(offset).toBe(temporary.length);
          temporary.push(...decode(chunk));
          return ACK;
        },
        putFileEnd: async () => {
          live = [...temporary];
          if (attempt === 1) {
            throw new FirmwareCommandTimeoutError("PUT_FILE_END", "late-end-ack");
          }
          return ACK;
        },
      }),
      retries: 3,
      retryDelayMs: 0,
    });

    expect(attempt).toBe(2);
    expect(live).toEqual(Array.from(payload));
    expect(result.attempts).toBe(2);
  });

  it("retries safely when the first END command never reached firmware", async () => {
    const payload = bytes(98);
    const dataB64 = encode(payload);
    const temporary: number[] = [];
    let live: number[] = [];
    let attempt = 0;
    let endCalls = 0;

    const result = await pushFirmwareFile("/lib/captain/app.mpy", dataB64, {
      commands: commands({
        putFileBegin: async (_path, size) => {
          attempt += 1;
          expect(size).toBe(98);
          temporary.length = 0;
          return sizeAck(size);
        },
        putFileChunk: async (_path, chunk, offset) => {
          expect(offset).toBe(temporary.length);
          temporary.push(...decode(chunk));
          return ACK;
        },
        putFileEnd: async () => {
          endCalls += 1;
          if (attempt === 1) {
            // No rename occurred; the second BEGIN safely truncates this tmp.
            throw new FirmwareCommandTimeoutError("PUT_FILE_END", "lost-end-command");
          }
          live = [...temporary];
          return ACK;
        },
      }),
      retries: 3,
      retryDelayMs: 0,
    });

    expect(attempt).toBe(2);
    expect(endCalls).toBe(2);
    expect(live).toEqual(Array.from(payload));
    expect(result.attempts).toBe(2);
  });

  it("fails after the bounded number of complete attempts", async () => {
    let begins = 0;
    let ends = 0;
    await expect(pushFirmwareFile("/never-complete.py", encode(bytes(4)), {
      commands: commands({
        putFileBegin: async (_path, size) => { begins += 1; return sizeAck(size); },
        putFileChunk: async () => {
          throw new FirmwareCommandTimeoutError("PUT_FILE_CHUNK", `lost-${begins}`);
        },
        putFileEnd: async () => {
          ends += 1;
          throw new Error("error: size_mismatch");
        },
      }),
      retries: 3,
      retryDelayMs: 0,
    })).rejects.toThrow(/size_mismatch/);

    expect(begins).toBe(3);
    expect(ends).toBe(3);
  });
});
