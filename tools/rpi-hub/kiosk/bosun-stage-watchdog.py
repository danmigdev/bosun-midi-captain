#!/usr/bin/env python3
"""Restart the Stage kiosk when its page no longer runs.

A page process that dies leaves Chromium on its own error screen, and a Pi
without a network never leaves "This site can't be reached". cage and Chromium
still run, so systemd sees nothing wrong. The Stage page tells the hub every few
seconds that it runs; without that sign this service ends cage and systemd
starts the kiosk again. It never touches the hub, MIDI or the Captain.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time
import urllib.request


LOG = logging.getLogger("bosun.stage")
HUB = "http://127.0.0.1:8080"
# Only a Stage bundle that reports itself alive can be missed.
MARKER = b'name="bosun-kiosk-heartbeat"'
CHECK_SECONDS = 5.0
# Four missed beats.
SILENT_SECONDS = 20.0
# From the kiosk start to the first beat: the launcher's wait for the hub, then
# Chromium on a Pi 3 with cold caches.
LOAD_SECONDS = 120.0
MAX_LOAD_SECONDS = 900.0
STOP_SECONDS = 10.0


class PageHealth:
    """Decides when a kiosk whose page gives no sign of life must restart."""

    def __init__(self):
        self.pid = 0
        self.since = 0.0
        self.deadline = 0.0
        self.alive = False
        self.load_seconds = LOAD_SECONDS

    def observe(self, pid, age, now):
        """``pid`` is cage (0: stopped), ``age`` seconds since the last beat or None."""
        if pid != self.pid:
            self.pid, self.since, self.alive = pid, now, False
            self.deadline = now + self.load_seconds
        # The previous kiosk's last beat says nothing about this one.
        if age is not None and now - age >= self.since:
            self.alive = True
            self.load_seconds = LOAD_SECONDS
            self.deadline = now - age + SILENT_SECONDS
        return bool(pid) and now >= self.deadline

    def hub_silent(self, pid, now):
        """Without the hub nobody receives beats; give the page time once it is back."""
        self.observe(pid, None, now)
        self.deadline = max(self.deadline, now + SILENT_SECONDS)

    def restarting(self):
        """A kiosk that never shows Stage is retried ever less often."""
        if not self.alive:
            self.load_seconds = min(self.load_seconds * 2, MAX_LOAD_SECONDS)


def kiosk_pid():
    result = subprocess.run(
        ["systemctl", "show", "--property=MainPID", "--value", "bosun-kiosk.service"],
        capture_output=True, text=True, check=True, timeout=5,
    )
    return int(result.stdout.strip() or 0)


def fetch(path):
    with urllib.request.urlopen(HUB + path, timeout=3) as response:
        return response.read()


def running(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def restart_kiosk(pid, *, kill=os.kill, alive=running, pause=time.sleep):
    """End cage; Restart=always in bosun-kiosk.service brings the kiosk back."""
    try:
        kill(pid, signal.SIGTERM)
        for _ in range(int(STOP_SECONDS / 0.5)):
            if not alive(pid):
                return
            pause(0.5)
        LOG.warning("cage ignored the stop request; killing it")
        kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # already gone, systemd is restarting it


def main():
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    health = PageHealth()
    checked, supported = 0, False
    while True:
        time.sleep(CHECK_SECONDS)
        try:
            pid = kiosk_pid()
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            LOG.warning("Cannot read the kiosk's state: %s", exc)
            continue
        try:
            if pid != checked:
                supported = MARKER in fetch("/")
                checked = pid
            age = json.loads(fetch("/kiosk-alive"))["age"] if supported else None
        except (OSError, ValueError, KeyError, TypeError):
            health.hub_silent(pid, time.monotonic())
            continue
        if health.observe(pid, age, time.monotonic()) and supported:
            LOG.warning("Restarting the kiosk: the Stage page %s",
                        "stopped running" if health.alive else "did not start")
            health.restarting()
            restart_kiosk(pid)


if __name__ == "__main__":
    main()
