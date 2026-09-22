<script lang="ts">
  import type { KemperSetup } from '../lib/setup-guide';
  let { setup = $bindable() }: { setup: KemperSetup } = $props();
</script>
<label>Hardware generation
  <select bind:value={setup.generation}><option>MK1</option><option>MK2</option></select>
</label>
<label>Mode set on the Kemper
  <select bind:value={setup.mode}><option value="performance">Performance</option><option value="browse">Browse</option></select>
</label>
<p>Head, Rack and Stage share this experimental profile. Match these settings to your Kemper; hardware validation is pending.</p>
<style>
  label{display:flex;gap:.5rem;align-items:center;margin:.5rem 0}select{font:inherit;color:inherit;background:var(--bg-card);border:1px solid var(--border);padding:.4rem}p{font-size:.8rem;color:var(--text-muted)}
</style>
