import { test } from 'node:test';
import assert from 'node:assert/strict';
import { MidiFramer, virtualKemper } from './virtual-kemper.mjs';

test('fragmented SysEx, interleaved realtime and running status remain separate', () => {
  const packets = [], parser = new MidiFramer(message => packets.push(message));
  for (const byte of [0xf0, 0, 32, 0xf8, 51, 0xf7, 0xb0, 26, 1, 27, 0, 0xc0, 127, 0]) parser.push([byte]);
  assert.deepEqual(packets, [[0xf8], [0xf0, 0, 32, 51, 0xf7], [0xb0, 26, 1], [0xb0, 27, 0], [0xc0, 127], [0xc0, 0]]);
});
test('reconnect discards partial packets and running status', () => {
  const packets = [], parser = new MidiFramer(message => packets.push(message));
  parser.push([0xf0, 0, 32]); parser.reset(); parser.push([51, 0xf7]);
  parser.push([0xb0, 26]); parser.reset(); parser.push([127, 0xc0, 3]);
  assert.deepEqual(packets, [[0xf7], [0xc0, 3]]);
});
test('unknown parameters are not usable as successful coverage assertions', async () => {
  const model = virtualKemper();
  try {
    assert.equal(model.parameter(60, 3).value, 1);
    assert.throws(() => model.parameter(125, 88), /not explicitly modeled/);
    await model.client.doSend([0xf0, 0, 32, 51, 0, 127, 65, 0, 125, 88, 0xf7]);
    assert.ok(model.generated.size > 0);
    assert.throws(() => model.parameter(125, 88), /not explicitly modeled/);
    assert.deepEqual(model.errors, []);
  } finally { model.dispose(); }
});
