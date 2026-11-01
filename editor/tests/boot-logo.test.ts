import { createHash, webcrypto } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ sendAndAwait: vi.fn() }));
vi.mock("../src/lib/protocol", () => ({ sendAndAwait: mocks.sendAndAwait }));

import {
  CHUNK_BYTES, PI_PICTURE_BYTES, TFT_LOGO_BYTES, deletePedalLogo, fitRect, fnv1a32, hubLogoInfo,
  packBsplash, packRgb565, prepareLogo, resetHubLogo, toBase64, uploadHubLogo, uploadPedalLogo,
} from "../src/lib/boot-logo";

const bytes = (length: number) => Uint8Array.from({ length }, (_, i) => (i * 7) % 256);
const decoded = (data: string) => Uint8Array.from(Buffer.from(data, "base64"));

beforeEach(() => {
  vi.stubGlobal("crypto", webcrypto);
  mocks.sendAndAwait.mockReset();
});

describe("pixel formats", () => {
  it("packs RGB565 little-endian and ignores alpha", () => {
    const rgba = [0xff, 0x00, 0x00, 0xff, 0x00, 0xff, 0x00, 0x00, 0x00, 0x00, 0xff, 0x80, 0x12, 0x34, 0x56, 0xff];
    expect(Array.from(packRgb565(rgba, 2, 2))).toEqual([
      0x00, 0xf8, // red
      0xe0, 0x07, // green
      0x1f, 0x00, // blue
      0xaa, 0x11, // 0x12,0x34,0x56 -> (2<<11)|(13<<5)|10 = 0x11aa
    ]);
  });

  it("writes the BSPLASH1 header the Pi renderer reads", () => {
    const picture = packBsplash([1, 2, 3, 255, 4, 5, 6, 0], 2, 1);
    expect(new TextDecoder().decode(picture.subarray(0, 8))).toBe("BSPLASH1");
    const view = new DataView(picture.buffer);
    expect([view.getUint32(8, true), view.getUint32(12, true)]).toEqual([2, 1]);
    expect(Array.from(picture.subarray(16))).toEqual([1, 2, 3, 4, 5, 6]);
    expect(PI_PICTURE_BYTES).toBe(16 + 400 * 222 * 3);
  });

  it("rejects a pixel buffer of the wrong size", () => {
    expect(() => packRgb565([0, 0, 0], 1, 1)).toThrow();
    expect(() => packBsplash([0, 0, 0, 0], 2, 1)).toThrow();
  });
});

describe("image loading", () => {
  it("reads the picked file through a URL scheme the app CSP allows", async () => {
    // A blob: URL is refused by the Desktop CSP, which made every image fail.
    const csp = JSON.parse(readFileSync(resolve(process.cwd(), "src-tauri/tauri.conf.json"), "utf8"))
      .app.security.csp as string;
    const imgSrc = csp.split(";").map(part => part.trim()).find(part => part.startsWith("img-src"))!.split(/\s+/);
    const sources: string[] = [];
    vi.stubGlobal("Image", class {
      set src(value: string) { sources.push(value); }
      decode() { return Promise.reject(new Error("stop after loading")); }
    });
    await expect(prepareLogo(new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], "logo.png", { type: "image/png" })))
      .rejects.toThrow("not an image");
    expect(sources).toHaveLength(1);
    const scheme = sources[0].slice(0, sources[0].indexOf(":") + 1);
    expect(scheme).toBe("data:");
    expect(imgSrc).toContain(scheme);
    vi.unstubAllGlobals();
  });

  it("gives an SVG without a MIME type the SVG type", async () => {
    const sources: string[] = [];
    vi.stubGlobal("Image", class {
      set src(value: string) { sources.push(value); }
      decode() { return Promise.reject(new Error("stop after loading")); }
    });
    await expect(prepareLogo(new File(["<svg xmlns='http://www.w3.org/2000/svg'/>"], "logo.svg"))).rejects.toThrow();
    expect(sources[0].startsWith("data:image/svg+xml")).toBe(true);
    vi.unstubAllGlobals();
  });
});

describe("fitRect", () => {
  it("centers wide, tall and unknown-size images", () => {
    expect(fitRect(800, 200, 240, 200)).toEqual({ x: 0, y: 70, width: 240, height: 60 });
    expect(fitRect(100, 400, 400, 222)).toEqual({ x: 172, y: 0, width: 56, height: 222 });
    expect(fitRect(10, 10, 240, 200)).toEqual({ x: 20, y: 0, width: 200, height: 200 });
    expect(fitRect(0, 0, 240, 200)).toEqual({ x: 0, y: 0, width: 240, height: 200 });
  });
});

describe("checksums", () => {
  it("matches the published FNV-1a 32 vectors", () => {
    expect(fnv1a32(new Uint8Array())).toBe(0x811c9dc5);
    expect(fnv1a32(new TextEncoder().encode("a"))).toBe(0xe40c292c);
    expect(fnv1a32(new TextEncoder().encode("foobar"))).toBe(0xbf9cf968);
  });

  it("base64-encodes large buffers", () => {
    const data = bytes(70000);
    expect(decoded(toBase64(data))).toEqual(data);
  });
});

describe("pedal upload", () => {
  it("sends ordered chunks, then commits size and FNV-1a", async () => {
    const tft = bytes(TFT_LOGO_BYTES);
    const received: number[] = [];
    mocks.sendAndAwait.mockImplementation(async (message) => {
      if (message.type === "PUT_LOGO") {
        expect(message.offset).toBe(received.length ? received[received.length - 1] : 0);
        const end = message.offset + decoded(message.data).length;
        received.push(end);
        return { type: "ACK", received: end };
      }
      expect(message).toEqual({ type: "COMMIT_LOGO", size: TFT_LOGO_BYTES, fnv1a: fnv1a32(tft) });
      return { type: "ACK" };
    });
    const progress: number[] = [];
    await uploadPedalLogo(tft, fraction => progress.push(fraction));
    expect(received.length).toBe(Math.ceil(TFT_LOGO_BYTES / CHUNK_BYTES));
    expect(received[received.length - 1]).toBe(TFT_LOGO_BYTES);
    expect(progress[progress.length - 1]).toBe(1);
  });

  it("stops when the pedal reports a different position", async () => {
    mocks.sendAndAwait.mockResolvedValue({ type: "ACK", received: 5 });
    await expect(uploadPedalLogo(bytes(TFT_LOGO_BYTES))).rejects.toThrow("did not acknowledge");
    expect(mocks.sendAndAwait).toHaveBeenCalledTimes(1);
  });

  it("refuses a buffer of the wrong size before sending", async () => {
    await expect(uploadPedalLogo(bytes(10))).rejects.toThrow("wrong size");
    expect(mocks.sendAndAwait).not.toHaveBeenCalled();
  });

  it("deletes the custom logo", async () => {
    mocks.sendAndAwait.mockResolvedValue({ type: "ACK" });
    await deletePedalLogo();
    expect(mocks.sendAndAwait).toHaveBeenCalledWith({ type: "DELETE_LOGO" }, 8000);
  });
});

describe("Raspberry Pi upload", () => {
  it("uploads the PNG followed by the picture and commits", async () => {
    const png = bytes(30000);
    const picture = bytes(PI_PICTURE_BYTES);
    let stored = new Uint8Array();
    mocks.sendAndAwait.mockImplementation(async (message) => {
      if (message.type === "HUB_BOOT_LOGO_BEGIN") {
        const whole = Buffer.concat([png, picture]);
        expect(message).toEqual({ type: "HUB_BOOT_LOGO_BEGIN", png_size: png.length, picture_size: PI_PICTURE_BYTES,
                                   sha256: createHash("sha256").update(whole).digest("hex") });
        return { type: "HUB_BOOT_LOGO", upload: "u1", received: 0, size: whole.length };
      }
      if (message.type === "HUB_BOOT_LOGO_CHUNK") {
        expect(message.upload).toBe("u1");
        expect(message.offset).toBe(stored.length);
        stored = new Uint8Array([...stored, ...decoded(message.data)]);
        return { type: "HUB_BOOT_LOGO", upload: "u1", received: stored.length };
      }
      expect(message).toEqual({ type: "HUB_BOOT_LOGO_COMMIT", upload: "u1" });
      return { type: "HUB_BOOT_LOGO", supported: true, custom: true, early: "pending" };
    });
    const info = await uploadHubLogo(png, picture);
    expect(info).toMatchObject({ supported: true, custom: true, early: "pending" });
    expect(stored).toEqual(new Uint8Array([...png, ...picture]));
  });

  it("treats a forwarded ERROR from an older hub as unsupported", async () => {
    mocks.sendAndAwait.mockRejectedValue(new Error("error: unknown_type"));
    await expect(hubLogoInfo()).resolves.toEqual({ supported: false, custom: false });
  });

  it("keeps a lost connection as an error", async () => {
    mocks.sendAndAwait.mockRejectedValue(new Error("timeout"));
    await expect(hubLogoInfo()).rejects.toThrow("timeout");
  });

  it("reports the stored logo and resets it", async () => {
    mocks.sendAndAwait.mockResolvedValueOnce({ type: "HUB_BOOT_LOGO", supported: true, custom: true,
                                               early: "applied", png: "AAAA" });
    await expect(hubLogoInfo()).resolves.toMatchObject({ custom: true, early: "applied", png: "AAAA" });
    mocks.sendAndAwait.mockResolvedValueOnce({ type: "HUB_BOOT_LOGO", supported: true, custom: false, early: "pending" });
    await expect(resetHubLogo()).resolves.toMatchObject({ custom: false });
  });

  it("refuses oversized inputs before contacting the Pi", async () => {
    await expect(uploadHubLogo(bytes(300 * 1024), bytes(PI_PICTURE_BYTES))).rejects.toThrow("wrong size");
    await expect(uploadHubLogo(bytes(10), bytes(10))).rejects.toThrow("wrong size");
    expect(mocks.sendAndAwait).not.toHaveBeenCalled();
  });
});
