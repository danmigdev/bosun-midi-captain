#!/usr/bin/env python3
"""Rearm HDMI once after boot/hotplug without restarting Cage, Chromium or MIDI.

Some separately powered panels advertise EDID before they can display the signal.
KMS and Stage then look healthy while the panel remains black. A short HDMI
off/on cycle fixes that handshake. Keep kanshi as the normal output-policy owner,
but stop it during recovery so it cannot undo the deliberate off interval.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import time


LOG = logging.getLogger("bosun.hdmi")
SETTLE_SECONDS = 3.0
OFF_SECONDS = 2.0
RETRY_SECONDS = 5.0
MAX_ATTEMPTS = 3


class RecoverySchedule:
    def __init__(self):
        self.connected = frozenset()
        self.due = None
        self.attempts = 0

    def observe(self, connected, hotplug, now):
        connected = frozenset(connected)
        if connected != self.connected or hotplug:
            self.connected = connected
            self.attempts = 0
            self.due = now + SETTLE_SECONDS if connected else None

    def ready(self, now):
        return self.due is not None and now >= self.due

    def complete(self, success, now):
        self.attempts += 1
        self.due = (now + RETRY_SECONDS
                    if not success and self.attempts < MAX_ATTEMPTS else None)


def connected_hdmi():
    names = set()
    for path in Path("/sys/class/drm").glob("card*-HDMI-A-*/status"):
        try:
            if path.read_text().strip() == "connected":
                names.add(path.parent.name.split("-", 1)[1])
        except OSError:
            # Removal can race a sysfs read; the next event/check reconciles it.
            continue
    return names


def randr(*arguments):
    result = subprocess.run(
        ["wlr-randr", *arguments], capture_output=True, text=True,
        check=True, timeout=5,
    )
    return json.loads(result.stdout) if arguments == ("--json",) else None


class Policy:
    def __init__(self, config):
        self.config = config
        self.process = None

    def start(self):
        self.process = subprocess.Popen(["kanshi", "--config", self.config])

    def stop(self):
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=1)
        self.process = None


def rearm_hdmi(policy, *, query=randr, connected=connected_hdmi, pause=time.sleep):
    """Always return output ownership to kanshi, including on unplug/failure."""
    outputs = query("--json")
    present = connected()
    hdmi = next((o["name"] for o in outputs if o["name"] in present), None)
    if hdmi is None:
        # The DRM event can precede Cage publishing the Wayland output.
        return not present
    if not any(o["name"] == "HEADLESS-1" for o in outputs):
        LOG.warning("HDMI recovery requires the kiosk's virtual fallback output")
        return False

    policy.stop()
    try:
        query("--output", "HEADLESS-1", "--on", "--custom-mode", "1920x440@60Hz",
              "--pos", "0,0", "--output", hdmi, "--off")
        pause(OFF_SECONDS)
        if hdmi not in connected():
            LOG.info("HDMI disconnected during recovery; keeping virtual Stage")
            return True
        query("--output", hdmi, "--on", "--preferred", "--pos", "0,0",
              "--output", "HEADLESS-1", "--off")
        LOG.info("HDMI rearmed on %s using the panel's preferred mode", hdmi)
        return True
    finally:
        # Restores the ordinary HDMI/fallback policy even after a failed modeset.
        policy.start()


def drain_hotplug(monitor):
    hotplug = False
    while True:
        try:
            data, sender = monitor.recvfrom(65536)
        except BlockingIOError:
            return hotplug
        fields = set(data.split(b"\0"))
        # Only accept kernel-originated DRM hotplug notifications.
        if sender[0] == 0 and {b"SUBSYSTEM=drm", b"HOTPLUG=1"} <= fields:
            hotplug = True


def main(config):
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    policy = Policy(config)
    schedule = RecoverySchedule()
    monitor = None

    def stop(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        try:
            # NETLINK_KOBJECT_UEVENT group 1 is available to the bosun account.
            monitor = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, 15)
            monitor.bind((os.getpid(), 1))
            monitor.setblocking(False)
        except OSError:
            if monitor is not None:
                monitor.close()
            monitor = None
            LOG.warning("DRM event monitor unavailable; using connector checks")

        policy.start()
        while True:
            if policy.process.poll() is not None:
                raise RuntimeError("kanshi exited; restarting the output service")
            hotplug = drain_hotplug(monitor) if monitor is not None else False
            schedule.observe(connected_hdmi(), hotplug, time.monotonic())
            if schedule.ready(time.monotonic()):
                try:
                    success = rearm_hdmi(policy)
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    LOG.warning("HDMI recovery attempt failed: %s", exc)
                    success = False
                schedule.complete(success, time.monotonic())
                if not success and schedule.due is None:
                    LOG.error("HDMI recovery exhausted %d attempts; normal output policy remains active",
                              MAX_ATTEMPTS)
            # The fallback reads a small sysfs status file, not the framebuffer
            # or wlr-randr. Healthy idle operation never cycles the HDMI signal.
            delay = 2.0
            if schedule.due is not None:
                delay = min(delay, max(0.0, schedule.due - time.monotonic()))
            if monitor is None:
                time.sleep(delay)
            else:
                select.select([monitor], [], [], delay)
    finally:
        policy.stop()
        if monitor is not None:
            monitor.close()


if __name__ == "__main__":
    main(sys.argv[1])
