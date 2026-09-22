<script lang="ts">
  import { onDestroy } from 'svelte';
  import { sendAndAwait } from '../lib/protocol';
  let { connected }: { connected: boolean } = $props();
  let busy = $state(false), result = $state('');
  let disposed = false;
  onDestroy(() => { disposed = true; });
  async function detect() {
    busy = true; result = '';
    try {
      const first = await sendAndAwait({ type: 'GET_KEMPER_IDENTITY', request: true });
      if (first.type !== 'KEMPER_IDENTITY') throw new Error('Identity query is not supported by this firmware');
      await new Promise(resolve => setTimeout(resolve, 2100));
      if (disposed) return;
      const reply = await sendAndAwait({ type: 'GET_KEMPER_IDENTITY' });
      if (reply.type !== 'KEMPER_IDENTITY' || reply.profile !== first.profile) throw new Error('Profile changed during detection');
      result = reply.status === 'received'
        ? `Kemper identity received. Raw family / member / revision: ${reply.raw.map(n => n.toString(16).padStart(2, '0')).join(' ')}. Model, OS version and mode cannot be identified reliably from this reply; keep the manual settings.`
        : 'No Kemper identity reply received. Keep the manual model, generation and mode settings. Check both MIDI directions if effect feedback is also missing.';
    } catch (error) { result = String(error); }
    finally { busy = false; }
  }
</script>
<button disabled={!connected || busy} onclick={detect}>{busy ? 'Checking Kemper…' : 'Check Kemper identity'}</button>
{#if result}<p role="status">{result}</p>{/if}
<style>button{font:inherit;padding:.5rem;color:inherit;background:var(--bg-card);border:1px solid var(--border);border-radius:4px}p{font-size:.8rem;overflow-wrap:anywhere}</style>
