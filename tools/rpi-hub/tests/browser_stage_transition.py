"""Exercise the real Stage page through the real RPi/Captain/Kemper path.

The default is one cold browser load, a passive NAV + current-effect bootstrap
check, then one ACOUSTIC -> CLEAN transition. ``--stress`` repeats the
transition in the same browser, while ``--cold-smoke`` opens fresh Edge
profiles and checks passive bootstrap only.

``--effect-cycles`` additionally drives CLEAN's X/FLANG block OFF then ON by
sending a real MIDI CC to the Kemper through the RPi.  It then requires the
Kemper-derived Captain context, the Captain's physical LED framebuffer and the
Stage DOM to converge without a bounce.  This is intentionally opt-in because
it changes the live Kemper effect state (and restores the exact observed
initial X state in ``finally``).

Stage itself is display-only and the production Captain protocol has no
synthetic ``PRESS_SWITCH`` command.  Consequently this mode covers the real
Kemper -> Captain -> Stage feedback path, but does not pretend to exercise the
physical Captain switch/GPIO edge or its ``binding_fired`` optimistic update.
"""

import argparse
import asyncio
import base64
import errno
import json
import re
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

from websockets.asyncio.client import connect


EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PAGE = "http://192.168.1.91:8080/"
HUB = "ws://192.168.1.91:8081/"
EXPECTED_NAV = ["ACOUSTIC", "CLEAN", "CRUNCH", "HEAVY", "LEAD"]
CONTROL_CLIENT_MARKER = "transition-control"
PASSIVE_RIGS = ("auto", "acoustic", "clean")
WIRE_FRAME_LIMIT = 1000
CDP_COMMAND_TIMEOUT_SECONDS = 10.0
KEMPER_EFFECT_CC = {"X": 22}
SSH_CONNECT_TIMEOUT_SECONDS = 5
SSH_COMMAND_TIMEOUT_SECONDS = 12


SNAPSHOT_EXPRESSION = r"""
(() => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const switches = [...document.querySelectorAll(".stage__switch")].map((el) => ({
    id: clean(el.querySelector(".stage__switch-id")?.textContent),
    label: clean(el.querySelector(".stage__switch-label")?.textContent),
    active: el.classList.contains("stage__switch--active"),
  }));
  const rows = [...document.querySelectorAll(".stage__pedal-row")];
  const lower = rows.length ? rows[rows.length - 1] : null;
  return {
    rig: clean(document.querySelector(".stage__rig-name")?.textContent),
    meta: clean(document.querySelector(".stage__meta")?.textContent),
    switches,
    nav: lower
      ? [...lower.querySelectorAll(".stage__switch-label")].map((el) => clean(el.textContent))
      : [],
  };
})()
"""


TRACE_SCRIPT = r"""
(() => {
  window.__bosunStageObserver?.disconnect();
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const snapshot = () => {
    const switches = [...document.querySelectorAll(".stage__switch")].map((el) => ({
      id: clean(el.querySelector(".stage__switch-id")?.textContent),
      label: clean(el.querySelector(".stage__switch-label")?.textContent),
      active: el.classList.contains("stage__switch--active"),
    }));
    const rows = [...document.querySelectorAll(".stage__pedal-row")];
    const lower = rows.length ? rows[rows.length - 1] : null;
    return {
      rig: clean(document.querySelector(".stage__rig-name")?.textContent),
      meta: clean(document.querySelector(".stage__meta")?.textContent),
      switches,
      nav: lower
        ? [...lower.querySelectorAll(".stage__switch-label")].map((el) => clean(el.textContent))
        : [],
    };
  };
  const trace = { startedAt: performance.now(), records: [], last: "", snapshot };
  trace.capture = (reason) => {
    const state = snapshot();
    const encoded = JSON.stringify(state);
    if (encoded !== trace.last) {
      trace.last = encoded;
      trace.records.push({ ms: performance.now() - trace.startedAt, reason, state });
    }
    return state;
  };
  trace.begin = () => {
    trace.startedAt = performance.now();
    trace.records = [];
    trace.last = "";
    trace.capture("command");
    return trace.startedAt;
  };
  trace.read = () => {
    trace.capture("read");
    return { startedAt: trace.startedAt, records: trace.records, state: snapshot() };
  };
  const root = document.querySelector(".stage");
  if (!root) throw new Error("Stage root is missing");
  const observer = new MutationObserver(() => trace.capture("mutation"));
  observer.observe(root, {
    subtree: true,
    childList: true,
    characterData: true,
    attributes: true,
    attributeFilter: ["class"],
  });
  window.__bosunStageObserver = observer;
  window.__bosunStageTrace = trace;
  return true;
})()
"""


CONTROL_SCRIPT = r"""
(() => {
  const url = __HUB_URL__;
  const old = window.__bosunControl;
  if (old?.ws?.readyState === WebSocket.OPEN) return Promise.resolve(true);
  try { old?.ws?.close(); } catch (_) {}
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url);
    const control = { ws, pending: new Map(), malformed: [] };
    window.__bosunControl = control;
    let opened = false;
    const openTimer = setTimeout(() => {
      if (!opened) {
        try { ws.close(); } catch (_) {}
        reject(new Error("control WebSocket did not open within 5000 ms"));
      }
    }, 5000);
    const rejectPending = (error) => {
      for (const waiter of control.pending.values()) {
        clearTimeout(waiter.timer);
        waiter.reject(error);
      }
      control.pending.clear();
    };
    ws.addEventListener("open", () => {
      opened = true;
      clearTimeout(openTimer);
      resolve(true);
    });
    ws.addEventListener("error", () => {
      if (!opened) {
        clearTimeout(openTimer);
        reject(new Error("control WebSocket failed to open"));
      }
    });
    ws.addEventListener("close", (event) => {
      rejectPending(new Error(`control WebSocket closed (${event.code})`));
    });
    ws.addEventListener("message", (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch (error) {
        control.malformed.push(String(event.data).slice(0, 200));
        rejectPending(new Error(`malformed hub frame: ${String(error)}`));
        return;
      }
      const waiter = control.pending.get(message.id);
      if (!waiter) return;
      control.pending.delete(message.id);
      clearTimeout(waiter.timer);
      if (message.type === "ERROR") {
        waiter.reject(new Error(`${waiter.operation} failed: ${JSON.stringify(message)}`));
      } else if (message.type !== waiter.expectedType) {
        waiter.reject(new Error(
          `${waiter.operation} expected ${waiter.expectedType}, got: ${JSON.stringify(message)}`
        ));
      } else {
        const ackAt = performance.now();
        waiter.resolve({
          ...message,
          sentAt: waiter.sentAt,
          ackAt,
          ackMs: ackAt - waiter.sentAt,
        });
      }
    });
    control.request = (payload, id, expectedType, timeoutMs, beginTrace) => new Promise((ok, fail) => {
      if (ws.readyState !== WebSocket.OPEN) {
        fail(new Error(`control WebSocket is not open (${ws.readyState})`));
        return;
      }
      const operation = String(payload?.type || "request");
      const waiter = {
        resolve: ok, reject: fail, sentAt: 0, timer: null,
        operation, expectedType,
      };
      waiter.timer = setTimeout(() => {
        control.pending.delete(id);
        fail(new Error(`${operation} ${id} timed out after ${timeoutMs} ms`));
      }, timeoutMs);
      control.pending.set(id, waiter);
      // Same renderer clock as MutationObserver: this is the exact start of
      // command-to-DOM latency, not the later ACK receive time.
      waiter.sentAt = beginTrace
        ? window.__bosunStageTrace.begin()
        : performance.now();
      try {
        ws.send(JSON.stringify({ ...payload, id }));
      } catch (error) {
        clearTimeout(waiter.timer);
        control.pending.delete(id);
        fail(error);
      }
    });
    control.switchPatch = (bank, slot, id, timeoutMs) => control.request(
      { type: "SWITCH_PATCH", bank, slot }, id, "ACK", timeoutMs, true
    );
    control.getContext = (id, timeoutMs) => control.request(
      { type: "GET_CONTEXT" }, id, "CONTEXT", timeoutMs, false
    );
    control.ledDump = (id, timeoutMs) => control.request(
      { type: "LED_DUMP" }, id, "LED_DUMP", timeoutMs, false
    );
  });
})()
"""


def positive_int(value):
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def positive_float(value):
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def ssh_target(value):
    """Accept only an OpenSSH destination, never another command-line option."""
    if value.startswith("-") or re.fullmatch(r"[A-Za-z0-9_.@:\[\]-]+", value) is None:
        raise argparse.ArgumentTypeError("must be a plain SSH host or user@host")
    return value


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nav-only", action="store_true",
                        help="check passive bootstrap without switching rigs")
    parser.add_argument("--cycles", type=positive_int,
                        help="ACOUSTIC -> CLEAN cycles per browser (default: 1)")
    parser.add_argument("--cold-runs", type=positive_int,
                        help="fresh Edge profiles to run (default: 1)")
    parser.add_argument("--stress", action="store_true",
                        help="run 20 transition cycles unless --cycles is supplied")
    parser.add_argument("--cold-smoke", nargs="?", const=5, type=positive_int,
                        metavar="RUNS",
                        help="passive NAV/effect smoke using RUNS fresh profiles (default: 5)")
    passive = parser.add_mutually_exclusive_group()
    passive.add_argument(
        "--passive-effects", dest="passive_effects", action="store_true",
        help="verify the current rig's known effect state after NAV (default)",
    )
    passive.add_argument(
        "--no-passive-effects", dest="passive_effects", action="store_false",
        help="skip the passive current-effect assertion",
    )
    parser.set_defaults(passive_effects=True)
    parser.add_argument(
        "--passive-rig", choices=PASSIVE_RIGS, default="auto",
        help=(
            "passive effect oracle: infer ACOUSTIC/CLEAN from the selected "
            "rig, or require one explicitly (default: auto)"
        ),
    )
    parser.add_argument(
        "--max-passive-effects-ms", type=positive_float, default=4_000,
        help="maximum post-NAV wait for current effects (default: 4000)",
    )
    parser.add_argument(
        "--passive-stable-ms", type=positive_float, default=500,
        help="continuous stable window for passive effects (default: 500)",
    )
    parser.add_argument("--max-nav-ms", type=positive_float, default=12_000,
                        help="maximum browser-launch-to-NAV-ready time (default: 12000)")
    parser.add_argument("--max-source-ms", type=positive_float, default=4_000,
                        help="maximum command-to-ACOUSTIC-ready time (default: 4000)")
    parser.add_argument("--max-clean-ms", type=positive_float, default=2_500,
                        help="maximum command-to-CLEAN-effects-ready time (default: 2500)")
    parser.add_argument("--source-soak-seconds", type=positive_float, default=1.0,
                        help="stable ACOUSTIC precondition window (default: 1.0)")
    parser.add_argument("--observe-seconds", type=positive_float, default=3.0,
                        help="post-CLEAN bounce observation window (default: 3.0)")
    parser.add_argument("--switch-timeout-seconds", type=positive_float, default=5.0,
                        help="strict SWITCH_PATCH ACK timeout (default: 5.0)")
    parser.add_argument(
        "--effect-cycles", type=positive_int, default=0, metavar="N",
        help=(
            "after rig transitions, externally toggle CLEAN X/FLANG away "
            "from its observed state and back N times through the real Kemper "
            "(opt-in; default: disabled)"
        ),
    )
    parser.add_argument(
        "--effect-ssh-target", type=ssh_target, default="bosun-hub",
        help="RPi SSH destination used for opt-in MIDI injection (default: bosun-hub)",
    )
    parser.add_argument(
        "--ssh", default="ssh",
        help="OpenSSH client executable for --effect-cycles (default: ssh)",
    )
    parser.add_argument(
        "--max-effect-ms", type=positive_float, default=2_500,
        help="maximum external-MIDI-to-Stage effect convergence (default: 2500)",
    )
    parser.add_argument(
        "--effect-stable-ms", type=positive_float, default=500,
        help="continuous stable window for each effect target (default: 500)",
    )
    parser.add_argument(
        "--effect-observe-seconds", type=positive_float, default=1.0,
        help="additional post-convergence bounce observation (default: 1.0)",
    )
    parser.add_argument("--page", default=PAGE, help="Stage page URL")
    parser.add_argument("--hub", default=HUB, help="hub WebSocket URL")
    parser.add_argument("--edge", default=EDGE, help="Edge/Chromium executable")
    args = parser.parse_args(argv)
    if args.cold_smoke is not None:
        if args.cold_runs is not None:
            parser.error("--cold-smoke and --cold-runs cannot be combined")
        args.nav_only = True
        args.cold_runs = args.cold_smoke
    if args.cold_runs is None:
        args.cold_runs = 1
    if args.cycles is None:
        args.cycles = 20 if args.stress else 1
    if args.passive_stable_ms >= args.max_passive_effects_ms:
        parser.error("--passive-stable-ms must be less than --max-passive-effects-ms")
    if not args.passive_effects and args.passive_rig != "auto":
        parser.error("--passive-rig requires passive effects to be enabled")
    if args.effect_stable_ms >= args.max_effect_ms:
        parser.error("--effect-stable-ms must be less than --max-effect-ms")
    if args.effect_cycles and args.nav_only:
        parser.error("--effect-cycles cannot be combined with --nav-only/--cold-smoke")
    return args


async def cdp_socket(port, page_url):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/json" % port, timeout=1) as response:
                pages = json.load(response)
            candidates = [page for page in pages if page.get("type") == "page"]
            page = next(
                (item for item in candidates if item.get("url", "").startswith(page_url)),
                candidates[0],
            )
            return await connect(page["webSocketDebuggerUrl"], max_size=None)
        except Exception:
            await asyncio.sleep(0.1)
    raise RuntimeError("Edge CDP did not start")


def control_hub_url(hub_url):
    """Tag the test-only socket so CDP diagnostics can distinguish it.

    The Stage application and the command socket both connect to the same hub
    endpoint and therefore receive many identical broadcasts.  A query marker
    is protocol-neutral but gives ``Network.webSocketCreated`` an unambiguous
    URL; the hub ignores the request path/query.
    """
    parsed = urllib.parse.urlsplit(hub_url)
    query = [
        pair for pair in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if pair[0] != "bosun_client"
    ]
    query.append(("bosun_client", CONTROL_CLIENT_MARKER))
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))


_APLAY_PORT_RE = re.compile(r"^\s*(\d+:\d+)\s+(.+?)\s*$")
_KEMPER_PORT_NAME_RE = re.compile(r"^(?:Kemper|Profiler)(?:\s|$)", re.IGNORECASE)


def parse_kemper_aplay_port(listing):
    """Return the one writable Kemper ALSA sequencer port, or fail closed.

    ``aplaymidi -l`` lists only ports that accept outbound MIDI.  Requiring a
    whole-word Kemper/Profiler name avoids ever injecting into the Captain,
    MIDI Through or another attached controller merely because it happens to
    have a similar numeric card id.
    """
    matches = []
    for line in listing.splitlines():
        parsed = _APLAY_PORT_RE.match(line)
        if parsed is None or _KEMPER_PORT_NAME_RE.search(parsed.group(2)) is None:
            continue
        candidate = (parsed.group(1), parsed.group(2).strip())
        if candidate not in matches:
            matches.append(candidate)
    if len(matches) != 1:
        rendered = ", ".join("%s (%s)" % item for item in matches) or "none"
        raise RuntimeError(
            "expected exactly one writable Kemper/Profiler ALSA port; found "
            "%d: %s" % (len(matches), rendered)
        )
    return matches[0][0]


def midi_cc_smf(channel, control, value):
    """Build the smallest format-0 MIDI file containing one CC event."""
    if not 1 <= channel <= 16:
        raise ValueError("MIDI channel must be in 1..16")
    if not 0 <= control <= 127 or not 0 <= value <= 127:
        raise ValueError("MIDI control and value must be in 0..127")
    track = bytes((0, 0xB0 + channel - 1, control, value, 0, 0xFF, 0x2F, 0))
    return (
        b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x00\x60"
        + b"MTrk" + len(track).to_bytes(4, "big") + track
    )


class KemperMidiInjector:
    """Inject one CC through the RPi ALSA sequencer without touching routes."""

    def __init__(self, ssh_executable, target, runner=subprocess.run):
        self.ssh_executable = ssh_executable
        self.target = ssh_target(target)
        self.runner = runner
        self.port = None

    def _run(self, remote_command):
        command = [
            self.ssh_executable,
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=%d" % SSH_CONNECT_TIMEOUT_SECONDS,
            self.target,
            remote_command,
        ]
        try:
            result = self.runner(
                command, capture_output=True, text=True,
                timeout=SSH_COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "RPi MIDI command timed out after %d seconds" %
                SSH_COMMAND_TIMEOUT_SECONDS
            ) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "no diagnostic").strip()[:500]
            raise RuntimeError(
                "RPi MIDI command failed (exit %d): %s" %
                (result.returncode, detail)
            )
        return result.stdout

    def discover(self):
        if self.port is None:
            self.port = parse_kemper_aplay_port(
                self._run("LC_ALL=C aplaymidi -l")
            )
        return self.port

    def send_effect(self, block, active):
        cc = KEMPER_EFFECT_CC.get(block)
        if cc is None:
            raise ValueError("unsupported effect block for real test: %s" % block)
        port = self.discover()
        payload = base64.b64encode(
            midi_cc_smf(1, cc, 127 if active else 0)
        ).decode("ascii")
        # The only interpolated fields are a strict digits:digits ALSA port and
        # base64 generated locally.  mktemp + trap leaves no remote artefact,
        # including when aplaymidi rejects the device or is interrupted.
        remote = (
            "set -eu; effect_file=$(mktemp /tmp/bosun-effect-XXXXXX.mid); "
            "trap 'rm -f \"$effect_file\"' EXIT; "
            "printf '%%s' '%s' | base64 -d > \"$effect_file\"; "
            # aplaymidi otherwise waits two seconds after this one-event SMF
            # has ended.  That delay is for sustaining notes and is not part
            # of the Kemper/Stage convergence time this harness measures.
            "aplaymidi -d 0 -p %s \"$effect_file\""
        ) % (payload, port)
        self._run(remote)


def summarize_wire_payload(payload):
    """Return compact JSON evidence, keeping the fields useful for failures."""
    try:
        message = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return {"raw": str(payload)[:500], "malformed": True}
    if not isinstance(message, dict):
        return {"json": message}

    kind = message.get("type")
    if kind == "CONTEXT":
        return {
            key: message[key]
            for key in ("type", "id", "partial", "context")
            if key in message
        }
    if kind == "PATCH":
        patch = message.get("patch")
        summary = {
            key: message[key]
            for key in ("type", "id", "bank", "slot", "profile")
            if key in message
        }
        if isinstance(patch, dict):
            bindings = []
            for binding in patch.get("bindings", []):
                if not isinstance(binding, dict):
                    continue
                compact = {
                    key: binding.get(key)
                    for key in ("switch", "label", "mode")
                    if key in binding
                }
                # The real-transition LED check needs the two configured
                # colours, but never the much larger action/message graph.
                # Keeping just on/off makes the CDP trace useful without
                # turning every failure dump into a full patch export.
                led = binding.get("led")
                if isinstance(led, dict):
                    compact_led = {
                        key: led.get(key) for key in ("on", "off") if key in led
                    }
                    if compact_led:
                        compact["led"] = compact_led
                bindings.append(compact)
            summary["patch"] = {
                "name": patch.get("name"),
                "bindings": bindings,
            }
        return summary
    if kind == "LED_DUMP":
        # The correlated return value still contains every pixel. CDP keeps a
        # compact diagnostic copy so a transition failure does not print a
        # page of RGB triples twice (Stage + control sockets).
        pixels = message.get("pixels")
        summary = {
            key: message[key] for key in ("type", "id", "current", "switch_indices")
            if key in message
        }
        if isinstance(pixels, list):
            summary["pixel_count"] = len(pixels)
        return summary
    if kind == "PATCH_LIST":
        patches = message.get("patches")
        return {
            "type": kind,
            "id": message.get("id"),
            "patches": [
                {
                    key: patch.get(key)
                    for key in ("bank", "slot", "name")
                    if key in patch
                }
                for patch in patches or []
                if isinstance(patch, dict)
            ],
        }
    if kind in ("GLOBAL", "DEVICE_INFO"):
        device = message.get("device")
        summary = {
            key: message[key]
            for key in ("type", "id", "fw", "current")
            if key in message
        }
        if isinstance(device, dict):
            summary["preset_navigation"] = device.get("preset_navigation")
        return summary
    return message


def deduplicate_wire_frames(frames, window_seconds=0.050):
    """Merge only cross-socket copies of the same received hub broadcast.

    Repeated frames on one socket remain separate: they may be real upstream
    duplication and must stay visible.  Sent frames also remain separate.  This
    prevents the Stage + control subscriptions from making one broadcast look
    like an on/off bounce while preserving genuine repeats.
    """
    output = []
    candidates = {}
    for original in frames:
        frame = dict(original)
        socket_name = frame.pop("socket", "unknown")
        frame["sockets"] = [socket_name]
        frame["copies"] = 1
        signature = (
            frame.get("direction"),
            json.dumps(frame.get("message"), sort_keys=True, separators=(",", ":")),
        )
        timestamp = frame.get("timestamp")
        candidate = candidates.get(signature)
        can_merge = (
            frame.get("direction") == "RECV"
            and candidate is not None
            and socket_name not in candidate["sockets"]
            and isinstance(timestamp, (int, float))
            and isinstance(candidate.get("timestamp"), (int, float))
            and 0 <= timestamp - candidate["timestamp"] <= window_seconds
        )
        if can_merge:
            candidate["sockets"].append(socket_name)
            candidate["copies"] += 1
            continue
        output.append(frame)
        candidates[signature] = frame
    return output


def format_wire_frames(frames, limit=60):
    deduplicated = deduplicate_wire_frames(frames)
    selected = deduplicated[-limit:]
    numeric = [
        frame.get("timestamp") for frame in selected
        if isinstance(frame.get("timestamp"), (int, float))
    ]
    origin = numeric[0] if numeric else 0
    lines = []
    for frame in selected:
        timestamp = frame.get("timestamp")
        elapsed_ms = (timestamp - origin) * 1000 if isinstance(timestamp, (int, float)) else 0
        sockets = "+".join(frame.get("sockets", ["unknown"]))
        copies = frame.get("copies", 1)
        if copies > 1:
            sockets += " x%d" % copies
        payload = json.dumps(
            frame.get("message"), sort_keys=True, separators=(",", ":"),
        )
        lines.append("WIRE %+8.1fms %-4s %-18s %s" % (
            elapsed_ms, frame.get("direction", "?"), sockets, payload,
        ))
    return lines


class CdpSession:
    def __init__(self, websocket):
        self.websocket = websocket
        self.sequence = 0
        self.frames = []
        self.frame_count = 0
        self.sockets = {}
        self._stage_socket_count = 0

    def _socket_label(self, request_id):
        info = self.sockets.get(request_id)
        if info is not None:
            return info["label"]
        self._stage_socket_count += 1
        label = "stage" if self._stage_socket_count == 1 else "stage-%d" % self._stage_socket_count
        self.sockets[request_id] = {"label": label, "url": "<created before Network.enable>"}
        return label

    def observe(self, message):
        """Collect WebSocket lifecycle/frame events from one CDP message."""
        method = message.get("method")
        params = message.get("params", {})
        if method == "Network.webSocketCreated":
            request_id = params.get("requestId")
            url = params.get("url", "")
            marker = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get(
                "bosun_client", []
            )
            if CONTROL_CLIENT_MARKER in marker:
                label = "control"
            else:
                self._stage_socket_count += 1
                label = (
                    "stage" if self._stage_socket_count == 1
                    else "stage-%d" % self._stage_socket_count
                )
            self.sockets[request_id] = {"label": label, "url": url}
            return

        directions = {
            "Network.webSocketFrameSent": "SEND",
            "Network.webSocketFrameReceived": "RECV",
        }
        direction = directions.get(method)
        if direction is None:
            return
        response = params.get("response", {})
        payload = response.get("payloadData", "")
        frame = {
            "timestamp": params.get("timestamp"),
            "direction": direction,
            "socket": self._socket_label(params.get("requestId")),
            "request_id": params.get("requestId"),
            "message": summarize_wire_payload(payload),
        }
        self.frames.append(frame)
        self.frame_count += 1
        if len(self.frames) > WIRE_FRAME_LIMIT:
            del self.frames[:len(self.frames) - WIRE_FRAME_LIMIT]

    def frame_cursor(self):
        """Return a stable cursor even when the diagnostic ring is full."""
        return self.frame_count

    def frames_since(self, cursor):
        """Return retained frames after an absolute capture cursor.

        A list index is not a valid cursor once the bounded frame list starts
        discarding its oldest entries. Failing explicitly if one transition
        itself exceeds the whole ring is safer than silently inspecting an
        unrelated suffix and reporting that its PATCH was missing.
        """
        if isinstance(cursor, bool) or not isinstance(cursor, int):
            raise ValueError("wire cursor must be an integer")
        first_retained = self.frame_count - len(self.frames)
        if cursor < first_retained:
            raise RuntimeError(
                "wire evidence after cursor was truncated (%d frames retained)" %
                len(self.frames)
            )
        if cursor > self.frame_count:
            raise ValueError("wire cursor is newer than the capture")
        return self.frames[cursor - first_retained:]

    async def command(self, method, params=None):
        self.sequence += 1
        ident = self.sequence
        request = {"id": ident, "method": method}
        if params is not None:
            request["params"] = params
        deadline = time.monotonic() + CDP_COMMAND_TIMEOUT_SECONDS
        try:
            await asyncio.wait_for(
                self.websocket.send(json.dumps(request)),
                timeout=max(0, deadline - time.monotonic()),
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError("CDP %s send timed out" % method) from exc
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("CDP %s response timed out" % method)
            try:
                payload = await asyncio.wait_for(
                    self.websocket.recv(), timeout=remaining,
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError("CDP %s response timed out" % method) from exc
            message = json.loads(payload)
            self.observe(message)
            if message.get("id") == ident:
                if "error" in message:
                    raise RuntimeError("CDP %s failed: %s" % (method, message["error"]))
                return message.get("result", {})

    async def evaluate(self, expression, await_promise=False):
        result = await self.command("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        })
        if result.get("exceptionDetails"):
            remote = result.get("result", {})
            description = remote.get("description") or result["exceptionDetails"].get("text")
            raise RuntimeError("browser evaluation failed: %s" % description)
        return result.get("result", {}).get("value")

    async def close(self):
        await self.websocket.close()


def by_id(state):
    return {item.get("id"): item for item in state.get("switches", [])}


def named(state, expected):
    return expected.casefold() in state.get("rig", "").casefold()


def at_location(state, bank, slot):
    # This exact oracle also guards the requested title/bank/rig separators:
    # the normalized DOM text must be "· Bn · Rn", not "BANK n RIG n" nor
    # a header with only one separator.
    return state.get("meta") == "· B%d · R%d" % (bank, slot)


def source_identity(state):
    nav = by_id(state).get("A", {})
    return (
        named(state, "ACOUSTIC")
        and at_location(state, 1, 1)
        and nav.get("label") == "ACOUSTIC"
        and nav.get("active") is True
    )


def source_ready(state):
    harm = by_id(state).get("4", {})
    return (
        source_identity(state)
        and harm.get("label") == "HARM"
        and harm.get("active") is True
    )


def target_identity(state):
    nav = by_id(state).get("B", {})
    return (
        named(state, "CLEAN")
        and at_location(state, 1, 2)
        and nav.get("label") == "CLEAN"
        and nav.get("active") is True
    )


def target_ready(state):
    switches = by_id(state)
    flang = switches.get("3", {})
    boost = switches.get("UP", {})
    return (
        target_identity(state)
        and flang.get("label") == "FLANG"
        and flang.get("active") is True
        and boost.get("label") == "BOOST"
        and boost.get("active") is False
    )


def passive_effect_status(state, expected_rig="auto"):
    """Return ``(ready, resolved_rig, diagnostic)`` for passive bootstrap.

    Only ACOUSTIC and CLEAN have a known real-rig oracle in this regression
    test.  ``auto`` intentionally fails closed for another selected rig rather
    than claiming that arbitrary boolean DOM classes prove effect correctness.
    ``--no-passive-effects`` is the explicit escape hatch for other setups.
    """
    requested = expected_rig.casefold()
    if requested not in PASSIVE_RIGS:
        raise ValueError("unknown passive rig oracle: %s" % expected_rig)

    if requested == "auto":
        if source_identity(state):
            resolved = "acoustic"
        elif target_identity(state):
            resolved = "clean"
        else:
            return (
                False,
                None,
                "selected rig has no known ACOUSTIC/CLEAN effect oracle",
            )
    else:
        resolved = requested

    if resolved == "acoustic":
        ready = source_ready(state)
        expected = "ACOUSTIC with switch 4 HARM on"
    else:
        ready = target_ready(state)
        expected = "CLEAN with switch 3 FLANG on and UP BOOST off"
    return ready, resolved, expected


def compact_state(state):
    switches = by_id(state)
    top = []
    for ident in ("1", "2", "3", "4", "UP"):
        item = switches.get(ident, {})
        top.append("%s:%s:%s" % (
            ident, item.get("label", "?"),
            "ON" if item.get("active") else "off",
        ))
    selected = [
        item.get("label") for item in state.get("switches", [])[5:]
        if item.get("active")
    ]
    return "rig=%r meta=%r top=[%s] nav_on=%s" % (
        state.get("rig"), state.get("meta"), ", ".join(top), selected,
    )


async def snapshot(cdp):
    state = await cdp.evaluate(SNAPSHOT_EXPRESSION)
    if not isinstance(state, dict):
        raise RuntimeError("Stage snapshot was not an object: %r" % (state,))
    return state


async def read_trace(cdp):
    trace = await cdp.evaluate("window.__bosunStageTrace.read()")
    if not isinstance(trace, dict):
        raise RuntimeError("Stage trace was not an object: %r" % (trace,))
    return trace


async def snapshot_before_deadline(cdp, deadline):
    """Take one snapshot without allowing its CDP call to overrun a caller's deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise asyncio.TimeoutError
    return await asyncio.wait_for(snapshot(cdp), timeout=remaining)


async def wait_for_state(cdp, predicate, timeout_seconds, description):
    deadline = time.monotonic() + timeout_seconds
    last = {}
    while time.monotonic() < deadline:
        try:
            last = await snapshot_before_deadline(cdp, deadline)
        except asyncio.TimeoutError:
            break
        if time.monotonic() > deadline:
            break
        if predicate(last):
            return last
        await asyncio.sleep(0.025)
    raise AssertionError("%s did not appear within %.0f ms; last %s" % (
        description, timeout_seconds * 1000, compact_state(last),
    ))


async def wait_for_stable_state(cdp, predicate, timeout_seconds,
                                stable_seconds, description):
    """Require a state to remain true, so a pre-command DOM snapshot cannot
    make a same-rig reload pass before its reset/repaint has happened."""
    deadline = time.monotonic() + timeout_seconds
    stable_since = None
    last = {}
    while time.monotonic() < deadline:
        try:
            last = await snapshot_before_deadline(cdp, deadline)
        except asyncio.TimeoutError:
            break
        now = time.monotonic()
        if now > deadline:
            break
        if predicate(last):
            if stable_since is None:
                stable_since = now
            if now - stable_since >= stable_seconds:
                return last
        else:
            stable_since = None
        await asyncio.sleep(0.025)
    raise AssertionError("%s was not stable for %.0f ms within %.0f ms; last %s" % (
        description, stable_seconds * 1000, timeout_seconds * 1000,
        compact_state(last),
    ))


async def passive_nav(cdp, browser_started, max_nav_ms):
    deadline = browser_started + max_nav_ms / 1000
    last = {}
    last_error = None
    while time.monotonic() < deadline:
        try:
            last = await snapshot_before_deadline(cdp, deadline)
            last_error = None
        except asyncio.TimeoutError:
            break
        except RuntimeError as exc:
            # A Runtime.evaluate can briefly lose its execution context while
            # Page.navigate commits.  Keep the launch-to-ready deadline strict,
            # but don't mistake that expected navigation race for a Stage bug.
            last_error = str(exc)
            await asyncio.sleep(0.1)
            continue
        if last.get("nav") == EXPECTED_NAV:
            elapsed_ms = (time.monotonic() - browser_started) * 1000
            print("NAV PASS %.1fms %s" % (elapsed_ms, json.dumps(last["nav"])), flush=True)
            return last, elapsed_ms
        await asyncio.sleep(0.1)
    suffix = " (last browser error: %s)" % last_error if last_error else ""
    raise AssertionError("passive NAV bootstrap exceeded %.0f ms: %r%s" % (
        max_nav_ms, last.get("nav"), suffix,
    ))


async def passive_effects(cdp, browser_started, expected_rig,
                          timeout_ms, stable_ms):
    """Require the already-selected rig's known effects to settle passively."""
    deadline = time.monotonic() + timeout_ms / 1000
    stable_since = None
    stable_rig = None
    last = {}
    diagnostic = "no snapshot"
    while time.monotonic() < deadline:
        try:
            last = await snapshot_before_deadline(cdp, deadline)
        except asyncio.TimeoutError:
            break
        ready, resolved, diagnostic = passive_effect_status(last, expected_rig)
        now = time.monotonic()
        if now > deadline:
            break
        if ready:
            if stable_since is None or stable_rig != resolved:
                stable_since = now
                stable_rig = resolved
            if (now - stable_since) * 1000 >= stable_ms:
                elapsed_ms = (now - browser_started) * 1000
                print(
                    "EFFECTS PASS %.1fms oracle=%s stable=%.0fms %s" % (
                        elapsed_ms, resolved.upper(), stable_ms,
                        compact_state(last),
                    ),
                    flush=True,
                )
                return last, elapsed_ms, resolved
        else:
            stable_since = None
            stable_rig = None
        await asyncio.sleep(0.025)
    raise AssertionError(
        "passive effects did not become stable for %.0f ms within %.0f ms "
        "(%s); last %s" % (
            stable_ms, timeout_ms, diagnostic, compact_state(last),
        )
    )


async def send_switch(cdp, bank, slot, ident, timeout_seconds):
    expression = "window.__bosunControl.switchPatch(%d,%d,%s,%.0f)" % (
        bank, slot, json.dumps(ident), timeout_seconds * 1000,
    )
    response = await cdp.evaluate(expression, await_promise=True)
    if not isinstance(response, dict) or response.get("type") != "ACK":
        raise AssertionError("strict ACK missing for %s: %r" % (ident, response))
    return response


async def send_control_request(cdp, method, ident, expected_type,
                               timeout_seconds=8.0):
    """Run one correlated diagnostic request through the browser-owned WS."""
    if method not in ("getContext", "ledDump"):
        raise ValueError("unsupported control method: %s" % method)
    expression = "window.__bosunControl.%s(%s,%.0f)" % (
        method, json.dumps(ident), timeout_seconds * 1000,
    )
    response = await cdp.evaluate(expression, await_promise=True)
    if not isinstance(response, dict) or response.get("type") != expected_type:
        raise AssertionError(
            "strict %s missing for %s: %r" % (expected_type, ident, response)
        )
    return response


def clean_effect_state(state, active):
    switches = by_id(state)
    flang = switches.get("3", {})
    boost = switches.get("UP", {})
    return (
        target_identity(state)
        and flang.get("label") == "FLANG"
        and flang.get("active") is active
        and boost.get("label") == "BOOST"
        and boost.get("active") is False
    )


def switch_indices(led_dump, switch_name):
    """Validate and return one switch's physical LED indices."""
    mapping = led_dump.get("switch_indices")
    pixels = led_dump.get("pixels")
    if not isinstance(mapping, dict) or not isinstance(pixels, list):
        raise AssertionError("LED_DUMP is missing pixels/switch_indices")
    wanted = switch_name.casefold()
    key = next(
        (candidate for candidate in mapping
         if isinstance(candidate, str) and candidate.casefold() == wanted),
        None,
    )
    if key is None or not isinstance(mapping[key], list) or not mapping[key]:
        raise AssertionError("LED_DUMP has no mapping for switch %s" % switch_name)
    result = []
    for index in mapping[key]:
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(pixels):
            raise AssertionError("LED_DUMP has invalid %s pixel index: %r" % (switch_name, index))
        if index in result:
            raise AssertionError("LED_DUMP repeats %s pixel index: %r" % (switch_name, index))
        result.append(index)
    return tuple(result)


def switch_pixels(led_dump, switch_name):
    """Validate LED_DUMP's shape and return this switch's exact RGB tuples."""
    pixels = led_dump.get("pixels")
    result = []
    for index in switch_indices(led_dump, switch_name):
        rgb = pixels[index]
        if (not isinstance(rgb, list) or len(rgb) != 3
                or any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 255
                       for v in rgb)):
            raise AssertionError("LED_DUMP has invalid RGB at index %d: %r" % (index, rgb))
        result.append(tuple(rgb))
    return tuple(result)


def _binding_for_switch(patch, switch_name):
    if not isinstance(patch, dict) or not isinstance(patch.get("bindings"), list):
        raise AssertionError("CLEAN PATCH is unavailable for LED verification")
    wanted = switch_name.casefold()
    matches = [
        binding for binding in patch["bindings"]
        if isinstance(binding, dict)
        and isinstance(binding.get("switch"), str)
        and binding["switch"].casefold() == wanted
    ]
    if len(matches) != 1:
        raise AssertionError(
            "CLEAN PATCH needs exactly one %s binding for LED verification" % switch_name
        )
    binding = matches[0]
    if binding.get("mode") != "latched":
        raise AssertionError(
            "CLEAN %s binding is not latched; LED state cannot be proven" % switch_name
        )
    return binding


def _parse_led_rgb(value, description):
    if not isinstance(value, str) or re.fullmatch(r"#[0-9a-fA-F]{6}", value) is None:
        raise AssertionError("%s LED colour is unavailable or invalid: %r" % (description, value))
    return tuple(int(value[offset:offset + 2], 16) for offset in (1, 3, 5))


def _scaled_led(rgb, brightness):
    # Exact integer equivalent of firmware leds.py:Leds.scale(), where the
    # configured 0..255 brightness is converted to brightness / 255.
    return tuple((channel * brightness + 127) // 255 for channel in rgb)


def _binding_led_colours(binding, description):
    led = binding.get("led")
    if not isinstance(led, dict):
        raise AssertionError("%s LED config is unavailable; refusing an unproven pass" % description)
    on = _parse_led_rgb(led.get("on"), description + " on")
    if on == (0, 0, 0):
        raise AssertionError("%s on colour is black; physical ON cannot be proven" % description)
    raw_off = led.get("off")
    if raw_off is None:
        return on, None
    off = _parse_led_rgb(raw_off, description + " off")
    # Firmware treats missing and explicit black identically: dim the on
    # colour using the global 0..255 dim setting.
    return on, off if off != (0, 0, 0) else None


def _off_led(on, explicit_off, dim):
    if explicit_off is not None:
        return explicit_off
    return tuple((channel * dim + 127) // 255 for channel in on)


def _uniform_ring(led_dump, switch_name):
    values = switch_pixels(led_dump, switch_name)
    if len(set(values)) != 1:
        raise AssertionError(
            "Captain %s LED ring is internally inconsistent: %r" % (switch_name, values)
        )
    return values[0], values


def validate_clean_led_frame(led_dump, patch):
    """Prove switch 3 is ON and UP is OFF from the actual framebuffer.

    LED_DUMP has no boolean latch field and different bindings may use very
    different colours, so comparing raw brightness across switches can yield a
    false pass.  Instead, retain the PATCH's compact LED config in the CDP
    trace and enumerate the firmware's exact integer brightness/dim transform.
    Ambiguous configurations (for example dim=255, where OFF equals ON) fail
    explicitly rather than claiming that the pixels prove a state.
    """
    flang_binding = _binding_for_switch(patch, "3")
    boost_binding = _binding_for_switch(patch, "UP")
    flang_on, flang_explicit_off = _binding_led_colours(flang_binding, "FLANG")
    boost_on, boost_explicit_off = _binding_led_colours(boost_binding, "BOOST")

    flang_indices = switch_indices(led_dump, "3")
    boost_indices = switch_indices(led_dump, "UP")
    if set(flang_indices).intersection(boost_indices):
        raise AssertionError("LED_DUMP maps FLANG and BOOST to overlapping pixels")
    flang_pixel, flang_ring = _uniform_ring(led_dump, "3")
    boost_pixel, boost_ring = _uniform_ring(led_dump, "UP")

    # Infer every global (brightness, dim) pair compatible with the expected
    # ON/OFF frame. This avoids needing heavyweight GET_GLOBAL on the Captain.
    needs_dim = flang_explicit_off is None or boost_explicit_off is None
    candidates = []
    for brightness in range(256):
        if _scaled_led(flang_on, brightness) != flang_pixel:
            continue
        for dim in (range(256) if needs_dim else (0,)):
            boost_off = _off_led(boost_on, boost_explicit_off, dim)
            if _scaled_led(boost_off, brightness) == boost_pixel:
                candidates.append((brightness, dim))
    if not candidates:
        raise AssertionError(
            "Captain LEDs do not encode FLANG on / BOOST off for the captured PATCH: %r / %r" %
            (flang_ring, boost_ring)
        )

    # A framebuffer that can equally represent either wrong state proves
    # nothing. Be deliberately strict: configuration visibility is incomplete
    # here, so every compatible pair must distinguish both target states.
    for brightness, dim in candidates:
        flang_off = _off_led(flang_on, flang_explicit_off, dim)
        if _scaled_led(flang_off, brightness) == flang_pixel:
            raise AssertionError("Captain FLANG ON/OFF pixels are ambiguous; refusing a false pass")
        if _scaled_led(boost_on, brightness) == boost_pixel:
            raise AssertionError("Captain BOOST ON/OFF pixels are ambiguous; refusing a false pass")

    return {
        "flang": flang_ring,
        "boost": boost_ring,
        "brightness": tuple(sorted(set(item[0] for item in candidates))),
        "dim": tuple(sorted(set(item[1] for item in candidates))) if needs_dim else (),
    }


def validate_effect_surfaces(state, context_reply, led_dump, active):
    """Require Stage DOM, Kemper-derived context and Captain LEDs to agree."""
    if not clean_effect_state(state, active):
        raise AssertionError(
            "Stage did not show CLEAN FLANG %s: %s" %
            ("on" if active else "off", compact_state(state))
        )
    context = context_reply.get("context")
    if not isinstance(context, dict):
        raise AssertionError("CONTEXT response has no context object")
    if context_reply.get("partial") is True:
        raise AssertionError("CONTEXT response is partial, not authoritative")
    if (context.get("bank"), context.get("slot")) != (1, 2):
        raise AssertionError("CONTEXT is not for CLEAN B1/R2: %r" % context)
    expected = "on" if active else "off"
    if context.get("kemper_block_X") != expected:
        raise AssertionError(
            "Captain context X=%r, expected %s" %
            (context.get("kemper_block_X"), expected)
        )
    current = led_dump.get("current")
    if not isinstance(current, dict) or (current.get("bank"), current.get("slot")) != (1, 2):
        raise AssertionError("LED_DUMP is not for CLEAN B1/R2: %r" % current)
    return switch_pixels(led_dump, "3")


def latest_patch_from_wire(frames, bank, slot):
    """Return the latest compact PATCH observed by CDP for one location."""
    for frame in reversed(frames):
        message = frame.get("message", {})
        if (isinstance(message, dict) and message.get("type") == "PATCH"
                and message.get("bank") == bank and message.get("slot") == slot
                and isinstance(message.get("patch"), dict)):
            return message["patch"]
    raise AssertionError(
        "CLEAN PATCH was not captured on the wire; Captain LED state cannot be verified"
    )


def validate_clean_transition_surfaces(state, context_reply, led_dump, patch):
    """Require authoritative CLEAN state on DOM, context and both LED rings."""
    validate_effect_surfaces(state, context_reply, led_dump, True)
    if context_reply.get("partial") is True:
        raise AssertionError("correlated CLEAN CONTEXT was partial, not authoritative")
    context = context_reply.get("context")
    if context.get("kemper_block_Reverb") != "off":
        raise AssertionError(
            "Captain context Reverb=%r, expected authoritative off (unknown is not off)" %
            context.get("kemper_block_Reverb")
        )
    return validate_clean_led_frame(led_dump, patch)


async def read_clean_transition_surfaces(cdp, prefix, patch, timeout_seconds):
    """Read correlated CONTEXT + LED_DUMP under one strict total deadline."""
    deadline = time.monotonic() + timeout_seconds

    async def bounded_request(method, suffix, expected_type):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("CLEAN surface verification exceeded %.0f ms" %
                               (timeout_seconds * 1000))
        try:
            return await asyncio.wait_for(
                send_control_request(
                    cdp, method, prefix + suffix, expected_type,
                    timeout_seconds=remaining,
                ),
                timeout=remaining,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                "CLEAN %s verification exceeded %.0f ms" %
                (expected_type, timeout_seconds * 1000)
            ) from exc

    context_reply = await bounded_request("getContext", "-context", "CONTEXT")
    led_dump = await bounded_request("ledDump", "-leds", "LED_DUMP")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("CLEAN DOM verification exceeded %.0f ms" %
                           (timeout_seconds * 1000))
    try:
        state = await asyncio.wait_for(snapshot(cdp), timeout=remaining)
    except asyncio.TimeoutError as exc:
        raise TimeoutError("CLEAN DOM verification exceeded %.0f ms" %
                           (timeout_seconds * 1000)) from exc
    led_info = validate_clean_transition_surfaces(
        state, context_reply, led_dump, patch,
    )
    return state, context_reply, led_dump, led_info


def validate_led_levels(original_pixels, inverse_pixels, original_active):
    """Prove that the Captain framebuffer changed in the expected direction."""
    if inverse_pixels == original_pixels:
        raise AssertionError("Captain switch 3 LEDs did not change with Kemper X")
    original_energy = sum(sum(rgb) for rgb in original_pixels)
    inverse_energy = sum(sum(rgb) for rgb in inverse_pixels)
    if original_active and not original_energy > inverse_energy:
        raise AssertionError(
            "Captain X-on LEDs are not brighter than X-off: %d <= %d" %
            (original_energy, inverse_energy)
        )
    if not original_active and not inverse_energy > original_energy:
        raise AssertionError(
            "Captain X-on LEDs are not brighter than X-off: %d <= %d" %
            (inverse_energy, original_energy)
        )


async def read_effect_surfaces(cdp, prefix, active):
    context_reply = await send_control_request(
        cdp, "getContext", prefix + "-context", "CONTEXT",
    )
    led_dump = await send_control_request(
        cdp, "ledDump", prefix + "-leds", "LED_DUMP",
    )
    state = await snapshot(cdp)
    pixels = validate_effect_surfaces(state, context_reply, led_dump, active)
    return state, context_reply, led_dump, pixels


async def restore_effect_state(cdp, args, injector, prefix, active,
                               expected_pixels):
    """Restore X and prove all three observable surfaces remain converged.

    A successful ``aplaymidi`` process only proves that ALSA accepted the CC;
    it does not prove that the Kemper applied it.  Keep retrying the read-only
    correlated checks for one bounded stability window, but send the restore
    command exactly once so the recovery path cannot itself create a bounce.
    """
    await send_effect_midi(injector, "X", active)
    timeout_seconds = args.max_effect_ms / 1000
    stable_seconds = args.effect_stable_ms / 1000
    deadline = time.monotonic() + timeout_seconds
    stable_since = None
    last_error = None
    attempt = 0

    while time.monotonic() < deadline:
        attempt += 1
        remaining = deadline - time.monotonic()
        try:
            surfaces = await asyncio.wait_for(
                read_effect_surfaces(
                    cdp, "%s-confirm%d" % (prefix, attempt), active,
                ),
                timeout=remaining,
            )
            if surfaces[3] != expected_pixels:
                raise AssertionError(
                    "Captain switch 3 restore differs from observed baseline: "
                    "%r != %r" % (surfaces[3], expected_pixels)
                )
        except Exception as exc:
            stable_since = None
            last_error = exc
        else:
            now = time.monotonic()
            if stable_since is None:
                stable_since = now
            if now - stable_since >= stable_seconds:
                return surfaces

        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(min(0.025, remaining))

    detail = (
        "%s: %s" % (type(last_error).__name__, last_error)
        if last_error is not None
        else "three-surface state was not stable for %.0f ms" %
        args.effect_stable_ms
    )
    raise RuntimeError(
        "restore was not authoritative within %.0f ms (%s)" %
        (args.max_effect_ms, detail)
    )


def first_record(records, predicate):
    return next((record for record in records if predicate(record.get("state", {}))), None)


def clean_transition_milestones(frames, trace, command_id,
                                context_reply=None, led_dump=None):
    """Build one compact command-relative timeline across wire and DOM."""
    origin = None
    for frame in frames:
        message = frame.get("message", {})
        if (frame.get("direction") == "SEND" and isinstance(message, dict)
                and message.get("type") == "SWITCH_PATCH"
                and message.get("id") == command_id):
            origin = frame.get("timestamp")
            break

    def wire_ms(predicate):
        if not isinstance(origin, (int, float)):
            return None
        for frame in frames:
            timestamp = frame.get("timestamp")
            message = frame.get("message", {})
            if (isinstance(timestamp, (int, float)) and timestamp >= origin
                    and isinstance(message, dict) and predicate(frame, message)):
                return (timestamp - origin) * 1000
        return None

    records = trace.get("records", []) if isinstance(trace, dict) else []

    def dom_ms(predicate):
        match = first_record(records, predicate)
        return match.get("ms") if match is not None else None

    def renderer_reply_ms(reply):
        if not isinstance(reply, dict) or not isinstance(trace, dict):
            return None
        ack_at = reply.get("ackAt")
        started_at = trace.get("startedAt")
        if isinstance(ack_at, (int, float)) and isinstance(started_at, (int, float)):
            return ack_at - started_at
        return None

    def clean_bindings(state):
        switches = by_id(state)
        return (switches.get("3", {}).get("label") == "FLANG"
                and switches.get("UP", {}).get("label") == "BOOST")

    def clean_x_on(state):
        return clean_bindings(state) and by_id(state).get("3", {}).get("active") is True

    return {
        "ack": wire_ms(lambda _f, m: m.get("type") == "ACK"
                       and m.get("id") == command_id),
        "event": wire_ms(lambda _f, m: m.get("type") == "EVENT"
                         and m.get("event") == "patch_switched"
                         and m.get("bank") == 1 and m.get("slot") == 2),
        "patch": wire_ms(lambda _f, m: m.get("type") == "PATCH"
                         and m.get("bank") == 1 and m.get("slot") == 2),
        "reverb_off": wire_ms(lambda _f, m: m.get("type") == "CONTEXT"
                              and isinstance(m.get("context"), dict)
                              and m["context"].get("kemper_block_Reverb") == "off"),
        "x_on": wire_ms(lambda _f, m: m.get("type") == "CONTEXT"
                        and isinstance(m.get("context"), dict)
                        and m["context"].get("kemper_block_X") == "on"),
        "dom_identity": dom_ms(target_identity),
        "dom_bindings": dom_ms(clean_bindings),
        "dom_x_on": dom_ms(clean_x_on),
        "dom_ready": dom_ms(target_ready),
        "context_check": renderer_reply_ms(context_reply),
        "led_check": renderer_reply_ms(led_dump),
    }


def format_clean_milestones(cycle, milestones, led_info=None):
    def rendered(name):
        value = milestones.get(name)
        return "%s=%s" % (
            name,
            "-" if not isinstance(value, (int, float)) else "%.1fms" % value,
        )

    names = (
        "ack", "event", "patch", "reverb_off", "x_on",
        "dom_identity", "dom_bindings", "dom_x_on", "dom_ready",
        "context_check", "led_check",
    )
    suffix = ""
    if isinstance(led_info, dict):
        flang = sum(sum(rgb) for rgb in led_info.get("flang", ()))
        boost = sum(sum(rgb) for rgb in led_info.get("boost", ()))
        suffix = " led_energy=%d/%d" % (flang, boost)
    return "cycle %d CLEAN milestones %s%s" % (
        cycle, " ".join(rendered(name) for name in names), suffix,
    )


def dump_trace(label, records):
    print(label, flush=True)
    for record in records:
        print("  %7.1fms %-8s %s" % (
            record.get("ms", -1), record.get("reason", "?"),
            compact_state(record.get("state", {})),
        ), flush=True)


def validate_source_trace(trace, max_source_ms):
    records = trace.get("records", [])
    # A same-rig SWITCH_PATCH starts with the old DOM already ready. Ignore
    # that pre-command snapshot when a later reset occurs and measure the
    # first recovery after the final unready state.
    last_unready = -1
    for index, record in enumerate(records):
        if not source_ready(record.get("state", {})):
            last_unready = index
    ready = next(
        (record for record in records[last_unready + 1:]
         if source_ready(record.get("state", {}))),
        None,
    )
    if ready is None:
        raise AssertionError("ACOUSTIC precondition never became ready")
    if ready["ms"] > max_source_ms:
        raise AssertionError("ACOUSTIC needed %.1f ms (limit %.1f ms)" % (
            ready["ms"], max_source_ms,
        ))
    ready_index = records.index(ready)
    for record in records[ready_index:]:
        if not source_ready(record.get("state", {})):
            raise AssertionError("ACOUSTIC precondition regressed after becoming ready: %s" %
                                 compact_state(record.get("state", {})))
    return ready["ms"]


def validate_target_trace(trace, max_clean_ms):
    records = trace.get("records", [])
    ready = first_record(records, target_ready)
    if ready is None:
        raise AssertionError("CLEAN target never became ready")
    if ready["ms"] > max_clean_ms:
        raise AssertionError("CLEAN effects needed %.1f ms (limit %.1f ms)" % (
            ready["ms"], max_clean_ms,
        ))

    binding_index = None
    for index, record in enumerate(records):
        switches = by_id(record.get("state", {}))
        if (switches.get("3", {}).get("label") == "FLANG"
                and switches.get("UP", {}).get("label") == "BOOST"):
            binding_index = index
            break
    if binding_index is None:
        raise AssertionError("CLEAN FLANG/BOOST bindings never appeared")

    flang_values = []
    for record in records[binding_index:]:
        state = record.get("state", {})
        switches = by_id(state)
        flang = switches.get("3", {})
        boost = switches.get("UP", {})
        if flang.get("label") != "FLANG" or boost.get("label") != "BOOST":
            raise AssertionError("CLEAN binding labels disappeared: %s" % compact_state(state))
        if boost.get("active") is True:
            raise AssertionError("BOOST lit during CLEAN transition at %.1f ms" % record.get("ms", -1))
        flang_values.append(flang.get("active") is True)

    transitions = sum(left != right for left, right in zip(flang_values, flang_values[1:]))
    if transitions > 1:
        raise AssertionError("FLANG bounced %d times: %r" % (transitions, flang_values))

    ready_index = records.index(ready)
    for record in records[ready_index:]:
        if not target_ready(record.get("state", {})):
            raise AssertionError("CLEAN target regressed after becoming ready: %s" %
                                 compact_state(record.get("state", {})))
    return ready["ms"], transitions


def validate_effect_trace(trace, initial_active, target_active, max_effect_ms):
    """Reject missing updates, transient BOOST and every post-target bounce."""
    records = trace.get("records", [])
    if not records:
        raise AssertionError("effect trace is empty")
    if not clean_effect_state(records[0].get("state", {}), initial_active):
        raise AssertionError(
            "effect trace did not start from FLANG %s: %s" % (
                "on" if initial_active else "off",
                compact_state(records[0].get("state", {})),
            )
        )

    values = []
    ready = None
    for record in records:
        state = record.get("state", {})
        switches = by_id(state)
        flang = switches.get("3", {})
        boost = switches.get("UP", {})
        if not target_identity(state):
            raise AssertionError("CLEAN identity regressed during effect toggle: %s" % compact_state(state))
        if flang.get("label") != "FLANG" or boost.get("label") != "BOOST":
            raise AssertionError("CLEAN bindings regressed during effect toggle: %s" % compact_state(state))
        if boost.get("active") is True:
            raise AssertionError("BOOST lit during effect toggle at %.1f ms" % record.get("ms", -1))
        value = flang.get("active") is True
        values.append(value)
        if ready is None and value is target_active:
            ready = record

    if ready is None:
        raise AssertionError("FLANG never became %s" % ("on" if target_active else "off"))
    if ready.get("ms", max_effect_ms + 1) > max_effect_ms:
        raise AssertionError("FLANG needed %.1f ms (limit %.1f ms)" % (
            ready["ms"], max_effect_ms,
        ))
    ready_index = records.index(ready)
    for record in records[ready_index:]:
        if not clean_effect_state(record.get("state", {}), target_active):
            raise AssertionError(
                "FLANG regressed after becoming %s: %s" % (
                    "on" if target_active else "off",
                    compact_state(record.get("state", {})),
                )
            )
    transitions = sum(left != right for left, right in zip(values, values[1:]))
    if transitions != 1:
        raise AssertionError(
            "FLANG expected exactly one %s transition, saw %d: %r" %
            ("on" if target_active else "off", transitions, values)
        )
    return ready["ms"], transitions


async def send_effect_midi(injector, block, active):
    """Let an in-flight SSH finish even when the outer browser task cancels."""
    started = time.monotonic()
    task = asyncio.create_task(asyncio.to_thread(injector.send_effect, block, active))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
    return (time.monotonic() - started) * 1000


async def run_effect_transition(cdp, args, injector, prefix,
                                initial_active, target_active):
    await cdp.evaluate("window.__bosunStageTrace.begin()")
    injection_ms = await send_effect_midi(injector, "X", target_active)
    remaining = (args.max_effect_ms - injection_ms) / 1000
    if remaining <= args.effect_stable_ms / 1000:
        raise AssertionError(
            "MIDI injection took %.1f ms and left no time for %.0f ms "
            "stability within %.0f ms" % (
                injection_ms, args.effect_stable_ms, args.max_effect_ms,
            )
        )
    await wait_for_stable_state(
        cdp,
        lambda state: clean_effect_state(state, target_active),
        remaining,
        args.effect_stable_ms / 1000,
        "CLEAN FLANG %s after external Kemper CC" %
        ("on" if target_active else "off"),
    )
    await asyncio.sleep(args.effect_observe_seconds)
    trace = await read_trace(cdp)
    dump_trace("%s trace" % prefix, trace.get("records", []))
    effect_ms, transitions = validate_effect_trace(
        trace, initial_active, target_active, args.max_effect_ms,
    )
    surfaces = await read_effect_surfaces(cdp, prefix, target_active)
    return effect_ms, transitions, surfaces[3], injection_ms


async def run_effect_cycles(cdp, args, cold_run):
    """Toggle CLEAN X both ways, always restoring the observed initial state."""
    prefix = "browser-cold%d-effect-initial" % cold_run
    context_reply = await send_control_request(
        cdp, "getContext", prefix + "-context", "CONTEXT",
    )
    context = context_reply.get("context")
    if not isinstance(context, dict):
        raise AssertionError("initial CONTEXT response has no context object")
    raw_initial = context.get("kemper_block_X")
    if raw_initial not in ("on", "off"):
        raise AssertionError("initial Kemper-derived X state is not authoritative: %r" % raw_initial)
    initial_active = raw_initial == "on"
    led_dump = await send_control_request(
        cdp, "ledDump", prefix + "-leds", "LED_DUMP",
    )
    initial_state = await snapshot(cdp)
    # Do not trust the CLEAN default as the restore oracle: this exact value
    # came from the Captain's Kemper-derived context before any MIDI mutation.
    initial_pixels = validate_effect_surfaces(
        initial_state, context_reply, led_dump, initial_active,
    )
    injector = KemperMidiInjector(args.ssh, args.effect_ssh_target)
    inverse_active = not initial_active
    mutated = False
    primary_error = None
    try:
        # Discovery is a preflight and cannot change MIDI state. If it is
        # ambiguous or unavailable, fail before sending the first byte.
        port = await asyncio.to_thread(injector.discover)
        print(
            "EFFECT preflight PASS alsa_port=%s observed_X=%s "
            "stage/context/captain_leds=converged" % (port, raw_initial),
            flush=True,
        )
        for cycle in range(1, args.effect_cycles + 1):
            cycle_prefix = "browser-cold%d-effect%d" % (cold_run, cycle)
            mutated = True
            (
                away_ms, away_transitions,
                inverse_pixels, away_injection_ms,
            ) = await run_effect_transition(
                cdp, args, injector, cycle_prefix + "-away", initial_active,
                inverse_active,
            )
            validate_led_levels(initial_pixels, inverse_pixels, initial_active)
            (
                back_ms, back_transitions,
                restored_pixels, back_injection_ms,
            ) = await run_effect_transition(
                cdp, args, injector, cycle_prefix + "-restore", inverse_active,
                initial_active,
            )
            if restored_pixels != initial_pixels:
                raise AssertionError(
                    "Captain switch 3 LEDs did not return to baseline: %r != %r" %
                    (restored_pixels, initial_pixels)
                )
            print(
                "effect cycle %d PASS away_dom=%.1fms restore_dom=%.1fms "
                "midi_injection=%.1f/%.1fms transitions=%d/%d "
                "captain_leds=restored" % (
                    cycle, away_ms, back_ms,
                    away_injection_ms, back_injection_ms,
                    away_transitions, back_transitions,
                ),
                flush=True,
            )
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if mutated:
            try:
                # Sending is independent of CDP, but a successful ALSA command
                # is not enough: require context, framebuffer and DOM to prove
                # the exact observed initial state even after a primary error.
                await restore_effect_state(
                    cdp, args, injector,
                    "browser-cold%d-effect-final" % cold_run,
                    initial_active, initial_pixels,
                )
            except BaseException as restore_error:
                primary_detail = (
                    "%s: %s" % (type(primary_error).__name__, primary_error)
                    if primary_error is not None else "none"
                )
                restore_detail = "%s: %s" % (
                    type(restore_error).__name__, restore_error,
                )
                message = (
                    "restoration failed; final effect state unknown for observed "
                    "Kemper X=%s; primary failure=%s; restore failure=%s" %
                    (raw_initial, primary_detail, restore_detail)
                )
                print("FATAL: " + message, flush=True)
                raise RuntimeError(message) from restore_error


async def run_cycle(cdp, args, cold_run, cycle):
    prefix = "browser-cold%d-cycle%d" % (cold_run, cycle)
    source_ack = await send_switch(
        cdp, 1, 1, prefix + "-acoustic", args.switch_timeout_seconds,
    )
    await wait_for_stable_state(
        cdp, source_ready, args.max_source_ms / 1000, 0.6,
        "ACOUSTIC header/nav with switch 4 HARM active",
    )
    await asyncio.sleep(args.source_soak_seconds)
    source_trace = await read_trace(cdp)
    dump_trace("cycle %d ACOUSTIC trace" % cycle, source_trace.get("records", []))
    source_ms = validate_source_trace(source_trace, args.max_source_ms)

    target_id = prefix + "-clean"
    target_wire_start = cdp.frame_cursor()
    target_ack = await send_switch(
        cdp, 1, 2, target_id, args.switch_timeout_seconds,
    )
    target_context = None
    target_led_dump = None
    target_led_info = None
    try:
        await wait_for_state(
            cdp, target_ready, args.max_clean_ms / 1000,
            "CLEAN header/nav with FLANG on and BOOST off",
        )
        # DOM alone cannot distinguish an authoritative Reverb=off from a
        # freshly reset/unknown latch. Require a correlated full snapshot and
        # the Captain's actual LED framebuffer before calling CLEAN healthy.
        clean_patch = latest_patch_from_wire(
            cdp.frames_since(target_wire_start), 1, 2,
        )
        _, target_context, target_led_dump, target_led_info = (
            await read_clean_transition_surfaces(
                cdp, prefix + "-clean-surfaces", clean_patch,
                args.switch_timeout_seconds,
            )
        )
        await asyncio.sleep(args.observe_seconds)
        target_trace = await read_trace(cdp)
        clean_ms, transitions = validate_target_trace(target_trace, args.max_clean_ms)
    except Exception:
        target_trace = await read_trace(cdp)
        milestones = clean_transition_milestones(
            cdp.frames_since(target_wire_start), target_trace, target_id,
            target_context, target_led_dump,
        )
        print(format_clean_milestones(cycle, milestones, target_led_info), flush=True)
        dump_trace("cycle %d CLEAN failure trace" % cycle, target_trace.get("records", []))
        raise

    milestones = clean_transition_milestones(
        cdp.frames_since(target_wire_start), target_trace, target_id,
        target_context, target_led_dump,
    )
    print(format_clean_milestones(cycle, milestones, target_led_info), flush=True)

    print(
        "cycle %d PASS source_ack=%.1fms source_dom=%.1fms "
        "clean_ack=%.1fms clean_dom=%.1fms flang_transitions=%d "
        "context=X:on/Reverb:off captain_leds=FLANG:on/BOOST:off" % (
            cycle, source_ack["ackMs"], source_ms,
            target_ack["ackMs"], clean_ms, transitions,
        ),
        flush=True,
    )


def _transient_profile_cleanup_error(error):
    """Return whether Edge/Crashpad can reasonably release this path soon."""
    return (
        isinstance(error, PermissionError)
        or getattr(error, "winerror", None) in (32, 33, 145)
        or getattr(error, "errno", None) in (errno.EACCES, errno.EBUSY, errno.ENOTEMPTY)
    )


async def cleanup_browser_profile(
        profile, primary_error=None, attempts=31, retry_delay_seconds=0.1):
    """Remove an Edge profile despite short-lived Crashpad file handles.

    The retry window is deliberately bounded.  A cleanup failure is fatal when
    the browser test itself passed, but must never replace an earlier test
    failure: in that case retain the original exception and attach the cleanup
    detail as a note.
    """
    if attempts < 1:
        raise ValueError("cleanup attempts must be positive")
    last_error = None
    for attempt in range(attempts):
        try:
            profile.cleanup()
            return
        except OSError as error:
            last_error = error
            if (not _transient_profile_cleanup_error(error)
                    or attempt + 1 >= attempts):
                break
            await asyncio.sleep(retry_delay_seconds)

    detail = "browser profile cleanup failed after %d attempt(s): %s" % (
        attempts, last_error,
    )
    if primary_error is None:
        raise last_error
    if hasattr(primary_error, "add_note"):
        primary_error.add_note(detail)
    print("WARN: " + detail, flush=True)


async def run_browser(args, cold_run):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    profile = tempfile.TemporaryDirectory(prefix="bosun-stage-cdp-")
    browser_started = time.monotonic()
    browser = subprocess.Popen([
        args.edge, "--headless=new", "--disable-gpu", "--no-first-run",
        "--remote-debugging-port=%d" % port,
        # Attach CDP and enable Network before loading Stage.  Otherwise the
        # bootstrap WebSocket frames can be gone before diagnostics start.
        "--user-data-dir=" + profile.name, "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp = None
    primary_error = None
    try:
        cdp = CdpSession(await cdp_socket(port, args.page))
        await cdp.command("Network.enable")
        navigation = await cdp.command("Page.navigate", {"url": args.page})
        if navigation.get("errorText"):
            raise RuntimeError("Stage navigation failed: %s" % navigation["errorText"])
        nav_state, _ = await passive_nav(cdp, browser_started, args.max_nav_ms)
        grid_state = nav_state
        if args.passive_effects:
            grid_state, _, _ = await passive_effects(
                cdp,
                browser_started,
                args.passive_rig,
                args.max_passive_effects_ms,
                args.passive_stable_ms,
            )
        else:
            print("EFFECTS SKIP (--no-passive-effects)", flush=True)
        print("GRID", compact_state(grid_state), flush=True)
        if args.nav_only:
            return

        await cdp.evaluate(TRACE_SCRIPT)
        control_script = CONTROL_SCRIPT.replace(
            "__HUB_URL__", json.dumps(control_hub_url(args.hub)),
        )
        opened = await cdp.evaluate(control_script, await_promise=True)
        if opened is not True:
            raise RuntimeError("control WebSocket did not report ready")
        for cycle in range(1, args.cycles + 1):
            await run_cycle(cdp, args, cold_run, cycle)
        if args.effect_cycles:
            await run_effect_cycles(cdp, args, cold_run)
    except BaseException as error:
        primary_error = error
        if cdp is not None:
            # Preserve both command and response evidence. Identical hub
            # broadcasts received by the Stage and tagged control sockets are
            # collapsed only for display; same-socket repeats remain visible.
            print("WIRE failure evidence (cross-socket copies collapsed)", flush=True)
            wire_lines = format_wire_frames(cdp.frames)
            if wire_lines:
                for line in wire_lines:
                    print(line, flush=True)
            else:
                print("  (no WebSocket frames captured)", flush=True)
        raise
    finally:
        if cdp is not None:
            try:
                await cdp.evaluate("window.__bosunControl?.ws?.close(); true")
            except Exception:
                pass
            try:
                await cdp.close()
            except Exception:
                pass
        browser.terminate()
        try:
            browser.wait(timeout=5)
        except subprocess.TimeoutExpired:
            browser.kill()
            browser.wait(timeout=5)
        await cleanup_browser_profile(profile, primary_error)


async def main():
    args = parse_args()
    mode = (
        "passive bootstrap"
        if args.nav_only
        else "%d rig transition cycle(s), %d external effect cycle(s)" %
        (args.cycles, args.effect_cycles)
    )
    print("real Stage test: %d cold run(s), %s" % (args.cold_runs, mode), flush=True)
    for cold_run in range(1, args.cold_runs + 1):
        print("cold run %d/%d" % (cold_run, args.cold_runs), flush=True)
        await run_browser(args, cold_run)
    print("PASS: %d cold run(s), %s" % (args.cold_runs, mode), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
