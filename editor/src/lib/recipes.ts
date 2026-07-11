import {
  defaultMessageFromSchema,
  type Binding,
  type Manifest,
  type MessageSchema,
  type MidiMessage,
} from "./protocol";

// --------------------- recipe model ---------------------

/** One slot a recipe needs the user to fill in - typically "which switch". */
export interface RecipeRole {
  key: string;
  label: string;
  hint?: string;
  optional?: boolean;
}

/** Everything a recipe needs to decide availability and build bindings:
 * the active profile's plugin kind and the full manifest. */
export interface RecipeCtx {
  activeKind: string;
  manifest: Manifest;
}

/** A guided one-click setup. `available` gates whether the recipe is offered
 * (e.g. tuner only when the active plugin exposes a tuner message). `build`
 * turns the role->switch assignment into concrete Bindings. Both must be pure
 * and deterministic - no Date.now()/Math.random(). */
export interface Recipe {
  id: string;
  label: string;
  description: string;
  icon?: string;
  roles: RecipeRole[];
  available(ctx: RecipeCtx): boolean;
  build(assign: Record<string, string>, ctx: RecipeCtx): Binding[];
}

// --------------------- plugin message lookup helpers ---------------------

/** Search the ACTIVE plugin's messages for a type whose id ends in `suffix`
 * (e.g. "_tuner"). Returns the message type id, or undefined if the active
 * plugin is missing or has no matching message. Deterministic: iterates the
 * plugin's messages in insertion order and returns the first match. */
export function findPluginMessageType(ctx: RecipeCtx, suffix: string): string | undefined {
  const plugin = ctx.manifest?.plugins?.[ctx.activeKind];
  if (!plugin) return undefined;
  for (const type of Object.keys(plugin.messages)) {
    if (type.endsWith(suffix)) return type;
  }
  return undefined;
}

/** The active plugin's tuner message type, if any (a "*_tuner" message that
 * carries a `state` enum including "on"/"off"). */
export function pluginTunerType(ctx: RecipeCtx): string | undefined {
  const plugin = ctx.manifest?.plugins?.[ctx.activeKind];
  if (!plugin) return undefined;
  for (const [type, schema] of Object.entries(plugin.messages)) {
    if (!type.endsWith("_tuner")) continue;
    const state = schema.params?.state;
    const values = state?.values?.map(String) ?? [];
    if (values.includes("on") && values.includes("off")) return type;
  }
  return undefined;
}

/** The active plugin's tap-tempo message type, if any ("*_tap_tempo"). */
export function pluginTapTempoType(ctx: RecipeCtx): string | undefined {
  return findPluginMessageType(ctx, "_tap_tempo");
}

// --------------------- internal build helpers ---------------------

/** Look up a message schema (plugin message on the active plugin) so we can
 * seed defaults for every param before overriding the ones we care about. */
function pluginMessageSchema(ctx: RecipeCtx, type: string): MessageSchema | undefined {
  return ctx.manifest?.plugins?.[ctx.activeKind]?.messages?.[type];
}

/** Build a plugin message filled with schema defaults, then apply overrides. */
function pluginMessage(
  ctx: RecipeCtx,
  type: string,
  overrides: Record<string, unknown> = {},
): MidiMessage {
  const schema = pluginMessageSchema(ctx, type);
  const base = schema ? defaultMessageFromSchema(type, schema) : { type };
  return { ...base, ...overrides };
}

/** A single-action tap binding firing one message on `press`. */
function tapBinding(sw: string, label: string, msg: MidiMessage): Binding {
  return {
    switch: sw,
    mode: "tap",
    label,
    actions: { press: { messages: [msg] } },
  };
}

// --------------------- recipes ---------------------

export const RECIPES: Recipe[] = [
  {
    id: "preview",
    label: "Preset preview",
    description:
      "Scroll through patches on the screen without loading them, then load the one you pick or back out. No MIDI fires for patches you skip past.",
    icon: "🔍",
    roles: [
      { key: "up", label: "Scroll up", hint: "Steps to the next patch in the preview" },
      { key: "down", label: "Scroll down", hint: "Steps to the previous patch" },
      { key: "commit", label: "Confirm (load)", hint: "Loads the previewed patch" },
      { key: "cancel", label: "Cancel", hint: "Returns to the current patch", optional: true },
    ],
    available: () => true,
    build(assign) {
      const out: Binding[] = [];
      if (assign.up) {
        out.push(
          tapBinding(assign.up, "Preview +", {
            type: "captain_preview_step",
            delta: 1,
            scope: "patch",
          }),
        );
      }
      if (assign.down) {
        out.push(
          tapBinding(assign.down, "Preview -", {
            type: "captain_preview_step",
            delta: -1,
            scope: "patch",
          }),
        );
      }
      if (assign.commit) {
        out.push(
          tapBinding(assign.commit, "Load", { type: "captain_preview_commit" }),
        );
      }
      if (assign.cancel) {
        out.push(
          tapBinding(assign.cancel, "Cancel", { type: "captain_preview_cancel" }),
        );
      }
      return out;
    },
  },

  {
    id: "bank_nav",
    label: "Bank up / down",
    description:
      "Step through banks with two switches. Bank up moves to the next bank, bank down to the previous one.",
    icon: "↕",
    roles: [
      { key: "bankUp", label: "Bank up", hint: "Steps to the next bank" },
      { key: "bankDown", label: "Bank down", hint: "Steps to the previous bank" },
    ],
    available: () => true,
    build(assign) {
      const out: Binding[] = [];
      if (assign.bankUp) {
        out.push(
          tapBinding(assign.bankUp, "Bank +", { type: "captain_bank_step", delta: 1 }),
        );
      }
      if (assign.bankDown) {
        out.push(
          tapBinding(assign.bankDown, "Bank -", { type: "captain_bank_step", delta: -1 }),
        );
      }
      return out;
    },
  },

  {
    id: "tuner",
    label: "Toggle tuner",
    description:
      "Turn the tuner on and off from one switch. The switch latches: first press opens the tuner, next press closes it.",
    icon: "🎵",
    roles: [
      { key: "switch", label: "Tuner switch", hint: "Latches the tuner on and off" },
    ],
    available(ctx) {
      return pluginTunerType(ctx) !== undefined;
    },
    build(assign, ctx) {
      const type = pluginTunerType(ctx);
      if (!type || !assign.switch) return [];
      return [
        {
          switch: assign.switch,
          mode: "latched",
          label: "Tuner",
          actions: {
            toggle_on: { messages: [pluginMessage(ctx, type, { state: "on" })] },
            toggle_off: { messages: [pluginMessage(ctx, type, { state: "off" })] },
          },
        },
      ];
    },
  },

  {
    id: "tap_tempo",
    label: "Tap tempo",
    description:
      "Tap a switch in time to set the tempo. Each press sends a tap-tempo message to your device.",
    icon: "⏱",
    roles: [
      { key: "switch", label: "Tap switch", hint: "Tap in time to set the tempo" },
    ],
    available(ctx) {
      return pluginTapTempoType(ctx) !== undefined;
    },
    build(assign, ctx) {
      const type = pluginTapTempoType(ctx);
      if (!type || !assign.switch) return [];
      return [tapBinding(assign.switch, "Tap", pluginMessage(ctx, type))];
    },
  },
];
