// The Stage screensaver's logo motion (src/kiosk/screensaver.ts): each
// effect as a function of time, and the action that drives one element
// from requestAnimationFrame (faked here with the rest of the timers).

import { describe, it, expect, vi, afterEach } from "vitest";
import {
  BOUNCE_SPEED_X, BOUNCE_SPEED_Y, BREATHE_MIN_OPACITY, BREATHE_PERIOD_MS,
  DRIFT_CYCLE_MS, DRIFT_DARK_MS, DRIFT_FADE_MS, DRIFT_HOLD_MS,
  createMotion, screensaverMotion,
} from "../../src/kiosk/screensaver";

const ROOM = { x: 1000, y: 300 };

/** A fixed sequence of random numbers, repeated. */
function sequence(...values: number[]): () => number {
  let next = 0;
  return () => values[next++ % values.length];
}

describe("screensaver motion", () => {
  it("Breathe keeps the logo centered and fades it out and back in, never fully off", () => {
    const motion = createMotion("breathe");
    const at = (t: number) => motion(5_000 + t, ROOM);
    expect(at(0)).toEqual({ x: 500, y: 150, opacity: 1 });
    expect(at(BREATHE_PERIOD_MS / 4).opacity).toBeCloseTo((1 + BREATHE_MIN_OPACITY) / 2, 5);
    expect(at(BREATHE_PERIOD_MS / 2).opacity).toBeCloseTo(BREATHE_MIN_OPACITY, 5);
    expect(at(BREATHE_PERIOD_MS).opacity).toBeCloseTo(1, 5);
    for (let t = 0; t <= 2 * BREATHE_PERIOD_MS; t += 97) {
      const frame = at(t);
      expect(frame.opacity).toBeGreaterThanOrEqual(BREATHE_MIN_OPACITY - 1e-9);
      expect(frame.opacity).toBeLessThanOrEqual(1);
      expect([frame.x, frame.y]).toEqual([500, 150]);
    }
    // A logo larger than the screen stays centered too.
    expect(motion(5_000, { x: -40, y: -10 })).toMatchObject({ x: -20, y: -5 });
  });

  it("Drift fades in at a random place, holds, fades out and only moves while dark", () => {
    const motion = createMotion("drift", sequence(0.25, 0.5, 0.75, 0.1));
    const at = (t: number) => motion(1_000 + t, ROOM);
    expect(at(0)).toEqual({ x: 250, y: 150, opacity: 0 });
    expect(at(DRIFT_FADE_MS / 2).opacity).toBeCloseTo(0.5, 5);
    expect(at(DRIFT_FADE_MS).opacity).toBe(1);
    expect(at(DRIFT_FADE_MS + DRIFT_HOLD_MS - 1)).toEqual({ x: 250, y: 150, opacity: 1 });
    expect(at(DRIFT_FADE_MS + DRIFT_HOLD_MS + DRIFT_FADE_MS / 2).opacity).toBeCloseTo(0.5, 5);
    expect(at(2 * DRIFT_FADE_MS + DRIFT_HOLD_MS).opacity).toBe(0);
    expect(at(DRIFT_CYCLE_MS - 1)).toEqual({ x: 250, y: 150, opacity: 0 });
    expect(DRIFT_CYCLE_MS - 2 * DRIFT_FADE_MS - DRIFT_HOLD_MS).toBe(DRIFT_DARK_MS);

    // The next cycle reappears elsewhere, starting dark again.
    expect(at(DRIFT_CYCLE_MS)).toEqual({ x: 750, y: 30, opacity: 0 });
    expect(at(DRIFT_CYCLE_MS + DRIFT_FADE_MS + 10)).toEqual({ x: 750, y: 30, opacity: 1 });
    // A skipped cycle (a stalled Pi) still lands on a fresh place.
    expect(at(3 * DRIFT_CYCLE_MS + DRIFT_FADE_MS)).toEqual({ x: 250, y: 150, opacity: 1 });
  });

  it("Bounce travels at its speed and reflects off every edge without leaving the screen", () => {
    // Start in the middle, heading left and down.
    const motion = createMotion("bounce", sequence(0.5, 0.5, 0.1, 0.9));
    expect(motion(0, ROOM)).toEqual({ x: 500, y: 150, opacity: 1 });
    let frame = motion(1_000 / 60, ROOM);
    expect(frame.x).toBeCloseTo(500 - BOUNCE_SPEED_X / 60, 5);
    expect(frame.y).toBeCloseTo(150 + BOUNCE_SPEED_Y / 60, 5);

    const xs: number[] = [];
    const ys: number[] = [];
    for (let t = 2_000 / 60; t < 60_000; t += 1_000 / 60) {
      frame = motion(t, ROOM);
      expect(frame.opacity).toBe(1);
      expect(frame.x).toBeGreaterThanOrEqual(0);
      expect(frame.x).toBeLessThanOrEqual(ROOM.x);
      expect(frame.y).toBeGreaterThanOrEqual(0);
      expect(frame.y).toBeLessThanOrEqual(ROOM.y);
      xs.push(frame.x);
      ys.push(frame.y);
    }
    // A minute is long enough to touch both sides on each axis.
    expect(Math.min(...xs)).toBeLessThan(2);
    expect(Math.max(...xs)).toBeGreaterThan(ROOM.x - 2);
    expect(Math.min(...ys)).toBeLessThan(2);
    expect(Math.max(...ys)).toBeGreaterThan(ROOM.y - 2);
  });

  it("Bounce limits a stalled frame and copes with a screen that shrinks or is too small", () => {
    const motion = createMotion("bounce", sequence(0, 0, 0.9, 0.9));
    expect(motion(0, ROOM)).toMatchObject({ x: 0, y: 0 });
    // Ten seconds without a frame move it as far as a tenth of a second.
    const late = motion(10_000, ROOM);
    expect(late.x).toBeCloseTo(BOUNCE_SPEED_X / 10, 5);
    expect(late.y).toBeCloseTo(BOUNCE_SPEED_Y / 10, 5);

    const shrunk = motion(10_016, { x: 5, y: 2 });
    expect(shrunk.x).toBeGreaterThanOrEqual(0);
    expect(shrunk.x).toBeLessThanOrEqual(5);
    expect(shrunk.y).toBeGreaterThanOrEqual(0);
    expect(shrunk.y).toBeLessThanOrEqual(2);
    expect(motion(10_032, { x: -60, y: -20 })).toMatchObject({ x: -30, y: -10 });
  });
});

describe("screensaverMotion action", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  function mountLogo() {
    const area = document.createElement("div");
    const logo = document.createElement("img");
    area.append(logo);
    document.body.append(area);
    vi.spyOn(area, "clientWidth", "get").mockReturnValue(1_200);
    vi.spyOn(area, "clientHeight", "get").mockReturnValue(400);
    vi.spyOn(logo, "offsetWidth", "get").mockReturnValue(200);
    vi.spyOn(logo, "offsetHeight", "get").mockReturnValue(100);
    return { area, logo };
  }

  it("animates from requestAnimationFrame, switches effect in place and stops on destroy", async () => {
    vi.useFakeTimers();
    const { area, logo } = mountLogo();
    try {
      const action = screensaverMotion(logo, "breathe");
      expect(logo.style.transform).toBe(""); // nothing until the first frame
      await vi.advanceTimersByTimeAsync(20);
      expect(logo.style.transform).toBe("translate3d(500.0px, 150.0px, 0)");
      expect(Number(logo.style.opacity)).toBe(1);
      await vi.advanceTimersByTimeAsync(BREATHE_PERIOD_MS / 2);
      expect(Number(logo.style.opacity)).toBeCloseTo(BREATHE_MIN_OPACITY, 2);

      action.update("bounce");
      await vi.advanceTimersByTimeAsync(20);
      expect(Number(logo.style.opacity)).toBe(1);
      const start = logo.style.transform;
      await vi.advanceTimersByTimeAsync(1_000);
      expect(logo.style.transform).not.toBe(start);
      expect(logo.style.transform).toMatch(/^translate3d\(\d+\.\dpx, \d+\.\dpx, 0\)$/);

      action.update("drift");
      await vi.advanceTimersByTimeAsync(20);
      expect(Number(logo.style.opacity)).toBeLessThan(0.05);
      await vi.advanceTimersByTimeAsync(DRIFT_FADE_MS);
      expect(Number(logo.style.opacity)).toBe(1);

      action.destroy();
      const frozen = [logo.style.transform, logo.style.opacity];
      await vi.advanceTimersByTimeAsync(DRIFT_CYCLE_MS);
      expect([logo.style.transform, logo.style.opacity]).toEqual(frozen);
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      area.remove();
    }
  });

  it("writes a style only when it changes", async () => {
    vi.useFakeTimers();
    const { area, logo } = mountLogo();
    const writes: string[] = [];
    const style = logo.style;
    const setProperty = (name: string) => (value: string) => {
      writes.push(name);
      style.setProperty(name, value);
    };
    Object.defineProperty(logo, "style", {
      configurable: true,
      value: new Proxy(style, {
        set(target, key, value) {
          if (key === "transform" || key === "opacity") setProperty(key)(value);
          else Reflect.set(target, key, value);
          return true;
        },
        get(target, key) {
          return Reflect.get(target, key);
        },
      }),
    });
    try {
      const action = screensaverMotion(logo, "drift");
      // Most of a hold: one placement, then the fade-in steps, then nothing.
      await vi.advanceTimersByTimeAsync(DRIFT_FADE_MS + DRIFT_HOLD_MS - 100);
      const transforms = writes.filter((name) => name === "transform").length;
      const opacities = writes.filter((name) => name === "opacity").length;
      expect(transforms).toBe(1);
      expect(opacities).toBeLessThanOrEqual(Math.ceil(DRIFT_FADE_MS / 16) + 2);
      writes.length = 0;
      await vi.advanceTimersByTimeAsync(50);
      expect(writes).toEqual([]);
      action.destroy();
    } finally {
      area.remove();
    }
  });
});
