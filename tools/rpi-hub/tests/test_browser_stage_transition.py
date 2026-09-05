"""Offline tests for browser_stage_transition.py helpers.

These tests never launch a browser and never connect to the RPi/hardware.
"""

import asyncio
import importlib.util
import json
import re
import sys
import threading
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().with_name("browser_stage_transition.py")
SPEC = importlib.util.spec_from_file_location("browser_stage_transition_under_test", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def stage_state(rig, nav_id, top):
    switches = []
    for ident in ("1", "2", "3", "4", "UP"):
        label, active = top.get(ident, ("-", False))
        switches.append({"id": ident, "label": label, "active": active})
    for ident, label in zip(("A", "B", "C", "D", "DOWN"), MODULE.EXPECTED_NAV):
        switches.append({"id": ident, "label": label, "active": ident == nav_id})
    slot = {"A": 1, "B": 2, "C": 3, "D": 4, "DOWN": 5}[nav_id]
    return {
        "rig": rig,
        "meta": "· B1 · R%d" % slot,
        "switches": switches,
        "nav": list(MODULE.EXPECTED_NAV),
    }


def acoustic_state(active=True, label="HARM"):
    return stage_state("ACOUSTIC", "A", {"4": (label, active)})


def clean_state(flang=True, boost=False):
    return stage_state(
        "CLEAN", "B", {"3": ("FLANG", flang), "UP": ("BOOST", boost)},
    )


def record(ms, state):
    return {"ms": ms, "reason": "mutation", "state": state}


def wire(timestamp, direction, socket_name, message):
    return {
        "timestamp": timestamp,
        "direction": direction,
        "socket": socket_name,
        "request_id": socket_name,
        "message": message,
    }


def captain_led_dump(active=True):
    pixels = [[0, 0, 0] for _ in range(30)]
    rgb = [48, 4, 4] if active else [12, 1, 1]
    for index in (6, 7, 8):
        pixels[index] = list(rgb)
    return {
        "type": "LED_DUMP",
        "current": {"bank": 1, "slot": 2},
        "pixels": pixels,
        "switch_indices": {"3": [6, 7, 8], "up": [12, 13, 14]},
    }


def clean_patch_leds():
    return {
        "name": "CLEAN",
        "bindings": [
            {
                "switch": "3", "label": "FLANG", "mode": "latched",
                "led": {"on": "#ff0000", "off": "#000000"},
            },
            {
                "switch": "up", "label": "BOOST", "mode": "latched",
                "led": {"on": "#00ff00", "off": "#000000"},
            },
        ],
    }


def clean_led_dump(flang=True, boost=False, brightness=64, dim=4):
    pixels = [[0, 0, 0] for _ in range(30)]

    def colour(on, active):
        raw = on if active else tuple((channel * dim + 127) // 255 for channel in on)
        return [(channel * brightness + 127) // 255 for channel in raw]

    for index in (6, 7, 8):
        pixels[index] = colour((255, 0, 0), flang)
    for index in (12, 13, 14):
        pixels[index] = colour((0, 255, 0), boost)
    return {
        "type": "LED_DUMP",
        "current": {"bank": 1, "slot": 2},
        "pixels": pixels,
        "switch_indices": {"3": [6, 7, 8], "up": [12, 13, 14]},
    }


def test_browser_profile_cleanup_retries_transient_crashpad_lock():
    class LockedProfile:
        def __init__(self):
            self.calls = 0

        def cleanup(self):
            self.calls += 1
            if self.calls < 3:
                raise PermissionError(13, "Crashpad report is still open")

    profile = LockedProfile()

    asyncio.run(MODULE.cleanup_browser_profile(
        profile, attempts=3, retry_delay_seconds=0,
    ))

    assert profile.calls == 3


def test_browser_profile_cleanup_failure_does_not_mask_primary_error(capsys):
    class LockedProfile:
        def __init__(self):
            self.calls = 0

        def cleanup(self):
            self.calls += 1
            raise PermissionError(13, "Crashpad report remains open")

    profile = LockedProfile()
    primary = AssertionError("authoritative LED state mismatch")

    asyncio.run(MODULE.cleanup_browser_profile(
        profile, primary_error=primary, attempts=2, retry_delay_seconds=0,
    ))

    assert profile.calls == 2
    assert "browser profile cleanup failed" in primary.__notes__[0]
    assert "WARN: browser profile cleanup failed" in capsys.readouterr().out


def test_browser_profile_cleanup_failure_is_fatal_after_success():
    class LockedProfile:
        def cleanup(self):
            raise PermissionError(13, "Crashpad report remains open")

    with pytest.raises(PermissionError, match="Crashpad report remains open"):
        asyncio.run(MODULE.cleanup_browser_profile(
            LockedProfile(), attempts=2, retry_delay_seconds=0,
        ))


def test_passive_effect_auto_accepts_acoustic_and_clean_oracles():
    acoustic = MODULE.passive_effect_status(acoustic_state(), "auto")
    clean = MODULE.passive_effect_status(clean_state(), "auto")

    assert acoustic[:2] == (True, "acoustic")
    assert "HARM on" in acoustic[2]
    assert clean[:2] == (True, "clean")
    assert "FLANG on" in clean[2]


@pytest.mark.parametrize(
    "state",
    [
        acoustic_state(active=False),
        acoustic_state(label="-"),
        clean_state(flang=False),
        clean_state(boost=True),
    ],
)
def test_passive_effect_oracle_rejects_missing_or_wrong_effect_state(state):
    ready, resolved, _ = MODULE.passive_effect_status(state, "auto")

    assert ready is False
    assert resolved in ("acoustic", "clean")


def test_passive_auto_fails_closed_for_rig_without_known_oracle():
    state = stage_state("CRUNCH", "C", {"3": ("DRIVE", True)})

    ready, resolved, diagnostic = MODULE.passive_effect_status(state, "auto")

    assert ready is False
    assert resolved is None
    assert "no known" in diagnostic


def test_explicit_passive_rig_does_not_accept_the_other_rig():
    assert MODULE.passive_effect_status(clean_state(), "acoustic")[0] is False
    assert MODULE.passive_effect_status(acoustic_state(), "clean")[0] is False


def test_rig_oracles_require_both_exact_header_separators_and_location():
    acoustic = acoustic_state()
    acoustic["meta"] = "B1 · R1"
    clean = clean_state()
    clean["meta"] = "· B1 R2"

    assert MODULE.source_ready(acoustic) is False
    assert MODULE.target_ready(clean) is False


@pytest.mark.parametrize("header", ["· B%d · R%d", "· BANK %d · RIG %d"])
@pytest.mark.parametrize("bank,slot", [(1, 1), (1, 2), (25, 5)])
def test_location_accepts_default_and_saved_tft_prefixes(header, bank, slot):
    assert MODULE.at_location({"meta": header % (bank, slot)}, bank, slot)


@pytest.mark.parametrize("meta", [
    "BANK 1 · RIG 2", "· BANK 1 RIG 2", "BANK 1 RIG 2",
    "· BANK 1 · RIG 2 ·", "·· BANK 1 · RIG 2", "· B1 · R2 extra",
    "· BANK 1 · RIG 20", "· BANK 11 · RIG 2", "· BANK 2 · RIG 2",
    "· BANK 1 · RIG 1", "· BANK 1 · R2", "· B1 · RIG 2",
    "· BANK 01 · RIG 2", "· BANK1 · RIG2", "· BANK 1 · RIG 2 ",
    " · BANK 1 · RIG 2", "", None,
])
def test_location_rejects_wrong_coordinates_partial_headers_and_mixed_prefixes(meta):
    assert not MODULE.at_location({"meta": meta}, 1, 2)


def test_saved_tft_prefixes_preserve_passive_effect_and_transition_oracles():
    acoustic = acoustic_state()
    acoustic["meta"] = "· BANK 1 · RIG 1"
    clean = clean_state()
    clean["meta"] = "· BANK 1 · RIG 2"
    settling = clean_state(flang=False)
    settling["meta"] = clean["meta"]

    assert MODULE.passive_effect_status(acoustic)[:2] == (True, "acoustic")
    assert MODULE.passive_effect_status(clean)[:2] == (True, "clean")
    assert MODULE.passive_effect_status(settling)[:2] == (False, "clean")
    trace = {"records": [record(0, settling), record(40, clean)]}
    assert MODULE.validate_target_trace(trace, 100) == (40, 1)
    trace["records"].append(record(50, settling))
    with pytest.raises(AssertionError, match="bounced"):
        MODULE.validate_target_trace(trace, 100)


def test_source_trace_requires_harm_label_and_stays_ready():
    trace = {"records": [record(0, acoustic_state(False)), record(40, acoustic_state())]}

    assert MODULE.validate_source_trace(trace, 100) == 40

    wrong_label = {"records": [record(20, acoustic_state(label="OTHER"))]}
    with pytest.raises(AssertionError, match="never became ready"):
        MODULE.validate_source_trace(wrong_label, 100)


def test_target_trace_distinguishes_settle_from_real_bounce():
    settling = {
        "records": [
            record(0, clean_state(flang=False)),
            record(30, clean_state(flang=True)),
            record(100, clean_state(flang=True)),
        ]
    }
    assert MODULE.validate_target_trace(settling, 100) == (30, 1)

    bounced = {
        "records": [
            record(0, clean_state(flang=False)),
            record(10, clean_state(flang=True)),
            record(20, clean_state(flang=False)),
            record(30, clean_state(flang=True)),
        ]
    }
    with pytest.raises(AssertionError, match="bounced"):
        MODULE.validate_target_trace(bounced, 100)


def test_control_socket_url_is_tagged_without_losing_existing_query():
    tagged = MODULE.control_hub_url("ws://pi.local:8081/path?existing=yes")

    assert tagged.startswith("ws://pi.local:8081/path?")
    assert "existing=yes" in tagged
    assert "bosun_client=transition-control" in tagged
    assert MODULE.control_hub_url(tagged).count("bosun_client=") == 1


def test_cross_socket_receive_copies_collapse_but_same_socket_repeats_do_not():
    context = {"type": "CONTEXT", "context": {"kemper_block_X": "on"}}
    frames = [
        wire(10.000, "RECV", "stage", context),
        wire(10.004, "RECV", "control", context),
        wire(10.010, "RECV", "stage", context),
        wire(10.011, "SEND", "control", context),
        wire(10.012, "SEND", "stage", context),
    ]

    result = MODULE.deduplicate_wire_frames(frames)

    assert len(result) == 4
    assert result[0]["sockets"] == ["stage", "control"]
    assert result[0]["copies"] == 2
    assert result[1]["sockets"] == ["stage"]
    assert [item["direction"] for item in result[2:]] == ["SEND", "SEND"]


def test_cross_socket_copy_outside_time_window_remains_visible():
    message = {"type": "EVENT", "event": "patch_switched"}
    frames = [
        wire(1.0, "RECV", "stage", message),
        wire(1.2, "RECV", "control", message),
    ]

    assert len(MODULE.deduplicate_wire_frames(frames)) == 2


def test_cdp_collector_labels_both_sockets_and_both_directions():
    session = MODULE.CdpSession(websocket=None)
    session.observe({
        "method": "Network.webSocketCreated",
        "params": {"requestId": "stage-id", "url": "ws://pi.local:8081/"},
    })
    session.observe({
        "method": "Network.webSocketCreated",
        "params": {
            "requestId": "control-id",
            "url": MODULE.control_hub_url("ws://pi.local:8081/"),
        },
    })
    payload = json.dumps({"type": "SWITCH_PATCH", "id": "cycle-1", "bank": 1, "slot": 2})
    session.observe({
        "method": "Network.webSocketFrameSent",
        "params": {
            "requestId": "control-id", "timestamp": 4.0,
            "response": {"payloadData": payload},
        },
    })
    session.observe({
        "method": "Network.webSocketFrameReceived",
        "params": {
            "requestId": "stage-id", "timestamp": 4.1,
            "response": {"payloadData": payload},
        },
    })

    assert [(item["direction"], item["socket"]) for item in session.frames] == [
        ("SEND", "control"), ("RECV", "stage"),
    ]
    rendered = MODULE.format_wire_frames(session.frames)
    assert any("SEND control" in line and "SWITCH_PATCH" in line for line in rendered)
    assert any("RECV stage" in line and "SWITCH_PATCH" in line for line in rendered)


def test_cdp_command_collects_wire_events_while_waiting_for_its_response():
    class FakeWebSocket:
        def __init__(self):
            self.sent = []
            self.incoming = iter([
                json.dumps({
                    "method": "Network.webSocketCreated",
                    "params": {"requestId": "stage-id", "url": "ws://pi.local:8081/"},
                }),
                json.dumps({
                    "method": "Network.webSocketFrameReceived",
                    "params": {
                        "requestId": "stage-id", "timestamp": 2.0,
                        "response": {"payloadData": json.dumps({
                            "type": "CONTEXT",
                            "context": {"kemper_block_X": "on"},
                        })},
                    },
                }),
                json.dumps({"id": 1, "result": {}}),
            ])

        async def send(self, payload):
            self.sent.append(json.loads(payload))

        async def recv(self):
            return next(self.incoming)

    websocket = FakeWebSocket()
    session = MODULE.CdpSession(websocket)

    result = asyncio.run(session.command("Runtime.evaluate", {"expression": "1"}))

    assert result == {}
    assert websocket.sent == [{
        "id": 1, "method": "Runtime.evaluate", "params": {"expression": "1"},
    }]
    assert session.frames == [{
        "timestamp": 2.0,
        "direction": "RECV",
        "socket": "stage",
        "request_id": "stage-id",
        "message": {
            "type": "CONTEXT", "context": {"kemper_block_X": "on"},
        },
    }]


def test_cdp_command_has_one_global_deadline_even_if_no_response_arrives(monkeypatch):
    class HangingWebSocket:
        async def send(self, payload):
            return None

        async def recv(self):
            await asyncio.Future()

    monkeypatch.setattr(MODULE, "CDP_COMMAND_TIMEOUT_SECONDS", 0.01)
    session = MODULE.CdpSession(HangingWebSocket())

    with pytest.raises(RuntimeError, match="CDP Runtime.evaluate response timed out"):
        asyncio.run(session.command("Runtime.evaluate"))


def test_cdp_command_send_and_response_share_one_global_deadline(monkeypatch):
    clock = [100.0]
    observed_timeouts = []

    class SlowSendWebSocket:
        async def send(self, payload):
            clock[0] += 0.008

        async def recv(self):
            return json.dumps({"id": 1, "result": {}})

    real_wait_for = MODULE.asyncio.wait_for

    async def recording_wait_for(awaitable, timeout):
        observed_timeouts.append(timeout)
        return await real_wait_for(awaitable, timeout=1.0)

    monkeypatch.setattr(MODULE, "CDP_COMMAND_TIMEOUT_SECONDS", 0.010)
    monkeypatch.setattr(MODULE.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(MODULE.asyncio, "wait_for", recording_wait_for)

    assert asyncio.run(MODULE.CdpSession(SlowSendWebSocket()).command("Runtime.evaluate")) == {}
    assert observed_timeouts[0] == pytest.approx(0.010)
    assert observed_timeouts[1] == pytest.approx(0.002)


def test_polling_helpers_cannot_accept_a_snapshot_returned_after_their_deadline():
    class SlowReadyCdp:
        async def evaluate(self, expression, await_promise=False):
            await asyncio.sleep(0.025)
            return clean_state()

    cdp = SlowReadyCdp()
    with pytest.raises(AssertionError, match="did not appear within 5 ms"):
        asyncio.run(MODULE.wait_for_state(
            cdp, lambda _state: True, 0.005, "late state",
        ))
    with pytest.raises(AssertionError, match="was not stable.*within 5 ms"):
        asyncio.run(MODULE.wait_for_stable_state(
            cdp, lambda _state: True, 0.005, 0.001, "late stable state",
        ))
    with pytest.raises(AssertionError, match="NAV bootstrap exceeded 5 ms"):
        asyncio.run(MODULE.passive_nav(cdp, MODULE.time.monotonic(), 5))
    with pytest.raises(AssertionError, match="passive effects.*within 5 ms"):
        asyncio.run(MODULE.passive_effects(
            cdp, MODULE.time.monotonic(), "clean", 5, 1,
        ))


def test_wire_summary_keeps_effect_context_but_compacts_patch_actions():
    context = MODULE.summarize_wire_payload(json.dumps({
        "type": "CONTEXT", "partial": True,
        "context": {"kemper_block_X": "on"},
    }))
    patch = MODULE.summarize_wire_payload(json.dumps({
        "type": "PATCH", "bank": 1, "slot": 2,
        "patch": {
            "name": "CLEAN",
            "bindings": [{
                "switch": "3", "label": "FLANG", "mode": "latched",
                "led": {"on": "#ff0000", "off": "#000000", "animation": "ignored"},
                "actions": {"toggle_on": {"messages": ["large"]}},
            }],
        },
    }))

    assert context["context"] == {"kemper_block_X": "on"}
    assert patch["patch"]["bindings"] == [
        {
            "switch": "3", "label": "FLANG", "mode": "latched",
            "led": {"on": "#ff0000", "off": "#000000"},
        }
    ]
    assert "actions" not in json.dumps(patch)
    assert "animation" not in json.dumps(patch)


def test_wire_summary_does_not_invent_null_led_dump_response_fields_on_send():
    request = MODULE.summarize_wire_payload(json.dumps({
        "type": "LED_DUMP", "id": "cycle-1-leds",
    }))
    response = MODULE.summarize_wire_payload(json.dumps({
        "type": "LED_DUMP", "id": "cycle-1-leds",
        "current": {"bank": 1, "slot": 2},
        "pixels": [[1, 2, 3], [4, 5, 6]],
        "switch_indices": {"3": [0], "up": [1]},
    }))

    assert request == {"type": "LED_DUMP", "id": "cycle-1-leds"}
    assert response == {
        "type": "LED_DUMP", "id": "cycle-1-leds",
        "current": {"bank": 1, "slot": 2},
        "pixel_count": 2,
        "switch_indices": {"3": [0], "up": [1]},
    }


def test_wire_cursor_survives_bounded_trace_rollover(monkeypatch):
    monkeypatch.setattr(MODULE, "WIRE_FRAME_LIMIT", 3)
    session = MODULE.CdpSession(websocket=None)

    for index in range(3):
        session.observe({
            "method": "Network.webSocketFrameReceived",
            "params": {
                "requestId": "stage", "timestamp": float(index),
                "response": {"payloadData": json.dumps({"type": "OLD", "n": index})},
            },
        })
    cursor = session.frame_cursor()
    for index in range(2):
        session.observe({
            "method": "Network.webSocketFrameReceived",
            "params": {
                "requestId": "stage", "timestamp": 10.0 + index,
                "response": {"payloadData": json.dumps({"type": "NEW", "n": index})},
            },
        })

    assert [frame["message"]["n"] for frame in session.frames_since(cursor)] == [0, 1]
    with pytest.raises(RuntimeError, match="truncated"):
        session.frames_since(0)


def test_cli_defaults_to_passive_effects_and_cold_smoke_keeps_them_enabled():
    default = MODULE.parse_args([])
    cold = MODULE.parse_args(["--cold-smoke", "3"])
    opted_out = MODULE.parse_args(["--no-passive-effects", "--nav-only"])

    assert default.passive_effects is True
    assert default.passive_rig == "auto"
    assert cold.nav_only is True and cold.cold_runs == 3
    assert cold.passive_effects is True
    assert opted_out.passive_effects is False


def test_cli_rejects_impossible_stability_window_and_disabled_explicit_oracle():
    for arguments in (
        ["--passive-stable-ms", "4000", "--max-passive-effects-ms", "4000"],
        ["--no-passive-effects", "--passive-rig", "clean"],
    ):
        with redirect_stderr(StringIO()), pytest.raises(SystemExit):
            MODULE.parse_args(arguments)


def test_effect_stress_is_explicitly_opt_in_and_incompatible_with_passive_mode():
    assert MODULE.parse_args([]).effect_cycles == 0
    enabled = MODULE.parse_args(["--effect-cycles", "7", "--effect-ssh-target", "pi@hub.local"])
    assert enabled.effect_cycles == 7
    assert enabled.effect_ssh_target == "pi@hub.local"

    invalid = (
        ["--effect-cycles", "1", "--nav-only"],
        ["--effect-cycles", "1", "--cold-smoke"],
        ["--effect-stable-ms", "2500", "--max-effect-ms", "2500"],
        ["--effect-cycles", "1", "--effect-ssh-target", "-oProxyCommand=bad"],
        ["--effect-cycles", "1", "--effect-ssh-target", "hub;bad"],
    )
    for arguments in invalid:
        with redirect_stderr(StringIO()), pytest.raises(SystemExit):
            MODULE.parse_args(arguments)


def test_kemper_aplay_port_discovery_is_exact_and_fails_closed():
    listing = """\
 Port    Client name                      Port name
  20:0  MIDI Through                     MIDI Through Port-0
  28:0  MIDI Captain                     MIDI Captain MIDI 1
  32:0  Profiler                         Profiler MIDI 1
  40:0  NotAProfilerController            Other
"""
    assert MODULE.parse_kemper_aplay_port(listing) == "32:0"

    for ambiguous in (
        "20:0 MIDI Through MIDI Through Port-0\n",
        "32:0 Profiler MIDI 1\n33:0 Kemper Backup MIDI 1\n",
        "40:0 NotAProfilerController Other\n",
    ):
        with pytest.raises(RuntimeError, match="exactly one"):
            MODULE.parse_kemper_aplay_port(ambiguous)


def test_midi_cc_smf_contains_one_channel_one_x_message_and_track_end():
    payload = MODULE.midi_cc_smf(1, 22, 127)

    assert payload.startswith(b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x00\x60")
    assert payload.endswith(b"\x00\xb0\x16\x7f\x00\xff\x2f\x00")
    assert int.from_bytes(payload[18:22], "big") == 8

    for values in ((0, 22, 127), (17, 22, 127), (1, -1, 127), (1, 22, 128)):
        with pytest.raises(ValueError):
            MODULE.midi_cc_smf(*values)


def test_kemper_midi_injector_discovers_once_and_uses_safe_tempfile_command():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if command[-1] == "LC_ALL=C aplaymidi -l":
            return SimpleNamespace(
                returncode=0,
                stdout=" Port Client name Port name\n 32:0 Profiler Profiler MIDI 1\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    injector = MODULE.KemperMidiInjector("ssh.exe", "pi@hub.local", runner=runner)
    injector.send_effect("X", False)
    injector.send_effect("X", True)

    assert len(calls) == 3
    assert calls[0][0][:2] == ["ssh.exe", "-o"]
    assert all(call[0][-2] == "pi@hub.local" for call in calls)
    remote_off = calls[1][0][-1]
    remote_on = calls[2][0][-1]
    assert "mktemp /tmp/bosun-effect-XXXXXX.mid" in remote_off
    assert "trap 'rm -f \"$effect_file\"' EXIT" in remote_off
    # aplaymidi otherwise applies its post-song delay, which consumes most of
    # the browser transition budget after this one-event MIDI file has ended.
    assert "aplaymidi -d 0 -p 32:0 \"$effect_file\"" in remote_off
    encoded_off = re.search(r"printf '%s' '([^']+)'", remote_off).group(1)
    encoded_on = re.search(r"printf '%s' '([^']+)'", remote_on).group(1)
    import base64
    assert base64.b64decode(encoded_off).endswith(b"\x00\xb0\x16\x00\x00\xff\x2f\x00")
    assert base64.b64decode(encoded_on).endswith(b"\x00\xb0\x16\x7f\x00\xff\x2f\x00")


def test_kemper_midi_injector_propagates_discovery_command_and_timeout_failures():
    def command_failure(command, **kwargs):
        return SimpleNamespace(returncode=127, stdout="", stderr="aplaymidi: not found")

    with pytest.raises(RuntimeError, match="exit 127.*not found"):
        MODULE.KemperMidiInjector("ssh", "hub", runner=command_failure).discover()

    def timeout(command, **kwargs):
        raise MODULE.subprocess.TimeoutExpired(command, kwargs["timeout"])

    with pytest.raises(RuntimeError, match="timed out"):
        MODULE.KemperMidiInjector("ssh", "hub", runner=timeout).discover()


def test_effect_transition_reports_midi_injection_time_when_budget_is_consumed(
        monkeypatch):
    class FakeCdp:
        async def evaluate(self, expression):
            assert expression == "window.__bosunStageTrace.begin()"

    async def slow_injection(injector, block, active):
        assert block == "X"
        assert active is False
        return 2_001.25

    monkeypatch.setattr(MODULE, "send_effect_midi", slow_injection)
    args = MODULE.parse_args([
        "--max-effect-ms", "2500", "--effect-stable-ms", "500",
    ])

    with pytest.raises(
            AssertionError,
            match=r"MIDI injection took 2001\.2 ms.*500 ms stability.*2500 ms",
    ):
        asyncio.run(MODULE.run_effect_transition(
            FakeCdp(), args, object(), "slow", True, False,
        ))


def test_effect_surface_validation_requires_dom_context_location_and_led_shape():
    state = clean_state(flang=True, boost=False)
    context = {
        "type": "CONTEXT",
        "context": {"bank": 1, "slot": 2, "kemper_block_X": "on"},
    }
    led_dump = captain_led_dump(True)

    assert MODULE.validate_effect_surfaces(state, context, led_dump, True) == (
        (48, 4, 4), (48, 4, 4), (48, 4, 4),
    )

    bad_cases = []
    bad_context = json.loads(json.dumps(context))
    bad_context["context"]["kemper_block_X"] = "off"
    bad_cases.append((state, bad_context, led_dump, True))
    partial_context = json.loads(json.dumps(context))
    partial_context["partial"] = True
    bad_cases.append((state, partial_context, led_dump, True))
    bad_location = json.loads(json.dumps(led_dump))
    bad_location["current"]["slot"] = 1
    bad_cases.append((state, context, bad_location, True))
    bad_pixels = json.loads(json.dumps(led_dump))
    bad_pixels["switch_indices"]["3"] = [999]
    bad_cases.append((state, context, bad_pixels, True))
    bad_cases.append((clean_state(flang=False), context, led_dump, True))
    for case in bad_cases:
        with pytest.raises(AssertionError):
            MODULE.validate_effect_surfaces(*case)


def test_clean_transition_requires_authoritative_reverb_off_and_exact_led_states():
    state = clean_state(flang=True, boost=False)
    context = {
        "type": "CONTEXT",
        "context": {
            "bank": 1, "slot": 2,
            "kemper_block_X": "on", "kemper_block_Reverb": "off",
        },
    }
    led_dump = clean_led_dump()

    info = MODULE.validate_clean_transition_surfaces(
        state, context, led_dump, clean_patch_leds(),
    )
    assert info["flang"] == ((64, 0, 0),) * 3
    assert info["boost"] == ((0, 1, 0),) * 3
    assert info["brightness"] == (64,)
    assert 4 in info["dim"]

    unknown = json.loads(json.dumps(context))
    del unknown["context"]["kemper_block_Reverb"]
    with pytest.raises(AssertionError, match="unknown is not off"):
        MODULE.validate_clean_transition_surfaces(
            state, unknown, led_dump, clean_patch_leds(),
        )

    partial = json.loads(json.dumps(context))
    partial["partial"] = True
    with pytest.raises(AssertionError, match="partial"):
        MODULE.validate_clean_transition_surfaces(
            state, partial, led_dump, clean_patch_leds(),
        )


def test_clean_led_verification_fails_closed_for_wrong_or_unprovable_frames():
    patch = clean_patch_leds()

    with pytest.raises(AssertionError, match="ON/OFF pixels are ambiguous"):
        MODULE.validate_clean_led_frame(clean_led_dump(boost=True), patch)

    overlap = clean_led_dump()
    overlap["switch_indices"]["up"] = [6, 7, 8]
    with pytest.raises(AssertionError, match="overlapping"):
        MODULE.validate_clean_led_frame(overlap, patch)

    no_config = json.loads(json.dumps(patch))
    del no_config["bindings"][0]["led"]
    with pytest.raises(AssertionError, match="unavailable.*refusing"):
        MODULE.validate_clean_led_frame(clean_led_dump(), no_config)

    # dim=255 makes a latched OFF ring identical to ON. Never accept a
    # visually indistinguishable framebuffer merely because context says off.
    with pytest.raises(AssertionError, match="ambiguous"):
        MODULE.validate_clean_led_frame(
            clean_led_dump(boost=False, dim=255), patch,
        )


def test_clean_surface_read_is_correlated_and_bounded(monkeypatch):
    calls = []

    async def fake_request(cdp, method, ident, expected_type, timeout_seconds=8.0):
        calls.append((method, ident, expected_type, timeout_seconds))
        if method == "getContext":
            return {
                "type": "CONTEXT", "sentAt": 100.0, "ackAt": 110.0,
                "context": {
                    "bank": 1, "slot": 2,
                    "kemper_block_X": "on", "kemper_block_Reverb": "off",
                },
            }
        response = clean_led_dump()
        response.update({"sentAt": 111.0, "ackAt": 120.0})
        return response

    async def fake_snapshot(cdp):
        return clean_state(flang=True, boost=False)

    monkeypatch.setattr(MODULE, "send_control_request", fake_request)
    monkeypatch.setattr(MODULE, "snapshot", fake_snapshot)
    result = asyncio.run(MODULE.read_clean_transition_surfaces(
        object(), "cycle-1", clean_patch_leds(), 1.0,
    ))

    assert result[1]["context"]["kemper_block_Reverb"] == "off"
    assert [call[:3] for call in calls] == [
        ("getContext", "cycle-1-context", "CONTEXT"),
        ("ledDump", "cycle-1-leds", "LED_DUMP"),
    ]
    assert all(0 < call[3] <= 1.0 for call in calls)


def test_clean_milestones_align_wire_and_dom_to_the_switch_command():
    command_id = "cycle-1-clean"
    patch = clean_patch_leds()
    frames = [
        wire(10.000, "SEND", "control", {
            "type": "SWITCH_PATCH", "id": command_id, "bank": 1, "slot": 2,
        }),
        wire(10.600, "RECV", "stage", {
            "type": "EVENT", "event": "patch_switched", "bank": 1, "slot": 2,
        }),
        wire(10.605, "RECV", "control", {"type": "ACK", "id": command_id}),
        wire(11.085, "RECV", "stage", {
            "type": "PATCH", "bank": 1, "slot": 2, "patch": patch,
        }),
        wire(11.798, "RECV", "stage", {
            "type": "CONTEXT", "context": {"kemper_block_Reverb": "off"},
        }),
        wire(12.688, "RECV", "stage", {
            "type": "CONTEXT", "context": {"kemper_block_X": "on"},
        }),
    ]
    trace = {
        "startedAt": 5000.0,
        "records": [
            record(0, acoustic_state()),
            record(610, stage_state("CLEAN", "B", {})),
            record(1090, clean_state(flang=False)),
            record(2690, clean_state(flang=True)),
        ],
    }
    context = {"ackAt": 7700.0}
    leds = {"ackAt": 7750.0}

    milestones = MODULE.clean_transition_milestones(
        frames, trace, command_id, context, leds,
    )
    assert milestones["event"] == pytest.approx(600)
    assert milestones["patch"] == pytest.approx(1085)
    assert milestones["reverb_off"] == pytest.approx(1798)
    assert milestones["x_on"] == pytest.approx(2688)
    assert milestones["dom_bindings"] == 1090
    assert milestones["dom_ready"] == 2690
    assert milestones["context_check"] == 2700
    assert milestones["led_check"] == 2750
    rendered = MODULE.format_clean_milestones(1, milestones)
    assert "event=600.0ms" in rendered
    assert "x_on=2688.0ms" in rendered
    assert "dom_ready=2690.0ms" in rendered

    assert MODULE.latest_patch_from_wire(frames, 1, 2) == patch


def test_led_level_validation_distinguishes_dim_off_full_on_and_exact_restore():
    on = ((48, 4, 4),) * 3
    off = ((12, 1, 1),) * 3
    MODULE.validate_led_levels(on, off, True)
    MODULE.validate_led_levels(off, on, False)

    with pytest.raises(AssertionError, match="did not change"):
        MODULE.validate_led_levels(on, on, True)
    with pytest.raises(AssertionError, match="not brighter"):
        MODULE.validate_led_levels(off, on, True)


def test_effect_trace_requires_one_transition_and_rejects_bounce_or_boost_flash():
    clean_on = clean_state(flang=True)
    clean_off = clean_state(flang=False)
    trace = {"records": [record(0, clean_on), record(80, clean_off), record(600, clean_off)]}
    assert MODULE.validate_effect_trace(trace, True, False, 200) == (80, 1)

    bounce = {
        "records": [
            record(0, clean_on), record(20, clean_off),
            record(30, clean_on), record(40, clean_off),
        ]
    }
    with pytest.raises(AssertionError, match="regressed"):
        MODULE.validate_effect_trace(bounce, True, False, 200)

    boosted = clean_state(flang=False, boost=True)
    with pytest.raises(AssertionError, match="BOOST lit"):
        MODULE.validate_effect_trace(
            {"records": [record(0, clean_on), record(20, boosted)]},
            True, False, 200,
        )


@pytest.mark.parametrize("initial_raw,initial_active", [("on", True), ("off", False)])
def test_effect_cycle_failure_always_restores_observed_initial_value(
        monkeypatch, initial_raw, initial_active):
    calls = []
    current = {"active": initial_active, "surface_reads": 0}

    class FakeInjector:
        def __init__(self, *args, **kwargs):
            pass

        def discover(self):
            calls.append(("discover",))
            return "32:0"

        def send_effect(self, block, active):
            calls.append((block, active))
            current["active"] = active

    async def fake_request(cdp, method, ident, expected_type, timeout_seconds=8.0):
        current["surface_reads"] += 1
        if method == "getContext":
            return {
                "type": "CONTEXT",
                "context": {
                    "bank": 1, "slot": 2,
                    "kemper_block_X": "on" if current["active"] else "off",
                },
            }
        return captain_led_dump(current["active"])

    async def fake_snapshot(cdp):
        return clean_state(flang=current["active"])

    async def fail_after_mutation(cdp, args, injector, prefix,
                                  transition_initial, transition_target):
        injector.send_effect("X", transition_target)
        raise TimeoutError("synthetic CDP timeout")

    monkeypatch.setattr(MODULE, "KemperMidiInjector", FakeInjector)
    monkeypatch.setattr(MODULE, "send_control_request", fake_request)
    monkeypatch.setattr(MODULE, "snapshot", fake_snapshot)
    monkeypatch.setattr(MODULE, "run_effect_transition", fail_after_mutation)
    args = MODULE.parse_args([
        "--effect-cycles", "1", "--max-effect-ms", "100",
        "--effect-stable-ms", "20",
    ])

    with pytest.raises(TimeoutError, match="synthetic"):
        asyncio.run(MODULE.run_effect_cycles(object(), args, 1))

    assert calls == [
        ("discover",),
        ("X", not initial_active),
        ("X", initial_active),
    ]
    assert current["active"] is initial_active
    # Initial context + LED, followed by at least two complete correlated
    # reads which prove the restored state stayed converged.
    assert current["surface_reads"] >= 6


def test_ambiguous_port_fails_before_any_effect_mutation_or_restore(monkeypatch):
    calls = []

    class AmbiguousInjector:
        def __init__(self, *args, **kwargs):
            pass

        def discover(self):
            calls.append(("discover",))
            raise RuntimeError("expected exactly one writable port")

        def send_effect(self, block, active):
            calls.append((block, active))

    async def fake_request(cdp, method, ident, expected_type, timeout_seconds=8.0):
        if method == "getContext":
            return {
                "type": "CONTEXT",
                "context": {"bank": 1, "slot": 2, "kemper_block_X": "on"},
            }
        return captain_led_dump(True)

    async def fake_snapshot(cdp):
        return clean_state()

    monkeypatch.setattr(MODULE, "KemperMidiInjector", AmbiguousInjector)
    monkeypatch.setattr(MODULE, "send_control_request", fake_request)
    monkeypatch.setattr(MODULE, "snapshot", fake_snapshot)
    args = MODULE.parse_args(["--effect-cycles", "1"])

    with pytest.raises(RuntimeError, match="exactly one"):
        asyncio.run(MODULE.run_effect_cycles(object(), args, 1))

    assert calls == [("discover",)]


def test_restore_failure_is_never_hidden_by_the_primary_browser_failure(monkeypatch):
    calls = []

    class RestoreFailsInjector:
        def __init__(self, *args, **kwargs):
            pass

        def discover(self):
            return "32:0"

        def send_effect(self, block, active):
            calls.append((block, active))
            if active is True and len(calls) > 1:
                raise RuntimeError("restore transport down")

    async def fake_request(cdp, method, ident, expected_type, timeout_seconds=8.0):
        if method == "getContext":
            return {
                "type": "CONTEXT",
                "context": {"bank": 1, "slot": 2, "kemper_block_X": "on"},
            }
        return captain_led_dump(True)

    async def fake_snapshot(cdp):
        return clean_state()

    async def fail_after_mutation(cdp, args, injector, prefix,
                                  transition_initial, transition_target):
        injector.send_effect("X", transition_target)
        raise TimeoutError("browser disappeared")

    monkeypatch.setattr(MODULE, "KemperMidiInjector", RestoreFailsInjector)
    monkeypatch.setattr(MODULE, "send_control_request", fake_request)
    monkeypatch.setattr(MODULE, "snapshot", fake_snapshot)
    monkeypatch.setattr(MODULE, "run_effect_transition", fail_after_mutation)
    args = MODULE.parse_args(["--effect-cycles", "1"])

    with pytest.raises(
            RuntimeError,
            match=(
                "restoration failed; final effect state unknown.*"
                "primary failure=TimeoutError: browser disappeared.*"
                "restore failure=RuntimeError: restore transport down"
            ),
    ):
        asyncio.run(MODULE.run_effect_cycles(object(), args, 1))

    assert calls == [("X", False), ("X", True)]


def test_primary_failure_with_unconfirmed_restore_reports_final_state_unknown(monkeypatch):
    calls = []
    current = {"active": True}

    class IgnoredRestoreInjector:
        def __init__(self, *args, **kwargs):
            pass

        def discover(self):
            return "32:0"

        def send_effect(self, block, active):
            calls.append((block, active))
            # The first CC mutates the Kemper; model a dropped restore CC by
            # accepting the second command without changing the real state.
            if len(calls) == 1:
                current["active"] = active

    async def fake_request(cdp, method, ident, expected_type, timeout_seconds=8.0):
        if method == "getContext":
            return {
                "type": "CONTEXT",
                "context": {
                    "bank": 1, "slot": 2,
                    "kemper_block_X": "on" if current["active"] else "off",
                },
            }
        return captain_led_dump(current["active"])

    async def fake_snapshot(cdp):
        return clean_state(flang=current["active"])

    async def fail_after_mutation(cdp, args, injector, prefix,
                                  transition_initial, transition_target):
        injector.send_effect("X", transition_target)
        raise TimeoutError("synthetic transition failure")

    monkeypatch.setattr(MODULE, "KemperMidiInjector", IgnoredRestoreInjector)
    monkeypatch.setattr(MODULE, "send_control_request", fake_request)
    monkeypatch.setattr(MODULE, "snapshot", fake_snapshot)
    monkeypatch.setattr(MODULE, "run_effect_transition", fail_after_mutation)
    args = MODULE.parse_args([
        "--effect-cycles", "1", "--max-effect-ms", "60",
        "--effect-stable-ms", "10",
    ])

    with pytest.raises(RuntimeError) as captured:
        asyncio.run(MODULE.run_effect_cycles(object(), args, 1))

    message = str(captured.value)
    assert "restoration failed; final effect state unknown" in message
    assert "primary failure=TimeoutError: synthetic transition failure" in message
    assert "restore was not authoritative within 60 ms" in message
    assert calls == [("X", False), ("X", True)]


def test_primary_cancellation_still_restores_and_confirms_observed_state(monkeypatch):
    calls = []
    current = {"active": True}

    class FakeInjector:
        def __init__(self, *args, **kwargs):
            pass

        def discover(self):
            return "32:0"

        def send_effect(self, block, active):
            calls.append((block, active))
            current["active"] = active

    async def fake_request(cdp, method, ident, expected_type, timeout_seconds=8.0):
        if method == "getContext":
            return {
                "type": "CONTEXT",
                "context": {
                    "bank": 1, "slot": 2,
                    "kemper_block_X": "on" if current["active"] else "off",
                },
            }
        return captain_led_dump(current["active"])

    async def fake_snapshot(cdp):
        return clean_state(flang=current["active"])

    async def cancel_after_mutation(cdp, args, injector, prefix,
                                    transition_initial, transition_target):
        injector.send_effect("X", transition_target)
        raise asyncio.CancelledError

    monkeypatch.setattr(MODULE, "KemperMidiInjector", FakeInjector)
    monkeypatch.setattr(MODULE, "send_control_request", fake_request)
    monkeypatch.setattr(MODULE, "snapshot", fake_snapshot)
    monkeypatch.setattr(MODULE, "run_effect_transition", cancel_after_mutation)
    args = MODULE.parse_args([
        "--effect-cycles", "1", "--max-effect-ms", "100",
        "--effect-stable-ms", "20",
    ])

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(MODULE.run_effect_cycles(object(), args, 1))

    assert calls == [("X", False), ("X", True)]
    assert current["active"] is True


def test_send_effect_midi_finishes_inflight_thread_before_propagating_cancel():
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    class BlockingInjector:
        def send_effect(self, block, active):
            assert (block, active) == ("X", False)
            entered.set()
            assert release.wait(1.0)
            completed.set()

    async def exercise():
        call = asyncio.create_task(
            MODULE.send_effect_midi(BlockingInjector(), "X", False),
        )
        while not entered.is_set():
            await asyncio.sleep(0)
        call.cancel()
        await asyncio.sleep(0)
        assert not call.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await call

    asyncio.run(exercise())
    assert completed.is_set()


def test_invalid_initial_context_fails_before_injector_or_midi_send(monkeypatch):
    constructed = []

    class MustNotRunInjector:
        def __init__(self, *args, **kwargs):
            constructed.append("constructed")

        def send_effect(self, block, active):
            constructed.append((block, active))

    async def unknown_context(cdp, method, ident, expected_type, timeout_seconds=8.0):
        return {
            "type": "CONTEXT",
            "context": {"bank": 1, "slot": 2, "kemper_block_X": "unknown"},
        }

    monkeypatch.setattr(MODULE, "KemperMidiInjector", MustNotRunInjector)
    monkeypatch.setattr(MODULE, "send_control_request", unknown_context)
    args = MODULE.parse_args(["--effect-cycles", "1"])

    with pytest.raises(AssertionError, match="not authoritative"):
        asyncio.run(MODULE.run_effect_cycles(object(), args, 1))

    assert constructed == []
