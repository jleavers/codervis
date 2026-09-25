"""The relay that publishes the dashboard, whose own container has no route off the host.

Hermetic: the target is a loopback server started by the test.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import socket
import time

import pytest

from app import ingress
from app.ingress import (
    CONNECT_TIMEOUT_S,
    MAX_CONNECTIONS,
    MAX_REQUEST_HEAD_BYTES,
    REQUEST_TIMEOUT_S,
    Relay,
    parse_target,
    serve,
)

HEAD = b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"


def asynctest(fn):
    """Run an async test body on a fresh loop; the suite needs no asyncio plugin for this."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


class _Target:
    """A loopback server standing in for the dashboard: echoes what it is sent."""

    async def start(self) -> None:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            data = await reader.read(64)
            writer.write(b"pong:" + data)
            await writer.drain()
            writer.close()

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()


def _exchange(port: int, payload: bytes, *, half_close: bool = False) -> bytes:
    with socket.create_connection(("127.0.0.1", port), 5) as sock:
        sock.settimeout(5)
        if payload:
            sock.sendall(payload)
        if half_close:
            sock.shutdown(socket.SHUT_WR)
        chunks = []
        while chunk := sock.recv(4096):
            chunks.append(chunk)
        return b"".join(chunks)


@asynctest
async def test_bytes_are_relayed_both_ways() -> None:
    target = _Target()
    await target.start()
    server = await serve("127.0.0.1", target.port, bind="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    assert await asyncio.to_thread(_exchange, port, b"GET / HTTP/1.1\r\n\r\n") == (
        b"pong:GET / HTTP/1.1\r\n\r\n"
    )
    server.close()
    await target.stop()


@asynctest
async def test_a_client_that_half_closes_still_gets_its_answer() -> None:
    target = _Target()
    await target.start()
    server = await serve("127.0.0.1", target.port, bind="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    answer = await asyncio.to_thread(_exchange, port, HEAD, half_close=True)
    assert answer == b"pong:" + HEAD
    server.close()
    await target.stop()


@asynctest
async def test_a_dashboard_that_is_down_is_a_502_and_not_a_hang() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]
    server = await serve("127.0.0.1", dead, bind="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    answer = await asyncio.to_thread(_exchange, port, b"GET / HTTP/1.1\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 502 Bad Gateway\r\n")
    server.close()


@asynctest
async def test_the_relay_refuses_a_flood_before_opening_anything() -> None:
    assert MAX_CONNECTIONS > 0
    relay = Relay("127.0.0.1", 9, max_connections=0)
    server = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    answer = await asyncio.to_thread(_exchange, port, b"")
    assert answer.startswith(b"HTTP/1.1 503 Service Unavailable\r\n")
    server.close()


@asynctest
async def test_a_closed_connection_gives_its_slot_back() -> None:
    target = _Target()
    await target.start()
    relay = Relay("127.0.0.1", target.port, max_connections=1)
    server = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    for _ in range(3):
        assert await asyncio.to_thread(_exchange, port, HEAD) == b"pong:" + HEAD
    server.close()
    await target.stop()


async def _relay(target_port: int, **kwargs) -> tuple[asyncio.AbstractServer, int]:
    relay = Relay("127.0.0.1", target_port, **kwargs)
    server = await asyncio.start_server(relay.handle, "127.0.0.1", 0, limit=MAX_REQUEST_HEAD_BYTES)
    return server, server.sockets[0].getsockname()[1]


@asynctest
async def test_a_client_that_says_nothing_is_timed_out_before_the_dashboard_sees_it() -> None:
    """Silent sockets are the cheapest way to hold every slot, so a slot is only kept by a
    client that finishes its request head in time, and the dashboard is not dialled before."""
    accepted = []

    async def count(reader, writer) -> None:
        accepted.append(1)
        writer.close()

    counting = await asyncio.start_server(count, "127.0.0.1", 0)
    server, port = await _relay(counting.sockets[0].getsockname()[1], request_timeout_s=0.3)
    loop = asyncio.get_running_loop()
    started = loop.time()
    answer = await asyncio.to_thread(_exchange, port, b"")
    assert answer.startswith(b"HTTP/1.1 408 Request Timeout\r\n")
    assert loop.time() - started < 3
    assert accepted == []
    server.close()
    counting.close()


@asynctest
async def test_a_client_that_drips_its_request_head_is_timed_out_too() -> None:
    target = _Target()
    await target.start()
    server, port = await _relay(target.port, request_timeout_s=0.5)

    def drip() -> bytes:
        with socket.create_connection(("127.0.0.1", port), 5) as sock:
            sock.settimeout(5)
            for byte in b"GET / HTTP/1.1\r\nX: ":
                try:
                    sock.sendall(bytes([byte]))
                except OSError:
                    break
                time.sleep(0.1)
            return sock.recv(4096)

    answer = await asyncio.to_thread(drip)
    assert answer.startswith(b"HTTP/1.1 408 Request Timeout\r\n")
    server.close()
    await target.stop()


@asynctest
async def test_a_request_head_that_never_ends_is_bounded() -> None:
    target = _Target()
    await target.start()
    server, port = await _relay(target.port)
    # Just over the bound, so the flood fits the socket buffers and the answer is not lost to
    # the reset that closing on unread input sends; one read, since that reset follows it.
    flood = b"GET / HTTP/1.1\r\n" + b"X: y\r\n" * (MAX_REQUEST_HEAD_BYTES // 6 + 16)

    def first_answer() -> bytes:
        with socket.create_connection(("127.0.0.1", port), 5) as sock:
            sock.settimeout(5)
            sock.sendall(flood)
            return sock.recv(4096)

    answer = await asyncio.to_thread(first_answer)
    assert answer.startswith(b"HTTP/1.1 431 ")
    server.close()
    await target.stop()


@asynctest
async def test_the_port_the_dashboard_is_published_on_carries_the_head_cap_too() -> None:
    """The bound has to be armed by `serve()`, which is the only thing production calls.

    Every other test here builds its own server, and `_relay` passes the cap in itself, so
    `serve()` could stop arming it -- leaving the published port on asyncio's own default --
    with the whole suite green (#46). The flood is sized off the literal bound rather than the
    constant, so raising the constant is caught here as well.
    """
    target = _Target()
    await target.start()
    server = await serve("127.0.0.1", target.port, bind="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    # Just over 16 KiB, as above: small enough to fit the socket buffers, so the answer is not
    # lost to the reset that closing on unread input sends.
    flood = b"GET / HTTP/1.1\r\n" + b"X: y\r\n" * (16 * 1024 // 6 + 16)

    def first_answer() -> bytes:
        with socket.create_connection(("127.0.0.1", port), 5) as sock:
            sock.settimeout(5)
            sock.sendall(flood)
            return sock.recv(4096)

    try:
        answer = await asyncio.to_thread(first_answer)
    except TimeoutError:
        # Without the cap the head fits asyncio's own larger default, so nothing answers until
        # the head deadline does. Named here, because a bare "timed out" says nothing.
        pytest.fail("no answer: an oversized head is only a prompt 431 while the cap is armed")
    assert answer.startswith(b"HTTP/1.1 431 ")
    server.close()
    await target.stop()


@asynctest
async def test_serve_leaves_the_relay_on_those_bounds(monkeypatch) -> None:
    """The other half of arming them: the values above are only a bound if `serve()` takes them.

    The head cap is armed on the listening socket, and the test above that. The other three live
    on the relay, where `serve()` passing its own would override them silently -- the whole suite
    would stay green, because every other test here builds its `Relay` itself. So this watches
    what `serve()` hands the relay, rather than reaching into the relay for what it holds: an
    override is allowed only where it is the documented value anyway.
    """
    handed: list[dict[str, object]] = []
    documented = {
        "connect_timeout_s": CONNECT_TIMEOUT_S,
        "request_timeout_s": REQUEST_TIMEOUT_S,
        "max_connections": MAX_CONNECTIONS,
    }

    class Recording(Relay):
        def __init__(self, host: str, port: int, **kwargs: object) -> None:
            handed.append(kwargs)
            super().__init__(host, port, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ingress, "Relay", Recording)
    server = await serve("127.0.0.1", 9, bind="127.0.0.1", port=0)
    try:
        (overrides,) = handed
        widened = {
            name: overrides[name]
            for name, value in documented.items()
            if name in overrides and overrides[name] != value
        }
        assert not widened, (
            f"serve() handed the relay bounds other than the documented ones: {widened}"
        )
    finally:
        server.close()


def test_the_front_doors_bounds_are_the_ones_it_documents() -> None:
    """The values, because none of the tests above reads a default of its own accord.

    They each pass the bound they exercise in, and the flood test sizes its flood off
    `MAX_REQUEST_HEAD_BYTES`, so a cap raised to 16 MiB or a deadline raised to ten minutes
    was invisible to the whole suite (#46). These are the numbers `README.md` and `CLAUDE.md`
    describe -- a complete head at most 16 KiB, within 10 s, and at most 256 connections; the
    16 KiB is `CLAUDE.md`'s, and README repeats the other two -- so widening one is a change made
    here and in those documents, on purpose. `CONNECT_TIMEOUT_S`
    is the fourth and no shipped document states it: it bounds the relay's own dial to the
    dashboard rather than anything a peer can do, and it is pinned here alone. The values rather
    than only the wiring: a default that still reads its constant says nothing about what that
    constant became.
    """
    assert (
        MAX_REQUEST_HEAD_BYTES,
        REQUEST_TIMEOUT_S,
        CONNECT_TIMEOUT_S,
        MAX_CONNECTIONS,
    ) == (16 * 1024, 10.0, 10.0, 256)
    # And a relay built the way `serve()` builds one gets them, rather than a default that has
    # drifted away from the constant beside it.
    defaults = {
        name: parameter.default for name, parameter in inspect.signature(Relay).parameters.items()
    }
    assert defaults["connect_timeout_s"] == CONNECT_TIMEOUT_S
    assert defaults["request_timeout_s"] == REQUEST_TIMEOUT_S
    assert defaults["max_connections"] == MAX_CONNECTIONS


@asynctest
async def test_bytes_after_the_head_are_relayed_too() -> None:
    request = b"POST / HTTP/1.1\r\nContent-Length: 4\r\n\r\nbody"

    async def echo_all(reader, writer) -> None:
        writer.write(b"pong:" + await reader.readexactly(len(request)))
        await writer.drain()
        writer.close()

    target = await asyncio.start_server(echo_all, "127.0.0.1", 0)
    server, port = await _relay(target.sockets[0].getsockname()[1])
    assert await asyncio.to_thread(_exchange, port, request) == b"pong:" + request
    server.close()
    target.close()


@asynctest
async def test_the_deadline_does_not_cut_off_a_long_response() -> None:
    """An SSE stream outlives any request deadline; once the head is in, nothing is timed."""

    async def slow(reader, writer) -> None:
        await reader.readuntil(b"\r\n\r\n")
        for chunk in (b"one ", b"two ", b"three"):
            writer.write(chunk)
            await writer.drain()
            await asyncio.sleep(0.3)
        writer.close()

    slow_server = await asyncio.start_server(slow, "127.0.0.1", 0)
    slow_port = slow_server.sockets[0].getsockname()[1]
    server, port = await _relay(slow_port, request_timeout_s=0.2)
    assert await asyncio.to_thread(_exchange, port, HEAD) == b"one two three"
    server.close()
    slow_server.close()


@pytest.mark.parametrize(
    ("text", "expected"),
    [("codervis:8000", ("codervis", 8000)), ("127.0.0.1:9", ("127.0.0.1", 9))],
)
def test_parse_target(text: str, expected: tuple[str, int]) -> None:
    assert parse_target(text) == expected


@pytest.mark.parametrize("text", ["codervis", "codervis:", "codervis:x", "http://codervis:8000"])
def test_parse_target_refuses_the_rest(text: str) -> None:
    assert parse_target(text) is None
