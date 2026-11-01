// Startup logo: one picked image, rendered for the Captain TFT and the
// Raspberry Pi startup screens (docs/plans/20261101_102803_custom_startup_logo.md).
// The pedal and the Pi only receive fixed-size pixel boxes plus a PNG for
// Chromium, so neither decodes a user's image format.
import { sendAndAwait } from "./protocol";

export const TFT_BOX = { width: 240, height: 200 } as const;
export const TFT_LOGO_BYTES = TFT_BOX.width * TFT_BOX.height * 2;
export const PI_BOX = { width: 400, height: 222 } as const;
export const PI_PICTURE_BYTES = 16 + PI_BOX.width * PI_BOX.height * 3;
/** Chromium shows the PNG at up to the box size; 2x keeps it sharp on HiDPI. */
export const PI_PNG_BOX = { width: PI_BOX.width * 2, height: PI_BOX.height * 2 } as const;
export const PI_PNG_MAX_BYTES = 256 * 1024;
/** Matches the Pi startup pictures' background (render.py). */
export const PI_BACKGROUND = "#0f141a";
export const CHUNK_BYTES = 12 * 1024;

export type PedalLogoState = "custom" | "default";
export type EarlyState = "applied" | "pending" | "unavailable" | "error";
export interface HubLogoInfo {
  supported: boolean;
  custom: boolean;
  early?: EarlyState;
  error?: string;
  png?: string;
}
export interface PreparedLogo {
  tft: Uint8Array;
  piPicture: Uint8Array;
  piPng: Uint8Array;
  /** Object URL of the Chromium PNG, for previews. Revoke when done. */
  previewUrl: string;
}

/** Largest centered rectangle with the source's aspect ratio inside the box. */
export function fitRect(width: number, height: number, boxWidth: number, boxHeight: number) {
  if (!(width > 0) || !(height > 0)) return { x: 0, y: 0, width: boxWidth, height: boxHeight };
  const scale = Math.min(boxWidth / width, boxHeight / height);
  const w = Math.max(1, Math.min(boxWidth, Math.round(width * scale)));
  const h = Math.max(1, Math.min(boxHeight, Math.round(height * scale)));
  return { x: Math.floor((boxWidth - w) / 2), y: Math.floor((boxHeight - h) / 2), width: w, height: h };
}

/** Opaque RGBA pixels to the TFT format: RGB565, little-endian, row-major. */
export function packRgb565(rgba: ArrayLike<number>, width: number, height: number): Uint8Array {
  if (rgba.length !== width * height * 4) throw new Error("Pixel buffer does not match its size.");
  const out = new Uint8Array(width * height * 2);
  for (let i = 0, o = 0; i < rgba.length; i += 4, o += 2) {
    const value = (rgba[i] >> 3) << 11 | (rgba[i + 1] >> 2) << 5 | rgba[i + 2] >> 3;
    out[o] = value & 0xff;
    out[o + 1] = value >> 8;
  }
  return out;
}

/** Opaque RGBA pixels to the Pi's BSPLASH1 picture format. */
export function packBsplash(rgba: ArrayLike<number>, width: number, height: number): Uint8Array {
  if (rgba.length !== width * height * 4) throw new Error("Pixel buffer does not match its size.");
  const out = new Uint8Array(16 + width * height * 3);
  out.set(Array.from("BSPLASH1", c => c.charCodeAt(0)));
  const view = new DataView(out.buffer);
  view.setUint32(8, width, true);
  view.setUint32(12, height, true);
  for (let i = 0, o = 16; i < rgba.length; i += 4, o += 3) {
    out[o] = rgba[i]; out[o + 1] = rgba[i + 1]; out[o + 2] = rgba[i + 2];
  }
  return out;
}

export function fnv1a32(bytes: Uint8Array): number {
  let hash = 0x811c9dc5;
  for (const byte of bytes) hash = Math.imul(hash ^ byte, 0x01000193) >>> 0;
  return hash >>> 0;
}

export async function sha256Hex(bytes: Uint8Array<ArrayBuffer>): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
}

export function toBase64(bytes: Uint8Array): string {
  let text = "";
  for (let i = 0; i < bytes.length; i += 4096) text += String.fromCharCode(...bytes.subarray(i, i + 4096));
  return btoa(text);
}

// ---- Rendering (needs a real canvas; jsdom has none) ----

async function loadImage(file: Blob): Promise<HTMLImageElement> {
  const url = URL.createObjectURL(file);
  try {
    const image = new Image();
    image.src = url;
    await image.decode();
    return image;
  } catch {
    throw new Error("This file is not an image the editor can read. Use PNG, JPEG, SVG or WebP.");
  } finally {
    URL.revokeObjectURL(url);
  }
}

function canvas(width: number, height: number) {
  const element = document.createElement("canvas");
  element.width = width; element.height = height;
  const context = element.getContext("2d");
  if (!context) throw new Error("The editor cannot draw images on this system.");
  context.imageSmoothingQuality = "high";
  return { element, context };
}

/** Draw the image fitted into the box; `background` null keeps transparency. */
function drawFitted(image: HTMLImageElement, width: number, height: number, background: string | null) {
  const { element, context } = canvas(width, height);
  if (background) { context.fillStyle = background; context.fillRect(0, 0, width, height); }
  // SVG without intrinsic size reports 0; let it fill the box.
  const rect = fitRect(image.naturalWidth, image.naturalHeight, width, height);
  context.drawImage(image, rect.x, rect.y, rect.width, rect.height);
  return { element, pixels: context.getImageData(0, 0, width, height).data };
}

function pngBlob(element: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => element.toBlob(
    blob => blob ? resolve(blob) : reject(new Error("The editor could not encode the logo.")), "image/png"));
}

/** Render the picked file for every target. */
export async function prepareLogo(file: Blob): Promise<PreparedLogo> {
  const image = await loadImage(file);
  const tft = packRgb565(drawFitted(image, TFT_BOX.width, TFT_BOX.height, "#000000").pixels,
                         TFT_BOX.width, TFT_BOX.height);
  const piPicture = packBsplash(drawFitted(image, PI_BOX.width, PI_BOX.height, PI_BACKGROUND).pixels,
                                PI_BOX.width, PI_BOX.height);
  // Trim the PNG to the logo itself: the page sizes it with max-width/height.
  let box = fitRect(image.naturalWidth, image.naturalHeight, PI_PNG_BOX.width, PI_PNG_BOX.height);
  for (let attempt = 0; ; ++attempt) {
    const { element } = drawFitted(image, box.width, box.height, null);
    const blob = await pngBlob(element);
    if (blob.size <= PI_PNG_MAX_BYTES) {
      return { tft, piPicture, piPng: new Uint8Array(await blob.arrayBuffer()), previewUrl: URL.createObjectURL(blob) };
    }
    // Photographic logos compress poorly; shrink until the Pi accepts it.
    if (attempt === 6) throw new Error("This image is too detailed for the startup screen. Try a simpler logo.");
    box = { ...box, width: Math.max(1, Math.round(box.width * 0.8)), height: Math.max(1, Math.round(box.height * 0.8)) };
  }
}

// ---- Pedal (Captain TFT) ----

export async function uploadPedalLogo(tft: Uint8Array, onProgress?: (fraction: number) => void): Promise<void> {
  if (tft.length !== TFT_LOGO_BYTES) throw new Error("The pedal logo has the wrong size.");
  for (let offset = 0; offset < tft.length; offset += CHUNK_BYTES) {
    const end = Math.min(offset + CHUNK_BYTES, tft.length);
    const reply = await sendAndAwait(
      { type: "PUT_LOGO", offset, data: toBase64(tft.subarray(offset, end)) }, 8000);
    if (reply.type !== "ACK" || reply.received !== end) {
      throw new Error("The pedal did not acknowledge the logo upload.");
    }
    onProgress?.(end / tft.length);
  }
  const reply = await sendAndAwait({ type: "COMMIT_LOGO", size: tft.length, fnv1a: fnv1a32(tft) }, 15000);
  if (reply.type !== "ACK") throw new Error("The pedal did not save the logo.");
}

export async function deletePedalLogo(): Promise<void> {
  const reply = await sendAndAwait({ type: "DELETE_LOGO" }, 8000);
  if (reply.type !== "ACK") throw new Error("The pedal did not remove the logo.");
}

// ---- Raspberry Pi hub ----

function hubInfo(reply: Awaited<ReturnType<typeof sendAndAwait>>): HubLogoInfo {
  if (reply.type !== "HUB_BOOT_LOGO") throw new Error("The Raspberry Pi returned an unexpected logo response.");
  return { supported: reply.supported !== false, custom: reply.custom === true,
           early: reply.early, error: reply.error, png: reply.png };
}

/** An older hub forwards the request to the pedal, which answers ERROR. */
export async function hubLogoInfo(): Promise<HubLogoInfo> {
  let reply;
  try { reply = await sendAndAwait({ type: "HUB_BOOT_LOGO_INFO" }, 8000); }
  catch (error) {
    if (error instanceof Error && error.message.startsWith("error:")
        && !error.message.startsWith("error: disconnected")) return { supported: false, custom: false };
    throw error;
  }
  return hubInfo(reply);
}

export async function uploadHubLogo(png: Uint8Array, picture: Uint8Array,
                                    onProgress?: (fraction: number) => void): Promise<HubLogoInfo> {
  if (picture.length !== PI_PICTURE_BYTES || !png.length || png.length > PI_PNG_MAX_BYTES) {
    throw new Error("The Raspberry Pi logo has the wrong size.");
  }
  const upload = new Uint8Array(png.length + picture.length);
  upload.set(png); upload.set(picture, png.length);
  const begin = await sendAndAwait({ type: "HUB_BOOT_LOGO_BEGIN", png_size: png.length,
                                     picture_size: picture.length, sha256: await sha256Hex(upload) }, 8000);
  if (begin.type !== "HUB_BOOT_LOGO" || typeof begin.upload !== "string") {
    throw new Error("The Raspberry Pi could not start the logo upload.");
  }
  for (let offset = 0; offset < upload.length; offset += CHUNK_BYTES) {
    const end = Math.min(offset + CHUNK_BYTES, upload.length);
    const reply = await sendAndAwait({ type: "HUB_BOOT_LOGO_CHUNK", upload: begin.upload, offset,
                                       data: toBase64(upload.subarray(offset, end)) }, 8000);
    if (reply.type !== "HUB_BOOT_LOGO" || reply.received !== end) {
      throw new Error("The Raspberry Pi did not acknowledge the logo upload.");
    }
    onProgress?.(end / upload.length);
  }
  return hubInfo(await sendAndAwait({ type: "HUB_BOOT_LOGO_COMMIT", upload: begin.upload }, 15000));
}

export async function resetHubLogo(): Promise<HubLogoInfo> {
  return hubInfo(await sendAndAwait({ type: "HUB_BOOT_LOGO_RESET" }, 8000));
}
