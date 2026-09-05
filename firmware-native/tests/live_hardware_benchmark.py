#!/usr/bin/env python3
"""Bounded, read-only by default Captain protocol baseline/acceptance recorder.

Run on the Pi with its hub active:
  python3 live_hardware_benchmark.py --output cp-hub.json --samples 30
Or release the data CDC from bosun-hub first (this script changes no services):
  python3 live_hardware_benchmark.py --serial /dev/ttyACM1 --output cp-usb.json

Only --switch-rigs changes live state, and the original rig is restored. No
PUT_*, SAVE_NOW, DISCARD, REBOOT, format or firmware command is sent. Restoring
a rig can fire its on-enter MIDI actions, just as normal navigation does.

--duration bounds the measured phase; setup/warmup/cleanup have finite request
deadlines. A conservative total bound is duration + timeout*(14+3*warmup),
excluding OS device-open/DNS scheduling delays. Use numeric IPs for TCP.

Each connection first acquires a correlated PING sentinel. A bounded number
of partial startup frames may precede its ACK; their bytes are recorded in
observations.startup_sync. Parsing is strict after that initial ACK. The sync
exchange is excluded from all measurement distributions.
"""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import platform
import socket
import time
import uuid


KINDS = {"PING": "ACK", "GET_CONTEXT": "CONTEXT", "GET_GLOBAL": "GLOBAL",
         "STATS": "STATS", "SWITCH_PATCH": "ACK", "GET_RIG_INFO": "RIG_INFO",
         "GET_DEVICE_INFO": "DEVICE_INFO", "GET_PATCH": "PATCH",
         "PUT_GLOBAL": "ACK", "REBOOT": "ACK"}
ERROR_COUNTERS = ("usb_tx_dropped", "midi_tx_failed", "queue_overflows",
                  "invalid_messages", "protocol_errors", "storage_errors",
                  "midi_events_dropped")
MAX_LINE = 256 * 1024
STARTUP_DISCARD_MAX_FRAMES = 4
STARTUP_DISCARD_MAX_BYTES = 16 * 1024
STARTUP_PREFIX_BYTES = 256
MALFORMED_FRAMES_RETAINED = 4
REQUEST_FAILURES_RETAINED = 8
FAILED_PENDING_BYTES_RETAINED = 4096


class Client:
    """Correlated newline JSON without socket.makefile timeout poisoning."""

    def __init__(self, transport, is_serial=False, observations=None):
        self.transport, self.is_serial = transport, is_serial
        self.pending = bytearray()
        self.prefix, self.sequence = "bench-" + uuid.uuid4().hex[:12], 0
        self.observations = observations if observations is not None else {}
        self.observations.setdefault("ignored_message_types", {})
        self.observations.setdefault("ignored_examples", [])

    def close(self):
        self.transport.close()

    def synchronize(self, timeout):
        """Acquire the first PING ACK, retaining evidence of truncated startup.

        Like the production hub's sentinel, the leading newline terminates
        any old partial request. Unlike a buffer purge, startup discard here
        is counted and recorded, bounded, and unavailable after this request.
        """
        if self.sequence:
            raise RuntimeError("startup synchronization is only allowed before the first request")
        record = {"session": self.prefix, "timeout_s": timeout, "completed": False,
                  "discarded_partial_frames": 0, "discarded_bytes": 0,
                  "discarded_frames": [], "rx_prefix_hex": "", "rx_prefix_text": ""}
        self.observations.setdefault("startup_sync", []).append(record)
        started = time.monotonic()
        try:
            reply, _, _ = self._request("PING", timeout, startup=record)
            record["completed"] = True
            record["ack"] = reply
            return reply
        except (Exception, KeyboardInterrupt) as exc:
            record["error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            record["elapsed_ms"] = (time.monotonic() - started) * 1000

    def request(self, kind, timeout, **fields):
        started = time.monotonic()
        try:
            return self._request(kind, timeout, **fields)
        except (Exception, KeyboardInterrupt) as exc:
            pending = bytes(self.pending[:FAILED_PENDING_BYTES_RETAINED])
            failure = {"request_kind": kind, "request_id": "%s-%d" % (self.prefix, self.sequence),
                       "fields": dict(fields), "timeout_s": timeout,
                       "request_started_monotonic_s": started,
                       "elapsed_ms": (time.monotonic() - started) * 1000,
                       "failed_monotonic_s": time.monotonic(),
                       "exception_type": type(exc).__name__, "error": str(exc),
                       "pending_bytes": len(self.pending),
                       "pending_fragment_truncated": len(pending) < len(self.pending),
                       "latest_context": self.observations.get("latest_context"),
                       "latest_rig_reply": self.observations.get("latest_rig_reply"),
                       "latest_context_received_monotonic_s": self.observations.get("latest_context_received_monotonic_s"),
                       "latest_rig_reply_received_monotonic_s": self.observations.get("latest_rig_reply_received_monotonic_s")}
            try:
                failure["pending_fragment_utf8"] = pending.decode("utf-8", "strict")
            except UnicodeDecodeError:
                failure["pending_fragment_hex"] = pending.hex()
            self.observations["request_failure_count"] = self.observations.get("request_failure_count", 0) + 1
            failures = self.observations.setdefault("request_failures", [])
            failures.append(failure)
            del failures[:-REQUEST_FAILURES_RETAINED]
            # Keep the most recent states in every failure, independently of
            # the first-N unsolicited-message examples and later cleanup.
            self.observations["last_request_failure"] = failure
            raise

    def _request(self, kind, timeout, startup=None, **fields):
        self.sequence += 1
        ident = "%s-%d" % (self.prefix, self.sequence)
        payload = json.dumps(dict(type=kind, id=ident, **fields),
                             separators=(",", ":")).encode() + b"\n"
        if startup is not None:
            payload = b"\n" + payload
        startup_prefix = bytearray()
        started = time.monotonic()
        deadline = started + timeout
        if self.is_serial:
            self.transport.write_timeout = timeout
            if self.transport.write(payload) != len(payload):
                raise IOError("partial serial request write")
        else:
            self.transport.settimeout(timeout)
            self.transport.sendall(payload)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("%s timed out after %.3fs" % (kind, timeout))
            newline = self.pending.find(b"\n")
            if newline >= 0:
                line = bytes(self.pending[:newline])
                del self.pending[:newline + 1]
                if len(line) > MAX_LINE:
                    raise ValueError("protocol line exceeds 256 KiB")
                if not line.strip():
                    continue
                try:
                    reply = json.loads(line)
                except (ValueError, UnicodeDecodeError) as exc:
                    # Preserve wire evidence before any strict failure. These
                    # artifacts already contain private configuration; never
                    # commit them. MAX_LINE and the retained-frame cap bound
                    # this diagnostic even across repeated new connections.
                    self.observations["malformed_frame_count"] = self.observations.get("malformed_frame_count", 0) + 1
                    frames = self.observations.setdefault("malformed_frames", [])
                    if len(frames) < MALFORMED_FRAMES_RETAINED:
                        frame = {"phase": "startup_sync" if startup is not None else "session",
                                 "request_kind": kind, "request_id": ident,
                                 "frame_bytes": len(line) + 1,
                                 "exception_type": type(exc).__name__, "error": str(exc)}
                        if isinstance(exc, json.JSONDecodeError):
                            frame["json_error_position"] = exc.pos
                        try:
                            frame["raw_utf8"] = (line + b"\n").decode("utf-8", "strict")
                        except UnicodeDecodeError:
                            frame["raw_hex"] = (line + b"\n").hex()
                        frames.append(frame)
                    # A broken reply to this PING is not a harmless fragment
                    # from before our connection and must fail acquisition.
                    if startup is None or ident.encode() in line:
                        raise
                    startup["discarded_partial_frames"] += 1
                    startup["discarded_bytes"] += len(line) + 1
                    prefix = line[:STARTUP_PREFIX_BYTES]
                    startup["discarded_frames"].append({
                        "bytes": len(line) + 1, "raw_prefix_hex": prefix.hex(),
                        "raw_prefix_text": prefix.decode("utf-8", "replace"),
                        "error": str(exc)})
                    if (startup["discarded_partial_frames"] > STARTUP_DISCARD_MAX_FRAMES
                            or startup["discarded_bytes"] > STARTUP_DISCARD_MAX_BYTES):
                        raise ValueError("startup synchronization discard budget exceeded") from exc
                    continue
                if not isinstance(reply, dict):
                    raise ValueError("protocol reply is not an object")
                if reply.get("type") in ("CONTEXT", "RIG_INFO"):
                    key = "latest_context" if reply["type"] == "CONTEXT" else "latest_rig_reply"
                    self.observations[key] = reply
                    self.observations[key + "_received_monotonic_s"] = time.monotonic()
                if reply.get("id") == ident:
                    if reply.get("type") != KINDS[kind]:
                        raise RuntimeError("%s: %s" % (kind, reply))
                    return reply, (time.monotonic() - started) * 1000, len(line) + 1
                label = str(reply.get("type", "unknown"))
                counts = self.observations["ignored_message_types"]
                counts[label] = counts.get(label, 0) + 1
                if len(self.observations["ignored_examples"]) < 20:
                    self.observations["ignored_examples"].append(reply)
                continue
            if len(self.pending) > MAX_LINE:
                raise ValueError("unterminated protocol line exceeds 256 KiB")
            if self.is_serial:
                self.transport.timeout = min(remaining, 0.05)
                chunk = self.transport.read(max(1, min(self.transport.in_waiting, 4096)))
            else:
                self.transport.settimeout(remaining)
                chunk = self.transport.recv(4096)
                if not chunk:
                    raise ConnectionError("TCP peer disconnected")
            self.pending.extend(chunk)
            if startup is not None and len(startup_prefix) < STARTUP_PREFIX_BYTES:
                startup_prefix.extend(chunk[:STARTUP_PREFIX_BYTES - len(startup_prefix)])
                startup["rx_prefix_hex"] = startup_prefix.hex()
                startup["rx_prefix_text"] = startup_prefix.decode("utf-8", "replace")


def connect(args, observations):
    if args.serial:
        import serial
        # No reset, console control characters, input-buffer purge or bootloader
        # magic. Select the data CDC, not the CircuitPython REPL CDC.
        port = serial.Serial(args.serial, 115200, timeout=0.05,
                             write_timeout=args.timeout, exclusive=True)
        client = Client(port, True, observations)
    else:
        client = Client(socket.create_connection((args.host, args.port), args.timeout),
                        observations=observations)
    try:
        client.synchronize(args.timeout)
    except (Exception, KeyboardInterrupt):
        client.close()
        raise
    return client


def distribution(values):
    if not values:
        return {"count": 0}
    values = sorted(values)
    def percentile(fraction):
        position = (len(values) - 1) * fraction
        low, high = math.floor(position), math.ceil(position)
        return values[low] + (values[high] - values[low]) * (position - low)
    return {"count": len(values), "min_ms": values[0],
            "mean_ms": sum(values) / len(values), "p50_ms": percentile(.5),
            "p95_ms": percentile(.95), "p99_ms": percentile(.99),
            "max_ms": values[-1]}


def timing_scope(args):
    return {
        "transport": "direct_serial" if args.serial else "hub_tcp",
        "unit": "milliseconds", "clock": "host time.monotonic",
        "includes": ("host scheduling, serial USB transport, firmware processing"
                     if args.serial else "host/network, hub scheduling and USB/firmware roundtrip"),
        "excludes": "isolated firmware execution time, physical TFT emission and footswitch latency",
        "GET_GLOBAL": ("direct firmware reply; no hub cache" if args.serial else
                       "forwarded bulk request in this repository's production hub; not cached"),
        "GET_CONTEXT": ("direct firmware reply" if args.serial else
                        "may join an in-flight hub request; no retained context cache"),
        "hub_assumption": "Remote hub must match tools/rpi-hub/bosun_hub/hub.py for these semantics.",
        "memory": "CP mem_free/mem_alloc are heap readings; native counters are not heap or stack headroom.",
        "startup": "A bounded PING synchronization precedes each connection; startup fragments are recorded separately and excluded from timings.",
    }


def confirmed(client, bank, slot, timeout, verify_kemper):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        latest = client.request("GET_CONTEXT", deadline - time.monotonic())[0]
        ctx = latest.get("context", {})
        match = ((ctx.get("bank"), ctx.get("slot")) == (bank, slot)
                 and ctx.get("preview") in (None, "", "off", False))
        if match and verify_kemper:
            match = (ctx.get("kemper_connected") == "on"
                     and (ctx.get("kemper_bank"), ctx.get("kemper_rig_in_bank")) == (bank, slot))
            if match:
                rig = client.request("GET_RIG_INFO", max(.001, deadline - time.monotonic()), request=False)[0]
                match = rig.get("fresh") is True and rig.get("rig") == (bank - 1) * 5 + slot
                if match:
                    return {"context_reply": latest, "rig_reply": rig}
        elif match:
            return {"context_reply": latest}
        time.sleep(min(.1, max(0, deadline - time.monotonic())))
    raise TimeoutError("rig B%d R%d was not confirmed: %s" % (bank, slot, latest))


def evaluate(result):
    initial, final = result["initial"], result["final"]
    checks = {}
    if "GET_GLOBAL" in initial and "GET_GLOBAL" in final:
        checks["global_configuration_unchanged"] = initial["GET_GLOBAL"].get("device") == final["GET_GLOBAL"].get("device")
    if "STATS" in initial and "STATS" in final:
        before, after = initial["STATS"], final["STATS"]
        start, end = before.get("uptime_ms"), after.get("uptime_ms")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            # RP2040 milliseconds can wrap after ~49 days. A small negative
            # change is a reboot/counter reset; a uint32 wrap is allowed.
            checks["no_observed_uptime_reset"] = end >= start or start - end > 2**31
        result["counter_deltas"] = {key: after[key] - before[key]
                                    for key in ERROR_COUNTERS
                                    if key in before and key in after}
        for key, delta in result["counter_deltas"].items():
            checks[key + "_unchanged"] = delta == 0
        if "storage_ready" in after:
            checks["native_storage_ready"] = after["storage_ready"] is True
    result["checks"] = checks
    for check, passed in checks.items():
        if not passed:
            result["errors"].append({"phase": "acceptance", "error": check})
    result["memory_observations"] = {}
    for key in ("mem_free", "mem_alloc"):
        values = [entry["reply"][key] for entry in result["stats_samples"]
                  if isinstance(entry["reply"].get(key), (int, float))]
        if values:
            result["memory_observations"][key] = {"min_bytes": min(values), "max_bytes": max(values)}


def run(args, connector=connect):
    started = time.monotonic()
    result = {"schema_version": 1, "metadata": {
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "label": args.label, "host_platform": platform.platform(),
        "python": platform.python_version(), "arguments": vars(args).copy(),
        "timing_scope": timing_scope(args)},
        "initial": {}, "final": {}, "samples": [], "stats_samples": [],
        "rig_switches": [], "errors": [], "observations": {},
        "restoration": {"needed": False}, "termination": "setup"}
    client = None
    original = None
    phase = "setup"
    def error(exc):
        result["errors"].append({"phase": phase, "type": type(exc).__name__, "error": str(exc)})
    def snapshot(destination, kind):
        reply = client.request(kind, args.timeout)[0]
        destination[kind] = reply
        if kind == "STATS":
            result["stats_samples"].append({"elapsed_s": time.monotonic() - started, "reply": reply})
    try:
        client = connector(args, result["observations"])
        for kind in ("PING", "GET_GLOBAL", "GET_CONTEXT", "STATS"):
            snapshot(result["initial"], kind)
        context = result["initial"]["GET_CONTEXT"]["context"]
        original = (context["bank"], context["slot"])
        if args.switch_rigs and context.get("preview") not in (None, "", "off", False):
            raise ValueError("cannot safely restore an active preview; exit preview before rig testing")
        phase = "warmup"
        for _ in range(args.warmup):
            for kind in ("PING", "GET_CONTEXT", "GET_GLOBAL"):
                client.request(kind, args.timeout)
        phase = "measurement"
        deadline = time.monotonic() + args.duration
        result["termination"] = "samples"
        for index in range(args.samples):
            if time.monotonic() >= deadline:
                result["termination"] = "duration"
                break
            if args.switch_rigs:
                bank, slot = args.switch_rigs[index % len(args.switch_rigs)]
                result["restoration"]["needed"] = True  # Even a lost ACK may have switched.
                switch_start = time.monotonic()
                reply, ack_ms, _ = client.request("SWITCH_PATCH", min(args.timeout, deadline - switch_start), bank=bank, slot=slot)
                confirmation = confirmed(client, bank, slot, min(args.timeout, max(.001, deadline - time.monotonic())), args.verify_kemper)
                result["rig_switches"].append({"bank": bank, "slot": slot,
                    "ack_ms": ack_ms, "command_to_confirmation_ms": (time.monotonic() - switch_start) * 1000,
                    "confirmation": confirmation})
            for kind in ("PING", "GET_CONTEXT", "GET_GLOBAL"):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    result["termination"] = "duration"
                    break
                reply, elapsed, size = client.request(kind, min(args.timeout, remaining))
                result["samples"].append({"kind": kind, "elapsed_ms": elapsed,
                    "reply_bytes": size, "since_start_s": time.monotonic() - started})
            if (index + 1) % args.stats_every == 0 and time.monotonic() < deadline:
                reply = client.request("STATS", min(args.timeout, deadline - time.monotonic()))[0]
                result["stats_samples"].append({"elapsed_s": time.monotonic() - started, "reply": reply})
    except (Exception, KeyboardInterrupt) as exc:
        if isinstance(exc, TimeoutError) and phase == "measurement" and time.monotonic() >= deadline:
            # An operation cut off by our overall budget is censored, not an
            # observed firmware timeout. Do not include it in percentiles.
            result["termination"] = "duration"
            result["incomplete_request_at_deadline"] = str(exc)
        else:
            result["termination"] = "error"
            error(exc)
    finally:
        if client:
            client.close()
        client = None
        phase = "restoration" if result["restoration"]["needed"] else "final_snapshot"
        try:
            client = connector(args, result["observations"])
            if result["restoration"]["needed"]:
                client.request("SWITCH_PATCH", args.timeout, bank=original[0], slot=original[1])
                result["restoration"]["confirmation"] = confirmed(client, *original, args.timeout, args.verify_kemper)
                result["restoration"]["succeeded"] = True
        except (Exception, KeyboardInterrupt) as exc:
            if result["restoration"]["needed"]:
                result["restoration"]["succeeded"] = False
            error(exc)
        phase = "final_snapshot"
        if client:
            for kind in ("GET_GLOBAL", "GET_CONTEXT", "STATS"):
                try:
                    snapshot(result["final"], kind)
                except (Exception, KeyboardInterrupt) as exc:
                    error(exc)
            client.close()
    result["elapsed_s"] = time.monotonic() - started
    result["distributions"] = {kind: distribution([s["elapsed_ms"] for s in result["samples"] if s["kind"] == kind])
                               for kind in ("PING", "GET_CONTEXT", "GET_GLOBAL")}
    if not result["samples"]:
        result["errors"].append({"phase": "acceptance", "error": "no measured samples"})
    evaluate(result)
    result["passed"] = not result["errors"]
    return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9876)
    parser.add_argument("--serial", help="Direct data CDC path, with hub serial access stopped")
    parser.add_argument("--output", required=True, help="New JSON artifact path; refuses overwrite")
    parser.add_argument("--label", default="unspecified", help="Firmware/build identity and test conditions")
    parser.add_argument("--samples", type=int, default=30, help="Maximum samples per command")
    parser.add_argument("--duration", type=float, default=120, help="Maximum measurement seconds")
    parser.add_argument("--timeout", type=float, default=8, help="Per-request/confirmation deadline seconds")
    parser.add_argument("--warmup", type=int, default=2, help="Unrecorded warmup rounds after initial snapshots")
    parser.add_argument("--stats-every", type=int, default=5)
    parser.add_argument("--switch-rigs", default="", help="Opt-in cyclic targets, e.g. 1:1,1:2,1:3; original rig restored")
    parser.add_argument("--verify-kemper", action="store_true", help="Require connected, matching Kemper context and fresh correlated rig info")
    args = parser.parse_args(argv)
    for key, low, high in (("samples", 1, 10000), ("duration", .01, 3600),
                           ("timeout", .01, 120), ("warmup", 0, 100),
                           ("stats_every", 1, 10000), ("port", 1, 65535)):
        value = getattr(args, key)
        if not math.isfinite(value) or not low <= value <= high:
            parser.error("%s must be in [%s, %s]" % (key, low, high))
    try:
        args.switch_rigs = [tuple(map(int, pair.split(":"))) for pair in args.switch_rigs.split(",") if pair]
        if any(len(pair) != 2 or not 1 <= pair[0] <= 125 or not 1 <= pair[1] <= 5 for pair in args.switch_rigs):
            raise ValueError()
    except ValueError:
        parser.error("switch-rigs must contain bank:slot pairs (bank 1..125, slot 1..5)")
    if args.verify_kemper and not args.switch_rigs:
        parser.error("verify-kemper requires switch-rigs")
    return args


def main(argv=None):
    args = parse_args(argv)
    # Reserve the artifact before touching a device. Even failed runs leave
    # their full evidence, and an earlier known-good baseline cannot be lost.
    with Path(args.output).open("x", encoding="utf-8") as output:
        result = run(args)
        json.dump(result, output, indent=2, ensure_ascii=False)
        output.write("\n")
    print(json.dumps({"passed": result["passed"], "output": args.output,
                      "elapsed_s": result["elapsed_s"], "distributions": result["distributions"],
                      "errors": result["errors"]}, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
