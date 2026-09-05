#!/usr/bin/env python3
"""Verify the live Captain protocol after a firmware deployment."""

import argparse
import json
import math
import time

from push_firmware import open_transport


def _write_all(transport, payload):
    view = memoryview(payload)
    while view:
        written = transport.write(view)
        if not isinstance(written, int) or written <= 0:
            raise OSError("transport write made no progress")
        view = view[written:]


def call(transport, rx_buffer, message, deadline):
    """Send one request within an absolute, verification-wide deadline."""
    payload = (json.dumps(message) + "\n").encode()
    _write_all(transport, payload)

    while time.monotonic() < deadline:
        while b"\n" in rx_buffer:
            raw, _, tail = bytes(rx_buffer).partition(b"\n")
            rx_buffer[:] = tail
            try:
                reply = json.loads(raw.strip())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(reply, dict) and reply.get("id") == message["id"]:
                return reply
        chunk = transport.read(4096)
        if chunk:
            rx_buffer.extend(chunk)
        else:
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    raise TimeoutError("no response to %s#%s" %
                       (message["type"], message["id"]))


def _close_quietly(transport):
    if transport is not None:
        try:
            transport.close()
        except OSError:
            pass


def wait_for_initial_ping(port, deadline):
    """Open the hub route and wait out only its transient ``link_down``.

    A newly active hub can accept ``socket://`` connections before its serial
    worker owns ACM1. That narrow readiness state is retryable. Any other
    correlated firmware ERROR is actionable and must fail immediately.
    """
    transport = None
    rx_buffer = bytearray()
    attempt = 0
    last_error = "transport has not opened"
    while time.monotonic() < deadline:
        if transport is None:
            remaining = deadline - time.monotonic()
            try:
                transport = open_transport(
                    port,
                    timeout=min(0.1, remaining),
                    write_timeout=min(2.0, remaining),
                )
                rx_buffer = bytearray()
                _write_all(transport, b"\n")
            except OSError as error:
                last_error = "connect failed: %s" % error
                _close_quietly(transport)
                transport = None
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                continue

        attempt += 1
        request = {
            "type": "PING", "id": "verify-ping-%d" % (attempt - 1),
        }
        try:
            reply = call(transport, rx_buffer, request, deadline)
        except OSError as error:
            last_error = "readiness connection failed: %s" % error
            _close_quietly(transport)
            transport = None
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            continue

        if reply.get("type") == "ACK":
            return transport, rx_buffer, reply, attempt
        if (reply.get("type") == "ERROR"
                and reply.get("error") == "link_down"):
            last_error = "hub reported link_down"
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            continue
        _close_quietly(transport)
        raise RuntimeError("PING returned %r, expected ACK" % (reply,))

    _close_quietly(transport)
    raise TimeoutError("Captain runtime did not become ready: " + last_error)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument(
        "--standalone", action="store_true",
        help="verify the Captain/Kemper runtime without Stage bootstrap data",
    )
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be positive and finite")

    deadline = time.monotonic() + args.timeout

    if args.standalone:
        requests = tuple(
            ("ping-%d" % index,
             {"type": "PING", "id": "verify-ping-%d" % index}, "ACK")
            for index in range(1, 20)
        ) + (("context", {"type": "GET_CONTEXT", "id": "verify-context"},
              "CONTEXT"),)
    else:
        requests = (
            ("device", {"type": "GET_DEVICE_INFO", "id": "verify-device"},
             "DEVICE_INFO"),
            ("stats", {"type": "STATS", "id": "verify-stats"}, "STATS"),
            ("patches", {"type": "LIST_PATCHES", "id": "verify-patches"},
             "PATCH_LIST"),
        )

    transport, rx_buffer, ready_reply, _ = wait_for_initial_ping(
        args.port, deadline,
    )
    replies = {"ping-0" if args.standalone else "ping": ready_reply}
    try:
        for name, request, expected_type in requests:
            reply = call(transport, rx_buffer, request, deadline)
            if reply.get("type") != expected_type:
                raise RuntimeError(
                    "%s returned %r, expected %s" %
                    (request["type"], reply, expected_type)
                )
            replies[name] = reply
    finally:
        transport.close()

    if args.standalone:
        context = replies["context"].get("context")
        if not isinstance(context, dict):
            raise RuntimeError("GET_CONTEXT has no context object")
        kemper = {key: value for key, value in context.items()
                  if key.startswith("kemper_")}
        print(json.dumps({"pings": 20, "context": kemper},
                         indent=2, sort_keys=True))
        return

    if "preset_navigation" not in replies["device"]:
        raise RuntimeError("DEVICE_INFO is from stale firmware")
    if not isinstance(replies["patches"].get("patches"), list):
        raise RuntimeError("PATCH_LIST has no patches array")

    summary = {
        "fw": replies["device"].get("fw"),
        "profile": replies["device"].get("profile"),
        "current": replies["device"].get("current"),
        "mem_free": replies["stats"].get("mem_free"),
        "mem_alloc": replies["stats"].get("mem_alloc"),
        "max_tick_ms": replies["stats"].get("max_tick_ms"),
        "patches": len(replies["patches"]["patches"]),
        "preset_navigation": replies["device"]["preset_navigation"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
