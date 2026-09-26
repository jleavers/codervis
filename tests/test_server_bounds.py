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
    TOO_MANY_CONNECTIONS,
    APP,
    MAX_CONNECTIONS,
    MAX_REQUEST_HEAD_BYTES,
    NO_REQUEST_IN_TIME,
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
    socks: list[socket.socket] = []
    with _running(max_connections=ceiling) as port:
        try:
            for _ in range(ceiling):
                socks.append(_connect(port))
            time.sleep(0.2)
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
            sock = _connect(port)
            try:
                sock.sendall(HEAD)
                assert _status(sock).startswith(b"HTTP/1.1 200 ")
            finally:
                sock.close()
            # The server drops the connection from its own set when the loss is delivered, which
            # is not synchronous with our close; the next dial is what gives it a chance to.
            time.sleep(0.02)


# The two layers together


@asynctest
async def test_both_front_doors_refuse_the_same_head_the_same_way() -> None:
    """The relation the numbers are chosen for, asserted by asking both doors rather than by
    comparing two constants: the relay refuses an oversized head before it dials the dashboard,
    the server refuses the identical head on a connection that never passed the relay, and the
    peer cannot tell which layer it reached. An ordinary request through the relay still gets
    through, so the outer layer is still a relay and not a second bound in the way."""
    with _running() as server_port:
        relay = await ingress.serve("127.0.0.1", server_port, bind="127.0.0.1", port=0)
        relay_port = relay.sockets[0].getsockname()[1]
        try:
            for size, expected in (
                (MAX_REQUEST_HEAD_BYTES, b"HTTP/1.1 200 "),
                (MAX_REQUEST_HEAD_BYTES + 1, b"HTTP/1.1 431 "),
            ):
                head = _head_of_exactly(size)
                through_the_relay = await asyncio.to_thread(_exchange, relay_port, head)
                straight_to_the_server = await asyncio.to_thread(_exchange, server_port, head)
                assert through_the_relay.startswith(expected), through_the_relay[:80]
                assert straight_to_the_server.startswith(expected), straight_to_the_server[:80]
                if expected != b"HTTP/1.1 200 ":
                    # The refused answer byte for byte; the served one carries a date and so
                    # cannot be compared that way.
                    assert through_the_relay == straight_to_the_server
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

    A head cap raised to 16 MiB, a deadline raised to ten minutes or a ceiling raised past what
    the process can hold would leave every one of them green. These are the numbers `README.md`,
    `CLAUDE.md` and `AGENTS.md` describe, so widening one is a change made here and in those
    documents, on purpose.
    """
    assert (MAX_REQUEST_HEAD_BYTES, REQUEST_TIMEOUT_S, MAX_CONNECTIONS) == (16 * 1024, 10.0, 320)
    assert BoundedHeadH11Protocol.max_request_head_bytes == MAX_REQUEST_HEAD_BYTES
    assert BoundedHeadH11Protocol.request_timeout_s == REQUEST_TIMEOUT_S


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
