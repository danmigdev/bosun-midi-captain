"""
A controllable fake of the Captain data port for tests: a TCP server
that speaks just enough of the Bosun protocol, can be told to push
unsolicited CONTEXT lines, and can drop the connection on command so
reconnect logic can be exercised.
"""

from __future__ import annotations

import json
import socket
import threading
from typing import Optional

FW = "9.9.9-fake"


class FakePedal:
    def __init__(self, backlog_lines: int = 0) -> None:
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port: int = self._srv.getsockname()[1]
        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()
        self._run = True
        self._backlog_lines = backlog_lines
        self.received: list[str] = []
        threading.Thread(target=self._accept_loop, daemon=True).start()

    # -- server loop --------------------------------------------------

    def _accept_loop(self) -> None:
        while self._run:
            try:
                self._srv.settimeout(0.5)
                conn, _ = self._srv.accept()
            except (socket.timeout, OSError):
                continue
            with self._lock:
                self._clients.append(conn)
            threading.Thread(target=self._client_loop, args=(conn,), daemon=True).start()

    def _client_loop(self, conn: socket.socket) -> None:
        conn.settimeout(0.5)
        # Stale backlog from a "previous session": dumped before we answer
        # any PING, exactly what the sentinel sync is meant to discard.
        for i in range(self._backlog_lines):
            self._send_to(conn, {"type": "CONTEXT", "context": {"stale": i}})
        buf = b""
        while self._run:
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                self.received.append(line)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                resp = self._reply(msg)
                if resp is not None:
                    self._send_to(conn, resp)
        with self._lock:
            if conn in self._clients:
                self._clients.remove(conn)
        conn.close()

    def _reply(self, msg: dict) -> Optional[dict]:
        t = msg.get("type")
        mid = msg.get("id", "")
        if t == "PING":
            return {"type": "ACK", "id": mid, "fw": FW}
        if t == "GET_DEVICE_INFO":
            return {"type": "DEVICE_INFO", "id": mid, "fw": FW,
                    "device": "fake", "current": {"bank": 1, "slot": 1}}
        if t == "GET_CONTEXT":
            return {"type": "CONTEXT", "id": mid, "context": {"kemper_rig_name": "Init"}}
        return {"type": "ACK", "id": mid}

    # -- test controls ----------------------------------------------

    def _send_to(self, conn: socket.socket, obj: dict) -> None:
        try:
            conn.sendall((json.dumps(obj) + "\n").encode("utf-8"))
        except OSError:
            pass

    def push(self, obj: dict) -> None:
        """Send an unsolicited line to every connected client."""
        with self._lock:
            clients = list(self._clients)
        for c in clients:
            self._send_to(c, obj)

    def drop_clients(self) -> None:
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for c in clients:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def close(self) -> None:
        self._run = False
        self.drop_clients()
        try:
            self._srv.close()
        except OSError:
            pass
