"""
Wire the MIDI Captain and the Kemper Player together in the ALSA
sequencer, both directions, by matching client names.

This is the whole "MIDI hub": once the two USB-MIDI devices are
connected in the kernel sequencer, MIDI (including the Kemper SysEx
beacon and its sensing frames) flows between them with no userspace
process in the path. The Captain firmware generates the beacon itself
(firmware/lib/plugins/kemper.py tick()), so nothing here has to.

Run it from udev on every sound-card add/change and, as a backstop,
from a short systemd timer. ``aconnect`` of an already-connected pair
just fails harmlessly, so re-running costs nothing.

Clock and active-sensing are NOT filtered (aconnect has no filter). If
the Player floods clock and it upsets the Captain, the fallback is a
small userspace forwarder; see the plan's risk R4.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys

log = logging.getLogger("bosun_hub.midi_connect")

# Substring/regex patterns for the two endpoints, case-insensitive.
KEMPER_PAT = re.compile(r"profiler|kemper", re.I)
CAPTAIN_PAT = re.compile(r"circuitpython|midi ?captain|\bcaptain\b|bosun|pico|rp2040", re.I)
# Never treat these as an endpoint.
EXCLUDE_PAT = re.compile(r"through|bcm2835|system|announce|timer", re.I)

_CLIENT_RE = re.compile(r"^client (\d+): '([^']*)'")
_PORT_RE = re.compile(r"^\s+(\d+) '")


class Endpoint:
    def __init__(self, client: int, name: str) -> None:
        self.client = client
        self.name = name
        self.ports: list[int] = []

    def __repr__(self) -> str:
        return f"{self.client}:{self.ports} '{self.name}'"


def list_endpoints() -> list[Endpoint]:
    out = subprocess.run(
        ["aconnect", "-l"], capture_output=True, text=True, check=True
    ).stdout
    endpoints: list[Endpoint] = []
    current: Endpoint | None = None
    for line in out.splitlines():
        m = _CLIENT_RE.match(line)
        if m:
            current = Endpoint(int(m.group(1)), m.group(2).strip())
            endpoints.append(current)
            continue
        m = _PORT_RE.match(line)
        if m and current is not None:
            current.ports.append(int(m.group(1)))
    return endpoints


def _find(endpoints: list[Endpoint], pattern: re.Pattern[str]) -> Endpoint | None:
    for ep in endpoints:
        if EXCLUDE_PAT.search(ep.name):
            continue
        if pattern.search(ep.name) and ep.ports:
            return ep
    return None


def _connect(src: Endpoint, dst: Endpoint) -> None:
    sp, dp = src.ports[0], dst.ports[0]
    r = subprocess.run(
        ["aconnect", f"{src.client}:{sp}", f"{dst.client}:{dp}"],
        capture_output=True,
        text=True,
    )
    if r.returncode == 0:
        log.info("connected %s:%d -> %s:%d", src.name, sp, dst.name, dp)
    else:
        # "Connection is already subscribed" is the normal idempotent case.
        msg = (r.stderr or r.stdout).strip()
        if "already" in msg.lower():
            log.debug("%s:%d -> %s:%d already connected", src.name, sp, dst.name, dp)
        else:
            log.warning("aconnect %s -> %s failed: %s", src.name, dst.name, msg)


def wire() -> int:
    endpoints = list_endpoints()
    log.debug("endpoints: %s", endpoints)
    kemper = _find(endpoints, KEMPER_PAT)
    captain = _find(endpoints, CAPTAIN_PAT)
    if kemper is None or captain is None:
        log.info(
            "waiting for both endpoints (kemper=%s captain=%s)",
            kemper.name if kemper else None,
            captain.name if captain else None,
        )
        return 1
    _connect(captain, kemper)
    _connect(kemper, captain)
    return 0


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="bosun-midi-connect: %(levelname)s %(message)s"
    )
    if "-v" in sys.argv:
        logging.getLogger("bosun_hub.midi_connect").setLevel(logging.DEBUG)
    sys.exit(wire())


if __name__ == "__main__":
    main()
