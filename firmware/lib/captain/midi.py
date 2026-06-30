import time

import busio
import usb_midi

from .board import UART_RX, UART_TX


class MidiParser:
    """Stream parser for MIDI messages. Handles running status, real-time
    interleave, and SYSEX collection.

    Channel-voice messages are returned as `(channel, status, data_list)`.
    SYSEX messages are returned as `(0, 0xF0, payload_list)` where payload
    excludes the leading 0xF0 and trailing 0xF7. This lets the caller
    iterate one homogeneous event stream."""

    def __init__(self):
        self._status = 0
        self._data = []
        self._expected = 0
        self._in_sysex = False
        self._sysex_buf = []

    def feed(self, data):
        out = []
        for b in data:
            if b >= 0xF8:
                continue                          # real-time, single byte, skip
            if self._in_sysex:
                if b == 0xF7:
                    out.append((0, 0xF0, list(self._sysex_buf)))
                    self._sysex_buf = []
                    self._in_sysex = False
                elif b < 0x80:
                    self._sysex_buf.append(b)
                else:
                    # Stray status byte inside SYSEX - abort current SYSEX,
                    # then re-process this byte through the normal path.
                    self._sysex_buf = []
                    self._in_sysex = False
                    # Fall through to the normal status handling below.
                    if b == 0xF0:
                        self._in_sysex = True
                        continue
                    self._status = b
                    self._data = []
                    self._expected = self._len_for(b)
                    if self._expected == 0:
                        self._status = 0
                continue
            if b == 0xF0:
                self._status = 0
                self._data = []
                self._expected = 0
                self._in_sysex = True
                continue
            if b >= 0xF0:
                self._status = 0                  # system common, drops running status
                self._data = []
                self._expected = 0
                continue
            if b >= 0x80:
                self._status = b
                self._data = []
                self._expected = self._len_for(b)
                if self._expected == 0:
                    self._status = 0
                continue
            # data byte
            if self._status == 0:
                continue
            self._data.append(b)
            if len(self._data) >= self._expected:
                channel = (self._status & 0x0F) + 1
                status = self._status & 0xF0
                out.append((channel, status, list(self._data)))
                self._data = []
        return out

    @staticmethod
    def _len_for(status):
        t = status & 0xF0
        return 1 if (t == 0xC0 or t == 0xD0) else 2


class MidiEngine:
    """USB MIDI + DIN UART MIDI in parallel. Every outbound message goes to
    both ports. Inbound is parsed independently per port (no running-status
    cross-contamination)."""

    def __init__(self):
        ports = usb_midi.ports
        self.usb_in = ports[0] if ports else None
        self.usb_out = ports[1] if len(ports) > 1 else None
        self.uart = busio.UART(tx=UART_TX, rx=UART_RX, baudrate=31250, timeout=0)
        self._din_parser = MidiParser()
        self._usb_parser = MidiParser()

    # ------------- outbound ----------------

    def send_cc(self, channel, cc, value):
        self._tx(bytes((
            0xB0 | ((channel - 1) & 0x0F),
            cc & 0x7F,
            value & 0x7F,
        )))

    def send_pc(self, channel, program):
        self._tx(bytes((
            0xC0 | ((channel - 1) & 0x0F),
            program & 0x7F,
        )))

    def send_note_on(self, channel, note, velocity):
        self._tx(bytes((
            0x90 | ((channel - 1) & 0x0F),
            note & 0x7F,
            velocity & 0x7F,
        )))

    def send_note_off(self, channel, note, velocity):
        self._tx(bytes((
            0x80 | ((channel - 1) & 0x0F),
            note & 0x7F,
            velocity & 0x7F,
        )))

    def send_sysex(self, payload):
        """Send a SYSEX message. `payload` is the inner byte sequence
        between 0xF0 and 0xF7 (manufacturer ID + body). The framing
        bytes are added here so callers don't need to remember them."""
        body = bytes(payload)
        self._tx(b"\xF0" + body + b"\xF7")

    def _tx(self, data):
        self.tx_count = getattr(self, "tx_count", 0) + 1
        if self.usb_out is not None:
            self._tx_usb(data)
        try:
            self.uart.write(data)
        except Exception as e:
            print("din midi tx error:", e)

    def _tx_usb(self, data):
        """Write `data` to USB MIDI out without dropping bytes mid-message.
        usb_midi.PortOut.write() can return a short count when the host's
        endpoint buffer is full; the original code ignored that and could
        emit truncated MIDI which the receiver then rejects, looking from
        the user side like "rig change didn't happen or arrived late".

        We retry the remainder for up to ~10 ms and then give up rather
        than block the main loop forever. Anything we drop counts but is
        not retried - better a missed CC than a stuck pedal."""
        n = len(data)
        sent = 0
        deadline = time.monotonic_ns() + 10_000_000  # 10 ms
        try:
            while sent < n and time.monotonic_ns() < deadline:
                chunk = data if sent == 0 else data[sent:]
                w = self.usb_out.write(chunk)
                if w is None:
                    w = len(chunk)
                if w == 0:
                    # Buffer full - tiny yield, then retry while still in budget.
                    time.sleep(0.0005)
                    continue
                sent += w
            if sent < n:
                self.usb_tx_dropped = getattr(self, "usb_tx_dropped", 0) + (n - sent)
        except Exception as e:
            print("usb midi tx error:", e)

    # ------------- inbound ----------------

    def poll(self):
        """Return a list of (port, channel, status, data) tuples for every
        complete channel-voice message received since the last poll. SYSEX
        is reported as (port, 0, 0xF0, payload_bytes) so the caller can
        process it through the same loop."""
        events = []
        if self.uart.in_waiting:
            data = self.uart.read(self.uart.in_waiting)
            if data:
                for ch, status, dlist in self._din_parser.feed(data):
                    events.append(("din", ch, status, dlist))
                    if status == 0xF0:
                        self.sysex_rx_count = getattr(self, "sysex_rx_count", 0) + 1
        if self.usb_in is not None:
            data = self.usb_in.read(32)
            if data:
                for ch, status, dlist in self._usb_parser.feed(data):
                    events.append(("usb", ch, status, dlist))
                    if status == 0xF0:
                        self.sysex_rx_count = getattr(self, "sysex_rx_count", 0) + 1
        return events
