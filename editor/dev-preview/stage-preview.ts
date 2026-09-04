// Dev-only interactive harness: mounts StageView with the real Tauri API
// swapped for a local shim (see vite.stage-preview.config.ts's alias), and
// drives it via the same PATCH/CONTEXT/EVENT firmware-message bus the real
// serial connection uses, but from on-page controls instead of hardware -
// lets you preview Stage View's look with no pedal or Kemper attached.
import { mount } from "svelte";
import StageView from "../src/components/StageView.svelte";
import type { Binding, BindingMode } from "../src/lib/protocol";

type Win = typeof window & { __stageInbox?: string[]; __stageDoorbell?: () => void };

function push(msg: unknown): Promise<void> {
  const w = window as Win;
  w.__stageInbox = [JSON.stringify(msg)];
  w.__stageDoorbell?.();
  return new Promise((r) => setTimeout(r, 10));
}

const SWITCHES = ["1", "2", "3", "4", "up", "A", "B", "C", "D", "down"];
const COLORS = ["#ff3b3b", "#3b82f6", "#22c55e", "#eab308", "#a855f7", "#ec4899", "#06b6d4", "#f97316", "#84cc16", "#64748b"];

type SwitchCfg = { enabled: boolean; mode: BindingMode; color: string; label: string; active: boolean };
const switches: Record<string, SwitchCfg> = Object.fromEntries(
  SWITCHES.map((sw, i) => [
    sw,
    { enabled: i < 6, mode: "latched" as BindingMode, color: COLORS[i], label: `Effect ${sw}`, active: false },
  ]),
);

let deviceInfo = { fw: "0.6.0", device: "midi_captain_10", bank: 1, slot: 1 };

mount(StageView, {
  target: document.getElementById("app")!,
  props: {
    deviceInfo,
    manifest: null,
    device: null,
    connected: true,
    patches: [],
    onExit: () => {},
  },
});

function bindingsFromState(): Binding[] {
  return SWITCHES.filter((sw) => switches[sw].enabled).map((sw) => ({
    switch: sw,
    mode: switches[sw].mode,
    actions: {},
    label: switches[sw].label,
    led: { on: switches[sw].color },
  }));
}

async function pushPatch(rigName: string, bank: number, slot: number): Promise<void> {
  deviceInfo = { ...deviceInfo, bank, slot };
  await push({
    type: "PATCH",
    bank,
    slot,
    patch: { name: rigName, bindings: bindingsFromState() },
  });
}

async function pushContext(bpm: string, tunerOn: boolean, tunerNote: string): Promise<void> {
  const context: Record<string, unknown> = {};
  if (bpm.trim() !== "") context.kemper_bpm = Number(bpm);
  context.kemper_tuner = tunerOn ? "on" : "off";
  if (tunerOn) {
    context.kemper_tuner_note = tunerNote;
    context.kemper_tuner_deviance = 8192;
  }
  await push({ type: "CONTEXT", context });
}

async function toggleActive(sw: string): Promise<void> {
  switches[sw].active = !switches[sw].active;
  await push({
    type: "EVENT",
    event: "binding_fired",
    switch: sw,
    action: switches[sw].active ? "toggle_on" : "toggle_off",
  });
  renderSwitchList();
}

function renderSwitchList(): void {
  const el = document.getElementById("switchList")!;
  el.innerHTML = SWITCHES.map((sw) => {
    const c = switches[sw];
    return `
      <div class="sw-row" data-sw="${sw}">
        <input type="checkbox" class="sw-enabled" ${c.enabled ? "checked" : ""} title="Bound" />
        <input type="text" class="sw-label" value="${c.label}" />
        <select class="sw-mode">
          ${(["tap", "latched", "momentary"] as BindingMode[])
            .map((m) => `<option value="${m}" ${c.mode === m ? "selected" : ""}>${m}</option>`)
            .join("")}
        </select>
        <input type="color" class="sw-color" value="${c.color}" />
        <button class="toggle-active ${c.active ? "on" : ""}" title="Fire binding_fired">${sw}</button>
      </div>`;
  }).join("");

  el.querySelectorAll<HTMLInputElement>(".sw-enabled").forEach((input) => {
    input.addEventListener("change", () => {
      switches[input.closest<HTMLElement>(".sw-row")!.dataset.sw!].enabled = input.checked;
    });
  });
  el.querySelectorAll<HTMLInputElement>(".sw-label").forEach((input) => {
    input.addEventListener("input", () => {
      switches[input.closest<HTMLElement>(".sw-row")!.dataset.sw!].label = input.value;
    });
  });
  el.querySelectorAll<HTMLSelectElement>(".sw-mode").forEach((sel) => {
    sel.addEventListener("change", () => {
      switches[sel.closest<HTMLElement>(".sw-row")!.dataset.sw!].mode = sel.value as BindingMode;
    });
  });
  el.querySelectorAll<HTMLInputElement>(".sw-color").forEach((input) => {
    input.addEventListener("input", () => {
      switches[input.closest<HTMLElement>(".sw-row")!.dataset.sw!].color = input.value;
    });
  });
  el.querySelectorAll<HTMLButtonElement>(".toggle-active").forEach((btn) => {
    btn.addEventListener("click", () => {
      void toggleActive(btn.closest<HTMLElement>(".sw-row")!.dataset.sw!);
    });
  });
}

function wireControls(): void {
  renderSwitchList();

  document.getElementById("toggleBtn")!.addEventListener("click", () => {
    document.getElementById("controls")!.classList.toggle("collapsed");
  });

  document.getElementById("applyBtn")!.addEventListener("click", () => {
    const rigName = (document.getElementById("rigName") as HTMLInputElement).value;
    const bank = Number((document.getElementById("bank") as HTMLInputElement).value) || 1;
    const slot = Number((document.getElementById("slot") as HTMLInputElement).value) || 1;
    const bpm = (document.getElementById("bpm") as HTMLInputElement).value;
    const tunerOn = (document.getElementById("tunerOn") as HTMLInputElement).checked;
    const tunerNote = (document.getElementById("tunerNote") as HTMLInputElement).value;
    void pushPatch(rigName, bank, slot).then(() => pushContext(bpm, tunerOn, tunerNote));
  });
}

wireControls();
// Push an initial state so Stage View isn't empty on first load.
void pushPatch(
  (document.getElementById("rigName") as HTMLInputElement).value,
  1,
  1,
).then(() => pushContext("120", false, "A"));
