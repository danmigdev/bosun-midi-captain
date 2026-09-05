// Stands in for `@tauri-apps/api/event` in the kiosk build. Routes the
// events `src/lib/protocol.ts` and `StageView.svelte` subscribe to
// (`firmware-data-ready`, `firmware-disconnected`,
// `firmware-reconnecting`, `firmware-reconnected`) to the WebSocket link.
import { wsLink } from "./ws-link";

export type UnlistenFn = () => void;

export async function listen(
  event: string,
  handler: (event: { payload: unknown }) => void,
): Promise<UnlistenFn> {
  return wsLink.on(event, handler);
}

export async function once(
  event: string,
  handler: (event: { payload: unknown }) => void,
): Promise<UnlistenFn> {
  const off = wsLink.on(event, (e) => {
    off();
    handler(e);
  });
  return off;
}

export async function emit(): Promise<void> {
  // The kiosk never emits toward a backend.
}
