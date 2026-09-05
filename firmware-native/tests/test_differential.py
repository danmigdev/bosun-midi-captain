#!/usr/bin/env python3
"""Compare native engines with canonical CircuitPython sources, without devices.

The C driver links the actual production modules and can run under ASan/UBSan.
Python supplies deterministic streams, timed gestures and Kemper traces, then
compares every emitted packet and observable state at each step. Hardware-only
imports are replaced with inert objects; no USB, serial, network or flash.
"""

import argparse
import heapq
import importlib.util
import json
from pathlib import Path
import random
import subprocess
import sys
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "firmware/lib"
BLOCKS = ("A", "B", "C", "D", "X", "Mod", "Delay", "Reverb")
TYPE_PAGES = (50, 51, 52, 53, 56, 58, 60, 61)
ON_PAGES = (50, 51, 52, 53, 56, 58, 74, 75)
ON_ADDR = (3, 3, 3, 3, 3, 3, 2, 2)
CCS = (17, 18, 19, 20, 22, 24, 27, 29)
ARGS = None


class Pin:
    def __init__(self, _pin):
        self.value = True


def load_python(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    digitalio = types.SimpleNamespace(
        DigitalInOut=Pin, Direction=types.SimpleNamespace(INPUT=0),
        Pull=types.SimpleNamespace(UP=0))
    stubs = {
        "_native_oracle": types.ModuleType("_native_oracle"),
        "_native_oracle.board": types.SimpleNamespace(
            UART_RX=None, UART_TX=None, FOOTSWITCHES={}),
        "busio": types.ModuleType("busio"),
        "usb_midi": types.ModuleType("usb_midi"),
        "digitalio": digitalio,
    }
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


class Driver:
    def __init__(self):
        self.process = subprocess.Popen(
            [str(ARGS.driver)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=None, text=True, encoding="ascii", bufsize=1)
        self.last = []

    def command(self, text):
        self.last.append(text)
        self.last = self.last[-8:]
        self.process.stdin.write(text + "\n")
        self.process.stdin.flush()
        reply = self.process.stdout.readline()
        if not reply:
            raise AssertionError("native driver stopped: " + repr(self.last))
        return json.loads(reply)

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.write("q\n")
            self.process.stdin.flush()
        code = self.process.wait(timeout=10)
        self.process.stdin.close()
        self.process.stdout.close()
        if code:
            raise AssertionError("native driver exited %d" % code)


class PythonMidi:
    def __init__(self, app):
        self.app = app
        self.events = []

    def send_sysex(self, data):
        self.events.append([0, 0, (b"\xf0" + bytes(data) + b"\xf7").hex()])

    def send_cc(self, channel, cc, value):
        self.events.append([0, 0, bytes((0xb0 | ((channel - 1) & 15), cc & 127, value & 127)).hex()])

    def send_pc(self, channel, program):
        packet = [0, 0, bytes((0xc0 | ((channel - 1) & 15), program & 127)).hex()]
        if self.app.defer_pc:
            self.app.pending_pc = (self.app.now + 5, packet)
        else:
            self.events.append(packet)


class PythonKemper:
    def __init__(self, mask=0, channel=1):
        self.plugin = load_python("_native_oracle.kemper", FIRMWARE / "plugins/kemper.py")
        self.device = {"kemper": {}, "midi_channel": channel}
        self.now = 0
        self.current_bank = self.current_slot = 1
        self.display_context = {"bank": 1, "slot": 1}
        self._pending_context_updates = {}
        self.latched = {}
        self.mask = mask
        self.midi = PythonMidi(self)
        self.defer_pc = False
        self.pending_pc = None
        self.pending_target = None

    def _now_ms(self):
        return self.now

    def current_bindings(self):
        return [(str(i), {"actions": {"toggle_on": {"messages": [
            {"type": "kemper_effect_toggle", "slot": block}]}}})
            for i, block in enumerate(BLOCKS) if self.mask & (1 << i)]

    def update_context(self, values):
        self.display_context.update(values)

    def set_switch_latched(self, switch, on):
        self.latched[switch] = on

    def switch_patch(self, bank, slot, **_kwargs):
        self.pending_pc = None
        self.current_bank, self.current_slot = bank, slot
        self.display_context.update(bank=bank, slot=slot)
        self.plugin.on_patch_switch_started(self, source="midi_in", fire_on_enter=False)
        return True

    def begin(self, rig):
        self.current_bank, self.current_slot = (rig - 1) // 5 + 1, (rig - 1) % 5 + 1
        self.display_context.update(bank=self.current_bank, slot=self.current_slot)
        self.plugin.on_patch_switch_started(self)

    def select(self, bank, slot, channel=1):
        self.begin((bank - 1) * 5 + slot)
        self.pending_target = {"type": "kemper_rig", "bank": bank, "rig": slot, "channel": channel}
        self.defer_pc = True
        try:
            with patch("time.sleep", lambda _duration: None):
                self.plugin.dispatch(self.pending_target, self.midi)
        finally:
            self.defer_pc = False

    def tick(self):
        if self.pending_pc is not None and self.now >= self.pending_pc[0]:
            self.midi.events.append(self.pending_pc[1])
            self.pending_pc = None
            with patch("time.monotonic_ns", return_value=self.now * 1_000_000):
                self.plugin.update_context(self.pending_target, self.display_context)
        self.plugin.tick(self, self.now)

    def state(self):
        p = self.plugin
        context = self.display_context
        rig = (self.current_bank - 1) * 5 + self.current_slot
        known = sum(1 << i for i, block in enumerate(BLOCKS) if "kemper_block_" + block in context)
        on = sum(1 << i for i, block in enumerate(BLOCKS) if context.get("kemper_block_" + block) == "on")
        name = context.get("kemper_rig_name", "")
        return [rig, known, on, {"": 0, "VOL": 1, "WAH": 2}[context.get("expression_mode", "")],
                int(context.get("kemper_connected") == "on"),
                int(context.get("kemper_tuner") == "on"),
                context.get("kemper_bpm", 0), context.get("kemper_tuner_deviance", 8192),
                int(bool(name) and p._RIG_INFO["rig"] == rig),
                p._BIDIR_STATE["generation"],
                sum(1 << BLOCKS.index(b) for b in p._BIDIR_STATE["reconcile_pending"]),
                p._BIDIR_STATE["reconcile_attempt"], int(p._transition_active())]


class DifferentialTests(unittest.TestCase):
    def setUp(self):
        self.native = Driver()

    def tearDown(self):
        self.native.close()

    def assert_kemper(self, oracle, command):
        reply = self.native.command(command)
        self.assertEqual(reply["events"], oracle.midi.events, "TX after " + command)
        oracle.midi.events.clear()
        self.assertEqual(reply["state"], oracle.state(), "state after " + command)
        self.assertEqual(bytes.fromhex(reply["name"]).decode(),
                         oracle.display_context.get("kemper_rig_name", ""), "name after " + command)
        self.assertEqual(bytes.fromhex(reply["note"]).decode(),
                         oracle.display_context.get("kemper_tuner_note", ""), "note after " + command)
        return reply

    def kemper_init(self, mask=0):
        self.native.command("K 1 %d" % mask)
        return PythonKemper(mask)

    def tick(self, oracle, now):
        oracle.now = now
        oracle.tick()
        return self.assert_kemper(oracle, "t %d" % (now & 0xffffffff))

    def select(self, oracle, now, bank, slot):
        oracle.now = now
        oracle.select(bank, slot)
        return self.assert_kemper(oracle, "r %d %d %d" % (now & 0xffffffff, bank, slot))

    def handle(self, oracle, now, status, data, channel=1):
        oracle.now = now
        oracle.plugin.on_midi_in("usb", channel, status, list(data), oracle)
        return self.assert_kemper(oracle, "h %d %d %d %s" % (now & 0xffffffff, channel, status, bytes(data).hex() or "-"))

    def param(self, oracle, now, page, address, value):
        return self.handle(oracle, now, 0xf0,
            (0, 0x20, 0x33, 2, 127, 1, 0, page, address, value >> 7, value & 127), 0)

    def name(self, oracle, now, text):
        return self.handle(oracle, now, 0xf0,
            bytes((0, 0x20, 0x33, 2, 127, 3, 0, 0, 1)) + text.encode("ascii") + b"\0", 0)

    def assert_json(self, wire):
        def reject_constant(value):
            raise ValueError(value)

        try:
            json.loads(wire.decode("utf-8"), parse_constant=reject_constant)
            valid = True
        except (ValueError, UnicodeError):
            valid = False
        reply = self.native.command("J " + (wire.hex() or "-"))
        self.assertEqual(reply["result"], 0 if valid else 1, repr(wire))
        tokens = reply["tokens"]
        if not valid:
            self.assertEqual(tokens, [])
            return
        self.assertGreater(len(tokens), 0)
        self.assertEqual(tokens[0][3], len(tokens))
        types = {dict: 0, list: 1, str: 2, int: 3, float: 3, type(None): 6}
        for index, (kind, start, end, following, decoded, quoted, integer) in enumerate(tokens):
            self.assertTrue(0 <= start < end <= len(wire))
            self.assertTrue(index < following <= len(tokens))
            raw = wire[start:end]
            value = json.loads(raw.decode("utf-8"))
            expected_kind = (4 if value else 5) if type(value) is bool else types[type(value)]
            self.assertEqual(kind, expected_kind, repr(raw))
            if kind not in (0, 1):
                self.assertEqual(following, index + 1)
            else:
                for child in tokens[index + 1:following]:
                    self.assertTrue(start < child[1] < child[2] < end)
            representable = isinstance(value, str) and "\0" not in value and not any(
                0xd800 <= ord(char) <= 0xdfff for char in value)
            if representable:
                self.assertEqual(bytes.fromhex(decoded).decode("utf-8"), value)
                self.assertEqual(json.loads(bytes.fromhex(quoted)), value)
            else:
                self.assertIsNone(decoded)
                self.assertIsNone(quoted)
            expected_integer = value if type(value) is int and -2147483648 <= value <= 2147483647 else None
            self.assertEqual(integer, expected_integer)

    def test_json_nested_unicode_and_mutations_match_python(self):
        rng = random.Random(0x150A)
        alphabet = 'abCD09 _-\\"\n\r\t\x01\x1f' + '\u00e8\u00e9\u03b1\u4e2d\U0001f3b8'

        def string():
            return ''.join(rng.choice(alphabet) for _ in range(rng.randrange(20)))

        def value(depth):
            choice = rng.randrange(7 if depth < 4 else 5)
            if choice == 0:
                return None
            if choice == 1:
                return bool(rng.randrange(2))
            if choice == 2:
                return rng.choice((-2147483649, -2147483648, -1, 0, 2147483647,
                                   2147483648, rng.randrange(-10**18, 10**18)))
            if choice == 3:
                return rng.uniform(-1e6, 1e6)
            if choice == 4:
                return string()
            if choice == 5:
                return [value(depth + 1) for _ in range(rng.randrange(5))]
            return {string(): value(depth + 1) for _ in range(rng.randrange(5))}

        seeds = [b'{"key":1,"key":2,"\\u006bey":3}', b'"a\\u0000z"',
                 b'"prefix\\ud800"', b'"\\udfff"', b'"\\ud83c\\udfb8"',
                 b'-0', b'-0.0', b'1e+30', b'1E-30', b'[]', b'{}']
        for _ in range(600):
            document = json.dumps(value(0), ensure_ascii=bool(rng.randrange(2)),
                                  indent=rng.choice((None, 1)))
            wire = document.encode("utf-8")
            if len(wire) < 3500:
                seeds.append(wire)
        for wire in seeds:
            self.assert_json(wire)
            for _ in range(5):
                mutated = bytearray(wire)
                position = rng.randrange(len(mutated) + 1)
                operation = rng.randrange(4)
                if operation == 0:
                    mutated.insert(position, rng.randrange(256))
                elif operation == 1 and position < len(mutated):
                    mutated[position] = rng.randrange(256)
                elif operation == 2 and position < len(mutated):
                    del mutated[position]
                else:
                    del mutated[position:]
                self.assert_json(bytes(mutated))

    def test_midi_random_fragmented_streams_match_python(self):
        module = load_python("_native_oracle.midi", FIRMWARE / "captain/midi.py")
        python = [module.MidiParser(), module.MidiParser()]
        rng = random.Random(0xB05A1)
        for index in range(2):
            self.native.command("I %d" % index)
        # Valid voice, running status, SysEx (including maximum/overflow),
        # realtime and system-common between messages; fragment at every byte.
        for _ in range(1600):
            port = rng.randrange(2)
            if rng.randrange(5) == 0:
                size = rng.choice((0, 1, 9, 13, 64, 1024, 1040))
                wire = bytearray((0xf0,))
                wire.extend(rng.randrange(128) for _ in range(size))
                wire.append(0xf7)
            else:
                status = rng.randrange(0x80, 0xf0)
                wire = bytearray((status,))
                count = 1 if status & 0xf0 in (0xc0, 0xd0) else 2
                wire.extend(rng.randrange(128) for _ in range(count * rng.randrange(1, 8)))
            for _ in range(rng.randrange(4)):
                wire.insert(rng.randrange(len(wire) + 1), rng.randrange(0xf8, 0x100))
            if rng.randrange(10) == 0:
                wire.extend((0xf2, 1, 2))
            offset = 0
            while offset < len(wire):
                size = rng.randrange(1, 97)
                chunk = bytes(wire[offset:offset + size]); offset += size
                expected = [[ch, status, bytes(data).hex()] for ch, status, data in python[port].feed(chunk)]
                actual = self.native.command("M %d %s" % (port, chunk.hex()))["events"]
                self.assertEqual(actual, expected)

    def test_midi_hostile_fuzz_is_independent_of_fragmentation(self):
        # All 256 byte values, malformed frames, repeated status and data.
        # Compare whole versus single-byte native feeds: malformed system
        # common inside SysEx is deliberately rejected (the Python parser
        # mislabels those invalid bytes as channel data, so is no oracle here).
        rng = random.Random(0xF022)
        for _ in range(180):
            wire = bytes(rng.randrange(256) for _ in range(rng.randrange(10, 500)))
            self.native.command("I 0"); self.native.command("I 1")
            whole = self.native.command("M 0 " + wire.hex())["events"]
            split = []
            for byte in wire:
                split.extend(self.native.command("M 1 %02x" % byte)["events"])
            self.assertEqual(whole, split)

    def test_switch_gestures_modes_resets_and_wrap_match_python(self):
        module = load_python("_native_oracle.bindings", FIRMWARE / "captain/bindings.py")
        modes = ("tap", "latched", "momentary", "long_press_alt", "double_tap")
        triggers = {"press": 1, "release": 2, "toggle_on": 4,
                    "toggle_off": 8, "long_press": 16, "double_tap": 32}
        rng = random.Random(0xF5A)
        for mode_index, mode in enumerate(modes):
            for start in (1000, 0xffffffff - 800):
                for auto in (False, True):
                    self.native.command("S 600 250 500 %d" % auto)
                    python = module.SwitchFsm("test", None, auto_momentary_on_hold=auto)
                    now, high = start, True
                    for _ in range(1600):
                        now += rng.choice((0, 1, 4, 5, 6, 20, 100, 251, 501, 601))
                        if rng.randrange(4) == 0:
                            high = not high
                        # Reset a held long-press on a patch reload. Resets
                        # of latched mode at uptime wrap retain Python's old
                        # zero timestamp semantics; exercise normal gestures
                        # across wrap and these reset cases separately.
                        reset = mode == "long_press_alt" and rng.randrange(40) == 0
                        if reset: python.reset()
                        python.io.value = high
                        edge, keys = python.poll(now, mode)
                        expected = [{None: 0, "press": 1, "release": 2}[edge],
                                    sum(triggers[key] for key in keys), int(python.latched_on),
                                    int(python.is_momentary_active(now, mode))]
                        actual = self.native.command("s %d %d %d %d" %
                            (now & 0xffffffff, high, mode_index, reset))["switch"]
                        self.assertEqual(actual, expected, (mode, start, auto, now))

    def test_kemper_queries_pc_echo_and_stale_replies_match_python(self):
        oracle = self.kemper_init((1 << 4) | (1 << 7))
        self.select(oracle, 100, 3, 2)
        self.tick(oracle, 105)
        self.name(oracle, 130, "Clean")
        self.tick(oracle, 600)
        self.handle(oracle, 601, 0xc0, (11,))
        self.param(oracle, 604, 56, 3, 1)
        self.param(oracle, 607, 75, 2, 0)
        self.select(oracle, 700, 1, 3)
        self.tick(oracle, 705)
        self.name(oracle, 720, "Clean")
        self.name(oracle, 820, "Crunch")
        self.param(oracle, 1000, 56, 3, 0)
        self.tick(oracle, 1200)
        self.tick(oracle, 1800)
        self.handle(oracle, 1801, 0xc0, (2,))
        self.handle(oracle, 1802, 0xb0, (22, 127))
        self.param(oracle, 1803, 56, 3, 0)
        self.param(oracle, 1804, 75, 2, 1)
        self.tick(oracle, 2300)

    def test_kemper_all_wah_slots_bypass_poll_and_timeout_match_python(self):
        for slot in range(8):
            oracle = self.kemper_init()
            self.handle(oracle, 100, 0xf0, (0, 0x20, 0x33, 2, 127, 0x7e), 0)
            self.handle(oracle, 101, 0xc0, (2,))
            self.name(oracle, 120, "Crunch")
            self.tick(oracle, 601)
            self.param(oracle, 602, 5, 21, 0)
            now = 622
            for index, page in enumerate(TYPE_PAGES):
                self.tick(oracle, now)
                self.param(oracle, now + 1, page, 0, 1 if index == slot else 0)
                now += 21
                if index == slot:
                    self.tick(oracle, now)
                    self.param(oracle, now + 1, ON_PAGES[index], ON_ADDR[index], 1)
                    now += 21
            self.handle(oracle, now, 0xb0, (CCS[slot], 0))
            self.handle(oracle, now + 1, 0xb0, (CCS[slot], 127))
            self.tick(oracle, now + 2)
            for tick in (now + 1202, now + 2402, now + 3602, now + 8602):
                self.tick(oracle, tick)

    def test_kemper_tuner_and_tempo_match_python(self):
        oracle = self.kemper_init()
        self.param(oracle, 10, 125, 84, 69)
        self.param(oracle, 11, 124, 15, 8300)
        self.param(oracle, 20, 127, 126, 1)
        for value in (0, 9, 60, 69, 127, 8192, 16383):
            self.param(oracle, 30 + value, 125, 84, value)
            self.param(oracle, 31 + value, 124, 15, value)
        self.param(oracle, 17000, 127, 126, 3)
        self.param(oracle, 17001, 125, 84, 69)
        for value in (40 * 64, 120 * 64 + 32, 121 * 64 + 32, 250 * 64, 16383):
            self.param(oracle, 18000, 4, 0, value)

    def test_kemper_boot_name_waits_for_authoritative_coordinates(self):
        oracle = self.kemper_init(1 << 4)
        # Intentional correction of CP's unsafe bootstrap assumption: the
        # first untagged name may describe rig3 while the local fallback is1.
        # Native does not publish it under rig1; test_kemper replays the full
        # name-before-PC capture and verifies bounded 0x43 recovery after PC.
        oracle.now = 100
        payload = (0, 0x20, 0x33, 2, 127, 3, 0, 0, 1) + tuple(b"Initial rig\0")
        oracle.plugin.on_midi_in("usb", 0, 0xf0, list(payload), oracle)
        reply = self.native.command("h 100 0 240 " + bytes(payload).hex())
        self.assertEqual(oracle.state()[8], 1)
        self.assertEqual(reply["state"][8], 0)
        self.assertEqual(reply["name"], "")

    def test_kemper_live_cc_at_settle_deadline_precedes_plugin_tick(self):
        oracle = self.kemper_init(1 << 4)
        self.select(oracle, 100, 2, 1)
        self.tick(oracle, 105)
        self.name(oracle, 200, "Lead")
        # The core drains MIDI before invoking the plugin tick. The deadline
        # has expired even though settle_until has not been cleared by tick.
        self.handle(oracle, 600, 0xb0, (22, 127))
        self.tick(oracle, 600)
        self.param(oracle, 601, 56, 3, 1)

    def test_kemper_deferred_pc_preserves_message_channel_override(self):
        oracle = self.kemper_init()
        oracle.now = 100
        oracle.select(3, 2, channel=10)
        reply = self.assert_kemper(oracle, "R 100 10 3 2")
        self.assertEqual(reply["channel"], 1)
        self.tick(oracle, 104)
        self.tick(oracle, 105)

    def test_kemper_reinitialization_and_wah_retry_across_uptime_wrap(self):
        for start in (0x90000000, 0xffffffff - 1000):
            oracle = self.kemper_init()
            self.handle(oracle, start, 0xf0, (0, 0x20, 0x33, 2, 127, 0x7e), 0)
            # Establish the current coordinates before testing the unrelated
            # WAH retirement/deadline behavior across uptime rollover.
            self.handle(oracle, start, 0xc0, (0,))
            self.name(oracle, start + 1, "Initial rig")
            self.tick(oracle, start + 501)
            self.param(oracle, start + 502, 5, 21, 1)
            self.tick(oracle, start + 522)
            for offset in (1722, 2922, 4122, 9122):
                self.tick(oracle, start + offset)

    def test_kemper_rapid_generation_changes_and_delayed_replies_match_python(self):
        oracle = self.kemper_init(0x93)  # A, B, X, Reverb, physical slot order.
        rng = random.Random(0xCE11)
        pending = []
        serial = 0
        device_rig = 1

        def queue(due, kind, values):
            nonlocal serial
            serial += 1
            heapq.heappush(pending, (due, serial, kind, values))

        def device_observes(reply, now):
            nonlocal device_rig
            for _channel, _status, encoded in reply["events"]:
                packet = bytes.fromhex(encoded)
                if packet[0] & 0xf0 == 0xc0:
                    old_rig, device_rig = device_rig, packet[1] + 1
                    queue(now + rng.choice((20, 180, 700, 2342)), "pc", (packet[1],))
                    queue(now + 20, "name", ("Rig %d" % old_rig,))
                    queue(now + 120, "name", ("Rig %d" % device_rig,))
                elif len(packet) == 11 and packet[6] == 0x41:
                    page, address = packet[8:10]
                    value = (device_rig + page) & 1
                    queue(now + rng.choice((20, 40, 450, 900, 1400)), "param", (page, address, value))

        next_switch = 100
        for now in range(100, 15000, 20):
            if now >= next_switch and now < 7000:
                rig = rng.randrange(1, 126)
                device_observes(self.select(oracle, now, (rig - 1) // 5 + 1, (rig - 1) % 5 + 1), now)
                next_switch = now + rng.choice((80, 180, 540, 700))
            while pending and pending[0][0] <= now:
                _due, _serial, kind, values = heapq.heappop(pending)
                if kind == "pc":
                    reply = self.handle(oracle, now, 0xc0, values)
                elif kind == "name":
                    reply = self.name(oracle, now, values[0])
                else:
                    reply = self.param(oracle, now, *values)
                device_observes(reply, now)
            if now % 600 == 0:
                device_observes(self.handle(oracle, now, 0xb0,
                    (rng.choice(CCS), rng.choice((0, 127)))), now)
            device_observes(self.tick(oracle, now), now)

    def test_kemper_command_golden_packets_match_python(self):
        oracle = self.kemper_init()
        cases = []
        for index, slot in enumerate(BLOCKS):
            for value in (0, 1):
                cases.append((0, index, value, {"type": "kemper_effect_toggle", "slot": slot,
                    "value": "on" if value else "off"}))
        for index, effect in enumerate(("Compressor", "Noise Gate", "Pure Booster", "Wah", "Transpose")):
            for value in (0, 1):
                cases.append((1, index, value, {"type": "kemper_fixed_toggle", "effect": effect,
                    "value": "on" if value else "off"}))
        cases.extend((
            (2, 0, 1, {"type": "kemper_tuner", "state": "on"}),
            (2, 0, 0, {"type": "kemper_tuner", "state": "off"}),
            (3, 0, 0, {"type": "kemper_tap_tempo"}),
            (5, 0, 64, {"type": "kemper_morph", "value": 64}),
            (6, 0, 1, {"type": "kemper_morph_trigger", "state": "on"}),
            (7, 0, 127, {"type": "kemper_wah", "value": 127}),
            (8, 0, 100, {"type": "kemper_volume", "value": 100}),
            (10, 0, 1, {"type": "kemper_rotary", "value": "fast"}),
            (11, 0, -1, {"type": "kemper_step_rig", "direction": "prev"}),
            (11, 0, 1, {"type": "kemper_step_rig", "direction": "next"}),
        ))
        cases.extend((4, 0, bpm, {"type": "kemper_set_tempo", "bpm": bpm}) for bpm in (0, 40, 127, 128, 250, 500))
        cases.extend((9, index, 1, {"type": "kemper_looper", "action": action})
                     for index, action in enumerate(("rec_play", "stop_erase", "trigger", "reverse", "half_speed")))
        for channel in (1, 3, 16):
            for command, index, value, message in cases:
                oracle.plugin.dispatch(dict(message, channel=channel), oracle.midi)
                reply = self.assert_kemper(oracle, "C %d %d %d %d" % (channel, command, index, value))
                self.assertEqual(reply["channel"], 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--driver", type=Path, required=True)
    ARGS, remaining = parser.parse_known_args()
    ARGS.driver = ARGS.driver.resolve(strict=True)
    unittest.main(argv=[sys.argv[0]] + remaining, verbosity=2)
