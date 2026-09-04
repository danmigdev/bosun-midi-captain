// Stands in for `@tauri-apps/api/core` in the kiosk build (see the alias
// in vite.stage-kiosk.config.ts). Only the commands `src/lib/protocol.ts`
// actually issues on the Stage path are wired to the WebSocket link; the
// rest return harmless defaults so nothing throws.
import { wsLink } from "./ws-link";

wsLink.start();

export async function invoke<T = unknown>(
  cmd: string,
  args?: Record<string, unknown>,
): Promise<T> {
  switch (cmd) {
    case "send_command":
      wsLink.send(String(args?.line ?? ""));
      return undefined as T;
    case "drain_inbox":
      return wsLink.drain() as unknown as T;
    case "is_connected":
      return wsLink.isConnected() as unknown as T;

    // Connection management: the kiosk is always "connected" to the hub.
    case "auto_connect":
      return "hub" as unknown as T;
    case "connect":
    case "tcp_connect":
    case "disconnect":
      return undefined as T;
    case "list_ports":
    case "tcp_list_ports":
      return [] as unknown as T;

    // MIDI bridge: the Pi does this in the kernel, not here.
    case "midi_list_ports":
      return { inputs: [], outputs: [] } as unknown as T;
    case "midi_bridge_status":
      return { active: false, kemper_port: null, pedal_port: null } as unknown as T;
    case "midi_bridge_start":
    case "midi_bridge_stop":
      return undefined as T;

    default:
      return undefined as T;
  }
}
