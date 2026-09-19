<script lang="ts">
  import { onMount } from "svelte";
  import { invoke } from "@tauri-apps/api/core";
  import { setups, faq, readSetup, saveSetup, piInstallCommand, type SetupId } from "../lib/setup-guide";
  import { IS_ANDROID } from "../lib/platform";
  import SetupDiagram from "./SetupDiagram.svelte";
  let { onClose, onInstall, onConfigure, standalone = false, connected = false, hasProfile = false }:
    { onClose?: () => void; onInstall?: () => void; onConfigure?: () => void; standalone?: boolean; connected?: boolean; hasProfile?: boolean } = $props();
  let selected = $state<SetupId | null>(readSetup()), step = $state(0);
  let dialog = $state<HTMLDialogElement>();
  let host = $state("bosun.local"), user = $state(""), archive = $state(""), error = $state("");
  let saving = $state(false), copied = $state(false);
  const setup = $derived(setups.find(s => s.id === selected));
  const titles = ["Choose your setup", "Prepare the Captain", "Prepare the host", "Connect your devices", "Test and FAQ"];
  const command = $derived(piInstallCommand(archive,user,host));
  onMount(() => { if (!standalone) dialog?.showModal(); });
  function choose(id: SetupId) { selected = id; saveSetup(id); }
  async function exportPi() {
    error = ""; saving = true;
    try { archive = await invoke<string | null>("export_pi_setup") || archive; copied = false; }
    catch (e) { error = String(e); } finally { saving = false; }
  }
  async function copy() {
    try { await navigator.clipboard.writeText(command); copied = true; }
    catch { error = "Select the command and copy it manually."; }
  }
</script>
{#snippet content()}
  <header><div><p class="eyebrow">BOSUN - FIRST SETUP GUIDE</p><h1>{titles[step]}</h1></div>
    {#if onClose}<button onclick={onClose} aria-label="Close guide">Close</button>{/if}</header>
  <nav aria-label="Setup steps">{#each titles as title,i}<button class:current={step === i} aria-current={step === i ? "step" : undefined} disabled={i > 0 && !setup} onclick={() => step = i}>{i+1}. {title}</button>{/each}</nav>
  <main>
    {#if step === 0}
      <p>How do you want to use your Captain? Choose the result you want: this guide will show the cables and steps you need. You can change your choice at any time.</p>
      <div class="choices">{#each setups as item}<button class:selected={selected === item.id} aria-pressed={selected === item.id} onclick={() => choose(item.id)}><strong>{item.title}</strong><span>{item.outcome}</span></button>{/each}</div>
      {#if setup}<SetupDiagram id={setup.id} /><h2>What you need</h2><ul>{#each setup.needs as item}<li>{item}</li>{/each}</ul>{/if}
    {:else if step === 1}
      <h2>Start directly with native firmware</h2>
      <ol><li>Download the latest <a href="https://github.com/danmigdev/bosun-midi-captain/releases/latest" target="_blank" rel="noreferrer">Bosun Desktop release</a>. On Windows, extract the whole folder and open Bosun.exe.</li>
        <li>Connect <strong>Captain USB-B to your computer</strong> with a data cable. This temporary connection is needed even if you will later use Android or Raspberry Pi.</li>
        <li>Choose <strong>Install native firmware</strong> and check the model, device and version. Bosun saves and verifies the original backup, installs the firmware and checks startup.</li>
        <li>Connect the Captain in the editor and create a <strong>Kemper Player</strong> profile. Assign the footswitches and save. If native Bosun is already installed, use the firmware update action to preserve your profiles.</li></ol>
      <div class="actions">
        {#if !standalone && !IS_ANDROID}<button class="primary" onclick={onInstall}>Install native firmware</button>{:else}<p>To install Captain firmware, open this guide in Bosun Desktop on a computer.</p>{/if}
        {#if connected && onConfigure}<button onclick={onConfigure}>{hasProfile ? "Open configuration" : "Create Kemper Player profile"}</button>{/if}
      </div>
      <p class="note">The backup preserves original firmware and files; factory settings are not automatically converted into Bosun profiles. Keep USB and power connected throughout backup and writing.</p>
    {:else if step === 2 && setup?.pi}
      <h2>1. Write the microSD card</h2>
      <p>Open <a href="https://www.raspberrypi.com/software/" target="_blank" rel="noreferrer">Raspberry Pi Imager</a> on your computer. Select <strong>Raspberry Pi 3</strong>, <strong>Raspberry Pi OS Lite (64-bit), based on Debian Trixie</strong>, and your microSD. Writing erases the selected card: check its name and capacity.</p>
      <p>In customisation, set the hostname to <strong>bosun</strong>, choose your own username and password, configure Wi-Fi if you are not using Ethernet, and enable <strong>SSH</strong>. Keep a note of your credentials. Wait for Imager to verify the card, eject it and insert it into the powered-off Pi.</p>
      <h2>2. Start the Pi on the same network as your computer</h2>
      <p>Ethernet to your router is the simplest first connection. The Pi needs Internet access to install packages. Allow its first boot to finish.</p>
      <h2>3. Install Bosun</h2>
      <p>The package includes Stage already built and automatic service installation. This command copies it to the Pi and starts setup; it does not flash Captain firmware.</p>
      {#if !standalone && !IS_ANDROID}
        <button onclick={exportPi} disabled={saving}>{saving ? "Preparing..." : "Save Raspberry Pi package"}</button>
        <div class="fields"><label>Username chosen in Imager<input bind:value={user} placeholder="e.g. musician" autocomplete="off" /></label><label>Raspberry Pi hostname or IP<input bind:value={host} placeholder="bosun.local" autocomplete="off" /></label></div>
        {#if archive}<p class="file">Package: {archive}</p>{/if}
        {#if command}<p>Open <strong>PowerShell</strong> on Windows and paste this command:</p><pre>{command}</pre><button onclick={copy}>{copied ? "Copied" : "Copy command"}</button>
        {:else}<p class="note">Save the package and enter your username and Pi address to get a ready-to-copy command.</p>{/if}
      {:else}<p>Open this setup in Bosun Desktop and choose <strong>Save Raspberry Pi package</strong> to get the package and installation command.</p>{/if}
      <p>SSH asks for the password you chose (no characters appear while typing). On first connection, verify your Pi's SSH fingerprint before accepting it. Wait for <strong>Bosun ready</strong>. If an error occurs, keep the message and retry after fixing the network or credentials.</p>
      <p class="note">This automates Bosun installation after Imager; Desktop does not yet write a complete microSD image itself. The generated command is for Windows. The macOS and Linux apps have not been tested.</p>
    {:else if step === 2}
      <h2>{selected === "android" ? "Prepare Android" : selected === "desktop" ? "Prepare your computer" : "You can connect directly"}</h2>
      {#if selected === "android"}<p>Download <strong>bosun.apk</strong> from the latest release and install it on Android with USB host/OTG support. Connect a USB hub through the OTG adapter; use a powered hub if the phone cannot supply enough power. Simultaneous charging depends on your phone and hub.</p>
      {:else if selected === "desktop"}<p>Use Bosun Desktop with two available USB ports or a USB hub. The computer hosts the MIDI bridge and must stay on with Bosun running while you play.</p>
      {:else}<p>You do not need software on Android or Raspberry Pi. Once you have saved the Captain profile, disconnect it from your computer and connect it to the Player.</p>{/if}
    {:else if step === 3 && setup}
      <p><strong>{setup.title}</strong> - {setup.outcome}</p><SetupDiagram id={setup.id} />
      <ol>{#each setup.steps as item}<li>{item}</li>{/each}</ol>
    {:else if setup}
      <h2>The final check</h2><p class="result">{setup.check}</p><p>Perform this check on your setup: the guide cannot verify cables, power and MIDI feedback by itself.</p>
      {#if setup.pi}<p>For Stage in a browser on the same network: <code>http://bosun.local:8080/</code>. Replace bosun.local with the hostname or IP address you assigned to the Pi.</p>{/if}
      <h2>Frequently asked questions</h2>{#each faq as [question,answer]}<details><summary>{question}</summary><p>{answer}</p></details>{/each}
    {/if}
    {#if error}<p role="alert" class="error">{error}</p>{/if}
  </main>
  <footer><button disabled={step === 0} onclick={() => step--}>Back</button><span>{step+1} / {titles.length}</span>{#if step < 4}<button class="primary" disabled={!setup} onclick={() => step++}>Next</button>{:else}<button onclick={() => step = 0}>Change setup</button>{/if}</footer>
{/snippet}
{#if standalone}<div class="guide standalone">{@render content()}</div>{:else}<dialog bind:this={dialog} class="guide" aria-label="Bosun setup guide" oncancel={event => { event.preventDefault(); onClose?.(); }}>{@render content()}</dialog>{/if}
<style>
  .guide{box-sizing:border-box;width:min(940px,96vw);max-height:92vh;margin:auto;padding:0;border:1px solid var(--border,#344053);border-radius:16px;background:var(--bg-card,#182130);color:var(--text,#edf4fa);font:15px/1.6 system-ui;box-shadow:0 25px 80px #0005}.standalone{max-height:none;margin:2rem auto}.guide::backdrop{background:#080d16cc;backdrop-filter:blur(5px)}header{display:flex;align-items:center;justify-content:space-between;padding:1.1rem 1.5rem;border-bottom:1px solid var(--border)}h1{font-size:1.65rem;margin:0}h2{font-size:1.1rem;margin:1.4rem 0 .6rem}.eyebrow{font-size:.65rem;letter-spacing:.14em;color:var(--accent,#64dcb0);margin:0 0 .3rem}nav{display:flex;flex-wrap:wrap;gap:.3rem;padding:.8rem 1.5rem;background:var(--bg,#101721)}nav button{font-size:.75rem;padding:.4rem .6rem}.current{border-color:var(--accent,#64dcb0);color:var(--accent,#64dcb0)}main{padding:.6rem 1.5rem 1.4rem}p{margin:.8rem 0}button,input{font:inherit;color:inherit;border:1px solid var(--border-strong,#4b5e73);background:var(--bg,#101721);border-radius:7px;padding:.55rem .9rem}button{cursor:pointer}button:disabled{opacity:.45;cursor:not-allowed}button:focus-visible,a:focus-visible,input:focus-visible{outline:3px solid #75baff;outline-offset:3px}.primary{background:var(--accent,#64dcb0);color:#102019;border-color:transparent;font-weight:650}.choices{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.8rem}.choices button{text-align:left;padding:1rem}.choices strong,.choices span{display:block}.choices span{font-size:.85rem;color:var(--text-muted,#b4c1d2);margin-top:.4rem}.selected{border-color:var(--accent,#64dcb0);box-shadow:inset 0 0 0 1px var(--accent,#64dcb0)}li{margin:.5rem 0}.note,.file{font-size:.84rem;color:var(--text-muted,#b4c1d2)}.file{overflow-wrap:anywhere}.actions,.fields{display:flex;flex-wrap:wrap;gap:.7rem}.fields{margin-top:1rem}label{display:flex;flex-direction:column;font-size:.85rem;flex:1}input{min-width:0}a{color:var(--accent,#64dcb0)}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:.8rem;background:var(--bg,#101721);padding:1rem;border-radius:8px}.error{color:var(--err,#ff8c8c)}.result{border-left:3px solid var(--accent,#64dcb0);padding:1rem;background:var(--bg,#101721)}details{border-bottom:1px solid var(--border,#344053);padding:.7rem 0}summary{cursor:pointer;font-weight:600}details p{color:var(--text-muted,#b4c1d2)}footer{display:flex;align-items:center;justify-content:space-between;padding:1rem 1.5rem;border-top:1px solid var(--border,#344053)}footer span{font-size:.8rem;color:var(--text-muted)}@media(max-width:600px){.choices{grid-template-columns:1fr}header,main,footer{padding-left:1rem;padding-right:1rem}h1{font-size:1.35rem}}
</style>
