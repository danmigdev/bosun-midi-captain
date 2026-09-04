// Kiosk transport: a WebSocket to the bosun-hub, presented to the rest
// of the app as the same `invoke("send_command" | "drain_inbox" | ...)`
// plus `listen("firmware-data-ready" | "firmware-disconnected" | ...)`
// surface the Tauri build uses. `vite.stage-kiosk.config.ts` aliases
// `@tauri-apps/api/core` and `@tauri-apps/api/event` onto the two thin
// wrappers next to this file, so `src/lib/protocol.ts` and
// `components/StageView.svelte` run unchanged.
//
// The hub sends one protocol line per WebSocket text message, plus
// `{"type":"HUB","link":"up"|"down"}` frames reporting whether the hub
// itself is talking to the pedal. Those are handled here (they drive the
// reconnecting/reconnected events) and never reach the firmware bus.

type Handler = (event: { payload: unknown }) => void;

const RECONNECT_BACKOFF_MS = [500, 1000, 2000, 3000, 5000];

function wsUrl(): string {
  const override = new URLSearchParams(location.search).get("ws");
  if (override) return override;
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  // The hub serves the kiosk bundle on :8080 and the protocol WS on :8081
  // of the same host.
  return `${proto}//${location.hostname}:8081/`;
}

class WsLink {
  private ws: WebSocket | null = null;
  private inbox: string[] = [];
  private doorbell: (() => void) | null = null;
  private listeners = new Map<string, Set<Handler>>();
  private wantOpen = false;
  private backoffStep = 0;
  private socketOpen = false;
  linkUp = false;

  start(): void {
    if (this.wantOpen) return;
    this.wantOpen = true;
    this.open();
  }

  private open(): void {
    let ws: WebSocket;
    try {
      ws = new WebSocket(wsUrl());
    } catch {
      this.scheduleReopen();
      return;
    }
    this.ws = ws;

    ws.onopen = () => {
      this.socketOpen = true;
      this.backoffStep = 0;
      this.emit("firmware-reconnected");
    };

    ws.onmessage = (ev) => {
      const raw = typeof ev.data === "string" ? ev.data : "";
      for (const line of raw.split("\n")) {
        const s = line.trim();
        if (!s) continue;
        if (this.handleControl(s)) continue;
        this.inbox.push(s);
      }
      this.doorbell?.();
    };

    ws.onclose = () => {
      this.socketOpen = false;
      this.linkUp = false;
      this.ws = null;
      if (this.wantOpen) {
        this.emit("firmware-disconnected");
        this.scheduleReopen();
      }
    };

    ws.onerror = () => {
      try { ws.close(); } catch { /* noop */ }
    };
  }

  private scheduleReopen(): void {
    const delay = RECONNECT_BACKOFF_MS[
      Math.min(this.backoffStep, RECONNECT_BACKOFF_MS.length - 1)
    ];
    this.backoffStep += 1;
    this.emit("firmware-reconnecting");
    setTimeout(() => { if (this.wantOpen) this.open(); }, delay);
  }

  /** Returns true if the line was a hub control frame (not a firmware line). */
  private handleControl(line: string): boolean {
    if (line.indexOf('"HUB"') === -1) return false;
    try {
      const obj = JSON.parse(line) as { type?: string; link?: string };
      if (obj.type !== "HUB") return false;
      const up = obj.link === "up";
      if (up !== this.linkUp) {
        this.linkUp = up;
        this.emit(up ? "firmware-reconnected" : "firmware-reconnecting");
      }
      return true;
    } catch {
      return false;
    }
  }

  // -- surface used by the tauri-core / tauri-event shims ---------------

  send(line: string): void {
    if (this.socketOpen && this.ws) {
      this.ws.send(line.replace(/\r?\n$/, ""));
    }
  }

  drain(): string[] {
    const out = this.inbox;
    this.inbox = [];
    return out;
  }

  isConnected(): boolean {
    return this.socketOpen && this.linkUp;
  }

  on(event: string, handler: Handler): () => void {
    if (event === "firmware-data-ready") {
      this.doorbell = () => handler({ payload: null });
      return () => { this.doorbell = null; };
    }
    let set = this.listeners.get(event);
    if (!set) { set = new Set(); this.listeners.set(event, set); }
    set.add(handler);
    return () => { set!.delete(handler); };
  }

  private emit(event: string): void {
    const set = this.listeners.get(event);
    if (set) for (const h of set) h({ payload: null });
    try { window.dispatchEvent(new CustomEvent(event)); } catch { /* noop */ }
  }
}

export const wsLink = new WsLink();
