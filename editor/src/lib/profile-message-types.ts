import type { Manifest } from "./protocol";

/** Command choices belong to the edited profile, not the Captain USB model.
 * A saved device config can identify older/offline profiles before their kind
 * arrives. Ambiguous or missing metadata exposes only the core MIDI commands. */
export function filterManifestForProfile(
  manifest: Manifest,
  activeKind: string,
  device?: Record<string, unknown> | null,
): Manifest {
  let kind = activeKind;
  if (!kind && device) {
    const candidates = Object.entries(manifest.plugins).filter(([, plugin]) => {
      const key = plugin.config_schema?.key;
      const block = key ? device[key] : undefined;
      return block !== null && typeof block === "object" && !Array.isArray(block);
    });
    if (candidates.length === 1) kind = candidates[0][0];
  }
  const plugin = kind ? Object.entries(manifest.plugins).find(([id]) => id === kind)?.[1] : undefined;
  return { ...manifest, plugins: plugin ? { [kind]: plugin } : {} };
}
