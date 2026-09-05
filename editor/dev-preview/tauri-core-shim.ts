// Dev-only shim so StageView can run in a plain browser tab (no Tauri host)
// for interactive preview. Not part of the production build - only reached
// via vite.stage-preview.config.ts's alias, which the real app build never
// loads.
type Win = typeof window & { __stageInbox?: string[] };

export function invoke<T = unknown>(cmd: string): Promise<T> {
  if (cmd === "drain_inbox") {
    const w = window as Win;
    const lines = w.__stageInbox ?? [];
    w.__stageInbox = [];
    return Promise.resolve(lines as unknown as T);
  }
  return Promise.resolve(undefined as unknown as T);
}
