import net from 'node:net';
import { once } from 'node:events';
import { MidiFramer, virtualKemper } from './virtual-kemper.mjs';

/** Test-only USB MIDI peer. Never discovers or opens physical MIDI devices. */
export class KemperBridge {
  constructor(productType = 0) {
    this.model = virtualKemper(productType);
    this.received = []; this.sent = []; this.held = []; this.hold = null;
    this.pending = Promise.resolve(); this.failure = null;
    this.timer = setInterval(() => {
      try { this.model.client.update(); this.flush(); }
      catch (error) { this.failure = error; }
    }, 10);
  }
  async connect(port) {
    const framer = new MidiFramer(message => {
      if (message[0] >= 0xf8) return;
      this.received.push(message);
      this.pending = this.pending.then(() => this.model.client.doSend(message))
        .then(() => this.flush()).catch(error => { this.failure = error; });
    });
    const socket = this.socket = net.createConnection({ host: '127.0.0.1', port });
    socket.on('error', error => { this.failure = error; });
    socket.on('data', bytes => { try { framer.push(bytes); } catch (error) { this.failure = error; } });
    await once(socket, 'connect'); socket.setNoDelay(true);
  }
  flush() {
    const queue = this.model.client.messageQueue.splice(0);
    for (const message of queue) {
      if (this.hold?.(message)) this.held.push(Array.from(message));
      else this.write(message);
    }
  }
  write(message) {
    if (!this.socket || this.socket.destroyed) return;
    this.sent.push(Array.from(message));
    this.socket.write(Buffer.from(message));
  }
  release() { const held = this.held.splice(0); this.hold = null; held.forEach(message => this.write(message)); }
  assertHealthy() {
    if (this.failure) throw this.failure;
    if (this.model.errors.length) throw new Error(this.model.errors.join('\n'));
    if (this.model.warnings.length) throw new Error(this.model.warnings.join('\n'));
  }
  disconnect() { this.socket?.destroy(); this.socket = null; this.held = []; this.hold = null; }
  dispose() { clearInterval(this.timer); this.disconnect(); this.model.dispose(); }
}
