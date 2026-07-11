<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import {
    cmd,
    continuousControlTypes,
    defaultMessageFromSchema,
    type Manifest,
    type ExpressionConfig,
    type MidiMessage,
  } from "../lib/protocol";

  type Props = {
    /** device.expression - the array this component edits in place. The parent
     * (Settings) owns persistence via its "Save settings" button. */
    expression: ExpressionConfig[];
    manifest: Manifest | null;
    /** Whether a pedal is connected - gates the live-calibration polling. */
    connected?: boolean;
  };
  let { expression = $bindable(), manifest = null, connected = true }: Props = $props();

  // Curve options the firmware understands. "linear" is the default; log/exp
  // shape the response for volume-style pedals.
  const CURVES = ["linear", "log", "exp"];

  // Message-type choices for a jack: "CC" plus any manifest continuous-control
  // message (a message whose params include a value int 0-127). Derived so a
  // newly-loaded manifest immediately widens the dropdown.
  let msgTypes = $derived(continuousControlTypes(manifest));

  // ----- live calibration polling (STATS.expression) -----
  // Same pattern as Dashboard.svelte: poll STATS on an interval while the
  // section is mounted and the pedal is connected. The response carries
  // per-jack raw (0..65535) + calibrated value (0..127) we render as bars.
  let live = $state<Record<number, { raw: number; value: number; armed?: boolean; present?: boolean }>>({});
  let statsTimer: ReturnType<typeof setInterval> | null = null;

  onMount(() => { if (connected) startPoll(); });
  onDestroy(() => stopPoll());
  $effect(() => { connected ? startPoll() : stopPoll(); });

  function startPoll() {
    if (statsTimer) return;
    pollOnce();
    statsTimer = setInterval(pollOnce, 1000);
  }
  function stopPoll() {
    if (statsTimer) { clearInterval(statsTimer); statsTimer = null; }
  }
  async function pollOnce() {
    try {
      const s = await cmd.getStats();
      const next: Record<number, { raw: number; value: number; armed?: boolean; present?: boolean }> = {};
      for (const e of s.expression ?? []) next[e.jack] = { raw: e.raw, value: e.value, armed: e.armed, present: e.present };
      live = next;
    } catch { /* ignore poll errors - firmware may lack expression support */ }
  }

  function liveFor(jack: number) {
    return live[jack] ?? null;
  }

  /** Auto-detected status badge for an enabled jack, driven by the firmware's
   * presence probe + arming (STATS.expression). Returns null when there's
   * nothing useful to show (disabled, disconnected, or older firmware that
   * doesn't report present/armed). */
  function detectStatus(exp: ExpressionConfig): { text: string; kind: string } | null {
    if (!exp.enabled || !connected) return null;
    const l = liveFor(exp.jack);
    if (!l || l.present === undefined) return null;   // fw without detection
    if (l.present === false) return { text: "no pedal detected", kind: "absent" };
    if (l.armed === false) return { text: "move pedal to activate", kind: "waiting" };
    return { text: "active", kind: "active" };
  }

  // Position 0..1 of the raw reading between the configured min/max, for the
  // filled portion of the calibrated bar. Clamped so an out-of-range raw
  // doesn't overflow the track.
  function calibratedPct(exp: ExpressionConfig): number {
    const l = liveFor(exp.jack);
    if (!l) return 0;
    const lo = exp.calibration?.min ?? 0;
    const hi = exp.calibration?.max ?? 65535;
    if (hi <= lo) return 0;
    let t = (l.raw - lo) / (hi - lo);
    if (exp.invert) t = 1 - t;
    return Math.max(0, Math.min(1, t)) * 100;
  }

  function ensureMessage(exp: ExpressionConfig): MidiMessage {
    if (!exp.message) exp.message = { type: "cc", channel: 1, cc: 11, value: 0 };
    return exp.message;
  }

  function onTypeChange(exp: ExpressionConfig, newType: string) {
    if (newType === "cc") {
      // Preserve the classic CC template shape (default expression pedal is
      // CC 11 on channel 1) so switching back to CC is predictable.
      exp.message = { type: "cc", channel: 1, cc: 11, value: 0 };
      return;
    }
    // Plugin continuous-control message: seed from its schema defaults so its
    // required params (e.g. channel) are present, then it substitutes `value`.
    const schema = manifest?.plugins
      ? Object.values(manifest.plugins).flatMap(p => Object.entries(p.messages)).find(([t]) => t === newType)?.[1]
      : undefined;
    exp.message = schema ? defaultMessageFromSchema(newType, schema) : { type: newType, value: 0 };
  }

  function captureMin(exp: ExpressionConfig) {
    const l = liveFor(exp.jack);
    if (!l) return;
    if (!exp.calibration) exp.calibration = { min: 0, max: 65535 };
    exp.calibration.min = l.raw;
  }
  function captureMax(exp: ExpressionConfig) {
    const l = liveFor(exp.jack);
    if (!l) return;
    if (!exp.calibration) exp.calibration = { min: 0, max: 65535 };
    exp.calibration.max = l.raw;
  }
</script>

<div class="exps">
  {#each expression as exp, i (exp.jack ?? i)}
    {@const msg = ensureMessage(exp)}
    <div class="exp" class:disabled={!exp.enabled}>
      <div class="exphead">
        <span class="jack">EXP {exp.jack}</span>
        <label class="cb">
          <input type="checkbox" bind:checked={exp.enabled} />
          enabled
        </label>
        <label class="cb">
          <input type="checkbox" bind:checked={exp.invert} />
          invert
        </label>
        {#if detectStatus(exp)}
          {@const st = detectStatus(exp)}
          <span class="detect {st?.kind}" title="Auto-detected from the pedal: no external plug-detect, sensed electrically">{st?.text}</span>
        {/if}
        <span class="grow"></span>
        <label class="curve">curve
          <select bind:value={exp.curve}>
            {#each CURVES as c}<option value={c}>{c}</option>{/each}
          </select>
        </label>
      </div>

      <!-- Live calibration bar. The track spans the configured min..max; the
           fill tracks the live raw reading mapped into that range. -->
      <div class="calibrate">
        {#if liveFor(exp.jack)}
          <div class="bar" title="raw {liveFor(exp.jack)?.raw} -> value {liveFor(exp.jack)?.value}">
            <div class="fill" style:width="{calibratedPct(exp)}%"></div>
          </div>
          <span class="readout">
            raw {liveFor(exp.jack)?.raw} · out {liveFor(exp.jack)?.value}
          </span>
        {:else}
          <div class="bar muted-bar"></div>
          <span class="readout muted">
            {connected ? "No live reading (move the pedal, or firmware lacks expression support)" : "Connect to calibrate"}
          </span>
        {/if}
      </div>

      <div class="calrow">
        <label>min <input type="number" min="0" max="65535" bind:value={exp.calibration.min} /></label>
        <button onclick={() => captureMin(exp)} disabled={!liveFor(exp.jack)} title="Record the current raw reading as the minimum">Capture min</button>
        <label>max <input type="number" min="0" max="65535" bind:value={exp.calibration.max} /></label>
        <button onclick={() => captureMax(exp)} disabled={!liveFor(exp.jack)} title="Record the current raw reading as the maximum">Capture max</button>
      </div>

      <div class="msgrow">
        <label>Sends
          <select value={msg.type} onchange={(e) => onTypeChange(exp, (e.target as HTMLSelectElement).value)}>
            {#each msgTypes as t}<option value={t.type}>{t.label}</option>{/each}
          </select>
        </label>
        {#if msg.type === "cc"}
          <label>channel <input type="number" min="1" max="16" bind:value={msg.channel} /></label>
          <label>CC # <input type="number" min="0" max="127" bind:value={msg.cc} /></label>
        {:else if msg.channel !== undefined}
          <label>channel <input type="number" min="1" max="16" bind:value={msg.channel} /></label>
        {/if}
      </div>
    </div>
  {/each}
</div>

<style>
  .exps { display: flex; flex-direction: column; gap: 0.75rem; }
  .exp {
    border: 1px solid var(--border); border-radius: 5px;
    padding: 0.6rem 0.75rem; background: var(--bg);
    transition: opacity 0.15s ease;
  }
  .exp.disabled { opacity: 0.6; }
  .exphead { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.55rem; }
  .exphead .grow { flex: 1; }
  .jack {
    background: var(--bg-hover); color: var(--warn-text);
    padding: 0.25rem 0.5rem; border-radius: 3px;
    font-family: ui-monospace, Consolas, monospace; font-size: 0.8rem;
    min-width: 4rem; text-align: center;
  }
  .cb { flex-direction: row !important; align-items: center; gap: 0.35rem; color: var(--text); font-size: 0.8rem; }
  .curve { flex-direction: row !important; align-items: center; gap: 0.4rem; }

  .detect {
    font-size: 0.68rem; padding: 0.15rem 0.45rem; border-radius: 3px;
    border: 1px solid var(--border-strong); white-space: nowrap;
  }
  .detect.absent  { color: var(--warn-text); border-color: var(--warn-text); }
  .detect.waiting { color: var(--text-muted); }
  .detect.active  { color: var(--accent); border-color: var(--accent); }

  .calibrate { display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0.5rem; }
  .bar {
    flex: 1; height: 12px; border-radius: 6px;
    background: var(--bg-hover); border: 1px solid var(--border-strong);
    overflow: hidden;
  }
  .bar.muted-bar { opacity: 0.5; }
  .fill { height: 100%; background: var(--accent); transition: width 0.15s linear; }
  .readout { font-size: 0.72rem; color: var(--text-muted); font-variant-numeric: tabular-nums; min-width: 9rem; }
  .readout.muted { color: var(--text-dim); }

  .calrow, .msgrow { display: flex; align-items: end; gap: 0.55rem; flex-wrap: wrap; }
  .calrow { margin-bottom: 0.5rem; }
  .calrow label, .msgrow label { display: flex; flex-direction: column; gap: 0.2rem; font-size: 0.72rem; color: var(--text-muted); }
  .calrow input, .msgrow input, .msgrow select {
    background: var(--bg-input); color: var(--text); border: 1px solid var(--border-strong);
    padding: 0.3rem 0.45rem; border-radius: 3px; font-size: 0.82rem;
  }
  .calrow button {
    padding: 0.32rem 0.6rem; font-size: 0.76rem; align-self: end;
  }
</style>
