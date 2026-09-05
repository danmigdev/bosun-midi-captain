#!/usr/bin/env python3
"""Compare a backed-up Captain snapshot and optionally test native persistence.

Read-only comparison (hub running):
  python3 live_native_persistence.py --snapshot protocol-snapshot.json --output comparison.json
Explicit reversible write/reboot acceptance:
  python3 live_native_persistence.py --snapshot protocol-snapshot.json --output persistence.json --exercise-writes
Direct data CDC, with hub serial ownership released: add --serial /dev/ttyACM1.

The supplied snapshot must contain the original active GET_GLOBAL,
GET_DEVICE_INFO and GET_PATCH records. Every captured GET_GLOBAL/GET_PATCH
is compared. Firmware identity must be native before any write or reboot.
The write exercise changes only the low bit of tft.layout[0].color, verifies
exact readback across a normal reboot, then restores the complete original
JSON and proves it survives a second normal reboot. The original live rig is
restored and confirmed with the Kemper. Do not operate other editor clients
during this test: an exact configuration comparison cannot merge concurrent
edits. No patch write, deletion, bootloader or factory reset is performed.

Each request has --timeout; each acquisition/reboot recovery has its own
--recovery-timeout. Recovery retries are logged and bounded by both that
deadline and --recovery-attempts. Expected reboot disconnects are recorded
separately from test errors. --output must be a new file; reserve it before
opening any hardware. JSON artifacts include private configuration data.
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import time

import live_hardware_benchmark as benchmark


class AcceptanceError(RuntimeError):
    pass


def require_native(info):
    version = str(info.get("fw", ""))
    if info.get("native_experimental") is not True and not re.match(r"^0\.1(?:\.\d+)?-native(?:\b|-)", version):
        raise AcceptanceError("native firmware required; refusing writes/reboots for fw=%r" % version)


def load_snapshot(path):
    raw = Path(path).read_bytes()
    source = json.loads(raw)
    records = source.get("records")
    if not isinstance(records, list):
        raise ValueError("snapshot must contain a records array")
    globals_, patches, info = [], [], None
    seen = set()
    for record in records:
        request, reply = record.get("request", {}), record.get("response", {})
        kind = request.get("type")
        if kind not in ("GET_GLOBAL", "GET_PATCH", "GET_DEVICE_INFO"):
            continue
        expected = benchmark.KINDS[kind]
        if reply.get("type") != expected:
            raise ValueError("snapshot contains unsuccessful " + kind)
        if kind == "GET_DEVICE_INFO":
            if info is not None:
                raise ValueError("snapshot has multiple DEVICE_INFO records")
            info = reply
            continue
        key = (kind, request.get("profile", ""), request.get("bank"), request.get("slot"))
        if key in seen:
            raise ValueError("snapshot has duplicate configuration selectors")
        seen.add(key)
        field = "device" if kind == "GET_GLOBAL" else "patch"
        if not isinstance(reply.get(field), dict):
            raise ValueError("snapshot configuration must be a JSON object")
        if kind == "GET_PATCH" and any(not isinstance(request.get(k), int) or isinstance(request.get(k), bool)
                                        or request[k] < 1 for k in ("bank", "slot")):
            raise ValueError("snapshot patch requires positive bank and slot")
        (globals_ if kind == "GET_GLOBAL" else patches).append({"request": request, "response": reply})
    active = [r for r in globals_ if not r["request"].get("profile")]
    if len(active) != 1 or not patches or info is None:
        raise ValueError("snapshot requires one active GET_GLOBAL, DEVICE_INFO and at least one GET_PATCH")
    for key in ("tft_colors", "tft_labels"):
        if not isinstance(info.get(key), dict):
            raise ValueError("snapshot DEVICE_INFO requires " + key)
    return {"sha256": hashlib.sha256(raw).hexdigest(), "globals": globals_, "patches": patches,
            "info": info, "original": active[0]["response"]["device"]}


def changed_color(original):
    modified = copy.deepcopy(original)
    try:
        item = modified["tft"]["layout"][0]
        color, field = item["color"], item["field"]
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color) or not isinstance(field, str) or not field:
            raise ValueError()
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError("write exercise requires tft.layout[0] with a field and #RRGGBB color") from exc
    item["color"] = "#%06x" % (int(color[1:], 16) ^ 1)
    return modified, {"path": "tft.layout[0].color", "field": field,
                      "original": color, "temporary": item["color"]}


def equal(actual, expected, label):
    if actual != expected:
        # Complete values are already recorded in the artifact. Avoid printing
        # a user's entire configuration into console error messages.
        raise AcceptanceError(label + " differs from expected JSON")


def transient(exc):
    # JSON corruption, application validation errors and wrong reply types
    # remain strict. Only acquisition/disconnect conditions can be retried.
    return isinstance(exc, OSError) or (isinstance(exc, RuntimeError) and
        any(token in str(exc) for token in ("'error': 'link_down'", '"error": "link_down"',
                                           "'error': 'link_busy'", "'error': 'context_busy'")))


class Acceptance:
    def __init__(self, args, snapshot, connector=benchmark.connect):
        self.args, self.snapshot, self.connector = args, snapshot, connector
        self.client, self.original_rig = None, None
        self.phase = "setup"
        self.started = time.monotonic()
        self.result = {"schema_version": 1, "metadata": {
            "started_utc": datetime.now(timezone.utc).isoformat(), "arguments": vars(args).copy(),
            "snapshot_sha256": snapshot["sha256"], "timing_scope": benchmark.timing_scope(args),
            "global_records": len(snapshot["globals"]), "patch_records": len(snapshot["patches"])},
            "records": [], "observations": {}, "errors": [], "recovery": [],
            "expected_reboots": [], "restoration": {"needed": False}, "checks": {}}

    def request(self, kind, timeout=None, **fields):
        started = time.monotonic()
        record = {"phase": self.phase, "request": dict(type=kind, **fields),
                  "since_start_s": started - self.started}
        self.result["records"].append(record)
        try:
            reply, elapsed, size = self.client.request(kind, self.args.timeout if timeout is None else timeout, **fields)
            record.update(response=reply, elapsed_ms=elapsed, reply_bytes=size)
            return reply
        except (Exception, KeyboardInterrupt) as exc:
            record.update(error={"type": type(exc).__name__, "message": str(exc)},
                          elapsed_ms=(time.monotonic() - started) * 1000)
            raise

    def close(self):
        if self.client is not None:
            self.client.close()
            self.client = None

    def report_error(self, exc):
        self.result["errors"].append({"phase": self.phase, "type": type(exc).__name__, "message": str(exc)})

    def recover(self, before_uptime=None, boot_epoch_upper=None):
        self.close()
        deadline = time.monotonic() + self.args.recovery_timeout
        record = {"phase": self.phase, "attempts": [], "completed": False}
        self.result["recovery"].append(record)
        for _ in range(self.args.recovery_attempts):
            if time.monotonic() >= deadline:
                break
            attempt = {"since_start_s": time.monotonic() - self.started}
            record["attempts"].append(attempt)
            try:
                options = copy.copy(self.args)
                # connect() includes socket open and one sentinel exchange.
                options.timeout = min(self.args.timeout, max(.001, (deadline - time.monotonic()) / 2))
                self.client = self.connector(options, self.result["observations"])
                info = self.request("GET_DEVICE_INFO", timeout=max(.001, min(self.args.timeout, deadline - time.monotonic())))
                require_native(info)
                stats_started = time.monotonic()
                stats = self.request("STATS", timeout=max(.001, min(self.args.timeout, deadline - time.monotonic())))
                stats_finished = time.monotonic()
                if stats.get("storage_ready") is not True:
                    raise AcceptanceError("native storage is not ready")
                uptime = stats.get("uptime_ms")
                if not isinstance(uptime, int) or isinstance(uptime, bool) or uptime < 0:
                    raise AcceptanceError("native STATS has invalid uptime_ms")
                boot_lower = stats_started - uptime / 1000
                attempt.update(info=info, stats=stats,
                               boot_epoch_bounds_s=[boot_lower, stats_finished - uptime / 1000])
                # STATS is sampled between send and receive. Disjoint boot
                # epoch intervals prove a reset even when reconnecting after
                # an early reboot takes longer than its previous uptime.
                reboot_proof = ("uptime decreased" if before_uptime is not None and uptime < before_uptime else
                                "boot epoch intervals advanced" if boot_epoch_upper is not None
                                and boot_lower > boot_epoch_upper + .002 else None)
                if before_uptime is None or reboot_proof:
                    record["completed"] = True
                    if reboot_proof:
                        record["reboot_proof"] = reboot_proof
                    return info, stats
                attempt["waiting_for_expected_reboot"] = True
            except (Exception, KeyboardInterrupt) as exc:
                attempt["error"] = {"type": type(exc).__name__, "message": str(exc)}
                if not transient(exc):
                    self.close()
                    raise
            self.close()
            time.sleep(min(self.args.retry_interval, max(0, deadline - time.monotonic())))
        raise TimeoutError("bounded native recovery failed" + (" to prove the expected uptime reset" if before_uptime is not None else ""))

    def info_projection(self, info, change=None):
        require_native(info)
        for key in ("device", "profile", "preset_navigation", "tft_colors", "tft_labels"):
            if key not in self.snapshot["info"]:
                continue
            expected = copy.deepcopy(self.snapshot["info"][key])
            if key == "tft_colors" and change:
                expected[change["field"]] = change["temporary"]
            equal(info.get(key), expected, "DEVICE_INFO." + key)

    def compare_snapshot(self):
        for record in self.snapshot["globals"] + self.snapshot["patches"]:
            request = record["request"]
            kind = request["type"]
            fields = {key: request[key] for key in ("profile", "bank", "slot") if key in request}
            actual = self.request(kind, **fields)
            field = "device" if kind == "GET_GLOBAL" else "patch"
            equal(actual.get(field), record["response"][field], "%s %s" % (kind, fields))
        self.info_projection(self.request("GET_DEVICE_INFO"))
        self.result["checks"][self.phase + "_snapshot_equal"] = True

    def verify_device(self, device, change=None):
        equal(self.request("GET_GLOBAL").get("device"), device, "active GET_GLOBAL")
        self.info_projection(self.request("GET_DEVICE_INFO"), change)

    def reboot(self):
        before = self.request("STATS")["uptime_ms"]
        boot_epoch_upper = time.monotonic() - before / 1000
        record = {"phase": self.phase, "mode": "normal", "uptime_before_ms": before,
                  "boot_epoch_before_upper_s": boot_epoch_upper, "ack_received": False, "proved": False}
        self.result["expected_reboots"].append(record)
        try:
            self.request("REBOOT", mode="normal")
            record["ack_received"] = True
        except Exception as exc:
            if not transient(exc):
                raise
            record["ack_disconnect_or_timeout"] = {"type": type(exc).__name__, "message": str(exc)}
        # A reconnect/readback alone does not prove flash persistence. Require
        # an uptime decrease or disjoint inferred boot-time intervals; a lost
        # REBOOT command cannot pass just because a new connection opened.
        _, after = self.recover(before, boot_epoch_upper)
        record.update(proved=True, uptime_after_ms=after["uptime_ms"],
                      proof=self.result["recovery"][-1]["reboot_proof"])

    def restore(self):
        self.phase = "restore_acquire"
        self.recover()
        self.phase = "restore_write"
        try:
            self.request("PUT_GLOBAL", device=self.snapshot["original"])
        except Exception as exc:
            if not transient(exc):
                raise
            self.result["restoration"]["ack_disconnect_or_timeout"] = {"type": type(exc).__name__, "message": str(exc)}
            self.recover()
        self.phase = "restore_readback"
        self.verify_device(self.snapshot["original"])
        self.phase = "restore_reboot"
        self.reboot()
        self.phase = "restore_persisted_readback"
        self.verify_device(self.snapshot["original"])
        self.result["restoration"]["original_json_persisted"] = True

    def restore_rig(self):
        self.phase = "restore_rig"
        if self.client is None:
            self.recover()
        try:
            self.request("SWITCH_PATCH", bank=self.original_rig[0], slot=self.original_rig[1])
        except Exception as exc:
            if not transient(exc):
                raise
            self.result["restoration"]["rig_ack_disconnect_or_timeout"] = {"type": type(exc).__name__, "message": str(exc)}
            self.recover()
        # Use the benchmark's strict correlated context+fresh rig-name check.
        confirmation = benchmark.confirmed(self.client, *self.original_rig,
                                           self.args.timeout, verify_kemper=True)
        self.result["restoration"]["rig_confirmation"] = confirmation
        self.result["restoration"]["original_rig_restored"] = True

    def run(self):
        try:
            self.recover()
            self.phase = "initial_compare"
            self.compare_snapshot()
            context = self.request("GET_CONTEXT")["context"]
            self.original_rig = (context["bank"], context["slot"])
            self.result["original_live_context"] = context
            if self.args.exercise_writes:
                if context.get("preview") not in (None, "", "off", False) or context.get("kemper_connected") != "on":
                    raise AcceptanceError("write exercise requires connected Kemper and no active preview")
                modified, change = changed_color(self.snapshot["original"])
                self.result["temporary_change"] = change
                self.phase = "temporary_write"
                self.result["restoration"]["needed"] = True  # A lost ACK may still have committed.
                self.request("PUT_GLOBAL", device=modified)
                self.phase = "temporary_readback"
                self.verify_device(modified, change)
                self.phase = "temporary_reboot"
                self.reboot()
                self.phase = "temporary_persisted_readback"
                self.verify_device(modified, change)
                self.result["checks"]["temporary_json_persisted"] = True
        except (Exception, KeyboardInterrupt) as exc:
            self.report_error(exc)
        finally:
            if self.result["restoration"]["needed"]:
                try:
                    self.restore()
                except (Exception, KeyboardInterrupt) as exc:
                    self.report_error(exc)
                # Rig restoration is independent: attempt it even if a JSON
                # restore/readback failed, provided native identity is known.
                try:
                    self.restore_rig()
                except (Exception, KeyboardInterrupt) as exc:
                    self.report_error(exc)
                if self.client:
                    try:
                        self.phase = "final_compare"
                        self.compare_snapshot()
                    except (Exception, KeyboardInterrupt) as exc:
                        self.report_error(exc)
                restoration = self.result["restoration"]
                restoration["succeeded"] = bool(restoration.get("original_json_persisted")
                                                and restoration.get("original_rig_restored")
                                                and self.result["checks"].get("final_compare_snapshot_equal"))
            self.close()
        self.result["elapsed_s"] = time.monotonic() - self.started
        self.result["passed"] = not self.result["errors"]
        return self.result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9876)
    parser.add_argument("--serial")
    parser.add_argument("--timeout", type=float, default=8)
    parser.add_argument("--recovery-timeout", type=float, default=45)
    parser.add_argument("--recovery-attempts", type=int, default=60)
    parser.add_argument("--retry-interval", type=float, default=.5)
    parser.add_argument("--exercise-writes", action="store_true")
    args = parser.parse_args(argv)
    for key, lower, upper in (("timeout", .01, 120), ("recovery_timeout", .01, 120),
                              ("recovery_attempts", 1, 300), ("retry_interval", 0, 5), ("port", 1, 65535)):
        if not math.isfinite(getattr(args, key)) or not lower <= getattr(args, key) <= upper:
            parser.error("%s must be within [%s, %s]" % (key, lower, upper))
    return args


def main(argv=None):
    args = parse_args(argv)
    snapshot = load_snapshot(args.snapshot)
    with Path(args.output).open("x", encoding="utf-8") as output:
        result = Acceptance(args, snapshot).run()
        json.dump(result, output, indent=2, ensure_ascii=False)
        output.write("\n")
    print(json.dumps({"passed": result["passed"], "output": args.output,
                      "checks": result["checks"], "restoration": result["restoration"],
                      "errors": result["errors"]}, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
