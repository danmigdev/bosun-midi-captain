import type { Manifest } from "./protocol";

/** A shared config block does not identify a model. Prefer the active plugin;
 * without its kind, expose only blocks that have an unambiguous owner. */
export function configuredPlugins(
  manifest: Manifest,
  activeKind: string,
  device?: Record<string, unknown> | null,
) {
  const entries = Object.entries(manifest.plugins);
  return entries.filter(([id, plugin]) => {
    if (id === activeKind) return true;
    const key = plugin.config_schema?.key;
    const block = key ? device?.[key] : undefined;
    if (!key || block === null || typeof block !== "object" || Array.isArray(block)) return false;
    return entries.filter(([, candidate]) => candidate.config_schema?.key === key).length === 1;
  });
}

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
    const candidates = configuredPlugins(manifest, "", device);
    if (candidates.length === 1) kind = candidates[0][0];
  }
  const plugin = kind ? Object.entries(manifest.plugins).find(([id]) => id === kind)?.[1] : undefined;
  if (!plugin) return { ...manifest, plugins: {} };
  const config = plugin.config_schema;
  const block = config ? device?.[config.key] : undefined;
  const values = block && typeof block === "object" && !Array.isArray(block)
    ? block as Record<string, unknown> : {};
  const messages = Object.fromEntries(Object.entries(plugin.messages).filter(([, schema]) =>
    Object.entries(schema.requires ?? {}).every(([key, expected]) =>
      (values[key] ?? config?.fields[key]?.default) === expected),
  ));
  return { ...manifest, plugins: { [kind]: { ...plugin, messages } } };
}
