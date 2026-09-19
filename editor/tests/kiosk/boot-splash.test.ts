import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import html from "../../stage-kiosk.html?raw";

describe("Stage boot splash", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      callback(0);
      return 0;
    });
    document.body.innerHTML = html.match(/<body>([\s\S]*)<\/body>/)![1];
    const script = [...document.querySelectorAll("script")].find((element) => !element.src)!;
    new Function(script.textContent!)();
  });
  afterEach(async () => {
    // Also disconnect the page observer in tests which never connected a pedal.
    const app = document.getElementById("app");
    if (app) app.innerHTML = '<div class="stage"></div>';
    await Promise.resolve();
    vi.clearAllTimers();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    document.body.innerHTML = "";
  });

  it("keeps branding during loading and removes it when Stage mounts", async () => {
    const app = document.getElementById("app")!;
    expect(document.getElementById("bosun-boot-splash")).not.toBeNull();
    expect(document.getElementById("bosun-boot-progress")!.getAttribute("aria-valuenow")).toBe("90");
    app.innerHTML = '<div class="kiosk-waiting">Waiting for the pedal</div>';
    await Promise.resolve();
    expect(document.getElementById("bosun-boot-splash")).not.toBeNull();
    app.innerHTML = '<div class="stage">Rig 1</div>';
    await Promise.resolve();
    expect(document.getElementById("bosun-boot-splash")).toBeNull();
    vi.advanceTimersByTime(15000);
  });

  it("explains a missing pedal without showing OS diagnostics", () => {
    document.getElementById("app")!.innerHTML = '<div class="kiosk-waiting"></div>';
    vi.advanceTimersByTime(15000);
    expect(document.getElementById("bosun-boot-percent")!.textContent).toBe("90%");
    expect(document.getElementById("bosun-boot-status")!.textContent).toBe("Waiting for the pedal…");
  });

  it("reaches 100 only after Stage mounts and paints before removing the splash", async () => {
    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => frames.push(callback));
    vi.advanceTimersByTime(60000);
    expect(document.getElementById("bosun-boot-percent")!.textContent).toBe("90%");
    document.getElementById("app")!.innerHTML = '<div class="stage">Rig 1</div>';
    await Promise.resolve();
    expect(document.getElementById("bosun-boot-progress")!.getAttribute("aria-valuenow")).toBe("100");
    expect(document.getElementById("bosun-boot-percent")!.textContent).toBe("100%");
    frames.shift()!(0);
    expect(document.getElementById("bosun-boot-splash")).not.toBeNull();
    frames.shift()!(0);
    expect(document.getElementById("bosun-boot-splash")).toBeNull();
  });
});
