<script lang="ts">
  let { value, onChange }: { value: unknown; onChange: (value: Record<string, unknown>) => void } = $props();
  let program = $state(0), bank = $state(1), slot = $state(1);
  const mapping = $derived(value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {});
  const valid = $derived(Number.isInteger(program) && program >= 0 && program <= 127 &&
    Number.isInteger(bank) && bank >= 1 && bank <= 125 && Number.isInteger(slot) && slot >= 1 && slot <= 10);
  function remove(key: string) { const next = { ...mapping }; delete next[key]; onChange(next); }
</script>
<h4>Follow external Browse programs (optional)</h4>
<p>Map incoming MIDI programs to existing Bosun patches. Entry actions are skipped to avoid sending the program back. Unmapped programs leave the Bosun patch unchanged.</p>
<div class="entry">
  <label>MIDI program (0–127)<input type="number" min="0" max="127" bind:value={program} /></label>
  <label>Bosun bank<input type="number" min="1" max="125" bind:value={bank} /></label>
  <label>Bosun slot<input type="number" min="1" max="10" bind:value={slot} /></label>
  <button disabled={!valid} onclick={() => onChange({ ...mapping, [program]: { bank, slot } })}>Add / replace mapping</button>
</div>
{#each Object.entries(mapping) as [key, destination]}
  <p>Program {key} → {JSON.stringify(destination)} <button onclick={() => remove(key)}>Remove program {key}</button></p>
{/each}
<p>Save settings to keep these mappings. A destination patch must already exist.</p>
<style>
  .entry{display:flex;flex-wrap:wrap;gap:.5rem;align-items:end}label{display:flex;flex-direction:column;font-size:.8rem}input{width:6rem}button,input{font:inherit;color:inherit;background:var(--bg-card);border:1px solid var(--border);padding:.4rem}p{font-size:.8rem;color:var(--text-muted)}
</style>
