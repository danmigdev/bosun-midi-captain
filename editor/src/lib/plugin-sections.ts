// Which plugin config sections the Settings page should show.
//
// Show the active plugin and unambiguous saved config blocks. A shared block
// such as `kemper` needs the profile kind to choose between Player and Head;
// it must never produce duplicate sections or select the wrong model while
// LIST_PROFILES is still pending.

import type { Manifest, PluginConfigSchema } from "./protocol";
import { configuredPlugins } from "./profile-message-types";

export function pluginSectionsToShow(
  manifest: Manifest | null | undefined,
  activeKind: string,
  device: Record<string, unknown> | null | undefined,
): PluginConfigSchema[] {
  if (!manifest) return [];
  const out: PluginConfigSchema[] = [];
  for (const [, plug] of configuredPlugins(manifest, activeKind, device)) {
    const cfg = plug.config_schema;
    if (!cfg) continue;
    out.push(cfg);
  }
  return out;
}
