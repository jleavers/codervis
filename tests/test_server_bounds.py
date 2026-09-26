"""The bound the dashboard's own server holds, rather than the one its relay holds for it.

`ingress` reads the first request head of each connection it accepts and nothing after it, and
connections made straight to ``codervis:8000`` never reach it at all, so every assertion here goes
through `app.server`'s own configuration to a real server on loopback and asks what the *server*
refuses (#43). Hermetic: a stub ASGI app, an ephemeral loopback port, no dashboard and no network.

Every oversized head is sent in three shapes, because only one of them was ever refused before this
was written: complete in one write, dripped a chunk at a time, and pipelined behind a request that
is fine. h11's own `max_incomplete_event_size` catches the dripped one alone.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import select
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import h11
import pytest
import uvicorn

from app import ingress
from app.server import (
    HEAD_TOO_LARGE,
    TOO_MANY_CONNECTIONS,
    APP,
    MAX_CONNECTIONS,
    MAX_REQUEST_HEAD_BYTES,
    NO_BODY_IN_TIME,
    NO_REQUEST_IN_TIME,
    REQUEST_BODY_TIMEOUT_S,
    REQUEST_TIMEOUT_S,
    BoundedHeadH11Protocol,
    build_config,
    main,
    pending_head_length,
)

ROOT = Path(__file__).resolve().parents[1]
HEAD = b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
BODY = b"ok"
# Enough over the cap to be unambiguous, and small enough that the kernel delivers it in one read
# on loopback -- which is the shape that used to be served.
OVERSIZED = 5 * MAX_REQUEST_HEAD_BYTES


def oversized_head(size: int = OVERSIZED) -> bytes:
    """One complete request head larger than the cap."""
    padding = b"a" * (size - len(HEAD) - len("X-Pad: \r\n"))
    return b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Pad: " + padding + b"\r\n\r\n"


class Impatient(BoundedHeadH11Protocol):
    """The shipped protocol with a deadline a test can wait out.

    The values are class attributes precisely so a test can do this without touching the shipped
    ones; `test_the_servers_bounds_are_the_ones_it_documents` pins those.
    """

    request_timeout_s = 0.75
    request_body_timeout_s = 0.75


async def _stub(scope: dict, receive, send) -> None:
    """The smallest thing uvicorn will serve: what is under test is the server, not the app."""
    if scope["type"] != "http":
        return
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", str(len(BODY)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": BODY})


async def _slow_stream(scope: dict, receive, send) -> None:
    """A response shaped like `/api/stream`: dispatched at once, then open far longer than the
    head deadline. Nothing may cut this off -- it is the reason the deadline stops at dispatch."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    for _ in range(6):
        await send({"type": "http.response.body", "body": b"tick\n", "more_body": True})
        await asyncio.sleep(Impatient.request_timeout_s / 2)
    await send({"type": "http.response.body", "body": b"", "more_body": False})


async def _body_reader(scope: dict, receive, send) -> None:
    """A handler that reads its request body before answering, which is what makes the answer say
    when the body *finished*. No route in this app takes a body, but the bound has to hold for one
    that did, and reading the body is the only shape where a slow one delays a response at all."""
    received = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return
        received += len(message.get("body", b""))
        if not message.get("more_body", False):
            break
    payload = str(received).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", str(len(payload)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": payload})


async def _websocket(scope: dict, receive, send) -> None:
    """Accepts an upgrade and holds it open for longer than any deadline here.

    `requirements.txt` pins `uvicorn[standard]`, so the image has a WebSocket library and this
    branch of uvicorn's `handle_events` is live whether or not the dashboard routes to it.
    """
    if scope["type"] != "websocket":
        await _stub(scope, receive, send)
        return
    await receive()
    await send({"type": "websocket.accept"})
    await asyncio.sleep(Impatient.request_body_timeout_s * 5)
    await send({"type": "websocket.close", "code": 1000})


async def _slow_stream_after_a_body(scope: dict, receive, send) -> None:
    """Reads a whole request body, then streams for far longer than the body deadline: the shape
    that proves the deadline ends with the body rather than running on into the response."""
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return
        if not message.get("more_body", False):
            break
    await _slow_stream(scope, receive, send)


@contextlib.contextmanager
def _running(app=_stub, **bounds) -> Iterator[int]:
    """The configuration `app/server.py` builds, on an ephemeral loopback port. Yields the port.

    Only what a test names is overridden; everything else is the shipped value, so a behaviour
    proved here is proved of the configuration the image runs.
    """
    server = uvicorn.Server(build_config(app, bind="127.0.0.1", port=0, **bounds))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert thread.is_alive(), "the server thread died before it started listening"
        assert time.monotonic() < deadline, "the server did not start listening in time"
        time.sleep(0.02)
    try:
        yield server.servers[0].sockets[0].getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(20)


def asynctest(fn):
    """Run an async test body on a fresh loop; the suite needs no asyncio plugin for this."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


def _connect(port: int) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), 10)
    sock.settimeout(20)
    return sock


def _status(sock: socket.socket) -> bytes:
    """The status line of the next response, or ``b""`` if the peer said nothing."""
    try:
        return sock.recv(4096).split(b"\r\n", 1)[0]
    except OSError:
        return b""


def _drain(sock: socket.socket) -> bytes:
    """Everything the peer sends until it closes."""
    chunks = []
    with contextlib.suppress(OSError):
        while chunk := sock.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


def _statuses(payload: bytes) -> list[bytes]:
    return [line for line in payload.split(b"\r\n") if line.startswith(b"HTTP/1.1 ")]


def _drip(sock: socket.socket, head: bytes, *, chunk: int = 1024) -> int:
    """Send ``head`` a chunk at a time, stopping as soon as the peer answers. Returns bytes sent.

    Stopping matters: a server that answers and closes straight after would draw a reset from the
    bytes still going the other way, and the reset discards the answer being asserted on.
    """
    sent = 0
    deadline = time.monotonic() + 20
    while sent < len(head) and time.monotonic() < deadline:
        if select.select([sock], [], [], 0.01)[0]:
            break
        try:
            sock.sendall(head[sent : sent + chunk])
        except OSError:
            break
        sent += chunk
    return min(sent, len(head))


# The size bound, in each of the three shapes


def test_an_oversized_head_arriving_in_one_write_is_refused() -> None:
    """The shape h11's own limit misses: it is only checked when the parser asks for more data, so
    a head that is complete in the buffer is extracted whatever its size. 20, 50 and 80 KiB heads
    were all served with 200 before `BoundedHeadH11Protocol` existed."""
    with _running() as port:
        sock = _connect(port)
        try:
            sock.sendall(oversized_head())
            assert _status(sock).startswith(b"HTTP/1.1 431 ")
        finally:
            sock.close()


def test_an_oversized_second_head_on_a_kept_alive_connection_is_refused() -> None:
    """front-door-1: the cap applies to every request on a connection, not the first alone.

    `ingress` parses one head per connection and then relays bytes blind, so this is the request
    no layer in front of the server sees.
    """
    with _running() as port:
        sock = _connect(port)
        try:
            sock.sendall(HEAD)
            assert _status(sock).startswith(b"HTTP/1.1 200 "), "the first request must be served"
            sock.sendall(oversized_head())
            assert _status(sock).startswith(b"HTTP/1.1 431 ")
        finally:
            sock.close()


def test_a_dripped_oversized_head_is_refused_before_it_is_all_sent() -> None:
    """The bytes accepted are asserted as well as the status: a parser that reads the whole head
    and complains afterwards has already paid for it."""
    with _running() as port:
        sock = _connect(port)
        try:
            sent = _drip(sock, oversized_head(64 * MAX_REQUEST_HEAD_BYTES))
            assert _status(sock).startswith(b"HTTP/1.1 431 ")
            assert sent <= 4 * MAX_REQUEST_HEAD_BYTES, (
                f"the server took {sent} bytes of one request head, "
                f"with a cap of {MAX_REQUEST_HEAD_BYTES}"
            )
        finally:
            sock.close()


def test_a_legal_head_is_neither_rescanned_nor_recopied_on_every_read(monkeypatch) -> None:
    """The cap must not be expensive to apply, or it is a cost a peer can inflict rather than one
    it is charged. A head inside the budget is neither scanned nor copied: the length is measured
    off h11's buffer without materialising it, and only a head already over its budget is copied
    and searched. Both halves are asserted, because fixing only the scan left the copying --
    `trailing_data` is `bytes(self._receive_buffer)` -- and that was all 134 MB of it.
    """
    from app import server as module

    scans = 0
    real_scan = module.pending_head_length

    def counting_scan(pending: bytes) -> int:
        nonlocal scans
        scans += 1
        return real_scan(pending)

    copied = 0
    real_property = h11.Connection.trailing_data

    def counting_copy(self):
        nonlocal copied
        data, closed = real_property.fget(self)
        copied += len(data)
        return data, closed

    monkeypatch.setattr(module, "pending_head_length", counting_scan)
    monkeypatch.setattr(h11.Connection, "trailing_data", property(counting_copy))
    head = _head_of_exactly(MAX_REQUEST_HEAD_BYTES - 10)
    with _running() as port:
        sock = _connect(port)
        try:
            for offset in range(0, len(head), 256):
                sock.sendall(head[offset : offset + 256])
            assert _status(sock).startswith(b"HTTP/1.1 200 ")
        finally:
            sock.close()
    assert scans == 0, f"a head inside the budget was scanned {scans} times"
    assert copied <= MAX_REQUEST_HEAD_BYTES, (
        f"{copied} bytes of h11's buffer were copied for a {len(head)}-byte head"
    )


def test_an_oversized_head_is_never_handed_to_the_parser() -> None:
    """Refusing early is the point, not only refusing: `data_received` checks before it lets h11
    have the bytes, so an oversized head is never buffered by the parser at all. Without that
    check the connection is still refused -- `handle_events` catches it a moment later -- but h11
    has taken a socket read's worth of head first, which is the cost the bound exists to avoid."""
    handed: list[int] = []

    class Watched(BoundedHeadH11Protocol):
        def connection_made(self, transport) -> None:  # type: ignore[override]
            super().connection_made(transport)
            received = self.conn.receive_data

            def watched(data: bytes) -> None:
                handed.append(len(data))
                received(data)

            self.conn.receive_data = watched  # type: ignore[method-assign]

    with _running(protocol=Watched) as port:
        sock = _connect(port)
        try:
            sock.sendall(oversized_head())
            assert _status(sock).startswith(b"HTTP/1.1 431 ")
        finally:
            sock.close()
    assert sum(handed) <= MAX_REQUEST_HEAD_BYTES, (
        f"h11 was handed {sum(handed)} bytes of a head the server refuses at "
        f"{MAX_REQUEST_HEAD_BYTES}"
    )


def test_an_oversized_head_pipelined_behind_a_good_one_is_never_served() -> None:
    """The third way in, and the one with no `data_received` of its own: a request the client
    pipelined arrives in the same read as the first and is parsed out of h11's buffer when that
    first response completes, which is why `handle_events` carries the check too.

    Asserted on what the *app* was asked for, not on what the client read back. The refusal is
    written, but closing with the peer's own bytes still unread draws a reset that can discard it,
    so a client-side assertion here passes just as happily when the oversized request was served
    and its answer lost -- which is exactly what happens with the check removed.
    """
    served: list[str] = []

    async def recording(scope: dict, receive, send) -> None:
        served.append(scope["path"])
        await _stub(scope, receive, send)

    with _running(recording) as port:
        sock = _connect(port)
        try:
            oversized = oversized_head().replace(b"GET / ", b"GET /oversized ", 1)
            sock.sendall(HEAD + oversized)
            _drain(sock)
        finally:
            sock.close()
    assert served == ["/"], f"the oversized pipelined head reached the app: {served}"


def _head_of_exactly(size: int) -> bytes:
    """A complete, valid request head of exactly ``size`` bytes."""
    stem = b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Pad: " + b"\r\n\r\n"
    head = stem[:-4] + b"a" * (size - len(stem)) + b"\r\n\r\n"
    assert len(head) == size
    return head


@pytest.mark.parametrize(
    ("size", "expected"),
    [(MAX_REQUEST_HEAD_BYTES, b"HTTP/1.1 200 "), (MAX_REQUEST_HEAD_BYTES + 1, b"HTTP/1.1 431 ")],
)
def test_the_cap_is_the_boundary_it_says_it_is(size: int, expected: bytes) -> None:
    """Both sides of it, to the byte. A cap that refused at exactly 16 KiB instead of over it
    would pass every other test here while putting the two front doors one byte apart -- and
    `test_both_front_doors_refuse_the_same_head_the_same_way` is the property that breaks."""
    with _running() as port:
        sock = _connect(port)
        try:
            sock.sendall(_head_of_exactly(size))
            assert _status(sock).startswith(expected)
        finally:
            sock.close()


def test_pipelined_bytes_are_not_charged_to_the_head_in_front_of_them() -> None:
    """`pending_head_length()` is what keeps the cap a *head* cap: a client that sends a body, or
    a second request, in the same write as a small head is not refused for the total."""
    assert pending_head_length(b"GET / HTTP/1.1\r\n\r\n" + b"x" * 10_000) == 18
    assert pending_head_length(b"GET / HTTP/1.1\n\n" + b"x" * 10_000) == 16
    # h11 ends a head on `\n\r?\n`, so `\n\r\n` is a third spelling: missing it charged a whole
    # body to the head in front of it and refused traffic h11 would have served.
    assert pending_head_length(b"GET / HTTP/1.1\n\r\n" + b"x" * 10_000) == 17
    assert pending_head_length(b"GET / HTTP/1.1\r\nHost: x\r\n") == 25


# The time bound


def test_a_socket_that_says_nothing_does_not_hold_its_slot() -> None:
    """The reason a deadline is here at all. `limit_concurrency` counts connections, so without
    this a peer could hold every slot with sockets that never send a byte and make the server
    answer 503 to everyone -- a worse outage than the unbounded head it replaced."""
    with _running(protocol=Impatient) as port:
        sock = _connect(port)
        try:
            started = time.monotonic()
            assert _status(sock).startswith(b"HTTP/1.1 408 ")
            assert time.monotonic() - started < 10
        finally:
            sock.close()


def test_a_head_dripped_slower_than_the_deadline_is_refused() -> None:
    """And the deadline is not renewed by each byte, which is the whole point: a client that keeps
    trickling would otherwise hold the connection for as long as it liked, inside the size cap.

    So *when* the refusal comes is the assertion, not just that one does. A deadline re-armed on
    every chunk still fires -- one interval after the client stops -- and a test that only waited
    for a 408 would call that a pass while a drip could be held for as long as the peer liked.
    """
    timeout = Impatient.request_timeout_s
    with _running(protocol=Impatient) as port:
        sock = _connect(port)
        try:
            sock.sendall(b"GET / HTTP/1.1\r\n")
            started = time.monotonic()
            drip_until = started + timeout * 8
            while time.monotonic() < drip_until:
                if select.select([sock], [], [], 0)[0]:
                    break
                with contextlib.suppress(OSError):
                    sock.sendall(b"X-P: a\r\n")
                time.sleep(timeout / 4)
            refused_after = time.monotonic() - started
            assert _status(sock).startswith(b"HTTP/1.1 408 ")
            assert refused_after < timeout * 3, (
                f"the drip was allowed to run for {refused_after:.1f}s against a {timeout}s "
                "deadline: the deadline is being renewed by the arriving bytes"
            )
        finally:
            sock.close()


def test_the_deadline_does_not_touch_a_response_in_flight() -> None:
    """SSE is the reason the bounds stop at dispatch: `/api/stream` lasts as long as the browser
    tab, and a deadline that covered it would cut every dashboard off after ten seconds."""
    with _running(_slow_stream, protocol=Impatient) as port:
        sock = _connect(port)
        try:
            sock.sendall(HEAD)
            body = _drain(sock)
            assert body.startswith(b"HTTP/1.1 200 "), body[:80]
            assert body.count(b"tick") == 6, body[-200:]
        finally:
            sock.close()


def test_a_second_head_dripped_on_a_served_connection_is_timed_out() -> None:
    """front-door-1's other shape: the issue names an oversized second head *and* a drip-fed one.
    The size cap answers the first; this is the second, and only the deadline answers it -- the
    connection has already been served once, so `ingress` relayed it blind and uvicorn's keep-alive
    timer was cancelled by its first byte and never re-armed."""
    timeout = Impatient.request_timeout_s
    with _running(protocol=Impatient) as port:
        sock = _connect(port)
        try:
            sock.sendall(HEAD)
            assert _status(sock).startswith(b"HTTP/1.1 200 ")
            sock.sendall(b"GET / HTTP/1.1\r\n")
            started = time.monotonic()
            drip_until = started + timeout * 8
            while time.monotonic() < drip_until:
                if select.select([sock], [], [], 0)[0]:
                    break
                with contextlib.suppress(OSError):
                    sock.sendall(b"X-P: a\r\n")
                time.sleep(timeout / 4)
            refused_after = time.monotonic() - started
            assert _status(sock).startswith(b"HTTP/1.1 408 ")
            assert refused_after < timeout * 3, refused_after
        finally:
            sock.close()


def test_the_deadline_refuses_only_while_a_head_is_awaited() -> None:
    """The guard at the moment the timer fires, which is what makes a stray one harmless.

    Two mechanisms keep a deadline off a response in flight: it is cancelled once a request is
    dispatched (below), and it refuses nothing unless the connection is still waiting for a head.
    They are each other's backstop, so `test_the_deadline_does_not_touch_a_response_in_flight`
    stays green when either one alone is broken. This pins the second directly; a fake connection
    is enough, since the branch is a question about h11's state and nothing else.
    """
    protocol = BoundedHeadH11Protocol.__new__(BoundedHeadH11Protocol)
    refused: list[bytes] = []
    protocol._refuse = refused.append  # type: ignore[method-assign]

    class InFlight:
        their_state = h11.SEND_BODY

    protocol.conn = InFlight()  # type: ignore[assignment]
    protocol._head_timed_out()
    assert refused == [], "a deadline that fired during a request refused it"

    class Awaiting:
        their_state = h11.IDLE

    protocol.conn = Awaiting()  # type: ignore[assignment]
    protocol._head_timed_out()
    assert refused == [NO_REQUEST_IN_TIME]


def test_the_deadline_is_cancelled_once_a_request_is_dispatched() -> None:
    """The first of the two mechanisms, pinned while a response is actually open: no timer is
    left armed against a stream, so nothing depends on the guard above to stay harmless."""
    live: list[BoundedHeadH11Protocol] = []

    class Reporting(Impatient):
        def connection_made(self, transport) -> None:  # type: ignore[override]
            super().connection_made(transport)
            live.append(self)

    with _running(_slow_stream, protocol=Reporting) as port:
        sock = _connect(port)
        try:
            sock.sendall(HEAD)
            assert _status(sock).startswith(b"HTTP/1.1 200 ")
            (protocol,) = live
            assert protocol._head_deadline is None, "a head deadline is armed against a stream"
        finally:
            sock.close()


def test_the_deadline_is_re_armed_when_a_response_ends() -> None:
    """And put back afterwards, from the end of that response rather than the next head's first
    byte: a connection that has been served once is a connection that can go quiet again, and
    nothing else here would arm it. uvicorn's own keep-alive timeout is shorter than the shipped
    deadline, so this is the check that would go unnoticed behind it."""
    live: list[BoundedHeadH11Protocol] = []

    class Reporting(BoundedHeadH11Protocol):
        def connection_made(self, transport) -> None:  # type: ignore[override]
            super().connection_made(transport)
            live.append(self)

    with _running(protocol=Reporting) as port:
        sock = _connect(port)
        try:
            sock.sendall(HEAD)
            assert _status(sock).startswith(b"HTTP/1.1 200 ")
            (protocol,) = live
            # The re-arm happens in the server's own loop after the response is written, so it is
            # waited for rather than assumed to have happened by the time the status line arrives.
            deadline = time.monotonic() + 10
            while protocol._head_deadline is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert protocol._head_deadline is not None, (
                "no head deadline is armed on a connection waiting for its next request"
            )
        finally:
            sock.close()


def test_a_second_request_after_a_long_response_is_still_served() -> None:
    """The deadline is re-armed between requests, so the arming has to be correct both ways: a
    connection that has just carried a slow response is not one that gets refused."""
    with _running(protocol=Impatient) as port:
        sock = _connect(port)
        try:
            sock.sendall(HEAD)
            assert _status(sock).startswith(b"HTTP/1.1 200 ")
            sock.sendall(HEAD)
            assert _status(sock).startswith(b"HTTP/1.1 200 ")
        finally:
            sock.close()


# The body deadline


def _post_head(length: int | None = None, *, chunked: bool = False) -> bytes:
    framing = b"Transfer-Encoding: chunked" if chunked else b"Content-Length: %d" % length
    return b"POST /takes-a-body HTTP/1.1\r\nHost: 127.0.0.1\r\n" + framing + b"\r\n\r\n"


def _dribble_body(sock: socket.socket, chunk: bytes, *, timeout: float) -> float:
    """Send ``chunk`` every quarter-deadline for eight deadlines, or until the peer answers.

    Returns how long the drip was allowed to run. Stopping on the first answer is what makes that
    number the assertion: a deadline renewed by each arriving byte still fires, one interval after
    the client gives up, so a test that only waited for a 408 would pass on a bound that bounds
    nothing.
    """
    started = time.monotonic()
    until = started + timeout * 8
    while time.monotonic() < until:
        if select.select([sock], [], [], 0)[0]:
            break
        try:
            sock.sendall(chunk)
        except OSError:
            break
        time.sleep(timeout / 4)
    return time.monotonic() - started


@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
def test_a_dribbled_request_body_is_refused_before_it_finishes(chunked: bool) -> None:
    """#66: the head bounds are all spent by the end of the head, so a peer that finished a head
    and then dribbled a body it never completed held a counted connection for as long as it liked.
    Measured against the shipped configuration before this deadline existed: a 40-byte body sent a
    byte at a time was served, 59 s after its head.

    Both framings, because h11 reaches `SEND_BODY` by either road and the deadline is armed on
    that state rather than on a header. *When* the refusal comes is asserted as well as that one
    does, for the same reason as the head deadline's own drip test.
    """
    timeout = Impatient.request_body_timeout_s
    body = b"5\r\nxxxxx\r\n" if chunked else b"x"
    with _running(_body_reader, protocol=Impatient) as port:
        sock = _connect(port)
        try:
            sock.sendall(_post_head(chunked=True) if chunked else _post_head(4096))
            ran_for = _dribble_body(sock, body, timeout=timeout)
            assert _status(sock).startswith(b"HTTP/1.1 408 ")
            assert ran_for < timeout * 3, (
                f"the body drip was allowed to run for {ran_for:.1f}s against a {timeout}s "
                "deadline: the deadline is being renewed by the arriving bytes"
            )
        finally:
            sock.close()


def test_the_body_refusal_is_the_one_the_server_names() -> None:
    """A silent body, so the whole answer can be compared byte for byte. It is deliberately not
    `NO_REQUEST_IN_TIME`: a request that never arrived and one that stopped half way are different
    things to whoever reads a capture, and the relay has no body refusal to match."""
    with _running(_body_reader, protocol=Impatient) as port:
        sock = _connect(port)
        try:
            sock.sendall(_post_head(64))
            assert _drain(sock) == NO_BODY_IN_TIME
        finally:
            sock.close()


def test_a_body_that_arrives_promptly_is_still_served() -> None:
    """The other side of the bound, under the *shipped* deadline rather than a shortened one: an
    ordinary POST whose body arrives at once is answered normally, and the whole of it reaches the
    application. A bound that refused every body would pass every refusal test above."""
    payload = b"x" * 4096
    with _running(_body_reader) as port:
        sock = _connect(port)
        try:
            sock.sendall(_post_head(len(payload)) + payload)
            answer = _drain(sock)
            assert answer.startswith(b"HTTP/1.1 200 "), answer[:80]
            assert answer.endswith(str(len(payload)).encode()), answer[-40:]
        finally:
            sock.close()


def test_a_second_body_on_a_kept_alive_connection_is_bounded_too() -> None:
    """Every request of every connection, as with the head bounds: the deadline is armed from the
    state h11 is in, so a connection that has already been served once is not a connection that
    has spent its body deadline."""
    timeout = Impatient.request_body_timeout_s
    with _running(_body_reader, protocol=Impatient) as port:
        sock = _connect(port)
        try:
            sock.sendall(_post_head(2) + b"ok")
            assert _status(sock).startswith(b"HTTP/1.1 200 ")
            sock.sendall(_post_head(4096))
            ran_for = _dribble_body(sock, b"x", timeout=timeout)
            assert _status(sock).startswith(b"HTTP/1.1 408 ")
            assert ran_for < timeout * 3, ran_for
        finally:
            sock.close()


def test_a_dribbled_body_under_an_already_answered_request_still_loses_its_slot() -> None:
    """The shape every route in this dashboard actually has: the application answers without
    reading the body -- as Starlette's own 405 does -- and the peer keeps dribbling. The
    connection is still what the peer is holding, so it still goes; what must not happen is a
    second response written over the one already sent.
    """
    timeout = Impatient.request_body_timeout_s
    with _running(protocol=Impatient) as port:
        sock = _connect(port)
        try:
            sock.sendall(_post_head(4096))
            started = time.monotonic()
            answer = b""
            while time.monotonic() < started + timeout * 8:
                try:
                    sock.sendall(b"x")
                except OSError:
                    break
                time.sleep(timeout / 4)
                try:
                    if not (chunk := sock.recv(65536)):
                        break
                except (TimeoutError, BlockingIOError):
                    continue
                except OSError:
                    break
                answer += chunk
            closed_after = time.monotonic() - started
            assert _statuses(answer) == [b"HTTP/1.1 200 OK"], answer[:200]
            assert closed_after < timeout * 4, (
                f"the connection was held for {closed_after:.1f}s after its request was answered"
            )
        finally:
            sock.close()


def test_the_body_deadline_does_not_touch_a_response_in_flight() -> None:
    """The acceptance criterion this bound is riskiest against: it is the first bound here armed
    *after* dispatch, and getting it wrong cuts off SSE, which the dashboard must never do.

    The stream runs for three times the body deadline, and the body it follows was complete, so
    nothing may be armed against it. `/api/stream` is a `GET` and never enters `SEND_BODY` at all;
    this is the harder case, where a body and a long response are on the same connection.
    """
    payload = b"x" * 32
    with _running(_slow_stream_after_a_body, protocol=Impatient) as port:
        sock = _connect(port)
        try:
            sock.sendall(_post_head(len(payload)) + payload)
            body = _drain(sock)
            assert body.startswith(b"HTTP/1.1 200 "), body[:80]
            assert body.count(b"tick") == 6, body[-200:]
        finally:
            sock.close()


def test_no_body_deadline_is_armed_against_a_bodiless_stream() -> None:
    """And the direct version of it, on the server's own state rather than on what the client
    read back: a `GET` that streams for longer than the body deadline has neither deadline armed
    against it. This is what keeps `test_..._does_not_touch_a_response_in_flight` from being the
    only thing between an SSE stream and a timer.
    """
    live: list[BoundedHeadH11Protocol] = []

    class Reporting(Impatient):
        def connection_made(self, transport) -> None:  # type: ignore[override]
            super().connection_made(transport)
            live.append(self)

    with _running(_slow_stream, protocol=Reporting) as port:
        sock = _connect(port)
        try:
            sock.sendall(HEAD)
            assert _status(sock).startswith(b"HTTP/1.1 200 ")
            (protocol,) = live
            assert protocol._body_deadline is None, "a body deadline is armed against a stream"
            assert protocol._head_deadline is None, "a head deadline is armed against a stream"
        finally:
            sock.close()


def test_the_body_deadline_refuses_only_while_a_body_is_awaited() -> None:
    """The guard at the moment the timer fires, which is the backstop for a stray one -- and it
    carries more here than the head deadline's does, because this timer is armed while an
    application is running and so has a response it could collide with. A fake connection is
    enough: every branch is a question about h11's state and uvicorn's cycle.
    """
    protocol = BoundedHeadH11Protocol.__new__(BoundedHeadH11Protocol)
    refused: list[bytes] = []
    closed: list[bool] = []
    protocol._refuse = refused.append  # type: ignore[method-assign]
    protocol._close = lambda: closed.append(True)  # type: ignore[method-assign]
    protocol.cycle = None  # type: ignore[assignment]

    class Sending:
        their_state = h11.SEND_BODY

    class Finished:
        their_state = h11.DONE

    protocol.conn = Finished()  # type: ignore[assignment]
    protocol._body_timed_out()
    assert (refused, closed) == ([], []), "a body deadline fired after the body was complete"

    protocol.conn = Sending()  # type: ignore[assignment]
    protocol._body_timed_out()
    assert refused == [NO_BODY_IN_TIME] and closed == []

    # And with a response already on the wire: the connection goes, but no second response is
    # written over the first. Two responses on one connection is worse than none.
    refused.clear()
    protocol.cycle = type("Cycle", (), {"response_started": True})()  # type: ignore[assignment]
    protocol._body_timed_out()
    assert (refused, closed) == ([], [True])


def test_no_deadline_survives_a_websocket_upgrade() -> None:
    """The one place h11's `SEND_BODY` does not mean "a body is coming".

    uvicorn hands the connection to the WebSocket protocol from inside `handle_events` and
    `return`s *before* the `EndOfMessage` that would leave `SEND_BODY`, then calls
    `transport.set_protocol()`, after which this protocol's `connection_lost` never runs. A body
    deadline armed on the way out of that call would therefore never be cancelled, and would fire
    into an established WebSocket stream: an HTTP 408 written mid-frame, and the connection closed
    under a peer that had done nothing wrong.

    So the assertion is on the wire and not on an attribute: nothing arrives after the handshake,
    and the connection is still open well past the deadline.
    """
    timeout = Impatient.request_body_timeout_s
    handshake = (
        b"GET /ws HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        b"Sec-WebSocket-Version: 13\r\n"
        b"\r\n"
    )
    with _running(_websocket, protocol=Impatient) as port:
        sock = _connect(port)
        try:
            sock.sendall(handshake)
            assert _status(sock).startswith(b"HTTP/1.1 101 "), "the upgrade was not accepted"
            after = b""
            until = time.monotonic() + timeout * 3
            while time.monotonic() < until:
                if not select.select([sock], [], [], 0.05)[0]:
                    continue
                chunk = sock.recv(65536)
                assert chunk, (
                    f"the upgraded connection was closed within {timeout * 3}s of its handshake"
                )
                after += chunk
            assert after == b"", f"bytes were written into a WebSocket stream: {after[:120]!r}"
        finally:
            sock.close()


# The concurrency ceiling


def test_connections_past_the_budget_are_refused_at_the_accept() -> None:
    """front-door-2: the budget has to bound how many connections are *held*, not just how many
    requests are served, because the peer that skipped the relay is the one with no accept-time
    budget in front of it.

    `limit_concurrency` alone does not do this: it is checked where a request is parsed, so an
    over-budget connection is admitted and counted and only its request is refused. Measured
    before this was added: 800 connections held at once against a ceiling of 320. So this asserts
    the surplus is refused *without sending a request at all*, and that the refusal is the relay's
    own 503 for the same case.
    """
    ceiling = 6
    held: list[BoundedHeadH11Protocol] = []

    class Counting(BoundedHeadH11Protocol):
        def connection_made(self, transport) -> None:  # type: ignore[override]
            super().connection_made(transport)
            held.append(self)

    socks: list[socket.socket] = []
    with _running(protocol=Counting, max_connections=ceiling) as port:
        try:
            for _ in range(ceiling):
                socks.append(_connect(port))
            # Waited for rather than slept over: the surplus is only surplus once the server has
            # accepted the ones before it.
            deadline = time.monotonic() + 10
            while len(held) < ceiling and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(held) == ceiling, len(held)
            surplus = _connect(port)
            socks.append(surplus)
            assert _drain(surplus) == TOO_MANY_CONNECTIONS
        finally:
            for sock in socks:
                sock.close()


def test_requests_past_the_concurrency_ceiling_are_refused_by_the_server_itself() -> None:
    """The other half of the same number, which is uvicorn's own and has its own semantics: the
    *request* is answered 503 once the count has reached the ceiling -- the arriving connection
    included. So the most that is served at once is one short of the ceiling, and that is what is
    asserted rather than a range that would hide an off-by-one. The ceiling is overridden to
    something a test can open; the shipped 320 is pinned below.
    """
    ceiling = 6
    socks: list[socket.socket] = []
    with _running(max_connections=ceiling) as port:
        try:
            statuses = []
            for _ in range(ceiling + 3):
                sock = _connect(port)
                socks.append(sock)
                sock.sendall(HEAD)
                statuses.append(_status(sock))
            served = [s for s in statuses if s.startswith(b"HTTP/1.1 200 ")]
            refused = [s for s in statuses if s.startswith(b"HTTP/1.1 503 ")]
            assert len(served) == ceiling - 1, statuses
            assert len(refused) == len(statuses) - len(served), statuses
            assert statuses[ceiling - 1 :] == refused, statuses
        finally:
            for sock in socks:
                sock.close()


def test_a_closed_connection_gives_its_slot_back() -> None:
    """The ceiling counts what is open, so an ordinary browsing session does not exhaust it."""
    ceiling = 4
    with _running(max_connections=ceiling) as port:
        for _ in range(ceiling * 3):
            # The server drops a connection from its own set when the loss is delivered, which is
            # not synchronous with our close, so a slot can take a moment to come back. Retried
            # rather than slept over, so a slow machine reads as slow and not as a broken bound.
            deadline = time.monotonic() + 10
            while True:
                sock = _connect(port)
                try:
                    sock.sendall(HEAD)
                    status = _status(sock)
                    if status.startswith(b"HTTP/1.1 200 ") or time.monotonic() > deadline:
                        break
                finally:
                    sock.close()
                time.sleep(0.05)
            assert status.startswith(b"HTTP/1.1 200 "), status


# The two layers together


@asynctest
async def test_both_front_doors_refuse_the_same_head_the_same_way() -> None:
    """The relation the numbers are chosen for, asserted by asking both doors rather than by
    comparing two constants -- and exact about where they differ.

    `ingress`'s `readuntil` measures the *offset* of the head terminator against its limit, so the
    relay admits four bytes more than the server: at the cap both serve, one byte over the server
    refuses (through the relay too, because the relay passed it on), and five over the relay
    refuses it itself without dialling the dashboard at all. The server being the stricter of the
    two is the safe direction for the inner layer, and the peer gets the same 431 whichever
    answers.
    """
    dialled = 0

    class Counting(BoundedHeadH11Protocol):
        def connection_made(self, transport) -> None:  # type: ignore[override]
            nonlocal dialled
            dialled += 1
            super().connection_made(transport)

    with _running(protocol=Counting) as server_port:
        relay = await ingress.serve("127.0.0.1", server_port, bind="127.0.0.1", port=0)
        relay_port = relay.sockets[0].getsockname()[1]
        try:
            for over, expected in ((0, b"HTTP/1.1 200 "), (1, b"HTTP/1.1 431 ")):
                head = _head_of_exactly(MAX_REQUEST_HEAD_BYTES + over)
                through_the_relay = await asyncio.to_thread(_exchange, relay_port, head)
                straight_to_the_server = await asyncio.to_thread(_exchange, server_port, head)
                assert through_the_relay.startswith(expected), through_the_relay[:80]
                assert straight_to_the_server.startswith(expected), straight_to_the_server[:80]
                if over:
                    # The refused answer byte for byte; a served one carries a date and cannot be
                    # compared that way.
                    assert through_the_relay == straight_to_the_server == HEAD_TOO_LARGE
            # Five over, and the relay answers out of its own bound: no dial at all.
            before = dialled
            answer = await asyncio.to_thread(
                _exchange, relay_port, _head_of_exactly(MAX_REQUEST_HEAD_BYTES + 5)
            )
            assert answer == HEAD_TOO_LARGE, answer[:80]
            assert dialled == before, "the relay dialled the dashboard for a head it refuses"
        finally:
            relay.close()
            await relay.wait_closed()


@asynctest
async def test_both_front_doors_time_out_a_silent_peer_the_same_way() -> None:
    """The deadline half of the same property, with both doors' own deadline shortened so the test
    is not ten seconds long: a peer that says nothing gets the identical 408 either way."""
    timeout = Impatient.request_timeout_s
    with _running(protocol=Impatient) as server_port:
        relay = ingress.Relay("127.0.0.1", server_port, request_timeout_s=timeout)
        server = await asyncio.start_server(
            relay.handle, "127.0.0.1", 0, limit=ingress.MAX_REQUEST_HEAD_BYTES
        )
        relay_port = server.sockets[0].getsockname()[1]
        try:
            through_the_relay = await asyncio.to_thread(_exchange, relay_port, b"")
            straight_to_the_server = await asyncio.to_thread(_exchange, server_port, b"")
            assert through_the_relay.startswith(b"HTTP/1.1 408 "), through_the_relay[:80]
            assert through_the_relay == straight_to_the_server == NO_REQUEST_IN_TIME
        finally:
            server.close()
            await server.wait_closed()


def _exchange(port: int, payload: bytes) -> bytes:
    sock = _connect(port)
    try:
        with contextlib.suppress(OSError):
            sock.sendall(payload)
        return _drain(sock)
    finally:
        sock.close()


# The values, and the command that arms them


def test_build_config_arms_the_documented_bounds() -> None:
    """The other half of naming them: a constant nothing hands to uvicorn bounds nothing."""
    config = build_config()
    assert config.app == APP
    assert config.http is BoundedHeadH11Protocol
    assert config.limit_concurrency == MAX_CONNECTIONS
    # Under the protocol class, for the incomplete head it does cover.
    assert config.h11_max_incomplete_event_size == MAX_REQUEST_HEAD_BYTES


def test_the_servers_bounds_are_the_ones_it_documents() -> None:
    """The values, because the behaviour tests above pass their own in.

    A head cap raised to 16 MiB, either deadline raised to ten minutes or a ceiling raised past
    what the process can hold would leave every one of them green. These are the numbers
    `README.md`, `CLAUDE.md` and `AGENTS.md` describe, so widening one is a change made here and
    in those documents, on purpose.
    """
    assert (MAX_REQUEST_HEAD_BYTES, REQUEST_TIMEOUT_S, MAX_CONNECTIONS) == (16 * 1024, 10.0, 320)
    assert REQUEST_BODY_TIMEOUT_S == 10.0
    assert BoundedHeadH11Protocol.max_request_head_bytes == MAX_REQUEST_HEAD_BYTES
    assert BoundedHeadH11Protocol.request_timeout_s == REQUEST_TIMEOUT_S
    assert BoundedHeadH11Protocol.request_body_timeout_s == REQUEST_BODY_TIMEOUT_S


def test_the_two_layers_bounds_stand_in_the_right_relation() -> None:
    """`ingress` is the outer layer, and neither bound may make the other pointless.

    Same head cap and same head deadline, so the relay's 431 and 408 are the server's own for the
    same peer. The server's concurrency ceiling sits *above* the relay's connection budget, so the
    relay is what refuses a flood at the published port, a connection that skipped the relay is
    still counted, and an SSE stream per tab -- admitted by the relay -- is never what the server
    refuses.
    """
    assert MAX_REQUEST_HEAD_BYTES == ingress.MAX_REQUEST_HEAD_BYTES
    assert REQUEST_TIMEOUT_S == ingress.REQUEST_TIMEOUT_S
    assert MAX_CONNECTIONS > ingress.MAX_CONNECTIONS


def _dockerfile_cmd() -> list[str]:
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        if line.startswith("CMD "):
            return json.loads(line[len("CMD ") :])
    raise AssertionError("the Dockerfile has no CMD in exec form")


def test_the_image_launches_the_bounded_server() -> None:
    """Where the bound has to be armed: the process the image actually starts.

    `uvicorn app.main:app` is the command this replaced, and it arms none of the above. A CMD
    that goes back to it, or to any other launcher, leaves `app/server.py` as documentation.
    """
    cmd = _dockerfile_cmd()
    assert cmd[:3] == ["python", "-m", "app.server"], cmd
    assert "uvicorn" not in cmd, (
        "the image must launch the bounded server, not uvicorn's own defaults"
    )


@pytest.mark.parametrize(
    ("argv", "bind", "port"),
    [([], "127.0.0.1", 8000), (["--bind", "0.0.0.0", "--port", "9000"], "0.0.0.0", 9000)],
)
def test_main_serves_with_those_bounds(monkeypatch, argv: list[str], bind: str, port: int) -> None:
    """`main()` is the entry point the CMD names: it must pass the flags on and add no bound of
    its own. The CMD's own `--bind 0.0.0.0 --port 8000` is one of the cases."""
    built: list[uvicorn.Config] = []
    run_calls: list[object] = []

    def record(config: uvicorn.Config) -> object:
        built.append(config)
        return type("Stub", (), {"run": lambda self: run_calls.append(config)})()

    monkeypatch.setattr(uvicorn, "Server", record)
    assert main(argv) == 0
    (config,) = built
    assert run_calls == [config], "main() built a server it never ran"
    assert (config.host, config.port) == (bind, port)
    assert config.app == APP
    assert config.http is BoundedHeadH11Protocol
    assert config.limit_concurrency == MAX_CONNECTIONS


def test_main_refuses_a_port_out_of_range(capsys) -> None:
    assert main(["--port", "70000"]) == 1
    assert "--port" in capsys.readouterr().err
