// Dev-only shim - see tauri-core-shim.ts.
type Win = typeof window & { __stageDoorbell?: () => void };

export function listen(event: string, handler: () => void): Promise<() => void> {
  if (event === "firmware-data-ready") {
    (window as Win).__stageDoorbell = handler;
  }
  return Promise.resolve(() => {});
}
