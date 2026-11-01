// Logo motion for the Stage screensaver. The kiosk's Chromium runs with
// --force-prefers-reduced-motion, which the Stage's CSS animations honour,
// so the screensaver is driven from requestAnimationFrame instead. It moves
// one element and writes only transform and opacity, and only when they
// change, which keeps a Raspberry Pi 3 on its compositor.
//
// Every effect is a function of the frame time, so a late frame (a busy
// Pi) catches up instead of slowing down: Bounce travels at a constant
// speed and reflects off the edges, Breathe fades the centered logo out and
// back in, Drift fades it out, shows it again somewhere else and holds it.

import type { ScreensaverEffect } from "./ws-link";

/** Bounce speed in CSS px per second; unequal so the path does not repeat soon. */
export const BOUNCE_SPEED_X = 90;
export const BOUNCE_SPEED_Y = 60;
/** A stalled frame advances Bounce by at most this much time. */
const BOUNCE_MAX_STEP_MS = 100;
/** One Breathe cycle: bright, dimmest (never off) halfway, bright again. */
export const BREATHE_PERIOD_MS = 8000;
export const BREATHE_MIN_OPACITY = 0.1;
/** Drift: fade in, hold, fade out, stay dark, then reappear elsewhere. */
export const DRIFT_FADE_MS = 1500;
export const DRIFT_HOLD_MS = 4000;
export const DRIFT_DARK_MS = 500;
export const DRIFT_CYCLE_MS = 2 * DRIFT_FADE_MS + DRIFT_HOLD_MS + DRIFT_DARK_MS;

/** Free room for the logo: the screen size minus the logo size, in px. */
export type Room = { x: number; y: number };
export type Frame = { x: number; y: number; opacity: number };
/** Gives the frame at a time in ms; the first call starts the effect. */
export type Motion = (now: number, room: Room) => Frame;

/** Half-cosine ease from 0 to 1. */
function ease(progress: number): number {
  return (1 - Math.cos(Math.PI * Math.min(Math.max(progress, 0), 1))) / 2;
}

/** A logo larger than the screen stays centered rather than pinned to an edge. */
function place(fraction: number, room: number): number {
  return room > 0 ? fraction * room : room / 2;
}

/** Moves along one axis and reflects off its edges; returns the new position and direction. */
function reflect(position: number, direction: number, room: number): [number, number] {
  if (room <= 0) return [room / 2, direction];
  if (position < 0) return [Math.min(-position, room), 1];
  if (position > room) return [Math.max(2 * room - position, 0), -1];
  return [position, direction];
}

export function createMotion(effect: ScreensaverEffect, random: () => number = Math.random): Motion {
  let start: number | null = null;
  if (effect === "breathe") {
    return (now, room) => {
      start ??= now;
      const phase = ((now - start) % BREATHE_PERIOD_MS) / BREATHE_PERIOD_MS;
      const level = (1 + Math.cos(2 * Math.PI * phase)) / 2;
      return {
        x: place(0.5, room.x), y: place(0.5, room.y),
        opacity: BREATHE_MIN_OPACITY + (1 - BREATHE_MIN_OPACITY) * level,
      };
    };
  }
  if (effect === "drift") {
    let cycle = -1;
    let spot = { x: 0, y: 0 };
    return (now, room) => {
      start ??= now;
      const elapsed = now - start;
      const current = Math.floor(elapsed / DRIFT_CYCLE_MS);
      if (current !== cycle) {
        // Only ever while the logo is dark: each cycle starts faded out.
        cycle = current;
        spot = { x: random(), y: random() };
      }
      const phase = elapsed - current * DRIFT_CYCLE_MS;
      let opacity = 0;
      if (phase < DRIFT_FADE_MS) opacity = ease(phase / DRIFT_FADE_MS);
      else if (phase < DRIFT_FADE_MS + DRIFT_HOLD_MS) opacity = 1;
      else if (phase < 2 * DRIFT_FADE_MS + DRIFT_HOLD_MS) {
        opacity = 1 - ease((phase - DRIFT_FADE_MS - DRIFT_HOLD_MS) / DRIFT_FADE_MS);
      }
      return { x: place(spot.x, room.x), y: place(spot.y, room.y), opacity };
    };
  }
  let last = 0;
  let x = 0;
  let y = 0;
  let dx = 1;
  let dy = 1;
  return (now, room) => {
    if (start === null) {
      start = last = now;
      x = place(random(), room.x);
      y = place(random(), room.y);
      dx = random() < 0.5 ? -1 : 1;
      dy = random() < 0.5 ? -1 : 1;
    }
    const step = Math.min(Math.max(now - last, 0), BOUNCE_MAX_STEP_MS) / 1000;
    last = now;
    [x, dx] = reflect(x + dx * BOUNCE_SPEED_X * step, dx, room.x);
    [y, dy] = reflect(y + dy * BOUNCE_SPEED_Y * step, dy, room.y);
    return { x, y, opacity: 1 };
  };
}

/** Runs one effect on the logo until the returned function stops it. */
function animate(node: HTMLElement, effect: ScreensaverEffect): () => void {
  const motion = createMotion(effect);
  let transform = "";
  let opacity = "";
  let handle = 0;
  const frame = (now: number) => {
    // Reads come first and nothing but transform/opacity is written, so
    // these never force a layout of their own.
    const area = node.parentElement;
    const room = {
      x: (area?.clientWidth ?? innerWidth) - node.offsetWidth,
      y: (area?.clientHeight ?? innerHeight) - node.offsetHeight,
    };
    const next = motion(now, room);
    const nextTransform = `translate3d(${next.x.toFixed(1)}px, ${next.y.toFixed(1)}px, 0)`;
    const nextOpacity = next.opacity.toFixed(3);
    if (nextTransform !== transform) node.style.transform = transform = nextTransform;
    if (nextOpacity !== opacity) node.style.opacity = opacity = nextOpacity;
    handle = requestAnimationFrame(frame);
  };
  handle = requestAnimationFrame(frame);
  return () => cancelAnimationFrame(handle);
}

/** Svelte action: animates the logo with an effect, restarting when it changes. */
export function screensaverMotion(node: HTMLElement, effect: ScreensaverEffect) {
  let stop = animate(node, effect);
  return {
    update(next: ScreensaverEffect) {
      stop();
      stop = animate(node, next);
    },
    destroy() {
      stop();
    },
  };
}
