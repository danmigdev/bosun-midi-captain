"""
Fan-out multiplexer between one :class:`UpstreamLink` (its own thread)
and many asyncio consumers (raw TCP clients, WebSocket clients).

Rules:

  - every upstream protocol line goes to every subscriber's queue. The
    queue is bounded and drops its oldest entry when full, so one stuck
    socket can never back-pressure the pedal reader.
  - any subscriber's line is forwarded to the upstream write queue,
    which the link serialises onto the wire.
  - concurrent ``GET_CONTEXT`` requests share one upstream snapshot.
    The hub gives that request a private id, then sends an individually
    re-correlated response to every waiting subscriber.  Unsolicited
    ``CONTEXT`` pushes (which have no private id) remain normal broadcasts.
  - concurrent canonical ``GET_PATCH`` requests for the same profile/bank/
    slot likewise share one upstream read, then populate the immutable patch
    cache. Different keys remain independent.
  - every other canonical protocol request carrying an ``id`` gets a private
    upstream id. Its first correlated reply is restored to the caller's exact
    id and delivered only to that subscriber. This prevents two independent
    clients using the same locally-generated id from accepting each other's
    ACK/ERROR.
  - link up/down transitions are delivered to subscribers that asked
    for status (the Stage kiosk, so it can show a "reconnecting"
    overlay). The raw TCP endpoint does not get these: it stays a
    line-JSON protocol-compatible endpoint with exact request ids.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Optional

from .link import UpstreamLink

log = logging.getLogger("bosun_hub.hub")

SUBSCRIBER_QUEUE_MAX = 512
CONTEXT_SINGLE_FLIGHT_TIMEOUT_S = 12.0
CONTEXT_WAITERS_MAX = 512
CONTEXT_WAITERS_PER_SUB_MAX = 64
PATCH_SINGLE_FLIGHT_TIMEOUT_S = 12.0
# The Captain can retain only one active background response plus eight
# queued generators.  Never use the ninth slot from the hub: it is deliberate
# headroom for a command which was already in the CDC/RX path when our last
# correlated reply arrived.  The class limits add up to the global bound and
# reserve enough capacity for Stage's DEVICE_INFO + PATCH_LIST + CONTEXT and
# current/post-switch PATCH snapshots even while editor diagnostics are busy.
BACKGROUND_INFLIGHT_MAX = 8
BACKGROUND_BULK_MAX = 3
BACKGROUND_PATCH_MAX = 2
BACKGROUND_CONTEXT_MAX = 1
BACKGROUND_DEVICE_INFO_MAX = 1
BACKGROUND_PATCH_LIST_MAX = 1

PATCH_FLIGHTS_MAX = BACKGROUND_PATCH_MAX
PATCH_WAITERS_MAX = 512
PATCH_WAITERS_PER_SUB_MAX = 64
REQUEST_TIMEOUT_S = 30.0
# A streamed background line makes every immediate ACK/ERROR wait in the
# Captain's 128-chunk deferred-output queue. Keep ordinary correlated fan-in
# to at most half that physical bound, with a per-client fairness limit, so
# unsolicited Stage/Kemper events retain substantial headroom too.
REQUESTS_MAX = 64
REQUESTS_PER_SUB_MAX = 32

_BACKGROUND_CLASS_BY_TYPE = {
    "GET_MANIFEST": "bulk",
    "GET_GLOBAL": "bulk",
    "STATS": "bulk",
    "LED_DUMP": "bulk",
    "LIST_FONTS": "bulk",
    "LIST_PROFILES": "bulk",
    "GET_PATCH": "patch",
    "GET_CONTEXT": "context",
    "GET_DEVICE_INFO": "device_info",
    "LIST_PATCHES": "patch_list",
}
_BACKGROUND_CLASS_LIMITS = {
    "bulk": BACKGROUND_BULK_MAX,
    "patch": BACKGROUND_PATCH_MAX,
    "context": BACKGROUND_CONTEXT_MAX,
    "device_info": BACKGROUND_DEVICE_INFO_MAX,
    "patch_list": BACKGROUND_PATCH_LIST_MAX,
}
assert sum(_BACKGROUND_CLASS_LIMITS.values()) == BACKGROUND_INFLIGHT_MAX

# Deliberately does not begin with link._HUB_ID_PREFIX (``__hub_``): the
# UpstreamLink consumes ids in that namespace as its own keepalive replies.
_CONTEXT_ID_STEM = "__bosun_ctx_"
_PATCH_ID_STEM = "__bosun_patch_"
_REQUEST_ID_STEM = "__bosun_req_"

# Commands which can make a snapshot already in progress stale.  If one is
# forwarded during a flight, later GET_CONTEXT callers wait for a second
# snapshot ordered *after* the mutation instead of joining the old one.
_CONTEXT_BARRIER_TYPES = {
    "PUT_GLOBAL", "PUT_PATCH", "PUT_BINDING", "DELETE_PATCH", "DISCARD",
    "SWITCH_PATCH", "CREATE_PROFILE", "SWITCH_PROFILE", "DELETE_PROFILE",
    "RENAME_PROFILE", "FACTORY_RESET", "REBOOT",
}
_CONTEXT_BARRIER_EVENTS = {"patch_switched", "binding_fired"}
_PATCH_LOCATION_MUTATIONS = {"PUT_PATCH", "PUT_BINDING", "DELETE_PATCH", "DISCARD"}
_PATCH_RESET_MUTATIONS = {
    "SWITCH_PROFILE", "DELETE_PROFILE", "CREATE_PROFILE", "RENAME_PROFILE",
    "PUT_FILE_BEGIN", "PUT_FILE_CHUNK", "PUT_FILE_END", "FACTORY_RESET",
    "REBOOT",
}
_PATCH_ORDERED_MUTATIONS = (
    _PATCH_LOCATION_MUTATIONS | _PATCH_RESET_MUTATIONS | {"SWITCH_PATCH"}
)
# Legacy profile-management callers used ``id`` as both the request id and
# the profile identifier.  The firmware still implements that fallback.  A
# private correlation id must therefore be accompanied by the original value
# in ``profile_id`` or it would target the hub's random private namespace.
_PROFILE_ID_FALLBACK_TYPES = {
    "CREATE_PROFILE", "SWITCH_PROFILE", "DELETE_PROFILE", "RENAME_PROFILE",
}


class Subscription:
    """One consumer's view of the stream. Async-iterate :meth:`lines` for
    inbound protocol lines; call :meth:`send` to write one upstream."""

    def __init__(self, hub: "Hub", want_status: bool) -> None:
        self._hub = hub
        self._want_status = want_status
        self._queue: asyncio.Queue[Optional[str]] = asyncio.Queue(
            maxsize=SUBSCRIBER_QUEUE_MAX
        )

    def _offer(self, line: str) -> None:
        try:
            self._queue.put_nowait(line)
        except asyncio.QueueFull:
            # A Stage client cannot safely resume after silently losing an
            # arbitrary transition. Collapse its backlog and force the same
            # down/up path as a reconnect, which re-pulls DEVICE_INFO,
            # CONTEXT and PATCH before delivering the newest line.
            if self._want_status:
                while True:
                    try:
                        self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                self._queue.put_nowait(self._hub._status_line(False))
                self._queue.put_nowait(self._hub._status_line(True))
            else:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                self._queue.put_nowait(line)
            except asyncio.QueueFull:
                pass

    async def lines(self):
        while True:
            line = await self._queue.get()
            if line is None:  # close sentinel
                return
            yield line

    def send(self, line: str) -> None:
        self._hub._send(self, line)

    def close(self) -> None:
        self._hub._remove(self)
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass


class _PatchFlight:
    """One keyed PATCH generation plus an optional post-mutation batch."""

    __slots__ = (
        "key", "waiters", "next_waiters", "flight_id", "sealed",
        "timeout_handle",
    )

    def __init__(self, key: tuple[str, int, int]) -> None:
        self.key = key
        self.waiters: list[tuple[Subscription, bool, object]] = []
        self.next_waiters: list[tuple[Subscription, bool, object]] = []
        self.flight_id: Optional[str] = None
        self.sealed = False
        self.timeout_handle: Optional[asyncio.TimerHandle] = None


class _RequestPending:
    """One ordinary request awaiting its first id-correlated reply."""

    __slots__ = (
        "sub", "has_id", "request_id", "kind", "broadcast",
        "timeout_handle",
    )

    def __init__(
        self,
        sub: Subscription,
        has_id: bool,
        request_id: object,
        kind: str,
        *,
        broadcast: bool = False,
    ) -> None:
        self.sub: Optional[Subscription] = sub
        self.has_id = has_id
        self.request_id = request_id
        self.kind = kind
        self.broadcast = broadcast
        self.timeout_handle: Optional[asyncio.TimerHandle] = None


class Hub:
    def __init__(
        self,
        target: Optional[str],
        *,
        context_timeout_s: float = CONTEXT_SINGLE_FLIGHT_TIMEOUT_S,
        patch_timeout_s: float = PATCH_SINGLE_FLIGHT_TIMEOUT_S,
        request_timeout_s: float = REQUEST_TIMEOUT_S,
    ) -> None:
        if context_timeout_s <= 0 or patch_timeout_s <= 0:
            raise ValueError("single-flight timeouts must be positive")
        if request_timeout_s <= 0:
            raise ValueError("request timeout must be positive")
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subs: set[Subscription] = set()
        self.link = UpstreamLink(
            target=target,
            on_line=self._on_upstream_line,
            on_state=self._on_upstream_state,
        )
        # Complete PATCH replies are immutable JSON snapshots.  Keeping them
        # here removes the slow Captain CDC round-trip when Stage revisits a
        # rig.  Access happens only on the asyncio thread (link callbacks are
        # posted through _post), so no lock is required.
        self._patch_cache: dict[tuple[str, int, int], dict] = {}
        self._patch_flights: dict[tuple[str, int, int], _PatchFlight] = {}
        self._patch_flight_ids: dict[str, _PatchFlight] = {}
        self._patch_flight_seq = 0
        self._patch_id_prefix = _PATCH_ID_STEM + secrets.token_hex(8) + "_"
        self._patch_timeout_s = patch_timeout_s
        # A GET_CONTEXT response is a point-in-time snapshot and cannot be
        # cached across rig/effect changes.  It *can*, however, satisfy every
        # request which arrived while that same snapshot was being streamed.
        # Waiter tuples retain whether ``id`` was absent versus explicitly
        # null so the downstream wire response preserves the request exactly.
        self._context_waiters: list[tuple[Subscription, bool, object]] = []
        self._context_next_waiters: list[tuple[Subscription, bool, object]] = []
        self._context_flight_id: Optional[str] = None
        self._context_flight_sealed = False
        self._context_flight_seq = 0
        self._context_id_prefix = _CONTEXT_ID_STEM + secrets.token_hex(8) + "_"
        self._context_timeout_s = context_timeout_s
        self._context_timeout_handle: Optional[asyncio.TimerHandle] = None
        # Ordinary ids are client-local.  Always replace them on the shared
        # upstream link, otherwise two TCP/WS clients using e.g. ``id: 1``
        # both see and may accept the same reply.  The reverse index makes a
        # subscriber close bounded by its own in-flight limit.
        self._request_pending: dict[str, _RequestPending] = {}
        self._request_ids_by_sub: dict[Subscription, set[str]] = {}
        self._request_seq = 0
        self._request_id_prefix = _REQUEST_ID_STEM + secrets.token_hex(8) + "_"
        self._request_timeout_s = request_timeout_s
        # Requests in this table have reached (or may already have reached)
        # the Captain and still occupy one of its tiny background-generator
        # slots.  This is intentionally independent of downstream waiter
        # state: closing a browser or timing out its promise does not cancel
        # work which is already queued in CircuitPython.
        self._background_tokens: dict[str, str] = {}

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.link.start()

    def stop(self) -> None:
        self._clear_all_context_waiters()
        self._clear_all_patch_waiters()
        self._clear_all_requests()
        self._background_tokens.clear()
        self.link.stop()
        for sub in list(self._subs):
            sub.close()

    # -- subscribers ----------------------------------------------------

    def subscribe(self, want_status: bool = False) -> Subscription:
        sub = Subscription(self, want_status)
        self._subs.add(sub)
        if want_status:
            sub._offer(self._status_line(self.link.connected))
        return sub

    def _remove(self, sub: Subscription) -> None:
        self._subs.discard(sub)
        if self._context_waiters:
            self._context_waiters = [
                waiter for waiter in self._context_waiters if waiter[0] is not sub
            ]
        if self._context_next_waiters:
            self._context_next_waiters = [
                waiter for waiter in self._context_next_waiters
                if waiter[0] is not sub
            ]
        for flight in self._patch_flights.values():
            if flight.waiters:
                flight.waiters = [
                    waiter for waiter in flight.waiters if waiter[0] is not sub
                ]
            if flight.next_waiters:
                flight.next_waiters = [
                    waiter for waiter in flight.next_waiters
                    if waiter[0] is not sub
                ]
        self._clear_subscriber_requests(sub)

    # -- upstream callbacks (called from the link thread) --------------

    def _post(self, fn, *args) -> None:
        """Hand work to the asyncio loop from the link thread, tolerating a
        loop that is shutting down."""
        if self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            pass  # loop closed during shutdown

    def _on_upstream_line(self, line: str) -> None:
        self._post(self._dispatch, line)

    def _on_upstream_state(self, up: bool, detail: str) -> None:
        log.info("upstream link %s (%s)", "up" if up else "down", detail)
        self._post(self._dispatch_status, self._status_line(up))

    # -- Captain background admission ----------------------------------

    def _reserve_background(
        self, private_id: str, kind: str
    ) -> bool:
        """Reserve one response-generator slot for an upstream request.

        A token lives until its private-id reply is observed or the upstream
        session ends.  In particular, a client timeout/disconnect cannot free
        it: neither event removes the already queued generator from Captain.
        """

        request_class = _BACKGROUND_CLASS_BY_TYPE.get(kind)
        if request_class is None:
            return True
        if len(self._background_tokens) >= BACKGROUND_INFLIGHT_MAX:
            return False
        class_limit = _BACKGROUND_CLASS_LIMITS[request_class]
        class_count = sum(
            existing == request_class
            for existing in self._background_tokens.values()
        )
        if class_count >= class_limit:
            return False
        self._background_tokens[private_id] = request_class
        return True

    def _release_background(self, private_id: Optional[str]) -> None:
        if private_id is not None:
            self._background_tokens.pop(private_id, None)

    def _orphan_background(self, private_id: Optional[str]) -> None:
        """Keep physical occupancy without consuming a live class quota.

        This lets a retry use (for example) the one live CONTEXT reservation
        after its predecessor timed out, while the orphan still counts toward
        the hard global eight-generator ceiling until its late reply/down.
        """

        if private_id in self._background_tokens:
            self._background_tokens[private_id] = "orphan"

    @staticmethod
    def _background_busy_error(kind: str) -> str:
        if kind == "GET_CONTEXT":
            return "context_busy"
        if kind == "GET_PATCH":
            return "patch_busy"
        return "background_busy"

    @staticmethod
    def _patch_key(msg: dict) -> Optional[tuple[str, int, int]]:
        bank, slot = msg.get("bank"), msg.get("slot")
        if (not isinstance(bank, int) or isinstance(bank, bool)
                or not isinstance(slot, int) or isinstance(slot, bool)):
            return None
        profile = msg.get("profile", "")
        if profile is None:
            profile = ""
        if not isinstance(profile, str):
            return None
        return profile, bank, slot

    @classmethod
    def _canonical_patch_key(cls, msg: dict) -> Optional[tuple[str, int, int]]:
        if any(
            key not in {"type", "id", "profile", "bank", "slot"}
            for key in msg
        ):
            return None
        return cls._patch_key(msg)

    def _send(self, sub: Subscription, line: str) -> None:
        """Coalesce GET_CONTEXT, serve cached GET_PATCH, forward the rest.

        Invalidations happen *before* forwarding a mutation.  A failed write
        therefore merely causes one extra read later, while never exposing a
        stale patch.  ACK handling isn't needed for cache correctness.
        """
        try:
            msg = json.loads(line)
        except (TypeError, ValueError):
            self.link.send(line)
            return
        if not isinstance(msg, dict):
            self.link.send(line)
            return

        kind = msg.get("type")
        if not isinstance(kind, str):
            self.link.send(line)
            return
        if kind == "GET_CONTEXT" and self._queue_context_request(sub, msg):
            return

        if kind in _CONTEXT_BARRIER_TYPES:
            self._seal_context_flight()

        if kind == "GET_PATCH" and self._queue_patch_request(sub, msg):
            return

        self._prepare_patch_mutation(kind, msg)

        # A non-canonical GET_CONTEXT/GET_PATCH must not be coalesced under
        # selectors we do not understand, but it still needs a private id and
        # a background-admission token.  Otherwise adding one future field (or
        # simply omitting id) bypasses the very bound which protects Captain.
        if self._queue_request(sub, msg):
            return

        accepted = self.link.send(line)
        if accepted is False and kind in _PATCH_ORDERED_MUTATIONS:
            self._offer_correlated(
                sub, "id" in msg, msg.get("id"),
                {"type": "ERROR",
                 "error": "link_down" if not self.link.connected else "link_busy",
                 "of": kind},
            )

    # -- generic per-subscriber request correlation --------------------

    def _queue_request(self, sub: Subscription, msg: dict) -> bool:
        """Privatise an ordinary request id before sharing the upstream.

        Ordinary requests without an id retain the historical transparent
        broadcast semantics. Background requests are the exception: they get
        a private upstream id even when the caller omitted one, so admission
        remains response-coupled; their reply is broadcast again with that
        private id removed. Canonical GET_CONTEXT/GET_PATCH requests have
        already returned through their specialised paths before this method.
        """
        kind = msg["type"]
        has_id = "id" in msg
        is_background = kind in _BACKGROUND_CLASS_BY_TYPE
        if not has_id and not is_background:
            return False
        if sub not in self._subs:
            return True  # late send from a socket which has already closed

        request_id = msg.get("id")
        if (kind in _PROFILE_ID_FALLBACK_TYPES
                and not msg.get("profile_id") and not request_id):
            # ``profile_id or id`` in legacy firmware cannot be preserved by
            # rewriting a false-y id: even an explicit false-y profile_id
            # would fall through to the new private id. Keep this malformed/
            # legacy edge transparent instead of changing its target.
            return False
        if not self.link.connected:
            self._offer_correlated(
                sub, has_id, request_id,
                {"type": "ERROR", "error": "link_down", "of": kind},
            )
            return True

        subscriber_ids = self._request_ids_by_sub.get(sub)
        subscriber_count = len(subscriber_ids) if subscriber_ids else 0
        if (len(self._request_pending) >= REQUESTS_MAX
                or subscriber_count >= REQUESTS_PER_SUB_MAX):
            self._offer_correlated(
                sub, has_id, request_id,
                {"type": "ERROR", "error": "request_busy", "of": kind},
            )
            return True

        self._request_seq += 1
        private_id = f"{self._request_id_prefix}{self._request_seq}"
        if not self._reserve_background(private_id, kind):
            self._offer_correlated(
                sub, has_id, request_id,
                {"type": "ERROR",
                 "error": self._background_busy_error(kind), "of": kind},
            )
            return True

        pending = _RequestPending(
            sub, has_id, request_id, kind, broadcast=not has_id,
        )
        self._request_pending[private_id] = pending
        if subscriber_ids is None:
            subscriber_ids = set()
            self._request_ids_by_sub[sub] = subscriber_ids
        subscriber_ids.add(private_id)

        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
        if loop is not None:
            pending.timeout_handle = loop.call_later(
                self._request_timeout_s, self._request_timed_out, private_id,
            )

        upstream = dict(msg)
        if kind in _PROFILE_ID_FALLBACK_TYPES and not upstream.get("profile_id"):
            upstream["profile_id"] = request_id
        upstream["id"] = private_id
        accepted = self.link.send(json.dumps(upstream, separators=(",", ":")))
        if accepted is False:
            self._take_request(private_id)
            self._release_background(private_id)
            self._offer_correlated(
                sub, has_id, request_id,
                {"type": "ERROR",
                 "error": "link_down" if not self.link.connected else "link_busy",
                 "of": kind},
            )
        return True

    def _take_request(self, private_id: str) -> Optional[_RequestPending]:
        pending = self._request_pending.pop(private_id, None)
        if pending is None:
            return None
        if pending.timeout_handle is not None:
            pending.timeout_handle.cancel()
            pending.timeout_handle = None
        if pending.sub is not None:
            subscriber_ids = self._request_ids_by_sub.get(pending.sub)
            if subscriber_ids is not None:
                subscriber_ids.discard(private_id)
                if not subscriber_ids:
                    self._request_ids_by_sub.pop(pending.sub, None)
        return pending

    def _deliver_request_reply(self, pending: _RequestPending, msg: dict) -> None:
        if pending.broadcast:
            for sub in self._subs:
                self._offer_correlated(
                    sub, pending.has_id, pending.request_id, msg,
                )
        elif pending.sub is not None and pending.sub in self._subs:
            self._offer_correlated(
                pending.sub, pending.has_id, pending.request_id, msg,
            )

    def _request_timed_out(self, private_id: str) -> None:
        self._orphan_background(private_id)
        pending = self._take_request(private_id)
        if pending is None:
            return
        log.warning(
            "%s request %s timed out", pending.kind, private_id,
        )
        self._deliver_request_reply(
            pending,
            {"type": "ERROR", "error": "request_timeout", "of": pending.kind},
        )

    def _clear_subscriber_requests(self, sub: Subscription) -> None:
        for private_id in list(self._request_ids_by_sub.pop(sub, ())):
            pending = self._request_pending.get(private_id)
            if pending is not None and pending.broadcast:
                # An accepted idless request belongs to the broadcast stream,
                # not to the socket which happened to issue it. Detach its
                # fairness owner but retain correlation/token state so a late
                # physical reply still reaches the remaining subscribers.
                pending.sub = None
            else:
                self._orphan_background(private_id)
                self._take_request(private_id)

    def _clear_all_requests(self) -> list[_RequestPending]:
        pending: list[_RequestPending] = []
        for private_id in list(self._request_pending):
            item = self._take_request(private_id)
            if item is not None:
                pending.append(item)
        return pending

    def _fail_all_requests(self, error: str) -> None:
        for pending in self._clear_all_requests():
            self._deliver_request_reply(
                pending,
                {"type": "ERROR", "error": error, "of": pending.kind},
            )

    def _consume_request_reply(self, msg: dict) -> bool:
        response_id = msg.get("id")
        if not (
            isinstance(response_id, str)
            and response_id.startswith(self._request_id_prefix)
        ):
            return False

        # Release even if the downstream waiter timed out/closed and its
        # correlation record is already gone.  The late reply is the first
        # proof that Captain no longer owns this physical background slot.
        self._release_background(response_id)
        pending = self._take_request(response_id)
        if pending is None:
            # Never leak a late reply containing a hub-private id to clients.
            log.debug("dropping stale ordinary request reply %s", response_id)
            return True
        self._deliver_request_reply(pending, msg)
        return True

    # -- GET_PATCH keyed single flight + cache --------------------------

    @staticmethod
    def _patch_location(msg: dict) -> Optional[tuple[int, int]]:
        bank, slot = msg.get("bank"), msg.get("slot")
        if (not isinstance(bank, int) or isinstance(bank, bool)
                or not isinstance(slot, int) or isinstance(slot, bool)):
            return None
        return bank, slot

    def _patch_waiter_counts(self, sub: Subscription) -> tuple[int, int]:
        total = 0
        per_subscriber = 0
        for flight in self._patch_flights.values():
            total += len(flight.waiters) + len(flight.next_waiters)
            per_subscriber += sum(w[0] is sub for w in flight.waiters)
            per_subscriber += sum(w[0] is sub for w in flight.next_waiters)
        return total, per_subscriber

    def _queue_patch_request(self, sub: Subscription, msg: dict) -> bool:
        """Serve, join or start a canonical keyed GET_PATCH request.

        Unknown future fields and structurally invalid selectors return false
        for transparent pass-through; they must not be merged under semantics
        the current hub does not understand.
        """
        key = self._canonical_patch_key(msg)
        if key is None:
            return False
        has_id = "id" in msg
        request_id = msg.get("id")
        if sub not in self._subs:
            return True

        if not self.link.connected:
            self._offer_correlated(
                sub, has_id, request_id,
                {"type": "ERROR", "error": "link_down", "of": "GET_PATCH"},
            )
            return True
        cached = self._patch_cache.get(key)
        if cached is not None:
            self._offer_correlated(sub, has_id, request_id, cached)
            return True

        total, per_subscriber = self._patch_waiter_counts(sub)
        if (total >= PATCH_WAITERS_MAX
                or per_subscriber >= PATCH_WAITERS_PER_SUB_MAX):
            self._offer_correlated(
                sub, has_id, request_id,
                {"type": "ERROR", "error": "patch_busy", "of": "GET_PATCH"},
            )
            return True

        waiter = (sub, has_id, request_id)
        flight = self._patch_flights.get(key)
        if flight is not None:
            if flight.sealed:
                flight.next_waiters.append(waiter)
            else:
                flight.waiters.append(waiter)
            return True
        if len(self._patch_flights) >= PATCH_FLIGHTS_MAX:
            self._offer_correlated(
                sub, has_id, request_id,
                {"type": "ERROR", "error": "patch_busy", "of": "GET_PATCH"},
            )
            return True

        flight = _PatchFlight(key)
        flight.waiters.append(waiter)
        self._patch_flights[key] = flight
        self._start_patch_flight(flight)
        return True

    def _start_patch_flight(self, flight: _PatchFlight) -> None:
        if flight.flight_id is not None or not flight.waiters:
            return
        self._patch_flight_seq += 1
        flight_id = f"{self._patch_id_prefix}{self._patch_flight_seq}"
        flight.flight_id = flight_id
        flight.sealed = False
        if not self._reserve_background(flight_id, "GET_PATCH"):
            self._fail_patch_flight(flight, "patch_busy")
            return
        self._patch_flight_ids[flight_id] = flight
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
        if loop is not None:
            flight.timeout_handle = loop.call_later(
                self._patch_timeout_s, self._patch_timed_out,
                flight.key, flight_id,
            )

        profile, bank, slot = flight.key
        request = {
            "type": "GET_PATCH", "id": flight_id,
            "bank": bank, "slot": slot,
        }
        if profile:
            request["profile"] = profile
        accepted = self.link.send(json.dumps(request, separators=(",", ":")))
        if accepted is False:
            self._fail_patch_flight(
                flight, "link_down" if not self.link.connected else "link_busy"
            )

    def _patch_timed_out(
        self, key: tuple[str, int, int], flight_id: str
    ) -> None:
        flight = self._patch_flights.get(key)
        if flight is None or flight.flight_id != flight_id:
            return
        log.warning(
            "GET_PATCH single flight %s key=%r timed out with %d waiter(s)",
            flight_id, key, len(flight.waiters),
        )
        # The client deadline cannot cancel the generator which is already in
        # Captain. Keep its admission token until a late reply or link-down.
        self._orphan_background(flight_id)
        self._fail_patch_flight(
            flight, "patch_timeout", release_background=False,
        )

    def _take_patch_generation(
        self, flight: _PatchFlight, *, release_background: bool = True
    ) -> list[tuple[Subscription, bool, object]]:
        handle = flight.timeout_handle
        flight.timeout_handle = None
        if handle is not None:
            handle.cancel()
        flight_id = flight.flight_id
        if flight_id is not None:
            self._patch_flight_ids.pop(flight_id, None)
            if release_background:
                self._release_background(flight_id)
        waiters = flight.waiters
        flight.waiters = []
        flight.flight_id = None
        flight.sealed = False
        return waiters

    def _finish_patch_flight(
        self,
        flight: _PatchFlight,
        response: dict,
        *,
        release_background: bool = True,
    ) -> None:
        waiters = self._take_patch_generation(
            flight, release_background=release_background,
        )
        for sub, has_id, request_id in waiters:
            if sub in self._subs:
                self._offer_correlated(sub, has_id, request_id, response)
        if flight.next_waiters:
            flight.waiters = flight.next_waiters
            flight.next_waiters = []
            self._start_patch_flight(flight)
        elif self._patch_flights.get(flight.key) is flight:
            self._patch_flights.pop(flight.key, None)

    def _fail_patch_flight(
        self,
        flight: _PatchFlight,
        error: str,
        *,
        release_background: bool = True,
    ) -> None:
        self._finish_patch_flight(
            flight,
            {"type": "ERROR", "error": error, "of": "GET_PATCH"},
            release_background=release_background,
        )

    def _clear_all_patch_waiters(
        self,
    ) -> list[tuple[Subscription, bool, object]]:
        waiters: list[tuple[Subscription, bool, object]] = []
        for flight in self._patch_flights.values():
            handle = flight.timeout_handle
            if handle is not None:
                handle.cancel()
            waiters.extend(flight.waiters)
            waiters.extend(flight.next_waiters)
        self._patch_flights = {}
        self._patch_flight_ids = {}
        return waiters

    def _fail_all_patch_waiters(self, error: str) -> None:
        if not self._patch_flights:
            return
        waiters = self._clear_all_patch_waiters()
        response = {"type": "ERROR", "error": error, "of": "GET_PATCH"}
        for sub, has_id, request_id in waiters:
            if sub in self._subs:
                self._offer_correlated(sub, has_id, request_id, response)

    def _seal_patch_location(self, location: tuple[int, int]) -> None:
        for key, flight in self._patch_flights.items():
            if key[1:] == location and flight.flight_id is not None:
                flight.sealed = True

    def _seal_all_patch_flights(self) -> None:
        for flight in self._patch_flights.values():
            if flight.flight_id is not None:
                flight.sealed = True

    def _invalidate_patch_location(self, location: tuple[int, int]) -> None:
        for key in [key for key in self._patch_cache if key[1:] == location]:
            self._patch_cache.pop(key, None)

    def _prepare_patch_mutation(self, kind: object, msg: dict) -> None:
        if kind in _PATCH_LOCATION_MUTATIONS:
            location = self._patch_location(msg)
            if location is None:
                self._patch_cache.clear()
                self._seal_all_patch_flights()
            else:
                # Active-profile and explicit-profile keys can alias the same
                # on-disk patch, so conservatively invalidate/seal every
                # profile variant at this bank/slot.
                self._invalidate_patch_location(location)
                self._seal_patch_location(location)
        elif kind == "SWITCH_PATCH":
            location = self._patch_location(msg)
            if location is None:
                self._seal_all_patch_flights()
            else:
                self._seal_patch_location(location)
        elif kind in _PATCH_RESET_MUTATIONS:
            self._patch_cache.clear()
            self._seal_all_patch_flights()

    def _consume_patch_reply(self, msg: dict) -> bool:
        response_id = msg.get("id")
        if not (
            isinstance(response_id, str)
            and response_id.startswith(self._patch_id_prefix)
        ):
            return False
        # A timed-out generation no longer has a _PatchFlight entry, but its
        # eventual private reply still proves that the Captain slot is free.
        self._release_background(response_id)
        flight = self._patch_flight_ids.get(response_id)
        if flight is None:
            log.debug("dropping stale GET_PATCH reply %s", response_id)
            return True

        kind = msg.get("type")
        response_key = self._patch_key(msg) if kind == "PATCH" else None
        # Legacy firmware did not understand cross-profile reads and omitted
        # (or returned an empty) profile. Preserve that response for callers
        # which implement the legacy-active-profile fallback, but never cache
        # it under the named profile because its provenance is ambiguous.
        legacy_profile_reply = bool(
            response_key is not None
            and flight.key[0]
            and response_key == ("", flight.key[1], flight.key[2])
        )
        if kind == "PATCH" and (
            not isinstance(msg.get("patch"), dict)
            or msg.get("partial") is True
            or (response_key != flight.key and not legacy_profile_reply)
        ):
            log.warning("malformed/mismatched reply for GET_PATCH flight %s", response_id)
            self._fail_patch_flight(flight, "patch_protocol")
        elif kind == "PATCH":
            if not flight.sealed and not legacy_profile_reply:
                snapshot = dict(msg)
                snapshot.pop("id", None)
                self._patch_cache[flight.key] = snapshot
            self._finish_patch_flight(flight, msg)
        elif kind == "ERROR":
            self._finish_patch_flight(flight, msg)
        else:
            log.warning(
                "unexpected %r reply for GET_PATCH flight %s", kind, response_id
            )
            self._fail_patch_flight(flight, "patch_protocol")
        return True

    # -- GET_CONTEXT single flight --------------------------------------

    def _queue_context_request(self, sub: Subscription, msg: dict) -> bool:
        """Join or start the current context snapshot.

        Only the current canonical request shape (``type`` plus optional
        ``id``) is coalesced.  A future protocol revision with selector fields
        keeps transparent pass-through semantics rather than being incorrectly
        merged with a request for a different snapshot.
        """
        if any(key not in {"type", "id"} for key in msg):
            return False

        has_id = "id" in msg
        request_id = msg.get("id")
        if sub not in self._subs:
            return True  # ignore a late send from an already closed socket
        if not self.link.connected:
            self._offer_correlated(
                sub,
                has_id,
                request_id,
                {"type": "ERROR", "error": "link_down", "of": "GET_CONTEXT"},
            )
            return True

        waiter_count = len(self._context_waiters) + len(self._context_next_waiters)
        per_subscriber = (
            sum(waiter[0] is sub for waiter in self._context_waiters)
            + sum(waiter[0] is sub for waiter in self._context_next_waiters)
        )
        if (waiter_count >= CONTEXT_WAITERS_MAX
                or per_subscriber >= CONTEXT_WAITERS_PER_SUB_MAX):
            self._offer_correlated(
                sub,
                has_id,
                request_id,
                {"type": "ERROR", "error": "context_busy", "of": "GET_CONTEXT"},
            )
            return True

        waiter = (sub, has_id, request_id)
        if self._context_flight_id is not None:
            if self._context_flight_sealed:
                self._context_next_waiters.append(waiter)
            else:
                self._context_waiters.append(waiter)
            return True

        self._context_waiters.append(waiter)
        self._start_context_flight()
        return True

    def _start_context_flight(self) -> None:
        if self._context_flight_id is not None or not self._context_waiters:
            return
        self._context_flight_seq += 1
        flight_id = f"{self._context_id_prefix}{self._context_flight_seq}"
        self._context_flight_id = flight_id
        self._context_flight_sealed = False
        if not self._reserve_background(flight_id, "GET_CONTEXT"):
            self._fail_context_flight("context_busy")
            return

        # The only coalesced request shape is canonical, so no request-owned
        # fields can be lost here.
        self._arm_context_timeout(flight_id)
        accepted = self.link.send(json.dumps(
            {"type": "GET_CONTEXT", "id": flight_id}, separators=(",", ":")
        ))
        if accepted is False:
            self._fail_context_flight(
                "link_down" if not self.link.connected else "link_busy"
            )

    def _seal_context_flight(self) -> None:
        if self._context_flight_id is not None:
            self._context_flight_sealed = True

    def _arm_context_timeout(self, flight_id: str) -> None:
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # Production calls start() from a running event loop.  This
                # fallback keeps synchronous construction/tests harmless.
                return
        self._context_timeout_handle = loop.call_later(
            self._context_timeout_s, self._context_timed_out, flight_id
        )

    def _context_timed_out(self, flight_id: str) -> None:
        if flight_id != self._context_flight_id:
            return  # timer from a completed/aborted older generation
        log.warning(
            "GET_CONTEXT single flight %s timed out with %d waiter(s)",
            flight_id,
            len(self._context_waiters),
        )
        # Timing out the downstream snapshot does not cancel its already
        # queued Captain generator. Its token remains until late reply/down.
        self._orphan_background(flight_id)
        self._fail_context_flight(
            "context_timeout", release_background=False,
        )

    def _take_context_flight(
        self,
        *,
        release_background: bool = True,
    ) -> list[tuple[Subscription, bool, object]]:
        handle = self._context_timeout_handle
        self._context_timeout_handle = None
        if handle is not None:
            handle.cancel()
        waiters = self._context_waiters
        self._context_waiters = []
        if release_background:
            self._release_background(self._context_flight_id)
        self._context_flight_id = None
        self._context_flight_sealed = False
        return waiters


    def _clear_all_context_waiters(
        self,
    ) -> list[tuple[Subscription, bool, object]]:
        waiters = self._take_context_flight()
        waiters.extend(self._context_next_waiters)
        self._context_next_waiters = []
        return waiters

    @staticmethod
    def _offer_correlated(
        sub: Subscription,
        has_id: bool,
        request_id: object,
        response: dict,
    ) -> None:
        correlated = dict(response)
        if has_id:
            correlated["id"] = request_id
        else:
            correlated.pop("id", None)
        sub._offer(json.dumps(correlated, separators=(",", ":")))

    def _finish_context_flight(
        self,
        response: dict,
        *,
        release_background: bool = True,
    ) -> None:
        waiters = self._take_context_flight(
            release_background=release_background,
        )
        for sub, has_id, request_id in waiters:
            if sub in self._subs:  # subscriber may have closed mid-stream
                self._offer_correlated(sub, has_id, request_id, response)
        if self._context_next_waiters:
            self._context_waiters = self._context_next_waiters
            self._context_next_waiters = []
            self._start_context_flight()

    def _fail_context_flight(
        self, error: str, *, release_background: bool = True
    ) -> None:
        if self._context_flight_id is None:
            return
        self._finish_context_flight(
            {"type": "ERROR", "error": error, "of": "GET_CONTEXT"},
            release_background=release_background,
        )

    def _fail_all_context_waiters(self, error: str) -> None:
        if self._context_flight_id is None and not self._context_next_waiters:
            return
        waiters = self._clear_all_context_waiters()
        response = {"type": "ERROR", "error": error, "of": "GET_CONTEXT"}
        for sub, has_id, request_id in waiters:
            if sub in self._subs:
                self._offer_correlated(sub, has_id, request_id, response)

    def _consume_context_reply(self, msg: dict) -> bool:
        """Consume replies in the hub-private context id namespace.

        Returning true prevents both an active private id and a late reply
        from an expired generation leaking to unrelated raw/WS clients.
        Ordinary CONTEXT pushes, including ones without an id, return false
        and keep the normal broadcast path.
        """
        response_id = msg.get("id")
        if not (
            isinstance(response_id, str)
            and response_id.startswith(self._context_id_prefix)
        ):
            return False

        # Also releases a timed-out/stale generation no longer represented by
        # _context_flight_id; private ids never belong to downstream clients.
        self._release_background(response_id)
        if response_id != self._context_flight_id:
            log.debug("dropping stale GET_CONTEXT reply %s", response_id)
            return True

        kind = msg.get("type")
        if kind == "CONTEXT" and (
            not isinstance(msg.get("context"), dict) or msg.get("partial") is True
        ):
            log.warning("malformed/partial reply for GET_CONTEXT flight %s", response_id)
            self._fail_context_flight("context_protocol")
        elif kind in {"CONTEXT", "ERROR"}:
            self._finish_context_flight(msg)
        else:
            # The random per-Hub namespace makes this unambiguously a reply
            # to our request, even if a broken/upgraded firmware returns an
            # unexpected type.  Fail now instead of leaking the private id
            # and pinning every waiter until the long safety timeout.
            log.warning(
                "unexpected %r reply for GET_CONTEXT flight %s", kind, response_id
            )
            self._fail_context_flight("context_protocol")
        return True

    def _dispatch(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except (TypeError, ValueError):
            msg = None
        if isinstance(msg, dict) and self._consume_context_reply(msg):
            return
        if isinstance(msg, dict) and self._consume_patch_reply(msg):
            return
        if isinstance(msg, dict) and self._consume_request_reply(msg):
            return
        if isinstance(msg, dict) and (
            msg.get("type") == "CONTEXT"
            or (msg.get("type") == "EVENT"
                and msg.get("event") in _CONTEXT_BARRIER_EVENTS)
        ):
            self._seal_context_flight()
        if (isinstance(msg, dict) and msg.get("type") == "EVENT"
                and msg.get("event") == "patch_switched"):
            location = self._patch_location(msg)
            if location is not None:
                self._seal_patch_location(location)
        for sub in self._subs:
            sub._offer(line)

    def _dispatch_status(self, line: str) -> None:
        try:
            if json.loads(line).get("link") == "down":
                self._patch_cache.clear()
                # Fail each request under its original id.  Raw clients do
                # not receive HUB status frames, so this is also their only
                # immediate signal that an awaited snapshot cannot finish.
                self._fail_all_context_waiters("link_down")
                self._fail_all_patch_waiters("link_down")
                self._fail_all_requests("link_down")
                # Session teardown is the only cancellation barrier which
                # proves every queued Captain generator has been discarded.
                self._background_tokens.clear()
        except (AttributeError, TypeError, ValueError):
            pass
        for sub in self._subs:
            if sub._want_status:
                sub._offer(line)

    @staticmethod
    def _status_line(up: bool) -> str:
        return json.dumps({"type": "HUB", "link": "up" if up else "down"})
