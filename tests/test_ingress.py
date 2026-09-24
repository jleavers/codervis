"""The relay that publishes the dashboard, whose own container has no route off the host.

Hermetic: the target is a loopback server started by the test.
"""

from __future__ import annotations

import asyncio
import functools
import socket

import pytest

from app.ingress import MAX_CONNECTIONS, Relay, parse_target, serve


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
    answer = await asyncio.to_thread(_exchange, port, b"hello", half_close=True)
    assert answer == b"pong:hello"
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
        assert await asyncio.to_thread(_exchange, port, b"x") == b"pong:x"
    server.close()
    await target.stop()


@pytest.mark.parametrize(
    ("text", "expected"),
    [("codervis:8000", ("codervis", 8000)), ("127.0.0.1:9", ("127.0.0.1", 9))],
)
def test_parse_target(text: str, expected: tuple[str, int]) -> None:
    assert parse_target(text) == expected


@pytest.mark.parametrize("text", ["codervis", "codervis:", "codervis:x", "http://codervis:8000"])
def test_parse_target_refuses_the_rest(text: str) -> None:
    assert parse_target(text) is None
