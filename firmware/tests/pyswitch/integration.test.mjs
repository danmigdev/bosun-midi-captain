import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import net from 'node:net';
import { once } from 'node:events';
import { setTimeout as pause } from 'node:timers/promises';
import { KemperBridge } from './bridge.mjs';

const executable = process.env.BOSUN_EMULATOR;
assert.ok(executable, 'Set BOSUN_EMULATOR to the compiled host executable');

// Waits only bound how long an expected state may take. Sanitizer builds on
// shared CI runners can be several times slower, so the default is generous;
// a state that never arrives still fails, after the full timeout.
async function until(check, message, timeout = 15000) {
  const deadline = Date.now() + timeout;
  let last;
  do { last = await check(); if (last) return last; await pause(20); } while (Date.now() < deadline);
  throw new Error(`Timed out: ${message}`);
}

class Captain {
  constructor(socket) {
    this.socket = socket; this.counter = 0; this.pending = new Map(); this.contexts = [];
    let buffer = '';
    socket.setNoDelay(true);
    socket.on('data', bytes => {
      buffer += bytes.toString();
      let end;
      while ((end = buffer.indexOf('\n')) >= 0) {
        const message = JSON.parse(buffer.slice(0, end)); buffer = buffer.slice(end + 1);
        if (message.type === 'CONTEXT' && !message.id) {
          this.contexts.push(message.context); if (this.contexts.length > 500) this.contexts.shift();
        }
        this.pending.get(message.id)?.(message);
      }
    });
    socket.on('error', error => { this.error = error; });
  }
  async request(type, fields = {}, expected = 'ACK') {
    if (this.error) throw this.error;
    const id = `test-${++this.counter}`;
    let timer;
    try {
      const reply = await new Promise((resolve, reject) => {
        timer = setTimeout(() => reject(new Error(`No ${type} reply`)), 5000);
        this.pending.set(id, resolve); this.socket.write(JSON.stringify({ type, id, ...fields }) + '\n');
      });
      assert.equal(reply.type, expected, JSON.stringify(reply)); return reply;
    } finally { clearTimeout(timer); this.pending.delete(id); }
  }
  async context() { return (await this.request('GET_CONTEXT', {}, 'CONTEXT')).context; }
  async execute(messages) {
    // Use the same public editing and switch activation protocol as Desktop/Stage.
    const { bank, slot } = (await this.request('GET_DEVICE_INFO', {}, 'DEVICE_INFO')).current;
    await this.request('PUT_PATCH', { bank, slot, patch: { name: 'Integration', bindings: [
      { switch: '1', mode: 'tap', actions: { press: { messages } } },
    ] } });
    await this.request('ACTIVATE_SWITCH', { bank, slot, switch: '1', profile: 'test' });
  }
}

async function fixture(t, kind = 'kemper_head', chunk = 7) {
  const root = mkdtempSync(path.join(tmpdir(), 'bosun-pyswitch-'));
  const profile = path.join(root, 'config/profiles/test');
  mkdirSync(path.join(profile, 'patches/01'), { recursive: true });
  const save = (name, data) => writeFileSync(name, JSON.stringify(data));
  save(path.join(root, 'config/active_profile.json'), { id: 'test' });
  save(path.join(profile, 'manifest.json'), { name: 'PySwitch integration', kind });
  save(path.join(profile, 'device.json'), { midi_channel: 1, kemper: { generation: 'MK1', mode: 'performance' }, autosave: { enabled: false } });
  save(path.join(profile, 'patches/01/01.json'), { name: 'Clean', bindings: [] });
  const child = spawn(executable, ['--root', root, '--port', '0', '--midi-port', '0', '--io-chunk', String(chunk)]);
  let output = '', errors = '', bridge, captain;
  child.stdout.on('data', bytes => { output += bytes; });
  child.stderr.on('data', bytes => { errors += bytes; });
  const exited = new Promise(resolve => child.once('exit', (code, signal) => resolve({ code, signal })));
  t.after(async () => {
    bridge?.dispose(); captain?.socket.destroy();
    if (child.exitCode === null) child.kill('SIGTERM');
    const timer = setTimeout(() => child.kill('SIGKILL'), 5000);
    const result = await exited; clearTimeout(timer);
    rmSync(root, { recursive: true, force: true });
    assert.equal(result.code, 0, errors);
    assert.doesNotMatch(errors, /AddressSanitizer|runtime error:/);
  });
  await until(() => {
    if (child.exitCode !== null) throw new Error(errors);
    return /MIDI tcp:\/\/127\.0\.0\.1:(\d+) port=USB/.test(output);
  }, 'emulator readiness', 15000);
  const dataPort = Number(output.match(/READY tcp:\/\/127\.0\.0\.1:(\d+)/)[1]);
  const midiPort = Number(output.match(/MIDI tcp:\/\/127\.0\.0\.1:(\d+)/)[1]);
  const socket = net.createConnection({ host: '127.0.0.1', port: dataPort });
  await once(socket, 'connect'); captain = new Captain(socket);
  bridge = new KemperBridge(kind === 'kemper_player' ? 2 : 0);
  await bridge.connect(midiPort);
  await until(async () => (await captain.context()).kemper_connected === 'on', 'bidirectional handshake');
  await until(async () => !!(await captain.context()).kemper_rig_name, 'initial rig name');
  t.after(() => bridge.assertHealthy());
  t.after(() => {
    t.diagnostic(`Unmodeled fallback parameters (not validated): ${JSON.stringify([...bridge.model.generated])}`);
    if (process.env.BOSUN_PYSWITCH_TRACE) t.diagnostic(JSON.stringify({ contexts: captain.contexts.slice(-5),
      incoming: bridge.received.slice(-60), outgoing: bridge.sent.slice(-60) }));
  });
  return { captain, bridge, midiPort };
}

test('PROFILER Performance addressing crosses every MIDI bank boundary through slot 625', async t => {
  const { captain, bridge } = await fixture(t);
  for (const rig of [1, 128, 129, 256, 257, 512, 513, 625]) {
    await captain.execute([{ type: 'kemper_rig', bank: Math.floor((rig - 1) / 5) + 1, rig: (rig - 1) % 5 + 1 }]);
    await until(() => bridge.model.client.getRigId() === rig - 1, `virtual Kemper selects ${rig}`);
    await until(async () => (await captain.context()).kemper_rig === rig, `Captain feedback ${rig}`);
    await until(() => captain.contexts.some(c => c.kemper_rig === rig), `Stage context ${rig}`);
  }
  // External controls must also reach Bosun without an outgoing selection loop.
  const before = bridge.received.filter(m => m[0] === 0xc0).length;
  bridge.model.client.setRigId(256);
  await until(async () => (await captain.context()).kemper_rig === 257, 'external Performance change');
  assert.equal(bridge.received.filter(m => m[0] === 0xc0).length, before);
});

test('effect commands and external feedback update Captain and unsolicited Stage context', async t => {
  const { captain, bridge } = await fixture(t);
  for (const [slot, page] of [['A', 50], ['Delay', 60], ['Reverb', 61]]) {
    for (const value of [0, 1]) {
      await captain.execute([{ type: 'kemper_effect_toggle', slot, value: value ? 'on' : 'off' }]);
      await until(() => (bridge.model.parameter(page, 3).value !== 0) === !!value, `${slot} command`);
      // Upstream omits Delay/Reverb from its automatic feedback parameter sets.
      // Explicitly emit the model's current value; this validates the MIDI
      // response path without pretending to test automatic device feedback.
      if (page === 60 || page === 61) bridge.model.parameter(page, 3).send();
      await until(async () => (await captain.context())[`kemper_block_${slot}`] === (value ? 'on' : 'off'), `${slot} feedback`);
    }
    captain.contexts.length = 0;
    const effect = bridge.model.parameter(page, 3); effect.setValue(0); effect.send();
    await until(() => captain.contexts.some(c => c[`kemper_block_${slot}`] === 'off'), `${slot} unsolicited feedback`);
  }
});

test('rapid Clean to Crunch then Delay recovers after deliberately stale effect replies', async t => {
  const { captain, bridge } = await fixture(t, 'kemper_head', 1);
  const delay = bridge.model.parameter(60, 3); delay.setValue(0); delay.send();
  await until(async () => (await captain.context()).kemper_block_Delay === 'off', 'initial delay off');
  // Capture a real upstream off reply and deliver it after the later on command.
  bridge.hold = m => m[0] === 0xf0 && m[6] === 1 && m[8] === 60 && m[9] === 3 && m[11] === 0;
  delay.send(); bridge.flush(); assert.ok(bridge.held.length);
  await captain.execute([{ type: 'kemper_rig', bank: 1, rig: 1 },
    { type: 'kemper_rig', bank: 1, rig: 2 }, { type: 'kemper_effect_toggle', slot: 'Delay', value: 'on' }]);
  await until(() => delay.value !== 0 && bridge.model.client.getRigId() === 1, 'Crunch and Delay on');
  bridge.release();
  await until(async () => (await captain.context()).kemper_block_Delay === 'on', 'Delay reconciliation');
  await pause(800);
  assert.equal((await captain.context()).kemper_block_Delay, 'on');
  // Stage must receive the same state unsolicited. On a slow sanitizer runner
  // the last push can trail the polled reply, so wait for it instead of
  // sampling once; a push that never arrives still fails the test.
  await until(() => captain.contexts.at(-1)?.kemper_block_Delay === 'on', 'Delay pushed to Stage');
});

test('tuner feedback and commanded Morph position pass through the actual MIDI stream', async t => {
  const { captain, bridge } = await fixture(t);
  bridge.model.parameter(125, 84).setValue(69);
  bridge.model.parameter(124, 15).setValue(8191);
  await captain.execute([{ type: 'kemper_tuner', state: 'on' }]);
  await until(() => bridge.model.parameter(127, 126).value === 127, 'Bosun tuner command received');
  // Upstream starts its note timer only for value 1, whereas Bosun sends the
  // also-valid MIDI on value 127. Exercise external note feedback explicitly;
  // do not silently normalize commands or patch the third-party model.
  assert.equal(bridge.model.client.tuner.running, false);
  bridge.model.parameter(127, 126).setValue(1);
  await until(async () => (await captain.context()).kemper_tuner === 'on', 'external tuner on');
  await until(async () => (await captain.context()).kemper_tuner_note === 'A', 'tuner note');
  await captain.execute([{ type: 'kemper_tuner', state: 'off' }]);
  await until(async () => (await captain.context()).kemper_tuner === 'off', 'tuner off');
  await captain.execute([{ type: 'kemper_morph', value: 64 }]);
  await until(() => bridge.model.parameter(0, 11).value === 8192, 'virtual Morph pedal');
  const context = await captain.context();
  assert.equal(context.kemper_morph_value, 64);
  assert.equal(context.kemper_morph_source, 'commanded');
});

test('MIDI stream reconnect discards partial SysEx and restores feedback after lease expiry', async t => {
  const { captain, bridge, midiPort } = await fixture(t);
  bridge.socket.write(Buffer.from([0xf0, 0, 32, 51, 0]));
  await pause(30); bridge.disconnect();
  await until(async () => (await captain.context()).kemper_connected === 'off', 'lease expiry', 18000);
  await bridge.connect(midiPort);
  await until(async () => (await captain.context()).kemper_connected === 'on', 'reconnection');
  const effect = bridge.model.parameter(50, 3); effect.setValue(1); effect.send();
  await until(async () => (await captain.context()).kemper_block_A === 'on', 'feedback on replacement stream');
});

test('Player retains its distinct product ID and interoperates with the same model', async t => {
  const { captain, bridge } = await fixture(t, 'kemper_player');
  await captain.execute([{ type: 'kemper_effect_toggle', slot: 'Delay', value: 'off' }]);
  await until(() => bridge.model.parameter(60, 3).value === 0, 'Player delay command');
  await until(async () => (await captain.context()).kemper_block_Delay === 'off', 'Player delay');
  assert.ok(bridge.received.some(m => m[0] === 0xf0 && m[4] === 2 && m[6] === 126));
});
