// Dev-only shim so StageView can run in a plain browser tab (no Tauri host)
// for interactive preview. Not part of the production build - only reached
// via vite.stage-preview.config.ts's alias, which the real app build never
// loads.
type Win = typeof window & {
  __stageInbox?: string[];
  __stageCommand?: (message: Record<string, unknown>) => void | Promise<void>;
  __stageInvoke?: (command: string, args?: unknown) => unknown | Promise<unknown>;
};

export function invoke<T = unknown>(cmd: string, args?: { line?: string }): Promise<T> {
  if (cmd === "drain_inbox") {
    const w = window as Win;
    const lines = w.__stageInbox ?? [];
    w.__stageInbox = [];
    return Promise.resolve(lines as unknown as T);
  }
  // Optional browser-fixture peer. Production uses the real transport;
  // this seam lets input checks observe commands and deliver correlated replies.
  if (cmd === "send_command" && args?.line) {
    return Promise.resolve((window as Win).__stageCommand?.(JSON.parse(args.line)))
      .then(() => undefined as T);
  }
  // The App parity fixture supplies connection metadata as well as messages;
  // ordinary Stage previews retain the no-host default.
  return Promise.resolve((window as Win).__stageInvoke?.(cmd, args) as T);
}
