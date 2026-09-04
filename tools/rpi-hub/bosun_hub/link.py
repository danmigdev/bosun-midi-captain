"""
Upstream link: the hub's single owned connection to the MIDI Captain's
data USB-CDC port, speaking the Bosun line-JSON protocol.

Why a dedicated thread and not asyncio: pyserial has no usable async
API, and the Android bosun backend
(editor/src-tauri/src/serial/android.rs) established that one thread
doing every read and write, with explicit stall detection and a hard
reopen, is the shape that survives a pedal reboot, a USB
re-enumeration, or a silent half-dead link. This is a port of that
logic to the Pi.

The thread:

  - walks a list of candidate targets (auto-discovered ``/dev/ttyACM*``
    in descending order, or a single ``tcp://host:port`` address for
    development against tools/tcp_firmware_emulator.py) and runs a
    PING/ACK sentinel handshake on each until one answers as the
    protocol port. The console CDC echoes bytes but never returns
    ``{"type":"ACK"}``, so it is skipped automatically.
  - discards everything received before the sentinel ACK: that is stale
    backlog from a previous session, and dropping it is what makes a
    reconnect start from clean state (same reasoning as
    serial/desktop.rs and serial_tcp_bridge.py).
  - reads bytes, splits on newlines, hands each complete line to
    ``on_line``.
  - drains a write queue, one command per iteration.
  - sends its own keepalive PING when the link has been idle, so a
    silent Stage feed (minutes can pass with no legitimate traffic) is
    not mistaken for a dead port. Replies to the hub's own ``__hub_*``
    request ids are swallowed, not broadcast.
  - reopens the port on a read/write error, on no traffic in either
    direction for ``STALL_S``, or on several writes in a row with no
    intervening read (the "write-only black hole" the Android backend
    also had to special-case). Closing a serial port drops DTR, which
    resets the RP2040 and forces re-enumeration.
"""

from __future__ import annotations

import glob
import json
import logging
import queue
import socket
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("bosun_hub.link")

BAUD = 115200
SYNC_TIMEOUT_SERIAL = 12.0  # the RP2040 reboots on the DTR edge when we open
SYNC_TIMEOUT_TCP = 8.0
KEEPALIVE_S = 6.0
STALL_S = 20.0
WRITE_ONLY_STALL = 5  # consecutive writes with zero reads in between -> reopen
REOPEN_BACKOFF = (0.5, 1.0, 2.0, 3.0, 5.0)
READ_CHUNK = 4096
RX_BUF_MAX = 1 << 20  # 1 MiB; matches the firmware's own overflow guard intent

_HUB_ID_PREFIX = "__hub_"


# --------------------------------------------------------------------------
# byte transports
# --------------------------------------------------------------------------


class Transport:
    """Minimal byte pipe. ``read`` returns ``b""`` on timeout and raises on
    a real error; ``write`` blocks briefly and raises on failure."""

    name = "?"

    def read(self, n: int) -> bytes:
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class SerialTransport(Transport):
    def __init__(self, path: str) -> None:
        import serial  # lazy: developing against tcp:// needs no pyserial

        self.name = path
        self._s = serial.Serial()
        self._s.port = path
        self._s.baudrate = BAUD
        self._s.timeout = 0.05
        self._s.write_timeout = 1.0
        # CircuitPython's CDC needs DTR asserted to treat the host as
        # "connected"; the DTR edge also soft-resets the RP2040.
        self._s.dtr = True
        self._s.rts = True
        self._s.open()
        try:
            self._s.reset_input_buffer()
        except Exception:
            pass

    def read(self, n: int) -> bytes:
        return self._s.read(n)

    def write(self, data: bytes) -> None:
        self._s.write(data)
        self._s.flush()

    def close(self) -> None:
        try:
            self._s.close()
        except Exception:
            pass


class TcpTransport(Transport):
    def __init__(self, host: str, port: int) -> None:
        self.name = f"tcp://{host}:{port}"
        self._sock = socket.create_connection((host, port), timeout=5.0)
        self._sock.settimeout(0.05)

    def read(self, n: int) -> bytes:
        try:
            data = self._sock.recv(n)
        except (socket.timeout, TimeoutError):
            return b""
        if data == b"":
            raise ConnectionError("peer closed the connection")
        return data

    def write(self, data: bytes) -> None:
        self._sock.settimeout(2.0)
        try:
            self._sock.sendall(data)
        finally:
            self._sock.settimeout(0.05)

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass


def _open_transport(target: str) -> Transport:
    if target.startswith("tcp://"):
        rest = target[len("tcp://") :]
        host, _, port = rest.partition(":")
        return TcpTransport(host, int(port or "9876"))
    return SerialTransport(target)


# --------------------------------------------------------------------------
# candidate discovery
# --------------------------------------------------------------------------


def discover_candidates(explicit: Optional[str]) -> list[str]:
    """Return the ordered list of targets the link should try.

    An explicit target (a device path or a ``tcp://`` address) is used
    as-is. Otherwise every ``/dev/ttyACM*`` is a candidate, highest
    number first: on the Captain's composite CDC the data interface
    enumerates after the console, so the higher ``ttyACMx`` is the one
    we want, and opening the console CDC would reset CircuitPython.
    """

    if explicit:
        return [explicit]
    ports = sorted(
        glob.glob("/dev/ttyACM*"),
        key=lambda p: int("".join(c for c in p if c.isdigit()) or "0"),
        reverse=True,
    )
    return ports


# --------------------------------------------------------------------------
# the link
# --------------------------------------------------------------------------


class UpstreamLink:
    """Single-threaded owner of the Captain data port.

    Construct with callbacks, then :meth:`start`. All public methods are
    safe to call from any thread.
    """

    def __init__(
        self,
        target: Optional[str],
        on_line: Callable[[str], None],
        on_state: Callable[[bool, str], None] = lambda up, detail: None,
    ) -> None:
        self._target = target
        self._on_line = on_line
        self._on_state = on_state

        self._tx: "queue.Queue[str]" = queue.Queue(maxsize=256)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._transport: Optional[Transport] = None
        self._rx = bytearray()
        self._connected = False

    # -- public API --------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="bosun-hub-link", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    @property
    def connected(self) -> bool:
        return self._connected

    def send(self, line: str) -> None:
        """Queue one protocol line for the pedal. Non-blocking; a full
        queue means the pedal is not draining and the oldest command is
        dropped rather than blocking a network handler."""

        line = line.rstrip("\r\n") + "\n"
        try:
            self._tx.put_nowait(line)
        except queue.Full:
            try:
                self._tx.get_nowait()
            except queue.Empty:
                pass
            try:
                self._tx.put_nowait(line)
            except queue.Full:
                log.warning("tx queue full, dropping command")

    # -- thread ----------------------------------------------------------

    def _run(self) -> None:
        backoff = iter(REOPEN_BACKOFF)
        while not self._stop.is_set():
            try:
                if self._open_and_sync():
                    backoff = iter(REOPEN_BACKOFF)
                    self._pump()
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                log.warning("link error: %s", exc)
            finally:
                self._set_state(False, "closed")
                if self._transport is not None:
                    self._transport.close()
                    self._transport = None
                self._rx.clear()
            if self._stop.is_set():
                break
            try:
                delay = next(backoff)
            except StopIteration:
                delay = REOPEN_BACKOFF[-1]
            self._stop.wait(delay)

    def _open_and_sync(self) -> bool:
        candidates = discover_candidates(self._target)
        if not candidates:
            log.info("no candidate ports (waiting for /dev/ttyACM*)")
            self._stop.wait(2.0)
            return False

        for target in candidates:
            if self._stop.is_set():
                return False
            try:
                transport = _open_transport(target)
            except Exception as exc:  # noqa: BLE001
                log.debug("open %s failed: %s", target, exc)
                continue
            if self._sentinel_sync(transport):
                self._transport = transport
                self._set_state(True, transport.name)
                log.info("linked to %s", transport.name)
                return True
            transport.close()
            log.debug("%s did not answer the protocol sentinel", target)
        return False

    def _sentinel_sync(self, transport: Transport) -> bool:
        """Send a uniquely-tagged PING and read until its ACK, discarding
        everything before it. Returns False if no ACK arrives in time."""

        marker = f"{_HUB_ID_PREFIX}sync_{int(time.time() * 1000)}"
        deadline = time.monotonic() + (
            SYNC_TIMEOUT_TCP
            if transport.name.startswith("tcp://")
            else SYNC_TIMEOUT_SERIAL
        )
        try:
            transport.write(b'\n{"type":"PING","id":"' + marker.encode() + b'"}\n')
        except Exception as exc:  # noqa: BLE001
            log.debug("sentinel write failed: %s", exc)
            return False

        buf = bytearray()
        while time.monotonic() < deadline and not self._stop.is_set():
            try:
                chunk = transport.read(READ_CHUNK)
            except Exception as exc:  # noqa: BLE001
                log.debug("sentinel read failed: %s", exc)
                return False
            if not chunk:
                continue
            buf.extend(chunk)
            # Accept both compact and space-after-colon JSON, same as the
            # Rust and Android sentinel matchers.
            text = buf.decode("utf-8", "replace")
            if marker in text and '"type":"ACK"' in text.replace('"type": "ACK"', '"type":"ACK"'):
                return True
        return False

    def _pump(self) -> None:
        assert self._transport is not None
        transport = self._transport
        last_rx = time.monotonic()
        last_tx = time.monotonic()
        writes_since_rx = 0

        while not self._stop.is_set():
            now = time.monotonic()

            # -- write one queued command ---------------------------------
            try:
                line = self._tx.get_nowait()
            except queue.Empty:
                line = None
            if line is None and now - max(last_rx, last_tx) >= KEEPALIVE_S:
                line = '{"type":"PING","id":"%skeepalive"}\n' % _HUB_ID_PREFIX
            if line is not None:
                transport.write(line.encode("utf-8"))
                last_tx = now
                writes_since_rx += 1
                if writes_since_rx >= WRITE_ONLY_STALL:
                    raise ConnectionError("write-only stall (pedal not answering)")

            # -- read ---------------------------------------------------
            chunk = transport.read(READ_CHUNK)
            if chunk:
                last_rx = time.monotonic()
                writes_since_rx = 0
                self._feed_bytes(chunk)
            elif now - last_rx >= STALL_S:
                raise ConnectionError(f"no data for {STALL_S:.0f}s")

            if not chunk and line is None:
                time.sleep(0.01)

    def _feed_bytes(self, chunk: bytes) -> None:
        self._rx.extend(chunk)
        if len(self._rx) > RX_BUF_MAX:
            cut = self._rx.rfind(b"\n")
            del self._rx[: cut + 1 if cut >= 0 else len(self._rx)]
            log.warning("rx buffer overflow, dropped a partial line")
        while True:
            nl = self._rx.find(b"\n")
            if nl < 0:
                break
            raw = bytes(self._rx[:nl])
            del self._rx[: nl + 1]
            line = raw.decode("utf-8", "replace").strip("\r")
            if not line:
                continue
            if self._is_own_reply(line):
                continue
            self._on_line(line)

    @staticmethod
    def _is_own_reply(line: str) -> bool:
        """True for ACKs to the hub's own keepalive/sync pings, which must
        not reach downstream consumers."""

        if _HUB_ID_PREFIX not in line:
            return False
        try:
            obj = json.loads(line)
        except ValueError:
            return False
        return isinstance(obj, dict) and str(obj.get("id", "")).startswith(_HUB_ID_PREFIX)

    def _set_state(self, up: bool, detail: str) -> None:
        if up == self._connected:
            return
        self._connected = up
        try:
            self._on_state(up, detail)
        except Exception:  # noqa: BLE001
            log.exception("on_state callback raised")
