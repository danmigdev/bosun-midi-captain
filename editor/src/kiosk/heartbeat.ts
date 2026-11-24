// Tells the hub that the kiosk page still runs. A crashed page shows
// Chromium's own error screen until the kiosk restarts, so the Pi's Stage
// watchdog (tools/rpi-hub/kiosk/bosun-stage-watchdog.py) restarts it when
// the beats stop.

export const HEARTBEAT_MS = 5_000;
const LOOPBACK = ["localhost", "127.0.0.1", "[::1]"];

/** Starts the beats and returns a function that stops them. Only the Pi's own
 *  browser opens Stage through loopback; every other viewer stays silent. */
export function startHeartbeat(): () => void {
  if (!LOOPBACK.includes(location.hostname)) return () => {};
  let frame: number | null = null;
  // Sent from an animation frame: a screen that gets no frames is not alive,
  // even while its scripts run.
  const beat = () => {
    frame ??= requestAnimationFrame(() => {
      frame = null;
      void fetch("/kiosk-alive", { method: "POST" }).catch(() => {});
    });
  };
  beat();
  const timer = setInterval(beat, HEARTBEAT_MS);
  return () => {
    clearInterval(timer);
    if (frame !== null) cancelAnimationFrame(frame);
  };
}
