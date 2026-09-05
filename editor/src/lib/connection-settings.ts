export type ConnectionMode = "usb" | "network";
export type ConnectionSettings = { mode: ConnectionMode; host: string; port: string };

const STORAGE_KEY = "BOSUN_CONNECTION";
const defaults = (): ConnectionSettings => ({ mode: "usb", host: "", port: "9876" });

export function readConnectionSettings(): ConnectionSettings {
  try {
    const value = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "null");
    if (value && (value.mode === "usb" || value.mode === "network")) {
      return {
        mode: value.mode,
        host: typeof value.host === "string" ? value.host.slice(0, 253) : "",
        port: typeof value.port === "string" ? value.port.slice(0, 5) : "9876",
      };
    }
  } catch { /* Storage may be unavailable in a restricted WebView. */ }
  return defaults();
}

export function saveConnectionSettings(settings: ConnectionSettings): void {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(settings)); } catch {}
}

/** Validate before opening a socket; bracket IPv6 for Rust's SocketAddr parser. */
export function networkAddress(hostInput: string, portInput: string): string {
  const host = hostInput.trim().replace(/^\[([^\]]+)\]$/, "$1");
  const port = portInput.trim();
  if (!host) throw new Error("Enter the Raspberry Pi IP address or hostname.");
  if (!/^\d{1,5}$/.test(port) || Number(port) < 1 || Number(port) > 65535) {
    throw new Error("Enter a port between 1 and 65535 (usually 9876).");
  }
  if (host.includes(":")) {
    try {
      // URL validates IPv6 syntax without performing any network request.
      new URL(`http://[${host}]:${Number(port)}/`);
      return `[${host}]:${Number(port)}`;
    } catch {
      throw new Error("Enter an IP address or hostname only; use the Port field for the port.");
    }
  }
  if (host.length > 253 || !host.split(".").every(label =>
    /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i.test(label))) {
    throw new Error("Enter a valid IP address or hostname, without a URL or path.");
  }
  if (/^[\d.]+$/.test(host) &&
      (host.split(".").length !== 4 || host.split(".").some(octet => Number(octet) > 255))) {
    throw new Error("Enter a valid IP address, such as 192.168.1.100.");
  }
  return `${host}:${Number(port)}`;
}
