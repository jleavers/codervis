"""The dashboard's own HTTP server, and the bound on what one peer can make it cost.

This is what the image launches, so it is where the bound belongs: this process is the one that
holds both OAuth tokens and does the parsing, and it is reached by more than one route. `ingress`
publishes the port and reads as far as the end of the *first* request head on each connection it
accepts; it can bound nothing after that, and nothing at all on a connection made to
``codervis:8000`` directly, which any process on that bridge can open (#43).

uvicorn's defaults bound none of it. `--http auto` prefers httptools, which caps a request head at
nothing at all, and no ceiling and no timer is armed until a response has been sent. So this module
names the four bounds and arms them:

- **a request head of at most 16 KiB, on every request of every connection** -- the budget
  `ingress` reads a first head with, and never wider than it. `BoundedHeadH11Protocol` below is
  what enforces it, and its docstring says why h11's own `max_incomplete_event_size` is not enough
  on its own.
- **a complete head within 10 s**, the same deadline `ingress` applies to a first head, applied
  here to every head. Without it a peer that dribbles a head, or opens a socket and says nothing at
  all, holds a counted connection for as long as it likes -- which under the budget below would be
  a way to make the server refuse everybody else.
- **at most 320 connections held at once**, above `ingress`'s 256 so the relay runs out of slots
  before the server does and an SSE stream per browser tab is never what is refused.
- **a complete request body within 10 s of its head**, refused with 408 (#66). uvicorn pauses
  reading a body at 64 KiB, so a body cannot grow memory without bound, but nothing timed one: a
  peer that dribbled a `Content-Length` body it never finished held a counted connection for as
  long as it liked, exactly as a dribbled head did before the deadline above. Measured against
  this configuration before it was armed: a 40-byte body sent a byte at a time was served, 59 s
  after its head.

Choosing h11 moves parsing from httptools' C parser to pure Python. For a dashboard one browser
polls it costs nothing worth measuring, and it is the price of a head limit that exists at all.

What stays unbounded, and must: a **response**. That is what an SSE response is, and it lasts as
long as the browser tab. The first three bounds are spent before a request is dispatched; the body
deadline is the one that is not, because uvicorn dispatches a request as soon as its head is
parsed and the body arrives underneath the running application. So it is armed on h11's own
question -- whether the *client* is still sending -- and never on how long the server has been
answering: it is armed only while `their_state` is `SEND_BODY`, which a request with no body
(every route this dashboard serves, `/api/stream` included) never enters at all. A WebSocket
upgrade is the one thing that sits in that state with no body coming, and is cancelled there.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import re
import sys
from collections.abc import Sequence

import h11
import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

from .egress import response

log = logging.getLogger("app.server")

APP = "app.main:app"
# Loopback, like `app/ingress.py`'s own default: the image's CMD passes `--bind 0.0.0.0` on
# purpose, because the relay has to reach this process across the container network.
DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8000

# The largest request head this server will parse, on every request of every connection. The same
# number `ingress` reads a first head with, and the two are deliberately not *identical*: the
# relay's `readuntil` measures the offset of the head terminator against its limit, so the relay
# admits four bytes more (the terminator itself). The inner layer being the stricter of the two is
# the safe direction; a head either layer refuses gets the same 431 either way.
MAX_REQUEST_HEAD_BYTES = 16 * 1024
# How long a connection may take over a complete request head, counted from the moment it could
# begin one -- when it is accepted, and again when the previous response ends -- and never renewed
# by an arriving byte. The same deadline, and the same reason, as `ingress.REQUEST_TIMEOUT_S`.
# uvicorn's own `timeout_keep_alive` (5 s) closes an idle kept-alive connection sooner than this,
# so in practice this is what bounds a head that has begun, and the accept of a silent socket.
REQUEST_TIMEOUT_S = 10.0
# How long a request body may take to arrive in full, counted from the moment its head was parsed
# and never renewed by an arriving byte, on the same reasoning as the head deadline above and with
# the same value: a peer that dribbles is a peer holding one of the connections below, and which
# half of its request it dribbles makes no difference to what that costs (#66). `ingress` has no
# counterpart -- it relays bytes blind once it has read a first head -- so this is the one bound
# here that only the server holds. No route in this app takes a body at all, so the value is
# generous for every request the dashboard has a use for, and a body still arriving ten seconds
# after its head is traffic it has none for.
REQUEST_BODY_TIMEOUT_S = 10.0
# The most connections this server holds at once. Above `ingress.MAX_CONNECTIONS` (256), so a peer
# at the published port runs the relay out of slots first and each open browser tab -- one
# connection and one streaming task -- is never what is refused. It is handed to uvicorn's
# `limit_concurrency` as well, which counts running tasks too and answers 503 to a request arriving
# once the count has reached it, so the most served at once is one short of this.
MAX_CONNECTIONS = 320

# Refusals byte-identical to the relay's, from the one helper that builds them, because a peer that
# is over the same bound should not be able to tell which layer it reached.
HEAD_TOO_LARGE = response(431, "Request Header Fields Too Large", "request head too large\n")
NO_REQUEST_IN_TIME = response(408, "Request Timeout", "no request in time\n")
TOO_MANY_CONNECTIONS = response(503, "Service Unavailable", "too many connections\n")
# The exception, and it has to be: the relay bounds no body, so there is no refusal of its own to
# be identical to, and a peer that got this one has reached the server whatever it is told. It is
# a separate string rather than `NO_REQUEST_IN_TIME` because the two say different things to
# whoever reads a capture -- one request never arrived, the other arrived and stopped half way.
NO_BODY_IN_TIME = response(408, "Request Timeout", "no request body in time\n")

# Where a request head ends -- h11's own rule (`h11._receivebuffer`), not an approximation of it,
# so this never counts less head than h11 will parse and never counts a body as head. It covers
# all three spellings h11 accepts: `\r\n\r\n`, a bare `\n\n`, and the mixed `\n\r\n`.
HEAD_END = re.compile(b"\n\r?\n")


def pending_head_length(pending: bytes) -> int:
    """How much of ``pending`` belongs to a request head that has not been parsed yet.

    Up to and including the terminator where there is one, so bytes a client pipelined behind the
    head, or sent as its body, are not charged to it; the whole of it where there is not, because
    then all of it is head so far. This is `asyncio.StreamReader.readuntil`'s accounting, which is
    what `ingress` bounds with, rather than "everything that has arrived".
    """
    end = HEAD_END.search(pending)
    return end.end() if end else len(pending)


class BoundedHeadH11Protocol(H11Protocol):
    """uvicorn's h11 protocol, plus the bounds on what one peer may cost that it does not hold.

    **The size.** h11 has a limit of its own, which uvicorn exposes as
    `h11_max_incomplete_event_size`, but it is checked in only one place: `h11.Connection`'s
    `next_event()` raises when it has to answer ``NEED_DATA`` and the buffer is already over the
    limit. A head that arrives *complete* inside one socket read is therefore extracted and served
    however large it is, because the parser never has to ask for more. Measured against this app
    before this class existed, with the 16 KiB limit set: a 20 KiB head in one write got 200, and
    so did 50 KiB and 80 KiB; the first refusal was at 200 KiB, where the kernel split the write.
    So the real bound was one read of the socket -- non-deterministic, and several times the figure
    every document here states. A head sent in one write, or dripped, is checked here *before* h11
    is handed the bytes. A head **pipelined** behind a request that is fine cannot be: the bytes in
    front of it have to reach the parser for that request to be served, so it is checked when h11's
    buffer is next parsed, by which time h11 holds one socket read of it (`flow.pause_reading()`
    stops a second) rather than as much as the peer cares to send.

    **The time.** uvicorn arms no timer until it has sent a response, and its keep-alive timer is
    cancelled by the first byte of the next request and not re-armed, so a peer could hold a
    connection open indefinitely without ever completing a head. The deadline here is armed while a
    connection is waiting for a complete head -- from when it is accepted, and again when the
    previous response ends -- and cancelled once a request has been dispatched, so it never touches
    a response in flight: an SSE stream is dispatched long before it is slow.

    **The number.** uvicorn's `limit_concurrency` is not admission control: it is checked where a
    `Request` event is parsed, so an over-budget connection is accepted and counted, and its
    *request* is answered 503. Measured with the shipped ceiling of 320: 800 connections were held
    at once, refused nothing, and were reclaimed only as each one's own head deadline expired. So
    the count is checked here too, where the connection is accepted, as `ingress` does.

    Those three are bounds `ingress` already applies at its own door -- to a first head, and to a
    connection -- applied here to every head and every route, with the relay's own refusals.
    A subclass is the least the job takes: `h11_max_incomplete_event_size` is still passed (it
    bounds h11's own buffer in the incomplete case), but on its own it is a document, not a bound.

    **The body** (#66) is the fourth, and the only one with no counterpart in the relay, which
    relays bytes blind once it has read a first head. uvicorn pauses reading a body at 64 KiB, so
    the memory is bounded; the *time* was not, and a peer that dribbled a `Content-Length` body it
    never finished held a counted connection for as long as it liked. Its deadline is armed when
    h11 says the client is still sending one (`their_state` is `SEND_BODY`) and cancelled the
    moment it stops, so it measures the peer's half of the exchange and never the server's.

    That distinction is what keeps it off SSE. uvicorn dispatches a request as soon as its head is
    parsed, so this is the one bound here that is armed *after* dispatch -- but a request with no
    body never enters `SEND_BODY` at all, and every route this dashboard serves is a `GET`, so
    `/api/stream` is never under it for an instant. Where a body and a long response do overlap,
    the deadline ends with the body and the response runs on untouched. A WebSocket upgrade is the
    one place h11 sits in `SEND_BODY` with no body coming; `handle_websocket_upgrade` below is
    what keeps a deadline off it.
    """

    # The bounds, as class attributes: uvicorn instantiates the protocol itself, with a fixed
    # signature, so a test that needs a shorter deadline subclasses this rather than reaching
    # into an instance. `build_config()` is handed the class, and its default is this one.
    max_request_head_bytes = MAX_REQUEST_HEAD_BYTES
    request_timeout_s = REQUEST_TIMEOUT_S
    request_body_timeout_s = REQUEST_BODY_TIMEOUT_S
    _head_deadline: asyncio.TimerHandle | None = None
    _body_deadline: asyncio.TimerHandle | None = None
    # Set once this connection has been handed to the WebSocket protocol; see
    # `handle_websocket_upgrade` for why a deadline must never outlive that.
    _upgraded = False
    # Shared by every connection this class serves, like uvicorn's own `connections` set, and only
    # so that a flood is one log line rather than one per socket. `ingress` keeps the same pair.
    _saturated = False

    # Protocol interface

    def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
        super().connection_made(transport)
        # The budget is the one number uvicorn was given (`limit_concurrency`), enforced here at
        # the accept as well as where uvicorn checks it. `super()` has already added this
        # connection to the shared set, so this count includes it: at most the budget is held.
        held = len(self.connections)
        if self.limit_concurrency is not None:
            if held > self.limit_concurrency:
                self._log_saturation(held)
                self._refuse(TOO_MANY_CONNECTIONS)
                return
            if self._saturated and held <= self.limit_concurrency * 3 // 4:
                type(self)._saturated = False
        # Armed from the moment it is accepted, not from its first byte: a socket that says nothing
        # is the cheapest way to hold a slot.
        self._arm_head_deadline()

    def _log_saturation(self, held: int) -> None:
        if not self._saturated:
            type(self)._saturated = True
            log.warning(
                "server_connections_exhausted held=%d limit=%s", held, self.limit_concurrency
            )

    def connection_lost(self, exc: Exception | None) -> None:
        self._cancel_head_deadline()
        self._cancel_body_deadline()
        super().connection_lost(exc)

    def data_received(self, data: bytes) -> None:
        if self._awaiting_head() and self._head_is_over_bound(data):
            # Before `super()`, so the oversized head never reaches the parser.
            self._refuse(HEAD_TOO_LARGE)
            return
        super().data_received(data)
        self._reconsider_deadlines()

    def handle_events(self) -> None:
        # The other way in: a request the client pipelined behind the last one is parsed out of
        # h11's buffer when the previous response completes, with no `data_received` to see it.
        if self._awaiting_head() and self._head_is_over_bound():
            self._refuse(HEAD_TOO_LARGE)
            return
        super().handle_events()
        self._reconsider_deadlines()

    def handle_websocket_upgrade(self, event: h11.Request) -> None:  # type: ignore[override]
        """Hand the connection on, with nothing of this protocol's left armed against it.

        This is the one place `their_state is SEND_BODY` and "the client is sending a body" come
        apart, and both halves of the gap are uvicorn's: it calls this from `handle_events` and
        `return`s straight afterwards, *before* the `EndOfMessage` that would take h11 out of
        `SEND_BODY`, and the last thing it does here is `transport.set_protocol()`, after which
        this object's `connection_lost` is never called again. So a body deadline armed on the way
        out of that `handle_events` would never be cancelled, and would fire ten seconds later
        against a transport that is now carrying an established WebSocket stream -- writing an
        HTTP 408 into the middle of it and closing it. `_upgraded` is what stops the re-arm, since
        cancelling here is not enough on its own: uvicorn's `return` lands back in the override
        above, which reconsiders the deadlines one last time. Both happen *before* `super()`, so
        a handover that raises part way leaves nothing armed either.
        """
        self._upgraded = True
        self._cancel_head_deadline()
        self._cancel_body_deadline()
        super().handle_websocket_upgrade(event)

    # The bounds' own state

    def _head_is_over_bound(self, arriving: bytes = b"") -> bool:
        """Whether the head being received, plus ``arriving``, is over the cap.

        The length is measured first, and the buffer is only copied and scanned when that total is
        over the cap -- `pending_head_length` can never return more than the length it is given, so
        the two agree on everything under it.

        Measuring it without copying is the point, and is why h11's buffer is reached for directly
        rather than through `trailing_data`: that property is `bytes(self._receive_buffer)`, a copy
        of the whole buffer, on every call. Copying it on every read is the cost this bound exists
        to prevent, pushed onto the server instead of the peer -- a 16 KiB head dripped a byte at a
        time measured 134 MB copied, for 16 KB sent, and a peer may repeat it on every request of a
        kept-alive connection. `HEAD_END` above already follows h11's own internals for the same
        reason, and `requirements.txt` pins the version; the fallback keeps a version without that
        attribute correct, merely slower.
        """
        buffer = getattr(self.conn, "_receive_buffer", None)
        held = len(buffer) if buffer is not None else len(self.conn.trailing_data[0])
        if held + len(arriving) <= self.max_request_head_bytes:
            return False
        buffered, _ = self.conn.trailing_data
        return pending_head_length(buffered + arriving) > self.max_request_head_bytes

    def _awaiting_head(self) -> bool:
        """Whether this connection is between requests, waiting for one to arrive.

        h11's `their_state` leaves `IDLE` when a `Request` event is parsed, and returns to it when
        the cycle after that response starts, so this is false for exactly as long as a request is
        being received or served. The body deadline below takes the first half of that span; the
        second -- a response in flight -- is the part nothing here may bound.
        """
        return self.conn.their_state is h11.IDLE

    def _awaiting_body(self) -> bool:
        """Whether the client is part way through sending a request body.

        h11's `their_state` is `SEND_BODY` from the moment a `Request` event with a body is parsed
        until its `EndOfMessage`, and a request with no body goes straight from `IDLE` to `DONE`
        without passing through it. So this is true for exactly the span the body deadline exists
        to bound, and false for every request this dashboard actually serves. The one shape where
        it is true and no body is coming is a WebSocket upgrade, which freezes h11 here and hands
        the transport away; `handle_websocket_upgrade` is where that is accounted for.
        """
        return self.conn.their_state is h11.SEND_BODY

    def _reconsider_deadlines(self) -> None:
        """Arm whichever deadline this connection's h11 state calls for, and cancel the other.

        The three states are exclusive by construction: waiting for a head, receiving a body, or
        neither -- the last being a request in flight or a response being written, which nothing
        here may bound. Each arm is a no-op while its own timer is already running, which is what
        keeps a deadline from being renewed by the bytes it is meant to be bounding.
        """
        if self._upgraded:
            # The transport belongs to another protocol now, and h11's state is frozen where the
            # upgrade left it. Nothing of this connection's may be armed; see the override above.
            self._cancel_head_deadline()
            self._cancel_body_deadline()
            return
        if self._awaiting_head():
            self._cancel_body_deadline()
            self._arm_head_deadline()
        elif self._awaiting_body():
            self._cancel_head_deadline()
            self._arm_body_deadline()
        else:
            self._cancel_head_deadline()
            self._cancel_body_deadline()

    def _arm_head_deadline(self) -> None:
        # Never renewed while one is armed. A deadline that each arriving byte pushed out would
        # bound nothing at all, which is the defect `ingress`'s own whole-head timeout avoids.
        if self._head_deadline is None:
            self._head_deadline = self.loop.call_later(
                self.request_timeout_s, self._head_timed_out
            )

    def _cancel_head_deadline(self) -> None:
        if self._head_deadline is not None:
            self._head_deadline.cancel()
            self._head_deadline = None

    def _head_timed_out(self) -> None:
        self._head_deadline = None
        if self._awaiting_head() and not self._upgraded:
            self._refuse(NO_REQUEST_IN_TIME)

    def _arm_body_deadline(self) -> None:
        # Never renewed while one is armed, for the same reason as the head's: a deadline each
        # arriving byte pushed out is what a dribbling peer would be renewing.
        if self._body_deadline is None:
            self._body_deadline = self.loop.call_later(
                self.request_body_timeout_s, self._body_timed_out
            )

    def _cancel_body_deadline(self) -> None:
        if self._body_deadline is not None:
            self._body_deadline.cancel()
            self._body_deadline = None

    def _body_timed_out(self) -> None:
        """Refuse a body that has not finished arriving, if one still has not.

        The guard is the same backstop the head deadline has, and it carries more here: this timer
        is armed while an application is running, so a stray firing has a response to collide with.
        A refusal is only *written* where uvicorn has not begun one -- two responses on one
        connection is worse than none, and an application that has already answered has nothing to
        be told. Either way the connection goes, which is the resource at stake.
        """
        self._body_deadline = None
        if self._upgraded or not self._awaiting_body():
            # `_upgraded` cannot be true here while the cancels above hold, and is checked anyway:
            # this is the branch that would write HTTP into a WebSocket stream if they ever did not.
            return
        cycle = self.cycle
        if cycle is not None:
            # Before anything is written: `RequestResponseCycle.send()` returns early once this is
            # set, so the application cannot append a response behind the refusal below. Closing
            # the transport alone does not guarantee that -- asyncio drops later writes on a
            # counter it only increments when the buffer was already empty.
            cycle.disconnected = True
            if cycle.response_started:
                log.debug("server_dropped reason=slow_body_under_a_begun_response")
                self._close()
                return
        self._refuse(NO_BODY_IN_TIME)

    def _refuse(self, payload: bytes) -> None:
        """Answer a raw response and close, without h11.

        h11 will not let a server send a response before it has received a request, and three of
        the four refusals here are sent before one has arrived; those three are byte for byte the
        ones `ingress` sends for the same cases, so a peer over a bound both layers hold cannot
        tell which one it reached. The fourth, a body that stopped half way, goes the same way for
        consistency and because h11's cycle is about to be abandoned with the connection anyway.

        The status alone is logged, never anything a peer sent: the exception raised for a
        malformed head quotes the head, and a head carries whatever the peer put in it.
        """
        if not self.transport.is_closing():
            log.debug("server_refused status=%s", payload[9:12].decode("ascii", "replace"))
            with contextlib.suppress(OSError):
                self.transport.write(payload)
        self._close()

    def _close(self) -> None:
        """Drop the connection, with no deadline of this connection's left to fire against it."""
        self._cancel_head_deadline()
        self._cancel_body_deadline()
        self.transport.close()


def build_config(
    app: object = APP,
    *,
    bind: str = DEFAULT_BIND,
    port: int = DEFAULT_PORT,
    protocol: type[H11Protocol] = BoundedHeadH11Protocol,
    max_connections: int = MAX_CONNECTIONS,
) -> uvicorn.Config:
    """The server configuration the image runs. ``app`` is an import string or an ASGI callable.

    Building it imports nothing of the app: uvicorn loads the import string when it starts.
    """
    return uvicorn.Config(
        app,
        host=bind,
        port=port,
        http=protocol,
        # h11's own limit, for the incomplete-head case it does cover. The protocol class above is
        # what makes the bound true for every shape; this is the layer under it.
        h11_max_incomplete_event_size=MAX_REQUEST_HEAD_BYTES,
        # The other half of the number: uvicorn's own check, which counts running tasks as well
        # as connections. The protocol class above is what refuses an over-budget connection at
        # the accept, which this does not do.
        limit_concurrency=max_connections,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.server", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--bind", default=DEFAULT_BIND)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        print("server: --port must be between 0 and 65535", file=sys.stderr)
        return 1
    # As `app.ingress` and `app.egress` do it, and for the same reason: without it the one line
    # that says the front door is refusing connections falls through to `logging.lastResort`, with
    # no level, time or logger name. uvicorn's own logging config leaves existing loggers alone and
    # does not touch the root, so this survives its start without doubling any line. The per-refusal
    # line stays below this level, for whoever turns the root logger down to DEBUG.
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    # A port that cannot be bound is uvicorn's own error to report: it logs the reason and exits
    # non-zero from inside `run()`, so there is nothing useful to add here.
    uvicorn.Server(build_config(bind=args.bind, port=args.port)).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
