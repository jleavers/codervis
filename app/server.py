"""The dashboard's own HTTP server, and the bound on what one peer can make it cost.

This is what the image launches, so it is where the bound belongs: this process is the one that
holds both OAuth tokens and does the parsing, and it is reached by more than one route. `ingress`
publishes the port and reads as far as the end of the *first* request head on each connection it
accepts; it can bound nothing after that, and nothing at all on a connection made to
``codervis:8000`` directly, which any process on that bridge can open (#43).

uvicorn's defaults bound none of it. `--http auto` prefers httptools, which caps a request head at
nothing at all, and no ceiling and no timer is armed until a response has been sent. So this module
names the three bounds and arms them:

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

Choosing h11 moves parsing from httptools' C parser to pure Python. For a dashboard one browser
polls it costs nothing worth measuring, and it is the price of a head limit that exists at all.

What stays unbounded, and must: time *after* a request has been dispatched. That is what an SSE
response is, and it lasts as long as the browser tab. Every bound above is spent before dispatch.
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
    """uvicorn's h11 protocol, plus the two bounds on a request head that it does not hold.

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

    All three are bounds `ingress` already applies at its own door -- to a first head, and to a
    connection -- applied here to every head and every route, with the relay's own refusals.
    A subclass is the least the job takes: `h11_max_incomplete_event_size` is still passed (it
    bounds h11's own buffer in the incomplete case), but on its own it is a document, not a bound.
    """

    # The bounds, as class attributes: uvicorn instantiates the protocol itself, with a fixed
    # signature, so a test that needs a shorter deadline subclasses this rather than reaching
    # into an instance. `build_config()` is handed the class, and its default is this one.
    max_request_head_bytes = MAX_REQUEST_HEAD_BYTES
    request_timeout_s = REQUEST_TIMEOUT_S
    _head_deadline: asyncio.TimerHandle | None = None
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
        super().connection_lost(exc)

    def data_received(self, data: bytes) -> None:
        if self._awaiting_head() and self._head_is_over_bound(data):
            # Before `super()`, so the oversized head never reaches the parser.
            self._refuse(HEAD_TOO_LARGE)
            return
        super().data_received(data)
        self._reconsider_head_deadline()

    def handle_events(self) -> None:
        # The other way in: a request the client pipelined behind the last one is parsed out of
        # h11's buffer when the previous response completes, with no `data_received` to see it.
        if self._awaiting_head() and self._head_is_over_bound():
            self._refuse(HEAD_TOO_LARGE)
            return
        super().handle_events()
        self._reconsider_head_deadline()

    # The head bound's own state

    def _head_is_over_bound(self, arriving: bytes = b"") -> bool:
        """Whether the head being received, plus ``arriving``, is over the cap.

        The length is checked first and the buffer only copied and scanned when that total is over
        the cap, because `pending_head_length` can never return more than the length it is given.
        Scanning every read would make this bound expensive in the one way it exists to prevent:
        a head dripped a byte at a time would be copied and re-scanned from the start each time,
        which measured 134 MB of copying for 16 KB sent -- a peer paying nothing to make the
        server pay.
        With the check below, the scan runs only for a head that is already over its budget.
        """
        buffered, _ = self.conn.trailing_data
        if len(buffered) + len(arriving) <= self.max_request_head_bytes:
            return False
        return pending_head_length(buffered + arriving) > self.max_request_head_bytes

    def _awaiting_head(self) -> bool:
        """Whether this connection is between requests, waiting for one to arrive.

        h11's `their_state` leaves `IDLE` when a `Request` event is parsed, and returns to it when
        the cycle after that response starts, so this is false for exactly as long as a request is
        being received or served -- which is the part that must not be bounded.
        """
        return self.conn.their_state is h11.IDLE

    def _reconsider_head_deadline(self) -> None:
        if self._awaiting_head():
            self._arm_head_deadline()
        else:
            self._cancel_head_deadline()

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
        if self._awaiting_head():
            self._refuse(NO_REQUEST_IN_TIME)

    def _refuse(self, payload: bytes) -> None:
        """Answer a raw response and close, without h11.

        h11 will not let a server send a response before it has received a request, and none of
        the three refusals here has one; these are the bytes `ingress` sends for the same cases,
        so a peer over a bound both layers hold cannot tell which one it reached.

        The status alone is logged, never anything a peer sent: the exception raised for a
        malformed head quotes the head, and a head carries whatever the peer put in it.
        """
        self._cancel_head_deadline()
        if self.transport.is_closing():
            return
        log.debug("server_refused status=%s", payload[9:12].decode("ascii", "replace"))
        with contextlib.suppress(OSError):
            self.transport.write(payload)
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
    # no level, time or logger name, and the debug line below it could never be emitted at all.
    # uvicorn's own logging config leaves existing loggers alone, so this survives its start.
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    # A port that cannot be bound is uvicorn's own error to report: it logs the reason and exits
    # non-zero from inside `run()`, so there is nothing useful to add here.
    uvicorn.Server(build_config(bind=args.bind, port=args.port)).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
