import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";


// --------------------- domain types ---------------------

export type BindingMode =
  | "tap"
  | "latched"
  | "momentary"
  | "long_press_alt"
  | "double_tap";

/** A MIDI message is opaque to the editor core - type + arbitrary params.
 * Concrete shape is described by the firmware's MANIFEST (core + plugins). */
export interface MidiMessage {
  type: string;
  [key: string]: unknown;
}

export interface Action {
  messages: MidiMessage[];
}

export interface Binding {
  switch: string;
  mode: BindingMode;
  label?: string;
  led?: { on?: string; off?: string };
  auto_momentary?: boolean;
  /** Text shown on the TFT (via the hold_effect field) while this latched +
   *  auto-momentary switch is held past the threshold. Empty/absent = nothing. */
  hold_text?: string;
  actions: Record<string, Action>;
}

export interface Patch {
  name?: string;
  tft_color?: string;
  on_enter?: Action;
  /** Optional message chain fired when leaving this patch (symmetric to
   * on_enter). Fires just before the next patch's on_enter. */
  on_exit?: Action;
  bindings: Binding[];
  /** Explicit links: when the user runs "Apply to linked", this patch's
   * full payload (everything except `linked_to`) is copied to each
   * (bank, slot) in this list. The implicit "same-slot-across-banks"
   * links are derived at the UI layer and merged with this list. */
  linked_to?: Array<{ bank: number; slot: number }>;
}


// --------------------- manifest schema (data-driven UI) ---------------------

export type ParamType = "int" | "string" | "enum" | "bool";

export interface ParamSchema {
  type: ParamType;
  label?: string;
  default?: unknown;
  min?: number;
  max?: number;
  pattern?: string;
  values?: Array<string | number>;
  /** Conditional visibility: shown only when all sibling-param values match. */
  if?: Record<string, unknown>;
}

export interface MessageSchema {
  label: string;
  params: Record<string, ParamSchema>;
  summary?: string;          // "Scene {scene}" - template for compact display
}

export interface PluginConfigFieldSchema {
  type: "bool" | "int" | "string" | "enum";
  label?: string;
  default?: boolean | number | string;
  min?: number;
  max?: number;
  values?: Array<string | number>;
  /** Persisted in device.json but not rendered in Settings (e.g. a debug
   * flag flipped by hand). The whole section is hidden if no field is visible. */
  hidden?: boolean;
}

export interface PluginConfigSchema {
  /** Section key under device.json (e.g. "kemper" → device.kemper). */
  key: string;
  /** Header text shown in Settings. */
  label: string;
  /** Optional hint paragraph rendered above the fields. */
  hint?: string;
  fields: Record<string, PluginConfigFieldSchema>;
}

export interface TftFieldSchema {
  label?: string;
  sample?: string | number;
}

export interface PluginRecipePcLayout {
  preset_regex: string;
  groups: Array<{ name: string; min?: number; max?: number }>;
  index_formula: string;
  pc_max: number;
  bank_msb_label: string;
  pc_label: string;
}

export interface PluginRecipeSchema {
  /** Route id - used as the nav item id and the URL fragment. */
  id: string;
  label: string;
  icon?: string;
  target_message_type: string;
  preset_field: string;
  channel_field: string;
  channel_default: number;
  hint?: string;
  missing_message?: string;
  instructions?: string;
  save_note?: string;
  /** Optional: if set, the page also computes a MIDI table from the
   * preset string. Plugins whose presets aren't addressable as
   * bank_msb + PC simply omit this field. */
  pc_layout?: PluginRecipePcLayout;
}

export interface PluginManifestEntry {
  label: string;
  version: string;
  messages: Record<string, MessageSchema>;
  config_schema?: PluginConfigSchema | null;
  recipe_schema?: PluginRecipeSchema | null;
  tft_fields?: Record<string, TftFieldSchema>;
  default_layout?: Array<Record<string, unknown>>;
}

export interface Manifest {
  core_messages: Record<string, MessageSchema>;
  plugins: Record<string, PluginManifestEntry>;
}

/** One expression-pedal jack config, persisted under device.expression[]. The
 * `message` is a template; the firmware substitutes the live 0..127 position
 * into its `value` field before sending. */
export interface ExpressionConfig {
  jack: number;
  enabled: boolean;
  invert: boolean;
  calibration: { min: number; max: number };
  curve: string;
  message: MidiMessage;
}

/** The core message types the firmware always handles, regardless of which
 * plugins are loaded. Mirrors firmware/lib/captain/messages.py CORE_MESSAGE_TYPES.
 * Used as a fallback so the editor stays usable (Patches/Editor with core MIDI
 * only) when GET_MANIFEST never lands - e.g. an older firmware truncating the
 * large plugin manifest response. Keep in sync with the firmware file. */
export const CORE_MESSAGE_TYPES: Record<string, MessageSchema> = {
  cc: {
    label: "Control Change",
    params: {
      channel: { type: "int", min: 1, max: 16, default: 1, label: "Channel" },
      cc:      { type: "int", min: 0, max: 127, default: 0, label: "CC #" },
      value:   { type: "int", min: 0, max: 127, default: 0, label: "Value" },
    },
    summary: "CC {cc}={value} ch {channel}",
  },
  pc: {
    label: "Program Change",
    params: {
      channel: { type: "int", min: 1, max: 16, default: 1, label: "Channel" },
      program: { type: "int", min: 0, max: 127, default: 0, label: "Program" },
    },
    summary: "PC {program} ch {channel}",
  },
  note_on: {
    label: "Note On",
    params: {
      channel:  { type: "int", min: 1, max: 16, default: 1, label: "Channel" },
      note:     { type: "int", min: 0, max: 127, default: 60, label: "Note" },
      velocity: { type: "int", min: 0, max: 127, default: 100, label: "Velocity" },
    },
    summary: "Note On {note} v{velocity} ch {channel}",
  },
  note_off: {
    label: "Note Off",
    params: {
      channel:  { type: "int", min: 1, max: 16, default: 1, label: "Channel" },
      note:     { type: "int", min: 0, max: 127, default: 60, label: "Note" },
      velocity: { type: "int", min: 0, max: 127, default: 64, label: "Velocity" },
    },
    summary: "Note Off {note} ch {channel}",
  },
  delay: {
    label: "Delay",
    params: {
      ms: { type: "int", min: 0, max: 5000, default: 100, label: "Milliseconds" },
    },
    summary: "Wait {ms}ms",
  },
  captain_patch: {
    label: "Switch Captain Patch",
    params: {
      bank: { type: "int", min: 1, max: 99, default: 1, label: "Bank" },
      slot: { type: "int", min: 1, max: 10, default: 1, label: "Slot" },
    },
    summary: "→ Captain {bank}/{slot}",
  },
  captain_bank_step: {
    label: "Step Captain Bank",
    params: {
      delta: { type: "int", min: -10, max: 10, default: 1, label: "Delta (banks)" },
    },
    summary: "Bank step {delta}",
  },
  captain_preview_step: {
    label: "Preview Step",
    params: {
      delta: { type: "int", min: -10, max: 10, default: 1, label: "Delta" },
      scope: { type: "enum", values: ["patch", "bank"], default: "patch", label: "Scope" },
    },
    summary: "Preview {scope} {delta}",
  },
  captain_preview_commit: {
    label: "Preview Commit",
    params: {},
    summary: "Preview commit",
  },
  captain_preview_cancel: {
    label: "Preview Cancel",
    params: {},
    summary: "Preview cancel",
  },
  captain_setlist_step: {
    label: "Setlist Step",
    params: {
      delta: { type: "int", min: -10, max: 10, default: 1, label: "Delta" },
    },
    summary: "Setlist step {delta}",
  },
};

/** Build a minimal manifest from the known core message types, with no
 * plugins. Used when the firmware's GET_MANIFEST never returns so the editor
 * stays usable with core MIDI messages only. */
export function fallbackManifest(): Manifest {
  // Deep-clone so callers can't mutate the shared CORE_MESSAGE_TYPES constant.
  return {
    core_messages: structuredClone(CORE_MESSAGE_TYPES),
    plugins: {},
  };
}

export interface FlattenedSchema extends MessageSchema {
  /** message type id (e.g. "cc" or "ampero_scene") */
  type: string;
  /** display source: "core" or the plugin's label */
  source: string;
}

export function flattenManifest(m: Manifest): FlattenedSchema[] {
  const out: FlattenedSchema[] = [];
  for (const [type, schema] of Object.entries(m.core_messages)) {
    out.push({ ...schema, type, source: "core" });
  }
  for (const plugin of Object.values(m.plugins)) {
    for (const [type, schema] of Object.entries(plugin.messages)) {
      out.push({ ...schema, type, source: plugin.label });
    }
  }
  return out;
}

/** Continuous-control message types usable as an expression-pedal target: any
 * message (core "cc" or a plugin message) whose params include a `value` int
 * field spanning 0..127 - the range the firmware substitutes the live pedal
 * position into. Returns `{ type, label }` entries for a dropdown. Always
 * includes "cc" first (it is a core message and always present). */
export function continuousControlTypes(m: Manifest | null): Array<{ type: string; label: string }> {
  const out: Array<{ type: string; label: string }> = [];
  const seen = new Set<string>();
  const consider = (type: string, schema: MessageSchema, source: string) => {
    if (seen.has(type)) return;
    const v = schema.params?.value;
    if (v && v.type === "int" && (v.min ?? 0) <= 0 && (v.max ?? 0) >= 127) {
      seen.add(type);
      out.push({ type, label: source === "core" ? schema.label : `${source} · ${schema.label}` });
    }
  };
  if (m) {
    for (const [type, schema] of Object.entries(m.core_messages)) consider(type, schema, "core");
    for (const plugin of Object.values(m.plugins)) {
      for (const [type, schema] of Object.entries(plugin.messages)) consider(type, schema, plugin.label);
    }
  }
  // Guarantee "cc" is present and first even if the manifest is missing.
  if (!seen.has("cc")) out.unshift({ type: "cc", label: "Control Change" });
  else {
    const idx = out.findIndex(o => o.type === "cc");
    if (idx > 0) out.unshift(out.splice(idx, 1)[0]);
  }
  return out;
}

/** Build a fresh message object filled with the schema defaults. */
export function defaultMessageFromSchema(type: string, schema: MessageSchema): MidiMessage {
  const out: MidiMessage = { type };
  for (const [name, p] of Object.entries(schema.params)) {
    if (p.default !== undefined) out[name] = p.default;
    else if (p.type === "int") out[name] = p.min ?? 0;
    else if (p.type === "enum" && p.values?.length) out[name] = p.values[0];
    else if (p.type === "bool") out[name] = false;
    else out[name] = "";
  }
  return out;
}

/** Render a message's summary using its schema template, falling back to JSON. */
export function summarizeMessage(msg: MidiMessage, schema?: MessageSchema): string {
  if (schema?.summary) {
    return schema.summary.replace(/\{(\w+)\}/g, (_, k) => String(msg[k] ?? ""));
  }
  const params = Object.entries(msg).filter(([k]) => k !== "type")
    .map(([k, v]) => `${k}=${v}`).join(" ");
  return `${msg.type}  ${params}`;
}


// --------------------- transport plumbing ---------------------

export interface PortInfo { name: string; kind: string; }
export interface PatchSummary {
  bank: number;
  slot: number;
  name: string;
  dirty: boolean;
  /** Explicit cross-bank links. The patches view uses this to draw
   *  connector lines between linked tiles. Optional - older firmware
   *  doesn't populate it. */
  linked_to?: Array<{ bank: number; slot: number }>;
}
export interface ProfileInfo { id: string; name: string; kind: string; color?: string | null; active: boolean; }

export interface FirmwareFile { rel: string; dst: string; size: number; }

export interface DeviceStats {
  uptime_ms: number;
  mem_free: number;
  mem_alloc: number;
  loop_iters: number;
  midi_rx_count: number;
  midi_tx_count: number;
  protocol_cmd_count: number;
  last_patch_switch_ms: number;
  current: { bank: number; slot: number };
  /** Live expression-jack readings (firmware 0.4.x+). `raw` is the ADC value
   * 0..65535; `value` is the calibrated 0..127 the firmware would send. Absent
   * on firmware without expression support. `armed` is false until the input
   * has moved (so a bare/parked jack sends nothing); `present` is false when
   * the presence probe finds no pedal plugged - both optional for older fw. */
  expression?: Array<{ jack: number; raw: number; value: number;
                       armed?: boolean; present?: boolean }>;
}

export interface MidiLearnEntry {
  channel: number;
  bank_msb: number;
  pc: number;
  /** Captain patch id in "BB/SS" zero-padded form, e.g. "01/03" */
  captain_patch: string;
}
export interface MidiLearnTable {
  pc_to_patch: MidiLearnEntry[];
}

export type MidiInKind = "cc" | "pc" | "note_on" | "note_off" | "poly_pressure" | "channel_pressure" | "pitch_bend" | "unknown";

export interface MidiInCapturedEvent {
  port: "din" | "usb";
  channel: number;
  kind: MidiInKind;
  data: number[];
}

export type FirmwareMessage =
  | { type: "ACK"; id?: string; fw?: string }
  | { type: "ERROR"; id?: string; error: string; of?: string; detail?: string }
  | { type: "DEVICE_INFO"; id?: string; fw: string; device: string; current: { bank: number; slot: number } }
  | { type: "GLOBAL"; id?: string; device: Record<string, unknown> }
  | { type: "PATCH_LIST"; id?: string; patches: PatchSummary[] }
  | { type: "PATCH"; id?: string; bank: number; slot: number; patch: Patch }
  | { type: "SAVED"; id?: string; patches: Array<{ bank: number; slot: number }> }
  | { type: "DIRTY"; id?: string; patches: Array<{ bank: number; slot: number }> }
  | { type: "MIDI_LEARN"; id?: string; table: MidiLearnTable }
  | { type: "MANIFEST"; id?: string; core_messages: Record<string, MessageSchema>; plugins: Record<string, PluginManifestEntry> }
  | ({ type: "STATS"; id?: string } & DeviceStats)
  | { type: "PROFILE_LIST"; id?: string; profiles: ProfileInfo[]; active: string }
  | { type: "FONT_LIST"; id?: string; fonts: string[] }
  // Current rig name (and best-effort colour) read from a device that can
  // report it (Kemper). `name` is the live rig name ("" if the device hasn't
  // broadcast one yet). `rig` is the flat rig index 1..125 the name belongs to
  // (null if unknown). `color` is a POSITION colour hint (the device does not
  // report a real per-rig colour) - may be null. `fresh` is true when the name
  // is tagged to the rig the device is currently on.
  | { type: "RIG_INFO"; id?: string; name: string; rig: number | null; color: string | null; fresh: boolean }
  | { type: "EVENT"; event: string; [k: string]: unknown };


export const ACTION_KEYS_BY_MODE: Record<BindingMode, string[]> = {
  tap:             ["press"],
  latched:         ["toggle_on", "toggle_off"],
  momentary:       ["press", "release"],
  long_press_alt:  ["press", "long_press"],
  double_tap:      ["press", "double_tap"],
};


export async function listPorts(): Promise<PortInfo[]> {
  return invoke<PortInfo[]>("list_ports");
}

// ---- USB-MIDI bridge (Kemper Player <-> pedal) ----
// Relays MIDI both ways so MIDI Learn capture and the bidirectional sync work
// without running tools/midi_bridge.py by hand. Separate from the CDC link.
export type MidiPorts = { inputs: string[]; outputs: string[] };
export type BridgeStatus = {
  active: boolean;
  kemper_port: string | null;
  pedal_port: string | null;
};
export async function midiListPorts(): Promise<MidiPorts> {
  return invoke<MidiPorts>("midi_list_ports");
}
export async function midiBridgeStart(kemper?: string, pedal?: string): Promise<BridgeStatus> {
  return invoke<BridgeStatus>("midi_bridge_start", { kemper: kemper ?? null, pedal: pedal ?? null });
}
export async function midiBridgeStop(): Promise<void> {
  await invoke("midi_bridge_stop");
}
export async function midiBridgeStatus(): Promise<BridgeStatus> {
  return invoke<BridgeStatus>("midi_bridge_status");
}
export async function connect(port: string): Promise<void> {
  await invoke("connect", { port });
}
export async function autoConnect(): Promise<string> {
  return invoke<string>("auto_connect");
}
export async function disconnect(): Promise<void> {
  // Unblock anyone still waiting on a response - the Rust side is about
  // to release the handle and won't deliver anything else.
  _failPending("explicit disconnect");
  await invoke("disconnect");
}
export async function isConnected(): Promise<boolean> {
  return invoke<boolean>("is_connected");
}

/** Wait for the firmware to come back online after a self-issued reboot
 * (SWITCH_PROFILE, REBOOT, etc). Tries autoConnect repeatedly within
 * `budgetMs` and verifies the link with a PING. Returns true on
 * success, false on timeout. Always force-disconnects first so a stale
 * Rust handle from before the reboot doesn't block the new connection.
 *
 * Use this from any flow that sends a reboot-causing command and needs
 * to keep talking to the pedal afterwards (export-across-profiles,
 * import-as-new-profile). The host-side disconnect/reconnect dance is
 * what the editor's UI uses too - the budget defaults are tuned to a
 * CircuitPython Pico cold boot (~3s for USB re-enumeration plus a
 * second for firmware init). */
export async function waitForReboot(budgetMs = 15000): Promise<boolean> {
  // Signal "connecting" to the editor shell so the topbar connection
  // pill shows the pulsing "Connecting…" indicator while we retry.
  // The shell tracks a counter so concurrent waitForReboot calls
  // don't deactivate each other prematurely.
  try { window.dispatchEvent(new CustomEvent("bosun-connecting", { detail: { active: true } })); } catch {}
  try {
    const deadline = Date.now() + budgetMs;
    // Initial wait so the Pico has time to actually drop the USB CDC -
    // attempting autoConnect immediately just races the still-alive
    // handle.
    await new Promise(r => setTimeout(r, 1500));
    let waitMs = 700;
    while (Date.now() < deadline) {
      try { await disconnect(); } catch {}
      try {
        await invoke<string>("auto_connect");
        // Confirm liveness with a PING - autoConnect's own probe only
        // checks the CDC layer, not that the firmware is responding to
        // protocol messages.
        try {
          await sendAndAwait({ type: "PING" }, 1500);
          // Tell the editor shell to re-sync: the Rust side is
          // connected again, but App.svelte's `connected` state was
          // flipped to false by the disconnect event during reboot.
          // The shell listens for this and refetches the world.
          try { window.dispatchEvent(new CustomEvent("connection-resynced")); } catch {}
          return true;
        } catch { /* port up but firmware not ready - retry */ }
      } catch { /* port not back yet */ }
      await new Promise(r => setTimeout(r, waitMs));
      waitMs = Math.min(waitMs + 400, 2500);
    }
    return false;
  } finally {
    try { window.dispatchEvent(new CustomEvent("bosun-connecting", { detail: { active: false } })); } catch {}
  }
}
async function send(message: object): Promise<void> {
  try {
    await invoke("send_command", { line: JSON.stringify(message) });
  } catch (e) {
    const msg = String(e);
    // Both "not connected" (Rust pre-check) and "write: ..." (port died
    // mid-write - Rust's send_command marks the handle dead and surfaces
    // the OS error) indicate the connection is gone. Fire the event for
    // either so the UI doesn't keep firing commands at a dead port.
    if (msg.includes("not connected") || msg.startsWith("write:") || msg.includes("write: ")) {
      try { window.dispatchEvent(new CustomEvent("rust-disconnected", { detail: msg })); } catch {}
    }
    throw e;
  }
}

/** Fail-fast all in-flight sendAndAwait promises. Called when we detect
 *  the connection died, so the UI doesn't have to wait per-call timeouts
 *  before unblocking. Each resolver sees an ERROR and rejects its promise. */
function _failPending(reason: string): void {
  if (_pending.size === 0) return;
  for (const [id, cb] of _pending) {
    try {
      cb({ type: "ERROR", id, error: "disconnected", detail: reason } as FirmwareMessage);
    } catch {}
  }
  _pending.clear();
}

if (typeof window !== "undefined") {
  window.addEventListener("rust-disconnected", (e) => {
    const detail = (e as CustomEvent).detail;
    _failPending("rust-disconnected: " + String(detail ?? ""));
  });
}

let _nextId = 1;
function nextId(): string { return String(_nextId++); }


// --------------------- request/response helper ---------------------

type PendingResolver = (msg: FirmwareMessage) => void;
const _pending = new Map<string, PendingResolver>();
let _awaitListener: UnlistenFn | null = null;

/** Set of subscribers receiving every firmware message after drain. */
const _firmwareSubscribers = new Set<(msg: FirmwareMessage) => void>();
/** Raw-line subscribers (used by debug listeners that want pre-parse text). */
const _firmwareRawSubscribers = new Set<(line: string) => void>();
let _doorbellListener: UnlistenFn | null = null;
let _draining = false;

async function _drainOnce(): Promise<void> {
  if (_draining) return;
  _draining = true;
  try {
    const lines = await invoke<string[]>("drain_inbox");
    for (const line of lines) {
      for (const sub of _firmwareRawSubscribers) sub(line);
      let obj: FirmwareMessage | null = null;
      try { obj = JSON.parse(line) as FirmwareMessage; }
      catch { console.warn("non-json firmware line:", line); continue; }
      const id = (obj as { id?: string }).id;
      if (id && _pending.has(id)) {
        const cb = _pending.get(id)!;
        _pending.delete(id);
        cb(obj);
      }
      for (const sub of _firmwareSubscribers) sub(obj);
    }
  } finally {
    _draining = false;
  }
}

async function _ensureDoorbell(): Promise<void> {
  if (_doorbellListener) return;
  _doorbellListener = await listen("firmware-data-ready", () => { void _drainOnce(); });
  // Always drain once on subscribe in case the reader queued data before
  // we registered (e.g. across HMR reloads with a live serial handle).
  void _drainOnce();
}

async function _ensureAwaitListener(): Promise<void> {
  await _ensureDoorbell();
}

/** Send a command and resolve on the matching `id` response. */
export async function sendAndAwait<T extends FirmwareMessage = FirmwareMessage>(
  message: { type: string; id?: string; [k: string]: unknown },
  timeoutMs = 5000,
): Promise<T> {
  await _ensureAwaitListener();
  if (!message.id) message.id = nextId();
  const id = message.id!;
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => {
      _pending.delete(id);
      reject(new Error(`timeout: ${message.type}#${id}`));
    }, timeoutMs);
    _pending.set(id, (msg) => {
      clearTimeout(timer);
      if (msg.type === "ERROR") {
        reject(new Error(`error: ${(msg as { error?: string }).error ?? "unknown"}`));
      } else {
        resolve(msg as T);
      }
    });
    invoke("send_command", { line: JSON.stringify(message) }).catch((e) => {
      clearTimeout(timer);
      _pending.delete(id);
      // Rust's send_command rejects with "not connected" when the
      // SerialHandle has been dropped (typically because the firmware
      // rebooted and the reader thread errored out). Fire a global
      // event so the App component can drop its `connected` flag and
      // offer a reconnect. We DON'T autoreconnect here - keeping it
      // explicit so the user knows what happened.
      const msg = String(e);
      if (msg.includes("not connected")) {
        try { window.dispatchEvent(new CustomEvent("rust-disconnected", { detail: msg })); } catch {}
      }
      reject(e);
    });
  });
}

export async function onFirmwareMessage(handler: (msg: FirmwareMessage) => void): Promise<UnlistenFn> {
  _firmwareSubscribers.add(handler);
  await _ensureDoorbell();
  return () => { _firmwareSubscribers.delete(handler); };
}

/** Subscribe to the raw line text as drained from the inbox (pre-JSON parse).
 *  Returns an unlisten function. Intended for debug instrumentation. */
export async function onFirmwareRawLine(handler: (line: string) => void): Promise<UnlistenFn> {
  _firmwareRawSubscribers.add(handler);
  await _ensureDoorbell();
  return () => { _firmwareRawSubscribers.delete(handler); };
}
export async function onDisconnected(handler: () => void): Promise<UnlistenFn> {
  return listen("firmware-disconnected", handler);
}


export const cmd = {
  ping:           () => send({ type: "PING",            id: nextId() }),
  getDeviceInfo:  () => send({ type: "GET_DEVICE_INFO", id: nextId() }),
  getGlobal:      () => send({ type: "GET_GLOBAL",      id: nextId() }),
  putGlobal:      (device: Record<string, unknown>) => sendAndAwait({ type: "PUT_GLOBAL", device }, 4000),
  getManifest:    () => send({ type: "GET_MANIFEST",    id: nextId() }),
  listPatches:    () => send({ type: "LIST_PATCHES",    id: nextId() }),
  getPatch:       (bank: number, slot: number) => send({ type: "GET_PATCH",       id: nextId(), bank, slot }),
  switchPatch:    (bank: number, slot: number) => send({ type: "SWITCH_PATCH",    id: nextId(), bank, slot }),
  putBinding:     (bank: number, slot: number, binding: Binding) =>
                    send({ type: "PUT_BINDING", id: nextId(), bank, slot, binding }),
  putPatch:       (bank: number, slot: number, patch: Patch) =>
                    send({ type: "PUT_PATCH", id: nextId(), bank, slot, patch }),
  // SAVE_NOW / DISCARD scope: omit bank/slot to act on every dirty
  // patch (the global toolbar's behavior); pass a specific bank+slot
  // to scope to a single patch (the per-patch buttons in the editor).
  saveNow:        (bank?: number, slot?: number) =>
                    send({ type: "SAVE_NOW", id: nextId(),
                           ...(bank !== undefined ? { bank, slot } : {}) }),
  discard:        (bank?: number, slot?: number) =>
                    send({ type: "DISCARD", id: nextId(),
                           ...(bank !== undefined ? { bank, slot } : {}) }),
  getDirty:       () => send({ type: "GET_DIRTY",       id: nextId() }),
  startLearn:     () => send({ type: "START_MIDI_LEARN", id: nextId() }),
  stopLearn:      () => send({ type: "STOP_MIDI_LEARN", id: nextId() }),
  getMidiLearn:   () => send({ type: "GET_MIDI_LEARN", id: nextId() }),
  putMidiLearn:   (table: MidiLearnTable) => send({ type: "PUT_MIDI_LEARN", id: nextId(), table }),
  reboot:         () => send({ type: "REBOOT", id: nextId() }),
  getStats:       () => sendAndAwait<{ type: "STATS" } & DeviceStats>({ type: "STATS" }, 6000),
  putFileBegin:   (path: string) => sendAndAwait({ type: "PUT_FILE_BEGIN", path }, 5000),
  putFileChunk:   (path: string, data_b64: string) => sendAndAwait({ type: "PUT_FILE_CHUNK", path, data_b64 }, 5000),
  putFileEnd:     (path: string) => sendAndAwait({ type: "PUT_FILE_END", path }, 5000),

  // ----- profiles -----
  listProfiles:   () => sendAndAwait<{ type: "PROFILE_LIST"; profiles: ProfileInfo[]; active: string }>(
                          { type: "LIST_PROFILES" }, 4000),
  // `color` is an optional hex swatch (e.g. "#6fd99b"). Passed through when
  // set; firmware that predates color support simply ignores the extra field.
  createProfile:  (profile_id: string, name: string, kind: string, color?: string) =>
                    sendAndAwait({ type: "CREATE_PROFILE", profile_id, name, kind,
                                   ...(color ? { color } : {}) }, 4000),
  switchProfile:  (profile_id: string) =>
                    sendAndAwait({ type: "SWITCH_PROFILE", profile_id }, 4000),
  deleteProfile:  (profile_id: string) =>
                    sendAndAwait({ type: "DELETE_PROFILE", profile_id }, 4000),
  renameProfile:  (profile_id: string, name: string) =>
                    sendAndAwait({ type: "RENAME_PROFILE", profile_id, name }, 4000),

  // ----- fonts -----
  listFonts:      () => sendAndAwait<{ type: "FONT_LIST"; fonts: string[] }>(
                          { type: "LIST_FONTS" }, 3000),

  // ----- device rig info (Kemper) -----
  // Read the current rig name (and best-effort position colour) from a device
  // that can report it. Routes to the active plugin on the firmware; ERRORs
  // ("no_rig_info") when the active profile has no such device. `request` asks
  // the device to refresh; pass false to read only the firmware's cache.
  getRigInfo:     (request = true) =>
                    sendAndAwait<{ type: "RIG_INFO"; name: string; rig: number | null; color: string | null; fresh: boolean }>(
                      { type: "GET_RIG_INFO", request }, 3000),

  // ----- MIDI monitor -----
  // Stream inbound/outbound MIDI to the editor as "midi" EVENTs. Gated: the
  // firmware only emits while this is on, so it stays off during normal use
  // and is enabled just while the monitor panel is open.
  setMidiMonitor: (on: boolean) => sendAndAwait<{ type: "ACK"; on: boolean }>(
                          { type: "SET_MIDI_MONITOR", on }, 3000),
};

export function patchIdOf(bank: number, slot: number): string {
  return `${String(bank).padStart(2,"0")}/${String(slot).padStart(2,"0")}`;
}
export function parsePatchId(id: string): { bank: number; slot: number } | null {
  const m = id.match(/^(\d+)\/(\d+)$/);
  if (!m) return null;
  return { bank: parseInt(m[1], 10), slot: parseInt(m[2], 10) };
}


const _debouncers = new Map<string, ReturnType<typeof setTimeout>>();

export function debouncedPutBinding(
  bank: number, slot: number, binding: Binding, key: string, ms = 300,
): void {
  const existing = _debouncers.get(key);
  if (existing) clearTimeout(existing);
  _debouncers.set(key, setTimeout(() => {
    cmd.putBinding(bank, slot, binding);
    _debouncers.delete(key);
  }, ms));
}


// --------------------- test-only introspection ---------------------
// Read-only sizes of the module's internal bookkeeping maps/sets. Used by
// the endurance/leak tests to assert these structures return to baseline
// (no unbounded growth) after churn. This getter is a pure observer: it
// never mutates state and has no effect on runtime behavior. Kept out of
// the public transport surface deliberately - it exists only so tests can
// prove the absence of leaks without reaching into module internals via
// hacks. Do not use from application code.
export function __getInternalSizes(): {
  pending: number;
  subscribers: number;
  rawSubscribers: number;
  debouncers: number;
} {
  return {
    pending: _pending.size,
    subscribers: _firmwareSubscribers.size,
    rawSubscribers: _firmwareRawSubscribers.size,
    debouncers: _debouncers.size,
  };
}
