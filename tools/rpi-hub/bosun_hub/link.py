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
  - drains a write queue in-order, retaining any non-blocking partial write
    suffix while interleaving reads between fragments.
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
import errno
import json
import logging
import os
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
WRITE_ONLY_GRACE_S = 3.0  # a normal bootstrap can enqueue >5 commands at once
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

    def write_some(self, data: bytes) -> int:
        """Write without requiring callers to replay an unknown prefix.

        Stream transports which cannot expose partial progress may keep the
        all-or-error :meth:`write` contract.  SerialTransport overrides this
        with a genuinely non-blocking write so the pump can alternate USB TX
        with RX while the Captain is producing a large response.
        """

        self.write(data)
        return len(data)

    def read_available(self, n: int) -> bytes:
        """Return bytes already buffered by the OS, without waiting."""

        return b""

    def close(self) -> None:
        raise NotImplementedError


class SerialTransport(Transport):
    def __init__(self, path: str) -> None:
        import serial  # lazy: developing against tcp:// needs no pyserial

        self.name = path
        self._close_lock = threading.Lock()
        self._closed = False
        self._s = serial.Serial()
        self._s.port = path
        self._s.baudrate = BAUD
        self._s.timeout = 0.05
        # A timed pyserial write can accept a prefix and then raise without
        # reporting how many bytes made it to the kernel. Retrying duplicates
        # that prefix; reconnecting leaves a truncated JSON command. In
        # non-blocking mode pyserial returns the exact accepted byte count,
        # which lets UpstreamLink retain and resume the remaining suffix while
        # continuing to drain the Captain's opposite-direction CDC traffic.
        self._s.write_timeout = 0
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
        if n <= 0:
            return b""

        # pyserial's ``read(size)`` waits until *size* bytes arrive or the
        # timeout expires.  Asking for READ_CHUNK (4096) on every pass delays
        # every short line and final partial chunk by up to 50 ms, and can
        # unnecessarily leave the Captain's small CDC FIFO back-pressured.
        # Drain everything the driver already has immediately; only use a
        # one-byte blocking read when idle so disconnect/reconnect polling
        # remains bounded by the configured timeout.
        ready = self.read_available(n)
        if ready:
            return ready
        return self._s.read(1)

    def read_available(self, n: int) -> bytes:
        if n <= 0:
            return b""
        waiting = self._s.in_waiting
        if waiting <= 0:
            return b""
        return self._s.read(min(n, waiting))

    def write(self, data: bytes) -> None:
        written = self.write_some(data)
        if written != len(data):
            raise OSError(f"short serial write ({written}/{len(data)} bytes)")

    def write_some(self, data: bytes) -> int:
        if not data:
            return 0
        try:
            # Do not use pyserial's Serial.write here. In pyserial 3.5's
            # POSIX backend, write_timeout=0 combines with EAGAIN into an
            # unbounded retry loop. The tty descriptor itself is O_NONBLOCK;
            # one os.write therefore gives us either exact partial progress or
            # a bounded would-block result.
            written = os.write(self._s.fileno(), data)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                return 0
            raise
        if not isinstance(written, int) or written < 0 or written > len(data):
            raise OSError(f"invalid serial write result {written!r}")
        return written

    def close(self) -> None:
        # pyserial's POSIX read/write loops provide explicit cancellation
        # pipes.  Use them before closing the fd: merely closing a descriptor
        # from another thread does not reliably wake a blocked syscall and can
        # instead race it into ``os.read(None, ...)``.  The lock also makes the
        # stop-thread/_run-finally double close harmless.
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            for method_name in ("cancel_read", "cancel_write"):
                method = getattr(self._s, method_name, None)
                if method is not None:
                    try:
                        method()
                    except Exception:
                        pass
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
        # Includes transports which are still doing the sentinel handshake.
        # stop() closes this handle to interrupt a wedged kernel/USB read; an
        # Event alone cannot wake a blocking pyserial call.
        self._transport_lock = threading.Lock()
        # Serialise the connected flag with TX admission/teardown. Without
        # this lock, send() could observe True, lose the race with the link
        # thread's queue purge, and enqueue a command afterwards for replay on
        # the next Captain session.
        self._state_lock = threading.Lock()
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
        with self._transport_lock:
            transport = self._transport
        if transport is not None:
            transport.close()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)
            if thread.is_alive():
                log.error("link thread did not stop after transport close")
            else:
                self._thread = None

    @property
    def connected(self) -> bool:
        with self._state_lock:
            return self._connected

    def send(self, line: str) -> bool:
        """Queue one protocol line for the pedal without blocking.

        Once accepted, a command is never evicted by a later command. This
        ordering guarantee matters for mutation -> read sequences: dropping
        the older mutation but retaining a later GET_PATCH could make the hub
        cache pre-mutation state. A full/down link rejects the new command and
        lets the Hub return a correlated error instead.
        """

        # Commands have no useful meaning across sessions. Replaying a rig
        # change or one half of a momentary press/release after reconnect is
        # actively dangerous during a performance.
        line = line.rstrip("\r\n") + "\n"
        with self._state_lock:
            if not self._connected:
                log.debug("dropping command while upstream link is down")
                return False
            try:
                self._tx.put_nowait(line)
                return True
            except queue.Full:
                log.warning("tx queue full, rejecting newest command")
                return False

    # -- thread ----------------------------------------------------------

    def _run(self) -> None:
        backoff = iter(REOPEN_BACKOFF)
        while not self._stop.is_set():
            try:
                if self._open_and_sync():
                    backoff = iter(REOPEN_BACKOFF)
                    self._pump()
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                if self._stop.is_set():
                    # close() deliberately interrupts an in-flight pyserial
                    # operation.  Some drivers surface that cancellation as
                    # OSError/TypeError; it is expected during shutdown.
                    log.debug("link stopped while I/O was active: %s", exc)
                else:
                    log.warning("link error: %s", exc)
            finally:
                # Mark down and purge atomically with respect to send(); a
                # command admitted by the old session must never land just
                # after the purge and be replayed following reconnect.
                self._set_state(False, "closed", discard_tx=True)
                with self._transport_lock:
                    transport = self._transport
                    self._transport = None
                if transport is not None:
                    transport.close()
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
            if self._stop.is_set():
                transport.close()
                return False
            # Publish before the potentially long sentinel sync so stop() can
            # abort it even when the USB driver never returns from read().
            with self._transport_lock:
                self._transport = transport
            if self._sentinel_sync(transport):
                self._set_state(True, transport.name)
                log.info("linked to %s", transport.name)
                return True
            transport.close()
            with self._transport_lock:
                if self._transport is transport:
                    self._transport = None
            log.debug("%s did not answer the protocol sentinel", target)
        return False

    def _sentinel_sync(self, transport: Transport) -> bool:
        """Send a uniquely-tagged PING and read until its ACK, discarding
        everything before it. Returns False if no ACK arrives in time."""

        if self._stop.is_set():
            return False
        marker = f"{_HUB_ID_PREFIX}sync_{int(time.time() * 1000)}"
        deadline = time.monotonic() + (
            SYNC_TIMEOUT_TCP
            if transport.name.startswith("tcp://")
            else SYNC_TIMEOUT_SERIAL
        )
        pending = b'\n{"type":"PING","id":"' + marker.encode() + b'"}\n'
        buf = bytearray()
        while time.monotonic() < deadline and not self._stop.is_set():
            written = 0
            if pending:
                try:
                    write_some = getattr(transport, "write_some", None)
                    if write_some is None:
                        # Preserve the Transport protocol's historical
                        # all-or-error duck type for third-party transports;
                        # SerialTransport supplies exact partial progress.
                        transport.write(pending)
                        written = len(pending)
                    else:
                        written = write_some(pending)
                except Exception as exc:  # noqa: BLE001
                    log.debug("sentinel write failed: %s", exc)
                    return False
                if (not isinstance(written, int) or written < 0 or
                        written > len(pending)):
                    log.debug("invalid sentinel write progress: %r", written)
                    return False
                if written:
                    pending = pending[written:]
            try:
                chunk = transport.read(READ_CHUNK)
            except Exception as exc:  # noqa: BLE001
                log.debug("sentinel read failed: %s", exc)
                return False
            if not chunk:
                if not written:
                    # Transport.read normally supplies a bounded wait, but
                    # the abstract contract also permits an immediate empty
                    # result. Avoid spinning while sending or awaiting ACK.
                    time.sleep(0.01)
                continue
            buf.extend(chunk)
            if len(buf) > RX_BUF_MAX:
                return False
            while b"\n" in buf:
                raw, _, rest = buf.partition(b"\n")
                buf = bytearray(rest)
                try:
                    obj = json.loads(raw.decode("utf-8", "strict").strip())
                except (UnicodeDecodeError, ValueError):
                    continue
                # Do not accept a coincidentally matching stale frame until
                # this session's entire sentinel has reached the transport.
                if (not pending and isinstance(obj, dict) and
                        obj.get("type") == "ACK" and obj.get("id") == marker):
                    return True
        return False

    def _discard_tx(self) -> None:
        while True:
            try:
                self._tx.get_nowait()
            except queue.Empty:
                return

    def _pump(self) -> None:
        assert self._transport is not None
        transport = self._transport
        last_rx = time.monotonic()
        last_tx = time.monotonic()
        writes_since_rx = 0
        pending = b""

        while not self._stop.is_set():
            now = time.monotonic()
            line = None
            written = 0

            # Drain bytes which are already in the host driver before trying
            # USB TX. This is non-blocking and prevents a large Captain reply
            # (notably MANIFEST) and a simultaneous host burst from filling
            # both CDC directions before either side gets to read.
            chunk = transport.read_available(READ_CHUNK)
            if chunk:
                last_rx = time.monotonic()
                writes_since_rx = 0
                self._feed_bytes(chunk)

            # -- write one queued command ---------------------------------
            if not pending:
                try:
                    line = self._tx.get_nowait()
                except queue.Empty:
                    line = None
                if line is None and now - max(last_rx, last_tx) >= KEEPALIVE_S:
                    line = '{"type":"PING","id":"%skeepalive"}\n' % _HUB_ID_PREFIX
                if line is not None:
                    pending = line.encode("utf-8")
            if pending:
                written = transport.write_some(pending)
                if written:
                    pending = pending[written:]
                    last_tx = time.monotonic()
                if not pending:
                    writes_since_rx += 1
                    if (writes_since_rx >= WRITE_ONLY_STALL
                            and now - last_rx >= WRITE_ONLY_GRACE_S):
                        raise ConnectionError("write-only stall (pedal not answering)")

            # -- read ---------------------------------------------------
            chunk = transport.read(READ_CHUNK)
            if chunk:
                last_rx = time.monotonic()
                writes_since_rx = 0
                self._feed_bytes(chunk)
            elif now - last_rx >= STALL_S:
                raise ConnectionError(f"no data for {STALL_S:.0f}s")

            if not chunk and not written:
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

    def _set_state(self, up: bool, detail: str,
                   discard_tx: bool = False) -> None:
        with self._state_lock:
            changed = up != self._connected
            self._connected = up
            if discard_tx:
                self._discard_tx()
        if not changed:
            return
        try:
            self._on_state(up, detail)
        except Exception:  # noqa: BLE001
            log.exception("on_state callback raised")
