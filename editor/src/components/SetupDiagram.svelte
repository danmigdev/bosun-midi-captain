<script lang="ts">
  import type { SetupId } from "../lib/setup-guide";
  let { id }: { id: SetupId } = $props();
  const direct = $derived(id === "direct");
  const pi = $derived(id.startsWith("pi"));
  const host = $derived(pi ? "Raspberry Pi 3" : id === "android" ? "Android + OTG" : "Computer");
</script>
<figure>
  <!-- svelte-ignore a11y_no_noninteractive_tabindex (The scrollable diagram needs keyboard focus for horizontal scrolling.) -->
  <div class="diagram-scroll" role="region" aria-label="Connection diagram; scroll horizontally on a small screen" tabindex="0">
  <svg viewBox="0 0 760 290" role="img" aria-label={direct ? "Captain USB-B connected to Kemper Player USB-A" : `Captain and Player connected over USB to ${host}${id === "pi-display" ? ", display connected over HDMI with separate power" : ""}`}>
    <g class="wires" fill="none" stroke-width="3">
      {#if direct}<path d="M205 125 H545" />
      {:else}
        <path d="M190 78 H285 V135 H310 M190 215 H285 V150 H310" />
        {#if id === "pi-display"}<path class="video" d="M475 135 H550" /><path class="power" d="M635 208 V173" />
        {:else if id === "pi-wireless" || id === "pi"}<path class="network" d="M475 135 H550" />{/if}
      {/if}
    </g>
    {#if direct}
      <rect x="15" y="78" width="190" height="100" rx="14" /><text x="110" y="115">MIDI Captain</text><text class="port" x="110" y="145">USB-B</text>
      <text class="cable" x="375" y="104">USB data cable</text>
      <rect x="545" y="78" width="200" height="100" rx="14" /><text x="645" y="115">Kemper Player</text><text class="port" x="645" y="145">USB-A (host)</text>
      <text class="note" x="380" y="239">Captain display · no software MIDI bridge</text>
    {:else}
      <rect x="15" y="38" width="175" height="80" rx="12" /><text x="102" y="70">MIDI Captain</text><text class="port" x="102" y="94">USB-B</text>
      <rect x="15" y="177" width="175" height="80" rx="12" /><text x="102" y="209">Kemper Player</text><text class="port" x="102" y="233">USB-B</text>
      <text class="cable" x="239" y="65">USB data</text><text class="cable" x="239" y="201">USB data</text>
      <rect class="host" x="310" y="88" width="165" height="104" rx="14" /><text x="392" y="116">{host}</text>
      <text class="port" x="392" y="140">{id === "android" ? "+ USB hub" : "USB-A ports"}</text><text class="port" x="392" y="169">{pi ? "Hub + MIDI" : "MIDI + Stage"}</text>
      {#if id === "pi-display"}
        <text class="cable" x="513" y="119">HDMI</text><rect x="550" y="91" width="190" height="82" rx="12" /><text x="645" y="124">Stage display</text><text class="port" x="645" y="150">Video</text>
        <text class="cable" x="635" y="228">Separate power supply</text><text class="note" x="635" y="251">Optional USB touch → Pi</text>
      {:else if pi}
        <text class="cable" x="515" y="117">Network</text><rect x="550" y="88" width="190" height="104" rx="12" /><text x="645" y="118">{id === "pi-wireless" ? "Android" : "App / browser"}</text><text class="port" x="645" y="143">Editor / Stage</text><text class="port" x="645" y="166">{id === "pi" ? "Optional" : "Wi-Fi"}</text>
      {:else}<text class="note" x="609" y="128">Keep Bosun running</text><text class="note" x="609" y="153">while playing</text>{/if}
    {/if}
  </svg>
  </div>
  <p class="scroll-hint">Swipe or scroll the diagram sideways to see every connection.</p>
  <figcaption>Lines show data connections. Power the Player with its own supply{pi ? ", and the Pi with its own supply" : ""}.</figcaption>
</figure>
<style>
  .diagram-scroll{overflow-x:auto;border-radius:12px}svg{min-width:640px}.scroll-hint{display:none;font-size:.8rem;color:var(--text-muted,#b4c1d2)}@media(max-width:700px){.scroll-hint{display:block}}
  figure{margin:1rem 0}svg{display:block;width:100%;background:var(--bg,#101721);border:1px solid var(--border,#334155);border-radius:12px}rect{fill:var(--bg-card,#1b2635);stroke:var(--border-strong,#526278);stroke-width:1.5}.host{stroke:var(--accent,#64dcb0)}text{fill:var(--text,#edf4fa);font:600 16px system-ui;text-anchor:middle}.port{font-size:13px;font-weight:400}.cable,.note{font-size:12px;font-weight:400;fill:var(--text-muted,#b4c1d2)}.wires{stroke:var(--accent,#64dcb0)}.video{stroke:#a89bff}.power{stroke:#e7b863}.network{stroke:#75baff;stroke-dasharray:6 5}figcaption{font-size:.8rem;color:var(--text-muted);line-height:1.5;margin-top:.5rem}
</style>
