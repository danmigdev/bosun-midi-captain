// Firmware update check - fetches the latest GitHub release for the Bosun
// repo and lets the editor decide whether to surface an "update firmware"
// affordance. One-shot at startup + manual refresh; no polling.
//
// The repo coordinates are placeholders until the public repo exists.
// Override at runtime by setting BOSUN_FIRMWARE_REPO in localStorage
// (`"owner/name"`), useful for testing against forks before merge.

import { invoke } from "@tauri-apps/api/core";

const DEFAULT_REPO_OWNER = "danmigdev";
const DEFAULT_REPO_NAME  = "bosun";

/** Version of the firmware tree bundled with this editor build (read from
 * the Tauri resource). The editor can always flash this over USB, so it is
 * the version an "update available" actually installs - independent of any
 * GitHub release. Returns "" if the backend can't read it (e.g. dev web
 * preview with no Tauri host). */
export async function fetchBundledVersion(): Promise<string> {
  try {
    return (await invoke<string>("bundled_firmware_version")) || "";
  } catch {
    return "";
  }
}

export interface FirmwareRelease {
  /** Bare version stripped of any leading `v` (e.g. "0.3.0"). */
  version: string;
  /** Raw tag as published (e.g. "v0.3.0"). */
  tag: string;
  /** Browser URL for the release page (download link target). */
  htmlUrl: string;
  /** ISO timestamp when GitHub published the release. */
  publishedAt: string;
}

export type UpdateStatus =
  | { kind: "idle" }
  | { kind: "checking" }
  | { kind: "ok"; installed: string; latest: FirmwareRelease | null; updateAvailable: boolean }
  | { kind: "error"; message: string };

function repoCoords(): { owner: string; name: string } {
  try {
    const override = localStorage.getItem("BOSUN_FIRMWARE_REPO");
    if (override && override.includes("/")) {
      const [o, n] = override.split("/", 2);
      if (o && n) return { owner: o, name: n };
    }
  } catch { /* localStorage may be unavailable */ }
  return { owner: DEFAULT_REPO_OWNER, name: DEFAULT_REPO_NAME };
}

export async function fetchLatestRelease(): Promise<FirmwareRelease | null> {
  const { owner, name } = repoCoords();
  const url = `https://api.github.com/repos/${owner}/${name}/releases/latest`;
  try {
    const r = await fetch(url, {
      headers: { Accept: "application/vnd.github+json" },
    });
    if (!r.ok) return null;
    const j = await r.json() as {
      tag_name?: string; html_url?: string; published_at?: string;
    };
    return {
      version: (j.tag_name ?? "").replace(/^v/, ""),
      tag: j.tag_name ?? "",
      htmlUrl: j.html_url ?? "",
      publishedAt: j.published_at ?? "",
    };
  } catch {
    return null;
  }
}

/** Semver-ish compare. Strips any pre-release suffix (`-scaffold`,
 * `-rc.1`, …) so a development build like `0.2.0-scaffold` is treated as
 * `0.2.0`. Positive when `a > b`, negative when `a < b`, zero on equal. */
export function compareVersions(a: string, b: string): number {
  const sanitize = (v: string) => v.split("-")[0];
  const pa = sanitize(a).split(".").map(n => parseInt(n, 10) || 0);
  const pb = sanitize(b).split(".").map(n => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const da = pa[i] || 0, db = pb[i] || 0;
    if (da !== db) return da - db;
  }
  return 0;
}

/** Convenience: compare the firmware version reported by the pedal with
 * the latest GitHub release and tell us whether an update is available
 * (returns null if either side is missing). */
export function evaluateUpdate(installed: string | null, latest: FirmwareRelease | null): boolean | null {
  if (!installed || !latest || !latest.version) return null;
  return compareVersions(latest.version, installed) > 0;
}
