import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import html from "../../stage-kiosk.html?raw";
import { HEARTBEAT_MS, startHeartbeat } from "../../src/kiosk/heartbeat";

describe("Stage kiosk heartbeat", () => {
  let frames: FrameRequestCallback[];
  let post: ReturnType<typeof vi.fn>;
  let stop = () => {};
  const paint = () => frames.splice(0).forEach((frame) => frame(0));
  const open = (hostname: string) => vi.stubGlobal("location", { hostname });

  beforeEach(() => {
    vi.useFakeTimers();
    frames = [];
    post = vi.fn(() => Promise.resolve(new Response(null, { status: 204 })));
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => frames.push(callback));
    vi.stubGlobal("cancelAnimationFrame", () => { frames = []; });
    vi.stubGlobal("fetch", post);
  });
  afterEach(() => {
    stop();
    vi.clearAllTimers();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("reports the Pi's own page at once and then regularly", () => {
    open("localhost");
    stop = startHeartbeat();
    paint();
    expect(post).toHaveBeenCalledExactlyOnceWith("/kiosk-alive", { method: "POST" });
    vi.advanceTimersByTime(HEARTBEAT_MS);
    paint();
    expect(post).toHaveBeenCalledTimes(2);
  });

  it("stays silent while the screen gets no frames and sends one beat when they return", () => {
    open("localhost");
    stop = startHeartbeat();
    vi.advanceTimersByTime(HEARTBEAT_MS * 10);
    expect(post).not.toHaveBeenCalled();
    paint();
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("keeps beating when the hub is away", async () => {
    open("127.0.0.1");
    post.mockImplementation(() => Promise.reject(new TypeError("Failed to fetch")));
    stop = startHeartbeat();
    paint();
    await vi.advanceTimersByTimeAsync(HEARTBEAT_MS);
    paint();
    expect(post).toHaveBeenCalledTimes(2);
  });

  it("says nothing from a browser elsewhere on the network", () => {
    open("bosun-hub.local");
    stop = startHeartbeat();
    vi.advanceTimersByTime(HEARTBEAT_MS * 3);
    paint();
    expect(post).not.toHaveBeenCalled();
  });

  it("stops", () => {
    open("localhost");
    stop = startHeartbeat();
    stop();
    vi.advanceTimersByTime(HEARTBEAT_MS * 3);
    paint();
    expect(post).not.toHaveBeenCalled();
  });

  it("marks the page for the watchdog, which ignores a Stage without heartbeat", () => {
    expect(html).toContain('<meta name="bosun-kiosk-heartbeat"');
  });
});
