// Which plugin config sections the Settings page should show.
//
// Rule: a plugin's section appears when EITHER the active profile is that
// plugin's kind, OR the device.json already carries that plugin's config
// block (its CONFIG_SCHEMA.key). The second clause matters because
// `activeKind` is resolved asynchronously (LIST_PROFILES after a connect /
// profile switch) and can briefly be "" - an imported profile whose
// device.json has e.g. a `kemper` block must still show its Kemper section
// even before activeKind has caught up. With no profile (empty device, no
// activeKind) nothing is shown, which is the intended "don't surface plugin
// settings when there's no profile for that plugin" behaviour.

import type { Manifest, PluginConfigSchema } from "./protocol";

export function pluginSectionsToShow(
  manifest: Manifest | null | undefined,
  activeKind: string,
  device: Record<string, unknown> | null | undefined,
): PluginConfigSchema[] {
  if (!manifest) return [];
  const out: PluginConfigSchema[] = [];
  for (const [id, plug] of Object.entries(manifest.plugins)) {
    const cfg = plug.config_schema;
    if (!cfg) continue;
    const isActiveKind = !!activeKind && id === activeKind;
    const block = device ? device[cfg.key] : undefined;
    const hasBlock = typeof block === "object" && block !== null;
    if (isActiveKind || hasBlock) out.push(cfg);
  }
  return out;
}
