"""The bound the dashboard's own server holds, rather than the one its relay holds for it.

`ingress` reads the first request head of each connection it accepts and nothing after it, and
connections made straight to ``codervis:8000`` never reach it at all, so every assertion here goes
through `app.server`'s configuration to a real server on loopback and asks what the *server*
refuses (#43). Hermetic: a stub ASGI app, an ephemeral loopback port, no dashboard and no network.
"""

from __future__ import annotations

import contextlib
import json
import select
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from app import ingress
from app.server import (
    APP,
    HTTP_PROTOCOL,
    MAX_CONNECTIONS,
    MAX_REQUEST_HEAD_BYTES,
    build_config,
    main,
)

ROOT = Path(__file__).resolve().parents[1]
HEAD = b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
# Every response below is a complete, kept-alive one, so a connection that got an answer stays
# open and keeps counting against the ceiling -- which is what an open browser tab does.
BODY = b"ok"


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


@contextlib.contextmanager
def _running(**bounds: int) -> Iterator[int]:
    """The configuration `app/server.py` builds, on an ephemeral loopback port. Yields the port.

    Only the bounds a test names are overridden; everything else is the shipped value, so a
    behaviour proved here is proved of the configuration the image runs.
    """
    server = uvicorn.Server(build_config(_stub, bind="127.0.0.1", port=0, **bounds))
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


def _connect(port: int) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), 10)
    sock.settimeout(10)
    return sock


def _status(sock: socket.socket) -> bytes:
    """The status line of the next response, or ``b""`` if the peer said nothing."""
    try:
        return sock.recv(4096).split(b"\r\n", 1)[0]
    except OSError:
        return b""


def _push_head(sock: socket.socket, *, limit: int) -> int:
    """Send request-head bytes until the server answers, refuses the write, or ``limit`` is sent.

    Stops as soon as the socket is readable, because a server that answers 400 closes straight
    after: bytes still going the other way would draw a reset that discards the answer we are
    about to assert on.
    """
    sock.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n")
    padding = b"X-Pad: " + b"a" * 1000 + b"\r\n"
    sent = 0
    deadline = time.monotonic() + 20
    while sent < limit and time.monotonic() < deadline:
        readable, _, _ = select.select([sock], [], [], 0.01)
        if readable:
            break
        try:
            sock.sendall(padding)
        except OSError:
            break
        sent += len(padding)
    return sent


def test_an_oversized_second_head_on_a_kept_alive_connection_is_refused() -> None:
    """front-door-1: the head cap applies to every request on a connection, not the first alone.

    `ingress` parses one head per connection and then relays bytes blind, so this is the request
    no layer in front of the server sees. The bytes accepted are asserted as well as the status:
    a parser that reads the whole head and complains afterwards has already paid for it.
    """
    with _running() as port:
        sock = _connect(port)
        try:
            sock.sendall(HEAD)
            assert _status(sock).startswith(b"HTTP/1.1 200 "), "the first request must be served"
            sent = _push_head(sock, limit=64 * MAX_REQUEST_HEAD_BYTES)
            assert _status(sock).startswith(b"HTTP/1.1 400 "), (
                "an oversized second head was not refused: the parser has no per-head cap"
            )
            assert sent <= 4 * MAX_REQUEST_HEAD_BYTES, (
                f"the server accepted {sent} bytes of one request head, "
                f"with a cap of {MAX_REQUEST_HEAD_BYTES}"
            )
        finally:
            sock.close()


def test_an_oversized_first_head_is_refused_too() -> None:
    """The relay answers this one 431 before dialling the dashboard -- but only on its own port."""
    with _running() as port:
        sock = _connect(port)
        try:
            sent = _push_head(sock, limit=64 * MAX_REQUEST_HEAD_BYTES)
            assert _status(sock).startswith(b"HTTP/1.1 400 ")
            assert sent <= 4 * MAX_REQUEST_HEAD_BYTES, sent
        finally:
            sock.close()


def test_connections_over_the_budget_are_refused_by_the_server_itself() -> None:
    """front-door-2's cost: a connection that skipped the relay is still counted and still capped.

    The ceiling is overridden to something a test can open, because the shipped 320 would be 320
    descriptors and a slow test; the pin on the shipped value is
    `test_the_servers_bounds_are_the_ones_it_documents` below. uvicorn refuses a request that
    arrives while the count has *reached* the ceiling, so the assertion is two-sided rather than
    an exact index: everything past the ceiling is refused, and nearly all of the budget is still
    served, since a ceiling that refused an ordinary tab would be a worse defect than no ceiling.
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
            over_budget = statuses[ceiling:]
            assert all(status.startswith(b"HTTP/1.1 503 ") for status in over_budget), (
                f"connections past the budget of {ceiling} were served: {statuses}"
            )
            assert len(served) >= ceiling - 1, (
                f"the budget of {ceiling} refused connections inside it: {statuses}"
            )
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


def test_build_config_arms_the_documented_bounds() -> None:
    """The other half of naming them: a constant nothing hands to uvicorn bounds nothing."""
    config = build_config()
    assert config.app == APP
    assert config.http == HTTP_PROTOCOL
    assert config.h11_max_incomplete_event_size == MAX_REQUEST_HEAD_BYTES
    assert config.limit_concurrency == MAX_CONNECTIONS


def test_the_servers_bounds_are_the_ones_it_documents() -> None:
    """The values, because the behaviour tests above pass their own ceiling in.

    A head cap raised to 16 MiB, or a ceiling raised past what the process can hold, would leave
    every one of them green. These are the numbers `README.md`, `CLAUDE.md` and `AGENTS.md`
    describe, so widening one is a change made here and in those documents, on purpose.
    """
    assert (HTTP_PROTOCOL, MAX_REQUEST_HEAD_BYTES, MAX_CONNECTIONS) == ("h11", 16 * 1024, 320)
    # `auto` is the default and it prefers httptools, which enforces no head limit at all: the
    # implementation is part of the bound.
    assert HTTP_PROTOCOL != "auto"


def test_the_two_layers_bounds_stand_in_the_right_relation() -> None:
    """`ingress` is the outer layer, and neither bound may make the other pointless.

    The head caps are the same size, so a head the relay refuses with 431 is one the server
    refuses with 400 when it never passed the relay. The server's connection ceiling sits above
    the relay's, so the relay is what refuses a flood at the published port while a connection
    that skipped the relay is still counted -- and an SSE stream per tab, admitted by the relay,
    is never what the server refuses.
    """
    assert MAX_REQUEST_HEAD_BYTES == ingress.MAX_REQUEST_HEAD_BYTES
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


@pytest.mark.parametrize("argv", [[], ["--bind", "127.0.0.1", "--port", "9000"]])
def test_main_serves_with_those_bounds(monkeypatch, argv: list[str]) -> None:
    """`main()` is the entry point the CMD names; it must not hand uvicorn bounds of its own."""
    built: list[uvicorn.Config] = []
    run_calls: list[object] = []

    def record(config: uvicorn.Config) -> object:
        built.append(config)
        return type("Stub", (), {"run": lambda self: run_calls.append(config)})()

    monkeypatch.setattr(uvicorn, "Server", record)
    assert main(argv) == 0
    (config,) = built
    assert run_calls == [config], "main() built a server it never ran"
    assert config.app == APP
    assert config.http == HTTP_PROTOCOL
    assert config.h11_max_incomplete_event_size == MAX_REQUEST_HEAD_BYTES
    assert config.limit_concurrency == MAX_CONNECTIONS


def test_main_refuses_a_port_out_of_range(capsys) -> None:
    assert main(["--port", "70000"]) == 1
    assert "--port" in capsys.readouterr().err
