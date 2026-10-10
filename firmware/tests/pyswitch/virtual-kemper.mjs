import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import vm from 'node:vm';

const upstream = new URL('./upstream/', import.meta.url);
const order = ['Tools', 'PeriodCounter', 'VirtualClient', 'VirtualKemperParameterKeys',
  'VirtualKemperParameter', 'VirtualKemperParameterDefault', 'VirtualKemperParameters',
  'VirtualKemperProtocol', 'VirtualKemperStats', 'VirtualKemperTempo', 'VirtualKemperTuner',
  'VirtualKemperMorph', 'VirtualKemperClientSetup', 'VirtualKemperClient'];

/** Load the unmodified, hash-verified upstream model, without its browser UI. */
export function virtualKemper(productType = 0) {
  const manifest = JSON.parse(readFileSync(new URL('manifest.json', upstream)));
  for (const item of manifest.files) {
    const bytes = readFileSync(new URL(item.file, upstream));
    if (createHash('sha256').update(bytes).digest('hex') !== item.sha256)
      throw new Error(`PySwitch source hash mismatch: ${item.file}`);
  }
  const errors = [], warnings = [], timers = new Set();
  const context = vm.createContext({
    console: { log() {}, warn: (...args) => warnings.push(args.map(String).join(' ')),
      error: (...args) => errors.push(args.map(String).join(' ')) },
    setTimeout(callback, delay) {
      const timer = setTimeout(() => { timers.delete(timer); callback(); }, delay);
      timers.add(timer); timer.unref(); return timer;
    }, clearTimeout, setInterval, clearInterval,
  });
  for (const name of order) vm.runInContext(readFileSync(new URL(`${name}.js`, upstream), 'utf8'), context, { filename: name });
  const model = vm.runInContext(`(() => {
    const defined = new Map(), generated = new Set();
    let booting = true;
    const init = VirtualKemperParameters.prototype.init;
    VirtualKemperParameters.prototype.init = function(options) {
      const parameter = init.call(this, options);
      const key = options.keys.getId();
      if (booting) defined.set(key, parameter); else generated.add(key);
      return parameter;
    };
    const client = new VirtualKemperClient({ productType: ${Number(productType)}, simulateMorphBug: false });
    booting = false;
    return { client, generated,
      parameter(page, index) {
        const id = new NRPNKey([page, index]).getId();
        if (!defined.has(id)) throw new Error('Parameter is not explicitly modeled by PySwitch: ' + id);
        return defined.get(id);
      }
    };
  })()`, context);
  return { ...model, errors, warnings, revision: manifest.commit,
    dispose() { for (const timer of timers) clearTimeout(timer); timers.clear(); } };
}

/** TCP is a byte stream: preserve split SysEx, running status and realtime bytes. */
export class MidiFramer {
  constructor(emit) { this.emit = emit; this.reset(); }
  reset() { this.pending = []; this.status = 0; this.expected = 0; this.sysex = false; }
  push(bytes) {
    for (const byte of bytes) {
      if (byte >= 0xf8) { this.emit([byte]); continue; }
      if (byte === 0xf0) { this.reset(); this.sysex = true; this.pending = [byte]; continue; }
      if (this.sysex) {
        if (byte === 0xf7) { this.pending.push(byte); this.emit(this.pending); this.reset(); continue; }
        if (byte < 0x80) {
          if (this.pending.length >= 65536) throw new Error('Oversized MIDI SysEx');
          this.pending.push(byte); continue;
        }
        this.reset(); // Status interrupts malformed SysEx; process the new status.
      }
      if (byte >= 0x80) {
        this.pending = [byte]; this.status = byte < 0xf0 ? byte : 0;
        this.expected = byte < 0xf0 ? ([0xc0, 0xd0].includes(byte & 0xf0) ? 2 : 3)
          : ({ 0xf1: 2, 0xf2: 3, 0xf3: 2 }[byte] ?? 1);
      } else {
        if (!this.pending.length) {
          if (!this.status) continue;
          this.pending = [this.status];
        }
        this.pending.push(byte);
      }
      if (this.pending.length === this.expected) { this.emit(this.pending); this.pending = []; }
    }
  }
}
