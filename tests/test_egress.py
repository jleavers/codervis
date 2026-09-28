"""The allow-listing CONNECT proxy that is the dashboard container's only route out.

Hermetic: every connection here is to a loopback socket the test started, so nothing reaches a
name the proxy would have to resolve. The check's on-link half is driven the same way -- a
loopback address standing in for the bridge gateway, a synthetic routing table where the case is
about what gets derived, and a stub probe where the case under test is "nothing answers", which
must not become a packet to whatever subnet the suite happens to be running on.

Two probes here do touch the machine, and both stay on it: `probe_on_link`'s silence case dials
169.254.0.1, a link-local address no route reaches, and `own_addresses` resolves this host's own
name. Neither leaves the host, and neither is the thing being asserted anywhere else. The IPv6
half is driven the same way -- a synthetic `ipv6_route` table for what gets derived, and a
listener the test started on `::1` for what a v6 literal does when it is dialled for real.

The compose topology around the proxy is covered by tests/test_compose_topology.py and, end to
end, by the CI `egress` job.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import errno
import functools
import inspect
import logging
import re
import socket
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

import pytest

from app import egress


from app.codex_quota import CHATGPT_HOST
from app.egress import (
    ALLOW_ENV,
    DEFAULT_ALLOW,
    DIRECT_ANSWERED,
    DIRECT_NO_DNS,
    DIRECT_NO_RESOLVER,
    DIRECT_NO_ROUTE,
    DIRECT_UNVERIFIED,
    MAX_CONNECTIONS,
    MAX_ONLINK_PROBES,
    MAX_REQUEST_BYTES,
    MAX_TUNNELS,
    ON_LINK_ACCEPTED,
    ON_LINK_NO_ANSWER,
    ON_LINK_REFUSED,
    ON_LINK_UNVERIFIED,
    PROBE_DENIED_HOST,
    PROBE_ONLINK_PORTS,
    IPV6_ROUTE_TABLE_PATH,
    ROUTE_TABLE_PATH,
    PROXY_ENV_NAMES,
    REQUEST_TIMEOUT_S,
    SHUTDOWN_DRAIN_S,
    UPSTREAM_TIMEOUT_S,
    Proxy,
    Rule,
    allow_rules,
    allowed,
    check,
    check_on_link,
    configured_proxy,
    escape_controls,
    format_result,
    has_default_route,
    normalise_host,
    on_link_addresses,
    own_addresses,
    parse_allow,
    parse_connect_target,
    parse_rule,
    probe_direct,
    probe_on_link,
    probe_proxy,
    read_ipv6_route_table,
    read_route_table,
    serve,
    serve_until_stopped,
    split_allow,
    split_proxy_url,
    split_upstream_url,
    upstream_targets,
    upstream_urls,
    without_userinfo,
)
from app.quota import CLAUDE_AI_HOST

def as_oserror(number: int) -> OSError:
    """An OSError carrying this errno and nothing else.

    `OSError(errno.ETIMEDOUT, "x")` does not build one: the two-argument form maps the errno to
    its subclass, so it returns `TimeoutError` and the `NO_ANSWER_ERRNOS` membership a table
    row means to exercise is never reached. Same for `EPERM` and `PermissionError`.
    """
    failure = OSError("probe failed")
    failure.errno = number
    return failure


ROOT_README = Path(__file__).resolve().parents[1] / "README.md"


def asynctest(fn):
    """Run an async test body on a fresh loop; the suite needs no asyncio plugin for this."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


def _dead_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _pin_tables(monkeypatch, route_table: str | None, ipv6_route_table: str | None = "") -> None:
    """Both routing tables, for a case that lets `check` read them.

    Both, or the case is the machine's: a runner with an IPv6 prefix on-link would contribute a
    candidate this test did not create -- a probe to an address the suite knows nothing about,
    and a result that differs between a laptop and a runner. `""` is a kernel with no IPv6,
    which is what these cases are about unless one says otherwise, and `None` is a table that
    could not be read.
    """
    monkeypatch.setattr(egress, "read_route_table", lambda *a, **k: route_table)
    monkeypatch.setattr(egress, "read_ipv6_route_table", lambda *a, **k: ipv6_route_table)


# --- the allow-list, as text ----------------------------------------------------------


def test_split_allow_takes_commas_or_whitespace() -> None:
    assert split_allow("a.example, b.example  c.example\nd.example") == [
        "a.example",
        "b.example",
        "c.example",
        "d.example",
    ]
    assert split_allow("") == []
    assert split_allow(None) == []


def test_a_bare_name_is_that_host_on_443() -> None:
    assert parse_rule("example.com") == Rule("example.com", 443, False)


def test_a_bare_name_refuses_every_name_under_it() -> None:
    """The other half of the leading dot, and the half nothing asked for (#78).

    `Rule.subdomains` is what tells `example.com` from `.example.com`, and deleting the
    `self.subdomains and` guard in `Rule.matches` left the whole suite green -- which makes
    `DEFAULT_ALLOW`'s two bare entries admit every name under `claude.ai` and `chatgpt.com`,
    and an operator's `EGRESS_ALLOW` entry admit a whole zone they named one host in. A test
    that only ever asked what a rule *admits* cannot see that; this asks what it refuses.
    """
    rule = parse_rule("example.com")
    assert rule == Rule("example.com", 443, False)
    assert rule is not None
    assert rule.matches("example.com", 443)
    for under in ("cdn.example.com", "a.b.example.com", "www.example.com"):
        assert not rule.matches(under, 443), under


def test_the_default_entries_admit_the_two_hosts_and_no_name_under_them() -> None:
    """`DEFAULT_ALLOW` is two bare entries, so the rule above is what bounds it in practice.

    Named separately from the `Rule` case because this is the consequence: an attacker-chosen
    name under an allowed zone is a destination the proxy exists to refuse, and the two
    entries are bare precisely so that it is.
    """
    rules, complaints = allow_rules({})
    assert complaints == ()
    # Restated rather than looped from `DEFAULT_ALLOW` alone: emptying that tuple would make
    # a loop over it vacuously green, which is the shape #78 is about even where a second
    # test pins the value.
    assert DEFAULT_ALLOW == ("claude.ai", "chatgpt.com")
    for host in ("claude.ai", "chatgpt.com"):
        assert allowed(host, 443, rules), host
        for under in (f"evil.{host}", f"a.b.{host}"):
            assert not allowed(under, 443, rules), under


def test_a_name_may_carry_its_own_port() -> None:
    assert parse_rule("registry.example.com:8443") == Rule("registry.example.com", 8443, False)


def test_a_leading_dot_admits_the_domain_and_everything_under_it() -> None:
    rule = parse_rule(".example.com")
    assert rule == Rule("example.com", 443, True)
    assert rule is not None
    assert rule.matches("example.com", 443)
    assert rule.matches("cdn.assets.example.com", 443)
    # The dot is a label boundary, not a string one.
    assert not rule.matches("notexample.com", 443)
    assert not rule.matches("example.com.evil.test", 443)


def test_the_port_is_part_of_the_rule() -> None:
    rule = parse_rule("example.com")
    assert rule is not None
    assert rule.matches("example.com", 443)
    assert not rule.matches("example.com", 8443)


@pytest.mark.parametrize(
    "entry",
    [
        "",
        "   ",
        "example.com:",
        "example.com:0",
        "example.com:70000",
        "example.com:https",
        "http://example.com",
        "user:pass@example.com",
        "example.com/path",
        "exa mple.com",
        "exämple.com",
        "example..com",
    ],
)
def test_an_entry_that_is_not_a_host_makes_no_rule(entry: str) -> None:
    assert parse_rule(entry) is None


def test_parse_allow_keeps_what_parsed_and_complains_about_the_rest() -> None:
    rules, complaints = parse_allow(["a.example", "not a host", "b.example", "a.example"])
    assert [str(rule) for rule in rules] == ["a.example:443", "b.example:443"]
    assert complaints == ("'not a host' is not a host name, or host:port",)


def test_the_default_list_is_the_two_usage_endpoints_and_nothing_else() -> None:
    assert DEFAULT_ALLOW == ("claude.ai", "chatgpt.com")


def test_the_default_list_carries_every_host_the_dashboard_itself_reaches() -> None:
    """A host the clients call by default must be admitted, or the panel goes `unavailable`
    with an allow-list error on a deployment nobody changed."""
    rules, _ = allow_rules({})
    for base in (CLAUDE_AI_HOST, CHATGPT_HOST):
        assert allowed(urlsplit(base).hostname or "", 443, rules), base


def test_the_operator_extends_the_default_and_never_replaces_it() -> None:
    rules, complaints = allow_rules({ALLOW_ENV: "usage.example.test proxy.example.test:8443"})
    assert complaints == ()
    assert [str(rule) for rule in rules] == [
        "claude.ai:443",
        "chatgpt.com:443",
        "usage.example.test:443",
        "proxy.example.test:8443",
    ]


def test_a_typo_in_the_operators_list_costs_its_own_entry_alone() -> None:
    rules, complaints = allow_rules({ALLOW_ENV: "usage.example.test, http://nope/"})
    assert len(complaints) == 1
    assert "usage.example.test" in [rule.name for rule in rules]
    assert allowed("claude.ai", 443, rules)


# --- host names -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Example.COM", "example.com"),
        ("example.com.", "example.com"),
        ("  example.com  ", "example.com"),
        ("[::1]", "::1"),
        ("127.0.0.1", "127.0.0.1"),
    ],
)
def test_normalise_host_spells_a_name_one_way(raw: str, expected: str) -> None:
    assert normalise_host(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", ".", "exa mple.com", "example.com/x", "user@example.com", "exämple.com", "a" * 254],
)
def test_normalise_host_refuses_what_is_not_a_name(raw: str) -> None:
    assert normalise_host(raw) is None


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("example.com:443", ("example.com", 443)),
        ("Example.com:8443", ("example.com", 8443)),
        ("[2001:db8::1]:443", ("2001:db8::1", 443)),
    ],
)
def test_parse_connect_target(target: str, expected: tuple[str, int]) -> None:
    assert parse_connect_target(target) == expected


@pytest.mark.parametrize(
    "target",
    ["example.com", "example.com:", "example.com:x", "example.com:0", "[2001:db8::1]", "", ":443"],
)
def test_parse_connect_target_refuses_the_rest(target: str) -> None:
    assert parse_connect_target(target) is None


# --- the proxy itself -----------------------------------------------------------------


class _Upstream:
    """A loopback server that echoes what it is sent, standing in for an allowed host."""

    def __init__(self) -> None:
        self.server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            data = await reader.read(64)
            writer.write(b"pong:" + data)
            await writer.drain()
            writer.close()

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()


async def _proxy(entries: list[str]) -> tuple[asyncio.AbstractServer, str]:
    rules, _ = parse_allow(entries)
    server = await serve(rules, bind="127.0.0.1", port=0)
    return server, f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"


def _speak(url: str, payload: bytes, *, then: bytes | None = None) -> bytes:
    """One request to the proxy; the first chunk back, plus the reply to ``then``."""
    host = url.removeprefix("http://")
    address, _, port = host.rpartition(":")
    with socket.create_connection((address, int(port)), 5) as sock:
        sock.settimeout(5)
        sock.sendall(payload)
        head = sock.recv(4096)
        if then is None:
            return head
        sock.sendall(then)
        return head + sock.recv(4096)


@asynctest
async def test_an_allowed_name_is_tunnelled_byte_for_byte() -> None:
    upstream = _Upstream()
    await upstream.start()
    server, url = await _proxy([f"localhost:{upstream.port}"])
    request = f"CONNECT localhost:{upstream.port} HTTP/1.1\r\nHost: x\r\n\r\n".encode()
    answer = await asyncio.to_thread(_speak, url, request, then=b"hello")
    assert answer.startswith(b"HTTP/1.1 200 Connection established\r\n\r\n")
    assert answer.endswith(b"pong:hello")
    server.close()
    await upstream.stop()


@asynctest
async def test_a_name_off_the_list_is_refused_and_told_where_to_add_it() -> None:
    server, url = await _proxy(["allowed.test"])
    answer = await asyncio.to_thread(_speak, url, b"CONNECT evil.test:443 HTTP/1.1\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 403 Forbidden\r\n")
    assert b"evil.test:443 is not on the egress allow-list" in answer
    assert ALLOW_ENV.encode() in answer
    server.close()


def test_a_denial_is_a_warning_except_for_the_healthchecks_own_probe(caplog) -> None:
    async def exercise() -> None:
        server, url = await _proxy(["allowed.test"])
        for host in (PROBE_DENIED_HOST, "evil.test"):
            await asyncio.to_thread(_speak, url, f"CONNECT {host}:443 HTTP/1.1\r\n\r\n".encode())
        server.close()

    with caplog.at_level(logging.WARNING, logger="app.egress"):
        asyncio.run(exercise())
    denied = [r.getMessage() for r in caplog.records if "egress_denied" in r.getMessage()]
    assert denied == ["egress_denied host=evil.test port=443"]


@asynctest
async def test_an_allowed_name_on_a_port_that_is_not_is_refused() -> None:
    server, url = await _proxy(["allowed.test"])
    answer = await asyncio.to_thread(_speak, url, b"CONNECT allowed.test:8443 HTTP/1.1\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 403 Forbidden\r\n")
    server.close()


@asynctest
async def test_plain_http_is_not_proxied_at_all() -> None:
    """CONNECT only: the proxy never sees a URL, a header or a body, so a bearer token cannot
    cross it in cleartext even when a host override says ``http://``."""
    server, url = await _proxy(["allowed.test"])
    request = b"GET http://allowed.test/secret HTTP/1.1\r\nHost: allowed.test\r\n\r\n"
    answer = await asyncio.to_thread(_speak, url, request)
    assert answer.startswith(b"HTTP/1.1 405 Method Not Allowed\r\n")
    assert b"Allow: CONNECT" in answer
    server.close()


@asynctest
async def test_an_unparseable_request_is_a_400() -> None:
    server, url = await _proxy(["allowed.test"])
    assert (await asyncio.to_thread(_speak, url, b"nonsense\r\n\r\n")).startswith(
        b"HTTP/1.1 400 Bad Request\r\n"
    )
    assert (await asyncio.to_thread(_speak, url, b"CONNECT nope HTTP/1.1\r\n\r\n")).startswith(
        b"HTTP/1.1 400 Bad Request\r\n"
    )
    server.close()


@asynctest
async def test_a_request_head_that_never_ends_is_bounded() -> None:
    server, url = await _proxy(["allowed.test"])
    flood = b"CONNECT allowed.test:443 HTTP/1.1\r\n" + b"X: y\r\n" * MAX_REQUEST_BYTES
    answer = await asyncio.to_thread(_speak, url, flood)
    assert answer.startswith(b"HTTP/1.1 431 ")
    server.close()


@asynctest
async def test_an_allowed_name_that_cannot_be_reached_is_a_502_and_not_a_hang() -> None:
    dead = _dead_port()
    server, url = await _proxy([f"localhost:{dead}"])
    answer = await asyncio.to_thread(
        _speak, url, f"CONNECT localhost:{dead} HTTP/1.1\r\n\r\n".encode()
    )
    assert answer.startswith(b"HTTP/1.1 502 Bad Gateway\r\n")
    server.close()


def test_a_tunnel_survives_a_client_that_half_closes_its_side() -> None:
    """The *reply* direction ends the tunnel, not whichever of the two finishes first."""

    async def exercise() -> bytes:
        upstream = _Upstream()
        await upstream.start()
        server, url = await _proxy([f"localhost:{upstream.port}"])
        try:

            def talk() -> bytes:
                host, _, port = url.removeprefix("http://").rpartition(":")
                with socket.create_connection((host, int(port)), 5) as sock:
                    sock.settimeout(5)
                    sock.sendall(f"CONNECT localhost:{upstream.port} HTTP/1.1\r\n\r\n".encode())
                    sock.recv(4096)
                    sock.sendall(b"hello")
                    sock.shutdown(socket.SHUT_WR)
                    return sock.recv(4096)

            return await asyncio.to_thread(talk)
        finally:
            server.close()
            await upstream.stop()

    assert asyncio.run(exercise()) == b"pong:hello"


@asynctest
async def test_the_proxy_refuses_a_flood_rather_than_running_out_of_descriptors() -> None:
    assert MAX_TUNNELS > 0
    rules, _ = parse_allow(["allowed.test"])
    proxy = Proxy(rules, max_tunnels=0)
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0, limit=MAX_REQUEST_BYTES)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    answer = await asyncio.to_thread(_speak, url, b"CONNECT allowed.test:443 HTTP/1.1\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 503 Service Unavailable\r\n")
    server.close()


def test_saturation_is_logged_once_an_episode_and_not_once_a_refusal(caplog) -> None:
    """A refused connection is the cheapest line to provoke, so one log line per refusal would
    be a flood's second payload. It takes falling back to three quarters of the ceiling to end
    an episode; a slot freeing and being retaken at the ceiling does not."""

    async def exercise() -> None:
        rules, _ = parse_allow(["allowed.test"])
        proxy = Proxy(rules, max_connections=4)
        server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0, limit=MAX_REQUEST_BYTES)
        port = server.sockets[0].getsockname()[1]

        def refuse() -> bytes:
            with socket.create_connection(("127.0.0.1", port), 5) as sock:
                sock.settimeout(5)
                return sock.recv(256)

        held = [socket.create_connection(("127.0.0.1", port), 5) for _ in range(4)]
        try:
            await asyncio.sleep(0.2)
            for _ in range(10):
                assert (await asyncio.to_thread(refuse)).startswith(b"HTTP/1.1 503")
            assert _exhausted(caplog) == 1

            for _ in range(10):
                held.pop().close()
                await asyncio.sleep(0.05)
                held.append(socket.create_connection(("127.0.0.1", port), 5))
                await asyncio.sleep(0.05)
                await asyncio.to_thread(refuse)
            assert _exhausted(caplog) == 1
        finally:
            for sock in held:
                sock.close()
            server.close()

    with caplog.at_level(logging.WARNING, logger="app.egress"):
        asyncio.run(exercise())


def _exhausted(caplog) -> int:
    return sum(
        1 for r in caplog.records if r.getMessage().startswith("egress_connections_exhausted")
    )


def test_the_connection_bound_sits_above_the_tunnel_bound() -> None:
    """Otherwise the tunnel bound is dead code: no connection could survive to establish one."""
    assert MAX_CONNECTIONS >= MAX_TUNNELS


def test_the_proxys_own_bounds_are_the_ones_it_documents() -> None:
    """Every number the proxy bounds a peer with, restated here rather than read from there.

    `tests/test_server_bounds.py` does this for the dashboard's own four; the proxy's went
    unstated, so widening the request-line deadline, the upstream connect deadline or the
    connection ceiling to ten minutes and a million left the suite green (#78). The behaviour
    tests around this one each pass their own value in, which is exactly why they cannot
    witness the module's: a test that constructs `Proxy(max_connections=2)` proves the
    mechanism works and says nothing about what is armed in the container.

    A deliberate change here is a line in this test in the same commit, and the reviewer sees
    the number move.
    """
    assert MAX_REQUEST_BYTES == 8 * 1024
    assert REQUEST_TIMEOUT_S == 10.0
    assert UPSTREAM_TIMEOUT_S == 10.0
    assert MAX_TUNNELS == 64
    assert MAX_CONNECTIONS == 256
    assert SHUTDOWN_DRAIN_S == 5.0


def test_the_proxy_is_built_on_those_bounds_and_not_on_something_wider() -> None:
    """The values above reach the object `serve()` builds, rather than stopping at the module.

    A default argument that drifted from the constant beside it would leave the pin above true
    and the running proxy on something else entirely.
    """
    proxy = Proxy(rules=[])
    assert proxy._max_connections == MAX_CONNECTIONS
    assert proxy._max_tunnels == MAX_TUNNELS
    assert proxy._request_timeout_s == REQUEST_TIMEOUT_S
    assert proxy._upstream_timeout_s == UPSTREAM_TIMEOUT_S


@asynctest
async def test_a_connection_that_says_nothing_is_bounded_before_it_is_read() -> None:
    """The descriptor bound is the *accepted* socket, not the established tunnel, so a peer
    that connects and sends nothing is refused without a byte being read."""
    rules, _ = parse_allow(["allowed.test"])
    proxy = Proxy(rules, max_connections=0)
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0, limit=MAX_REQUEST_BYTES)
    port = server.sockets[0].getsockname()[1]

    def listen() -> bytes:
        with socket.create_connection(("127.0.0.1", port), 5) as sock:
            sock.settimeout(5)
            return sock.recv(4096)

    answer = await asyncio.to_thread(listen)
    assert answer.startswith(b"HTTP/1.1 503 Service Unavailable\r\n")
    server.close()


@asynctest
async def test_a_silent_connection_is_released_and_does_not_leak_the_bound() -> None:
    rules, _ = parse_allow(["allowed.test"])
    proxy = Proxy(rules, max_connections=1)
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0, limit=MAX_REQUEST_BYTES)
    port = server.sockets[0].getsockname()[1]

    def connect_and_close() -> None:
        with socket.create_connection(("127.0.0.1", port), 5):
            pass

    for _ in range(3):
        await asyncio.to_thread(connect_and_close)
        await asyncio.sleep(0.05)

    # The bound is 1, so this only gets an answer if all three above were released.
    url = f"http://127.0.0.1:{port}"
    answer = await asyncio.to_thread(_speak, url, b"CONNECT denied.test:443 HTTP/1.1\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 403 Forbidden\r\n")
    server.close()


@asynctest
async def test_shutdown_does_not_wait_for_an_established_tunnel() -> None:
    """``docker compose up -d egress`` is how the allow-list changes, so a held tunnel must not
    keep the proxy alive until Docker's SIGKILL."""
    upstream_server = await asyncio.start_server(
        lambda r, w: asyncio.sleep(3600), "127.0.0.1", 0
    )
    upstream_port = upstream_server.sockets[0].getsockname()[1]
    rules, _ = parse_allow([f"localhost:{upstream_port}"])
    server = await serve(rules, bind="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"CONNECT localhost:{upstream_port} HTTP/1.1\r\n\r\n".encode())
    await writer.drain()
    assert (await reader.readline()).startswith(b"HTTP/1.1 200")

    stop = asyncio.Event()
    stop.set()
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await serve_until_stopped(server, stop=stop, drain_s=0.2) == 0
    assert loop.time() - started < 2

    writer.close()
    upstream_server.close()


# --- the probes and the check ---------------------------------------------------------


@asynctest
async def test_probe_proxy_reads_the_status_the_proxy_answered() -> None:
    upstream = _Upstream()
    await upstream.start()
    server, url = await _proxy([f"localhost:{upstream.port}"])
    assert await asyncio.to_thread(probe_proxy, url, "localhost", upstream.port) == (
        200,
        "Connection established",
    )
    assert (await asyncio.to_thread(probe_proxy, url, PROBE_DENIED_HOST))[:1] == (403,)
    server.close()
    await upstream.stop()


def test_probe_proxy_words_its_own_failure() -> None:
    answer = probe_proxy(f"http://127.0.0.1:{_dead_port()}", "example.com", timeout_s=2)
    assert isinstance(answer, str)
    assert "did not answer" in answer
    assert isinstance(probe_proxy("ftp://proxy.test", "example.com"), str)


# --- what a resolver that does not answer costs (#68) ----------------------------------
#
# The lookups here are stubbed rather than pointed at a name, because the case is a resolver
# that never answers and no name reliably produces one: a runner whose resolver is fast would
# make these pass without testing anything, and a name chosen to hang would make them depend on
# whatever network the suite happens to run on. What is real is the clock -- each case asserts
# the call came back inside the budget it was given, which is the thing #68 is about.


# What the stub sleeps for: longer than any budget below, so waking early cannot be what makes
# a case pass.
STALLED_RESOLVER_S = 5.0
# The margin a bounded call is allowed. Generous, because a loaded runner is slow and a flaky
# bound is worse than a loose one, and still two orders of magnitude short of the behaviour
# these cases are about: the unbounded call spent the whole of the stub's sleep, every time.
BOUNDED_S = 2.0


def _stalled_resolver(
    recorded: list[str] | None = None, entered: threading.Event | None = None
):
    """A `getaddrinfo` that does not answer: a container that cannot reach its resolver.

    It sleeps rather than blocking forever, so the daemon thread the bound abandons goes away on
    its own during the rest of the session. It raises `EAI_AGAIN` when it does wake, which is
    what a resolver that timed out really raises, so nothing here depends on the abandoned
    lookup staying silent afterwards.

    `entered` is set before the sleep, for a caller that needs to know the stub was reached
    rather than assuming the lookup thread was scheduled promptly.
    """

    def getaddrinfo(host, *_args: object, **_kwargs: object):
        if recorded is not None:
            recorded.append(host)
        if entered is not None:
            entered.set()
        time.sleep(STALLED_RESOLVER_S)
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

    return getaddrinfo


def test_probe_proxy_bounds_its_whole_cost_including_the_proxys_own_name_lookup() -> None:
    """The proxy is configured as a name, and resolving it used to sit outside `timeout_s`.

    `socket.create_connection`'s timeout does not start until `getaddrinfo` has returned, so a
    container whose resolver does not answer waited out the resolver's own budget first --
    `/etc/resolv.conf`'s `timeout:` times `attempts:` times the nameservers listed. `check`
    calls this once for the denied host and once per configured upstream, three times on the
    default configuration, so that was three unbounded waits before the direct half began.
    """
    asked: list[str] = []
    entered = threading.Event()
    with mock.patch.object(socket, "getaddrinfo", _stalled_resolver(asked, entered)):
        started = time.monotonic()
        answer = probe_proxy("http://egress:3128", CLAUDE_AI_HOST, 443, timeout_s=0.5)
        elapsed = time.monotonic() - started
        # Waited for *inside* the patch and after the measurement, so neither the clock
        # assertion nor `asked` depends on the lookup thread having been scheduled before the
        # join gave up on it. An oversubscribed runner would otherwise leave `asked` empty and
        # then let the abandoned thread reach the *real* resolver for `egress` once the patch
        # was unwound. Bounded, so a stub that is never entered fails the assertion below
        # rather than hanging the suite.
        entered.wait(timeout=BOUNDED_S)

    assert elapsed < BOUNDED_S, f"the probe spent {elapsed:.2f}s of a 0.5s budget"
    # It is the *proxy's* name that has to be resolved here, not the CONNECT target's: the
    # target is resolved by the proxy at the far end, and this probe never dials it.
    assert asked == ["egress"]
    assert answer == "egress:3128 did not answer (the resolver did not answer)"


def test_probe_proxy_words_a_resolver_that_did_not_answer_apart_from_one_that_declined() -> None:
    """Which of `probe_proxy`'s failure strings a resolver that did not answer produces.

    Two different things, which an operator does something different about -- a resolver to go
    and fix, or a proxy that is not there -- so they are not allowed to collapse into one
    string. A `gaierror` is the resolver *answering*, with a refusal of the name, and keeps the
    form every other unreachable-proxy failure has.
    """
    with mock.patch.object(socket, "getaddrinfo", _stalled_resolver()):
        stalled = probe_proxy("http://egress:3128", CLAUDE_AI_HOST, timeout_s=0.5)

    def declines(*_args: object, **_kwargs: object):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    with mock.patch.object(socket, "getaddrinfo", declines):
        declined = probe_proxy("http://egress:3128", CLAUDE_AI_HOST, timeout_s=0.5)

    assert stalled == "egress:3128 did not answer (the resolver did not answer)"
    assert declined == "egress:3128 did not answer (gaierror)"


def test_probe_proxy_bounds_a_proxy_that_answers_a_byte_at_a_time() -> None:
    """The other half of "`timeout_s` is the whole of what this costs".

    A socket timeout is *per operation*, so the loop reading the status line renewed it on every
    read that returned anything: a peer that never finishes the status line but keeps answering
    one byte at a time was bounded at 512 timeouts rather than at one budget. A peer that says
    nothing at all was always bounded -- one read, one timeout -- which is why this case dribbles
    rather than going quiet, and nothing here is stubbed.

    512 bytes is how much the loop will hold, so on the old code this was 512 reads of one byte,
    each inside its own fresh 0.5 s: about half a minute for a 0.5 s budget, and a peer willing
    to answer more slowly could make it a quarter of an hour.
    """
    dribbled = threading.Event()

    def dribble(listener: socket.socket) -> None:
        conn, _ = listener.accept()
        with conn:
            try:
                # Never a "\r\n", so the loop's own exit conditions are never what ends this.
                while not dribbled.is_set():
                    conn.sendall(b".")
                    time.sleep(0.02)
            except OSError:
                pass

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        server = threading.Thread(target=dribble, args=(listener,), daemon=True)
        server.start()
        try:
            started = time.monotonic()
            answer = probe_proxy(f"http://127.0.0.1:{port}", CLAUDE_AI_HOST, 443, timeout_s=0.5)
            elapsed = time.monotonic() - started
        finally:
            dribbled.set()
            server.join(timeout=BOUNDED_S)

    assert elapsed < BOUNDED_S, f"the probe spent {elapsed:.2f}s of a 0.5s budget"
    assert answer == f"127.0.0.1:{port} did not answer (TimeoutError)"


def test_every_address_of_the_proxys_name_is_dialled_inside_the_one_budget() -> None:
    """The bound may not cost a reachable proxy its verdict, which is the unsafe direction.

    `socket.create_connection` gave *each* address `getaddrinfo` returned its own full timeout,
    so an `egress` on a network with `enable_ipv6` whose first address blackholes still
    connected over the second. Bounding the total must not give that up: handing the first
    candidate everything up to the deadline means a blackholed first address spends the budget
    the second needed, and a reachable proxy is reported `did not answer` -- a whole deployment
    failing the check for nothing, which is exactly what `resolved_addresses` and README call
    the direction to err away from.

    So what is left is shared between the addresses not yet tried. Nothing is dialled here: the
    socket is a stand-in that consumes the timeout it was given and then reports the blackhole,
    which is what an address that swallows packets does and what no loopback address will do on
    request -- and `tests/conftest.py` fails a test that dials anything but loopback anyway.
    """
    budgets: list[float] = []

    class Blackholed:
        """Consumes whatever timeout it is handed, then reports what a silent address reports."""

        def __init__(self, *_args: object) -> None:
            self._timeout = 0.0

        def settimeout(self, timeout: float) -> None:
            self._timeout = timeout

        def connect(self, _sockaddr: object) -> None:
            budgets.append(self._timeout)
            time.sleep(self._timeout)
            raise TimeoutError("timed out")

        def close(self) -> None:
            pass

    candidates = [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 3128, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 3128)),
    ]
    budget_s = 1.0
    with mock.patch.object(socket, "socket", Blackholed):
        started = time.monotonic()
        answer = egress._connect_within(candidates, deadline=time.monotonic() + budget_s)
        elapsed = time.monotonic() - started

    # Both addresses were dialled, which is the regression this case exists for: on a budget
    # handed to the first candidate whole, the first `sleep` reaches the deadline and the second
    # address is never tried at all.
    assert len(budgets) == 2, budgets
    # Each got a real share of the budget, not the `MIN_DIAL_BUDGET_S` floor left over after the
    # first had taken everything. The threshold sits between the two on purpose: `share` is never
    # below the floor by construction, so an assertion about the floor would hold on the
    # regression too and would say nothing, while an even share here is 0.5 s -- so 0.3 s tells
    # the two apart with room for a `sleep` that overruns on a loaded runner.
    assert all(b >= budget_s * 0.3 for b in budgets), budgets
    # And the total is still the one budget, which is the whole point of sharing it.
    assert sum(budgets) <= budget_s + 0.01, budgets
    assert elapsed < budget_s + 0.5, f"the dial spent {elapsed:.2f}s of a {budget_s}s budget"
    assert isinstance(answer, OSError)


def test_a_real_failure_is_preferred_to_the_budget_running_out() -> None:
    """An address that refused said something about itself; "the budget ran out" did not.

    `probe_proxy` renders whichever comes back as `did not answer ({ExcType})`, so this is what
    an operator reads on the failing line: `ConnectionRefusedError` sends them to a proxy that
    is not listening, while `TimeoutError` sends them to a network that is dropping packets.
    Reporting the second for the first would be a wrong signpost.
    """

    class Refused:
        """Refuses, having taken long enough over it to leave the next candidate no budget."""

        def __init__(self, *_args: object) -> None:
            pass

        def settimeout(self, _timeout: float) -> None:
            pass

        def connect(self, _sockaddr: object) -> None:
            time.sleep(0.6)
            raise ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")

        def close(self) -> None:
            pass

    candidates = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 3128)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.2", 3128)),
    ]
    # The first candidate's refusal overruns the whole budget on its own, so the second is never
    # reached and the loop ends on its own budget check -- with a real failure in hand, which is
    # the one to word. The budget is comfortably above `MIN_DIAL_BUDGET_S` rather than just
    # above it, so a runner that stalls between the deadline being set and the loop reading the
    # clock cannot leave the first candidate undialled and fail this on the wrong type.
    with mock.patch.object(socket, "socket", Refused):
        answer = egress._connect_within(candidates, deadline=time.monotonic() + 0.5)

    assert isinstance(answer, ConnectionRefusedError), answer

    # With nothing reached at all, the budget *is* the honest answer, and then it is what comes
    # back rather than an errno no address actually produced.
    with mock.patch.object(socket, "socket", Refused):
        nothing_reached = egress._connect_within(candidates, deadline=time.monotonic() - 1.0)
    assert isinstance(nothing_reached, TimeoutError), nothing_reached

    # And an empty candidate list is neither: there was no budget problem and no address to
    # blame, which `probe_proxy` renders as `did not answer (OSError)`.
    empty = egress._connect_within([], deadline=time.monotonic() + 1.0)
    assert isinstance(empty, OSError) and not isinstance(empty, TimeoutError), empty

    # A socket that could not be created at all is reported *last*, since no packet was sent and
    # the module's errno note says such a failure is not evidence about the address -- but it is
    # reported where it is all there is, because "no address to dial" would be untrue.
    def unavailable_family(*_args: object) -> object:
        raise OSError(errno.EAFNOSUPPORT, "Address family not supported by protocol")

    with mock.patch.object(socket, "socket", unavailable_family):
        only_setup_failed = egress._connect_within(candidates, deadline=time.monotonic() + 1.0)
    assert isinstance(only_setup_failed, OSError), only_setup_failed
    assert only_setup_failed.errno == errno.EAFNOSUPPORT, only_setup_failed

    # With a dial failure beside it, the dial is what gets worded: it is the one that established
    # something about an address.
    def refuse_second(family: int, *_args: object) -> object:
        if family == socket.AF_INET6:
            raise OSError(errno.EAFNOSUPPORT, "Address family not supported by protocol")
        return Refused()

    mixed = [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 3128, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 3128)),
    ]
    with mock.patch.object(socket, "socket", refuse_second):
        both = egress._connect_within(mixed, deadline=time.monotonic() + 1.0)
    assert isinstance(both, ConnectionRefusedError), both


def test_resolved_addresses_and_own_addresses_are_bounded_too() -> None:
    """`check` labels an on-link candidate a peer through these, so the on-link half carried the
    same exposure the direct half did: `socket.getaddrinfo` with no bound at all.

    A lookup that does not answer costs the candidate its label, which is the cost a lookup that
    *fails* already had -- the address is dialled rather than accounted for, so the check fails
    rather than passing. The bound does not make the half quieter; it makes it finite.
    """
    with mock.patch.object(socket, "getaddrinfo", _stalled_resolver()):
        started = time.monotonic()
        addresses = egress.resolved_addresses("egress", timeout_s=0.5)
        resolved_elapsed = time.monotonic() - started

        started = time.monotonic()
        own = own_addresses(timeout_s=0.5)
        own_elapsed = time.monotonic() - started

    assert addresses == frozenset()
    assert own == frozenset()
    assert resolved_elapsed < BOUNDED_S, f"resolved_addresses spent {resolved_elapsed:.2f}s"
    assert own_elapsed < BOUNDED_S, f"own_addresses spent {own_elapsed:.2f}s"


def test_the_peer_labelling_bound_is_the_default_and_is_spent_per_name() -> None:
    """Two properties README's statement of what `check` costs rests on.

    The default is what a run really spends, because nothing passes one in. And the budget is
    per name rather than shared across the two `peer_addresses` looks up, because the second is
    the proxy's -- the label that decides whether a correct dual-stack deployment passes (#42).
    A shared budget would let this container's own hostname spend the proxy's.
    """
    assert egress.RESOLVE_TIMEOUT_S == 5.0
    for function in (egress.resolved_addresses, own_addresses):
        default = inspect.signature(function).parameters["timeout_s"].default
        assert default == egress.RESOLVE_TIMEOUT_S, function.__name__

    # The stub's default is a sentinel rather than `RESOLVE_TIMEOUT_S`, so that "the caller
    # passed the bound" and "the caller passed nothing and the stub's own default was recorded"
    # are not the same observation. `peer_addresses` does both: it forwards nothing for the
    # proxy's name and takes `own_addresses`' forwarded value for this container's, and either
    # way the budget a real run spends is `RESOLVE_TIMEOUT_S`, which the signature assertions
    # above pin as the default of both functions.
    took_the_default = "took the default"
    budgets: list[object] = []

    def resolved(host: str, *, timeout_s: object = took_the_default) -> frozenset[str]:
        budgets.append(timeout_s)
        return frozenset()

    with mock.patch.object(egress, "resolved_addresses", resolved):
        with mock.patch.object(socket, "gethostname", lambda: "codervis.test"):
            egress.peer_addresses("http://egress:3128")

    assert budgets == [egress.RESOLVE_TIMEOUT_S, took_the_default], budgets
    # Two names, one budget each, and neither of them shares with the other -- which is the
    # property README rests on. A `peer_addresses` that threaded one budget through both would
    # show up here as the second call receiving what the first had left.
    assert len(budgets) == 2


def test_a_resolver_call_in_the_module_is_made_only_from_the_bounded_lookup() -> None:
    """The enforcement point for "a resolver call in this module goes through `_resolve_within`".

    Prose is not one, and this module has had the same gap twice over: the probes were given
    connection timeouts while the lookups in front of them -- `probe_proxy`'s and
    `resolved_addresses`' -- were left alone, because nothing failed when they stayed.
    `getaddrinfo` takes no timeout and spends the platform resolver's own budget, so a call to
    it anywhere else here is an unbounded wait by construction, however carefully its caller is
    written -- there is nothing for a reviewer to check except that the call is not there.

    A new caller that needs a name resolved calls `_resolve_within`; one that needs to resolve
    differently widens `_resolve_within`'s arguments. A second call site has to come through
    this test, on purpose.

    **Be exact about what this does not cover**, because the name it used to carry claimed the
    module resolved no name outside a deadline and that is not true. `socket.create_connection`
    on a *name* is the same unbounded lookup wearing a timeout that does not cover it, and
    `probe_direct` still makes one: the public name's lookup is #51's, and README says so where
    it states what `check` costs. `probe_on_link` needs nothing, since every address it is
    handed is a literal from the routing table. So what is pinned here is the resolver calls,
    not every name that gets resolved.
    """
    # `gethostname` is deliberately absent: it reads the name this host was given and asks no
    # resolver, which is why `own_addresses` may call it and then pass the result through the
    # bounded lookup. `getfqdn`, `gethostbyaddr` and `getnameinfo` are present because each is
    # a resolver round trip with no timeout of its own, exactly like `getaddrinfo`.
    lookup_names = frozenset(
        {
            "getaddrinfo",
            "gethostbyname",
            "gethostbyname_ex",
            "gethostbyaddr",
            "getnameinfo",
            "getfqdn",
        }
    )

    def is_lookup(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        # Both shapes, because they are the same call: `socket.getaddrinfo(...)` is an
        # `Attribute`, and the `getaddrinfo(...)` an import-time `from socket import
        # getaddrinfo` binds is a `Name`. Matching only the first is what let the reader
        # modules' own surface check be narrower than it read, and the fix there was the same.
        called = node.func
        if isinstance(called, ast.Attribute):
            return called.attr in lookup_names
        return isinstance(called, ast.Name) and called.id in lookup_names

    # Attributed to the *top-level* statement that holds the call, so one call site counts once
    # however deeply it is nested -- `_resolve_within` does the lookup in a nested function, and
    # counting every enclosing scope would have counted it twice. A lookup at module level
    # belongs to no definition and is reported as such rather than passed over.
    tree = ast.parse(Path(egress.__file__).read_text())
    lookups = [
        (getattr(statement, "name", "<module level>"), node.lineno)
        for statement in tree.body
        for node in ast.walk(statement)
        if is_lookup(node)
    ]
    assert [name for name, _ in lookups] == ["_resolve_within"], lookups


def test_nothing_to_probe_names_where_the_candidates_should_have_come_from() -> None:
    """Both empty-candidate paths fail, and neither blames the other's source.

    A caller that passed an empty list was told the routing table yielded nothing, which is a
    table it never read.
    """
    def stub(*_args: object, **_kwargs: object) -> str:
        return ON_LINK_NO_ANSWER

    from_caller = egress._on_link_results([], ports=[443], probe=stub, timeout_s=1)
    assert [ok for ok, _ in from_caller] == [False]
    assert "the candidates passed in" in from_caller[0][1]
    assert "routing table" not in from_caller[0][1]

    # Header-only tables, so the other branch is pinned without reading this machine's state.
    with pytest.MonkeyPatch.context() as patch:
        _pin_tables(patch, ROUTE_HEADER)
        from_table = egress._on_link_results(None, ports=[443], probe=stub, timeout_s=1)
    assert [ok for ok, _ in from_table] == [False]
    assert "the routing table" in from_table[0][1]
    assert "the candidates passed in" not in from_table[0][1]


def test_has_default_route_reads_the_table_rather_than_the_network() -> None:
    """The evidence the direct half falls back on when a name will not resolve."""
    assert has_default_route(INTERNAL_ROUTE_TABLE) is False
    assert has_default_route(ROUTED_ROUTE_TABLE) is True
    assert has_default_route(ROUTE_HEADER) is False
    # A default route with no gateway named -- `ip route add default dev eth0`. It is still a
    # way off this container's subnets, so it must not read as "no default route" and turn the
    # direct half's one passing branch into a false pass. `on_link_addresses` has a fixture for
    # this table shape and derives nothing from it; this half must still see the route.
    on_link_default = ROUTE_HEADER + _route("eth0", "0.0.0.0", "0.0.0.0", "0.0.0.0", "0001")
    assert has_default_route(on_link_default) is True


@asynctest
async def test_a_name_that_will_not_resolve_passes_only_with_no_default_route() -> None:
    """A failed lookup is not a routing fact, and `check` stops treating it as one.

    An internal network's resolver declines public names, so a confined container reaches this
    branch on every run -- it has to pass there, or the check fails every correct deployment.
    What makes it a pass is the routing table naming no default route, not the lookup failing.
    A container that has a route off the host and merely cannot resolve is unverified, which is
    the case that used to read as "no route round the proxy".
    """
    async with _CheckRig() as rig:
        for table, expected, fragment in (
            (INTERNAL_ROUTE_TABLE, True, "names no default route"),
            (ROUTED_ROUTE_TABLE, False, "this container has a default route"),
            (None, False, f"{ROUTE_TABLE_PATH} could not be read"),
        ):
            with pytest.MonkeyPatch.context() as patch:
                _pin_tables(patch, table)
                results = await rig.run(direct_probe=lambda *_a, **_k: DIRECT_NO_DNS)
            line = [(ok, text) for ok, text in results if "example.com" in text]
            assert len(line) == 1, results
            assert line[0][0] is expected, line
            assert fragment in line[0][1], line
            assert expected or "unverified" in line[0][1], line


@asynctest
async def test_a_direct_probe_that_never_left_the_container_fails_the_check() -> None:
    """The other new branch: no packet went out, so nothing was established."""
    async with _CheckRig() as rig:
        results = await rig.run(direct_probe=lambda *_a, **_k: DIRECT_UNVERIFIED)
    line = [(ok, text) for ok, text in results if "example.com" in text]
    assert line and line[0][0] is False
    assert "unverified" in line[0][1]


def test_the_peer_wording_says_what_each_peer_is() -> None:
    """The constants themselves, not `peer_addresses` reading them back.

    Every other assertion about these lines builds its expectation from the same constant, so
    it holds for any wording -- including the claim this check used to print, that the proxy
    "leads nowhere off this compose project". The proxy is precisely what leads off it, by the
    one route the allow-list bounds, and an operator reading that line is reading the whole
    output of this half. So the claim is pinned here in words.
    """
    assert "leads nowhere" not in egress.PEER_PROXY
    assert "the allow-listed way off this project rather than a way round it" in egress.PEER_PROXY
    assert "{proxy}" in egress.PEER_PROXY
    assert "establishes nothing either way" in egress.PEER_SELF


def test_the_probe_outcome_vocabularies_are_the_ones_the_suite_drives() -> None:
    """A new outcome has to be driven, not just added.

    The README pin builds what `check` can print by running it with stubs, so a branch for an
    outcome no stub returns is invisible to it -- README could then go stale about a line the
    code really prints. Pinning the vocabulary makes adding a fifth constant fail here, which
    is the prompt to give it a scenario in `test_readme_shows_the_lines_the_check_actually_prints`
    and a reading in README.
    """
    names = {n for n in dir(egress) if n.startswith("ON_LINK_")}
    assert names == {
        "ON_LINK_ACCEPTED",
        "ON_LINK_REFUSED",
        "ON_LINK_NO_ANSWER",
        "ON_LINK_UNVERIFIED",
    }
    assert {n for n in dir(egress) if n.startswith("DIRECT_")} == {
        "DIRECT_ANSWERED",
        "DIRECT_NO_ROUTE",
        "DIRECT_NO_DNS",
        "DIRECT_NO_RESOLVER",
        "DIRECT_UNVERIFIED",
    }


def test_the_check_command_exits_nonzero_when_any_assertion_failed(capsys, monkeypatch) -> None:
    """The CLI's exit status, which is the whole of what CI gates on.

    `.github/workflows/ci.yml` runs `docker compose exec -T codervis python -m app.egress check`
    and reads nothing but the status, so a `main` that returned 0 unconditionally would disable
    the enforcement of #37 with every assertion in this file still green. Nothing called `main`
    before this.
    """
    for results, expected in (
        ([(True, "a"), (True, "b")], 0),
        ([(True, "a"), (False, "b")], 1),
        ([(False, "a")], 1),
        # `all([])` is True, so a check that asserted nothing would otherwise exit 0 -- the one
        # shape that turns CI's gate into a no-op while every test here stays green.
        ([], 1),
    ):
        monkeypatch.setattr(egress, "check", lambda _environ, _r=results: _r)
        assert egress.main(["check"]) == expected, results
        printed = capsys.readouterr().out.splitlines()
        assert printed == [egress.format_result(ok, line) for ok, line in results]


def test_readme_shows_the_lines_the_check_actually_prints(monkeypatch) -> None:
    """README's sample output is what an operator compares their own run against.

    It is quoted prose, so nothing else makes it follow the code. The expectation here is not a
    second copy of the wording -- it is `check` itself, run with every probe stubbed and its
    results formatted the way `main` formats them. Change any line `check` prints and this
    fails until README is changed with it, which is what a literal expectation would not do.
    """
    proxy = "http://egress:3128"
    gateway = "172.30.0.1"

    def admitted(_proxy: str, host: str, port: int = 443, **_kwargs: object):
        return (403, "Forbidden") if host == egress.PROBE_DENIED_HOST else (200, "OK")

    monkeypatch.setattr(egress, "probe_proxy", admitted)
    monkeypatch.setattr(egress, "own_addresses", frozenset)
    monkeypatch.setattr(egress, "resolved_addresses", lambda _host: frozenset({gateway}))

    results = check(
        {"HTTPS_PROXY": proxy},
        direct=("example.com", 443),
        direct_probe=lambda *_a, **_k: DIRECT_NO_ROUTE,
        on_link=[gateway],
        on_link_probe=lambda *_a, **_k: ON_LINK_NO_ANSWER,
        timeout_s=1,
    )
    assert all(ok for ok, _ in results), results
    produced = [egress.format_result(ok, line) for ok, line in results]

    readme = ROOT_README.read_text()
    fence = re.search(r"```text\n(\[ OK \] http://egress:3128.*?)```", readme, re.S)
    assert fence, "README no longer shows the expected `check` output"
    shown = [line for line in fence.group(1).splitlines() if line.strip()]
    assert shown == produced

    # The direct half's other three lines are shown in a fence of their own, because prose
    # that merely contains the wording pins nothing: a README could quote both lines verbatim
    # and still call them both passes. Comparing the formatted line -- verdict prefix included
    # -- is what ties each one to the `OK` or `FAIL` README claims for it.
    def direct_line(table: str | None, outcome: str = DIRECT_NO_DNS) -> str:
        with pytest.MonkeyPatch.context() as patch:
            _pin_tables(patch, table)
            results = check(
                {"HTTPS_PROXY": proxy},
                direct=("example.com", 443),
                direct_probe=lambda *_a, _o=outcome, **_k: _o,
                on_link=[gateway],
                on_link_probe=lambda *_a, **_k: ON_LINK_NO_ANSWER,
                timeout_s=1,
            )
        ok, text = next((ok, t) for ok, t in results if t.startswith("example.com"))
        return egress.format_result(ok, text)

    no_dns_fence = re.search(
        r"```text\n(\[ OK \] example\.com does not resolve.*?)```", readme, re.S
    )
    assert no_dns_fence, "README no longer shows the no-DNS outcomes"
    # Four, because a resolver that never answered is settled by the same tables and is not the
    # same line: only one of the two says the name does not resolve, which is a claim about DNS
    # the other did not establish.
    no_dns = [
        direct_line(INTERNAL_ROUTE_TABLE),
        direct_line(INTERNAL_ROUTE_TABLE, DIRECT_NO_RESOLVER),
        direct_line(ROUTED_ROUTE_TABLE),
        direct_line(None),
    ]
    assert [line for line in no_dns_fence.group(1).splitlines() if line.strip()] == no_dns

    # Pinning the two fences pins only what they quote. A second fence above them, showing the
    # same failures as passes, would be invisible to that -- and it is a fence an operator
    # diffs their own run against. So every sample line anywhere in README must be one `check`
    # can produce, with the verdict README gives it.
    # Several scenarios, not one: README is entitled to show an on-link address that answered,
    # one that is this container, or a list over the cap, and a set built from a single run
    # would reject those as "not producible" rather than checking them.
    def scenario(**over: object) -> list[str]:
        opts: dict = {
            "direct": ("example.com", 443),
            "direct_probe": lambda *_a, **_k: DIRECT_NO_ROUTE,
            "on_link": [gateway],
            "on_link_probe": lambda *_a, **_k: ON_LINK_NO_ANSWER,
            "timeout_s": 1,
        }
        opts.update(over)
        return [egress.format_result(ok, line) for ok, line in check({"HTTPS_PROXY": proxy}, **opts)]

    flowed = " ".join(readme.split())
    producible = set(produced) | set(no_dns)
    for over in (
        {"on_link_probe": lambda *_a, **_k: ON_LINK_ACCEPTED},
        {"on_link_probe": lambda *_a, **_k: ON_LINK_REFUSED},
        {"on_link_probe": lambda *_a, **_k: ON_LINK_UNVERIFIED},
        {"on_link": [f"10.0.0.{n}" for n in range(1, MAX_ONLINK_PROBES + 3)]},
        {"direct_probe": lambda *_a, **_k: DIRECT_ANSWERED},
        {"direct_probe": lambda *_a, **_k: DIRECT_UNVERIFIED},
    ):
        producible |= set(scenario(**over))
    with mock.patch.object(egress, "own_addresses", lambda: frozenset({gateway})):
        producible |= set(scenario())

    # Anchored with optional indentation: a fence indented inside a list item is valid Markdown,
    # renders as a code block, and is exactly as much a sample an operator diffs their run
    # against -- a column-0 anchor would not see it.
    samples = [m.strip() for m in re.findall(r"^[ \t]*(\[(?: OK |FAIL)\] .*?)[ \t]*$", readme, re.M)]
    assert samples, "README shows no sample output at all"
    assert set(samples) <= producible, sorted(set(samples) - producible)

    # A line quoted inline in prose rather than in a fence carries no verdict prefix, so the
    # sample scan above does not see it and it can go stale on its own. The one there is is
    # checked as a prefix of a line `check` can really print.
    inline = "example.com:443 was not settled"
    assert f"`{inline}`" in flowed, "README no longer quotes the unverified line"
    assert any(line[7:].startswith(inline) for line in producible), sorted(producible)

    # The verdicts the fences carry are also claimed in prose, which no fence comparison holds.
    assert "Only the first two are passes, and the routing table is what makes them so." in flowed

    # README counts the public-name line's forms for an operator checking they have seen them
    # all, so the count comes from the code rather than from whoever last edited the sentence.
    # One of them is in the first fence, hence "N more forms".
    # Both prefixes are seven characters, so the payload starts at 7; only the public-name
    # line's payload begins with the probed host.
    direct_forms = {line for line in producible if line[7:].startswith("example.com")}
    spelled = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven"}
    beyond_the_fence = len(direct_forms) - 1
    assert beyond_the_fence in spelled, (
        f"{len(direct_forms)} public-name forms is outside what README spells out; "
        f"give the count a word and update the sentence: {sorted(direct_forms)}"
    )
    assert f"{spelled[beyond_the_fence]} more forms" in flowed, sorted(direct_forms)

    # Acceptance criterion 5 lives in prose the fences cannot hold: an engine floor, and what
    # an operator on an older engine does instead. Deleting either left the suite green.
    assert "Docker Engine 28.0" in flowed
    assert "drops new inbound connections arriving on that bridge's interface" in flowed
    assert "A `FAIL` that says **unverified**" in flowed
    assert "is not a reachable host: it means the check could not ask" in flowed


def test_the_public_name_probe_reads_a_refusal_as_reach_not_as_no_route() -> None:
    """The defect this half had, in the half that was not touched first.

    A refused port means packets left the container and something answered with an RST, so a
    route round the proxy exists. Reading that as "no route" -- which is what the old blanket
    `except OSError` did -- printed `[ OK ]` over it.
    """
    assert probe_direct("127.0.0.1", _dead_port(), timeout_s=1) == DIRECT_ANSWERED


def test_the_public_name_probe_reads_an_accept_as_reach() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        assert probe_direct("127.0.0.1", listener.getsockname()[1], timeout_s=2) == DIRECT_ANSWERED


def _candidates(*addresses: str) -> list[tuple]:
    """`getaddrinfo`'s answer for a name with these addresses, in this order.

    Documentation-range literals: nothing here is dialled for real, because every case that
    uses this replaces the socket layer under it as well. The order is the point in the cases
    about candidates that disagree, so it is the order given.
    """
    return [
        (
            socket.AF_INET6 if ":" in address else socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (address, 443, 0, 0) if ":" in address else (address, 443),
        )
        for address in addresses
    ]


def _dialling(outcomes: dict[str, BaseException | None]):
    """A `socket.socket` that fails the way the address being connected to says it does.

    Keyed on the address, because that is what these cases are about: a name whose candidates
    disagree cannot be expressed by a stub that answers once for the whole name. `None` is a
    connection the address accepted. The list it returns records what was really dialled, in
    order, so "every candidate is classified" is asserted on the dials and not only on the
    verdict they produced.

    It replaces `socket.socket` rather than `socket.create_connection`, which `probe_direct`
    no longer calls: it resolves the name itself and dials each address, so this is the seam
    under it.
    """
    dialled: list[str] = []

    class FakeSocket:
        def __init__(self, family: int, socktype: int = 0, proto: int = 0) -> None:
            self.family = family

        def settimeout(self, timeout_s: float) -> None:
            # A non-positive timeout puts a real socket in non-blocking mode, where `connect`
            # returns EINPROGRESS at once and a candidate reads as a local failure it never
            # had. `probe_direct` must never hand one down, however little budget is left.
            assert timeout_s > 0, f"a candidate was dialled with timeout_s={timeout_s}"

        def connect(self, sockaddr: tuple) -> None:
            dialled.append(sockaddr[0])
            failure = outcomes[sockaddr[0]]
            if failure is not None:
                raise failure

        def close(self) -> None:
            pass

    return FakeSocket, dialled


@contextlib.contextmanager
def _name_resolving_to(outcomes: dict[str, BaseException | None]):
    """A name whose addresses are `outcomes`' keys, each answering the way its value says."""
    fake, dialled = _dialling(outcomes)
    with mock.patch.object(egress.socket, "getaddrinfo", lambda *_a, **_k: _candidates(*outcomes)):
        with mock.patch.object(egress.socket, "socket", fake):
            yield dialled


def test_the_public_name_probe_tells_no_route_from_no_dns_from_no_probe() -> None:
    """Three not-reached outcomes, and only one of them is evidence on its own.

    A timeout or an unreachable network means the probe went out and nothing came back. A name
    the resolver declines is neither verdict here -- `check` settles that one against the
    routing table, because a failed lookup says nothing about whether packets can leave. A
    connection that never left this container is a failure, for the same reason the on-link
    half fails one.

    The partition is the on-link half's, errno for errno, because one errno answered in
    opposite directions by the two halves of one check is one of them passing on what the
    other fails. `EAFNOSUPPORT` is a local failure in both, which would fail the check on a
    container with IPv6 off kernel-wide if such a candidate could arise -- and `AI_ADDRCONFIG`
    on the lookup is what keeps it from arising, rather than an exception to the partition.
    """
    for failure, expected in (
        (TimeoutError(), DIRECT_NO_ROUTE),
        (as_oserror(errno.ENETUNREACH), DIRECT_NO_ROUTE),
        (as_oserror(errno.EHOSTUNREACH), DIRECT_NO_ROUTE),
        (as_oserror(errno.EAFNOSUPPORT), DIRECT_UNVERIFIED),
        (as_oserror(errno.EPERM), DIRECT_UNVERIFIED),
        (as_oserror(errno.ENETDOWN), DIRECT_UNVERIFIED),
        (as_oserror(errno.EMFILE), DIRECT_UNVERIFIED),
        (ConnectionResetError(errno.ECONNRESET, "reset"), DIRECT_ANSWERED),
        (ConnectionRefusedError(errno.ECONNREFUSED, "refused"), DIRECT_ANSWERED),
        (None, DIRECT_ANSWERED),
    ):
        with _name_resolving_to({"192.0.2.1": failure}):
            assert probe_direct("example.com", 443, timeout_s=0.5) == expected, failure

    # `EAFNOSUPPORT` really arrives from `socket.socket`, before there is anything to connect,
    # so the row above reaches that classification by the other door. Both doors, or the one a
    # kernel with no IPv6 actually comes through is the untested one.
    def no_socket_in_that_family(*_args: object, **_kwargs: object) -> None:
        raise as_oserror(errno.EAFNOSUPPORT)

    def v6_only(*_args: object, **_kwargs: object) -> list[tuple]:
        return _candidates("2001:db8::1")

    with mock.patch.object(egress.socket, "getaddrinfo", v6_only):
        with mock.patch.object(egress.socket, "socket", no_socket_in_that_family):
            assert probe_direct("example.com", 443, timeout_s=0.5) == DIRECT_UNVERIFIED


def test_the_lookup_asks_only_for_families_this_container_holds_an_address_in() -> None:
    """`AI_ADDRCONFIG`, which is what keeps the errno partition above from biting.

    An AAAA candidate on a container with IPv6 off kernel-wide fails `EAFNOSUPPORT` at the
    socket, which is a local failure and fails the check -- where the same candidate on a
    container that merely holds no v6 address fails `ENETUNREACH`, which is no route and
    harmless. `socket.create_connection` did not ask for the flag and got away with it by
    reporting only the last candidate's error; this half reads every candidate, so the flag
    is load-bearing rather than tidy.
    """
    asked: dict[str, object] = {}

    def record(*args: object, **kwargs: object) -> list[tuple]:
        asked.update(kwargs)
        return _candidates("192.0.2.1")

    with mock.patch.object(egress.socket, "getaddrinfo", record):
        with mock.patch.object(egress.socket, "socket", _dialling({"192.0.2.1": None})[0]):
            probe_direct("example.com", 443, timeout_s=0.5)
    assert asked.get("flags", 0) & socket.AI_ADDRCONFIG


def test_a_lookup_that_did_not_come_back_is_told_from_a_name_that_is_not_there() -> None:
    """Which `EAI_*` codes are the resolver answering, and which are it failing to.

    The outcome must not turn on timing. A resolver configured `timeout:1 attempts:1`, a
    forwarder that gives up, a musl image: `getaddrinfo` *returns*, inside whatever budget the
    probe gave it, with `EAI_AGAIN`. Reading that as `DIRECT_NO_DNS` would have `check` print
    `[ OK ] example.com does not resolve here` over a resolver that said no such thing --
    which is the claim this module is careful everywhere else not to make.
    """
    for code, expected in (
        (socket.EAI_NONAME, DIRECT_NO_DNS),
        (getattr(socket, "EAI_NODATA", socket.EAI_NONAME), DIRECT_NO_DNS),
        (socket.EAI_AGAIN, DIRECT_NO_RESOLVER),
        (socket.EAI_FAIL, DIRECT_NO_RESOLVER),
        (socket.EAI_SYSTEM, DIRECT_NO_RESOLVER),
    ):
        failure = socket.gaierror(code, "lookup failed")
        with mock.patch.object(egress.socket, "getaddrinfo", side_effect=failure):
            assert probe_direct("example.com", 443, timeout_s=0.5) == expected, code


def test_a_refusal_from_any_candidate_of_the_name_reads_as_reach() -> None:
    """#51's second half: a dual-stack name must not be able to hide a refusal.

    `socket.create_connection` defaults to `all_errors=False` and raises the *last* candidate's
    error, so a name whose first address answered with an RST -- a live host, and therefore a
    route round the proxy -- and whose last was unreachable reported "no route", and the check
    printed `[ OK ]` over it. That is the accept-a-refusal-as-silence conflation #37 took out
    of this function's verdicts, in the one corner its verdicts did not cover.

    Either order, because "the last one decides" is exactly the defect: reading only the first
    would be the same bug facing the other way.
    """
    refused = ConnectionRefusedError(errno.ECONNREFUSED, "refused")
    unreachable = as_oserror(errno.ENETUNREACH)
    for outcomes in (
        {"2001:db8::1": refused, "192.0.2.1": unreachable},
        {"192.0.2.1": unreachable, "2001:db8::1": refused},
    ):
        with _name_resolving_to(outcomes):
            assert probe_direct("example.com", 443, timeout_s=1) == DIRECT_ANSWERED, outcomes


def test_the_public_name_probe_classifies_every_candidate_not_only_the_last() -> None:
    """What the disagreeing candidates settle on when none of them was reached.

    The least-established answer wins, which is the rule the rest of this module already
    follows: "no route" is claimed only where every address the name has was really dialled and
    nothing came back, and a candidate whose connection never left this container leaves the
    whole name unverified -- a probe not made establishes nothing, and `check` fails on it
    rather than passing.
    """
    unreachable = as_oserror(errno.ENETUNREACH)
    with _name_resolving_to({"192.0.2.1": unreachable, "2001:db8::1": unreachable}) as dialled:
        assert probe_direct("example.com", 443, timeout_s=1) == DIRECT_NO_ROUTE
    assert dialled == ["192.0.2.1", "2001:db8::1"], "every candidate is dialled, not just one"

    for outcomes in (
        {"192.0.2.1": unreachable, "2001:db8::1": as_oserror(errno.EPERM)},
        {"192.0.2.1": as_oserror(errno.EPERM), "2001:db8::1": unreachable},
    ):
        with _name_resolving_to(outcomes):
            assert probe_direct("example.com", 443, timeout_s=1) == DIRECT_UNVERIFIED, outcomes

    # A reachable candidate still wins over both of those: reach is established, and the other
    # candidates cannot unestablish it.
    with _name_resolving_to(
        {"192.0.2.1": ConnectionRefusedError(errno.ECONNREFUSED, "refused"), "2001:db8::1": None}
    ):
        assert probe_direct("example.com", 443, timeout_s=1) == DIRECT_ANSWERED


def test_a_resolver_that_does_not_answer_is_bounded_and_is_its_own_outcome() -> None:
    """#51's first half: `getaddrinfo` used to run outside the probe's timeout entirely.

    `socket.create_connection`'s timeout does not start until the name is resolved, so a
    container whose resolver is unreachable spent the resolver's own budget first -- 5 s an
    attempt by `/etc/resolv.conf`'s default, and it retries -- and `check` took far longer than
    the bound it gives itself. The lookup is joined with a deadline now, so the whole probe
    costs its budget.

    And it is its own outcome. Folding it into "does not resolve" would have the passing line
    claim the resolver declined the name, which nothing here established: the resolver said
    nothing at all. `check` settles both against the routing tables, which answer either way.
    """
    released = threading.Event()

    def resolver_that_does_not_answer(*_args: object, **_kwargs: object) -> list[tuple]:
        # Released in the `finally` below, so a run that asserts correctly does not leave a
        # thread sleeping out a wall-clock timeout behind it.
        released.wait(30)
        return _candidates("192.0.2.1")

    try:
        with mock.patch.object(egress.socket, "getaddrinfo", resolver_that_does_not_answer):
            started = time.monotonic()
            outcome = probe_direct("example.com", 443, timeout_s=1)
            elapsed = time.monotonic() - started
    finally:
        released.set()

    assert outcome == DIRECT_NO_RESOLVER
    # Above the budget with room for a loaded runner, and far below the thirty seconds the
    # resolver was going to take: the claim is that the probe stopped waiting on its own.
    assert elapsed < 4, f"the probe outlasted its own budget: {elapsed:.2f}s for a 1 s budget"


def test_the_lookup_gets_a_share_of_the_budget_rather_than_all_of_it() -> None:
    """What `RESOLVE_BUDGET_SHARE` is for, which nothing else here would notice was gone.

    The dials are what establish the thing this half asserts, so a resolver that is merely
    slow must not be able to spend their budget: given the whole window it takes as much of
    it as it likes and the dials get whatever is left, down to nothing. Cut at its share
    instead, a lookup that outlasts the share is reported for what it was -- a resolver that
    did not answer in the time it had -- which `check` settles against the routing tables
    like any other, on its own line.

    Driven on the outcome *and* the clock, because the two implementations differ in both. A
    lookup cut at its share comes back at the share with `DIRECT_NO_RESOLVER`; one given the
    whole window comes back when the resolver finally does, three times later, and dials what
    it found -- so it is `DIRECT_ANSWERED` here rather than a failure, and the clock is the
    other half of what says which implementation ran.
    """
    def slower_than_its_share(*_args: object, **_kwargs: object) -> list[tuple]:
        time.sleep(3.0)
        return _candidates("192.0.2.1")

    refused = ConnectionRefusedError(errno.ECONNREFUSED, "refused")
    with mock.patch.object(egress.socket, "getaddrinfo", slower_than_its_share):
        with mock.patch.object(egress.socket, "socket", _dialling({"192.0.2.1": refused})[0]):
            started = time.monotonic()
            outcome = probe_direct("example.com", 443, timeout_s=2)
            elapsed = time.monotonic() - started

    assert outcome == DIRECT_NO_RESOLVER, (
        f"a lookup taking 3 s of a 2 s budget read as {outcome!r}: it is being given the "
        "whole window rather than RESOLVE_BUDGET_SHARE of it, and the dials got what was left"
    )
    # Between the 1 s share and the 3 s the resolver was going to take, with a second of slack
    # either side: tight enough to mean something, loose enough for a loaded runner.
    assert elapsed < 2.0, f"the lookup was waited on for {elapsed:.2f}s of its 1 s share"

    # The other side of the same number: a lookup inside its share leaves the dials the rest
    # of the budget, which is what makes a name that resolves slowly still get dialled.
    def inside_its_share(*_args: object, **_kwargs: object) -> list[tuple]:
        time.sleep(0.4)
        return _candidates("192.0.2.1")

    with mock.patch.object(egress.socket, "getaddrinfo", inside_its_share):
        with mock.patch.object(egress.socket, "socket", _dialling({"192.0.2.1": refused})[0]):
            assert probe_direct("example.com", 443, timeout_s=2) == DIRECT_ANSWERED


class _AnsweringAfter:
    """A socket layer where every address answers, but not before `delay_s` has passed.

    Built rather than stubbed with an exception, because the cases below are about *when* an
    address answers relative to the budget the probe gave it: a stub that raises at once
    cannot tell a candidate that was waited for from one that was not.
    """

    def __init__(self, delay_s: float, failure: BaseException | None) -> None:
        self.delay_s = delay_s
        self.failure = failure
        self.dialled: list[str] = []

    def __call__(self, *_args: object, **_kwargs: object) -> object:
        layer = self

        class Socket:
            def __init__(self) -> None:
                self._timeout_s = 0.0

            def settimeout(self, timeout_s: float) -> None:
                self._timeout_s = timeout_s

            def connect(self, sockaddr: tuple) -> None:
                layer.dialled.append(sockaddr[0])
                if layer.delay_s > self._timeout_s:
                    time.sleep(self._timeout_s)
                    raise TimeoutError
                time.sleep(layer.delay_s)
                if layer.failure is not None:
                    raise layer.failure

            def close(self) -> None:
                pass

        return Socket()


def _probe_through(layer: _AnsweringAfter, addresses: list[str], *, timeout_s: float):
    def resolves(*_args: object, **_kwargs: object) -> list[tuple]:
        return _candidates(*addresses)

    with mock.patch.object(egress.socket, "getaddrinfo", resolves):
        with mock.patch.object(egress.socket, "socket", layer):
            started = time.monotonic()
            outcome = probe_direct("example.com", 443, timeout_s=timeout_s)
            return outcome, time.monotonic() - started


def test_an_address_that_answers_slowly_is_still_reach_when_the_name_has_many() -> None:
    """The bound must not be bought by making each address' window too short to answer in.

    This is what a share-of-the-budget-per-address implementation gets wrong, and it gets it
    wrong in the direction that matters: `example.com` publishes a dozen addresses, so each
    would get a twelfth of the probe's budget -- under Linux's own first SYN retransmit. A
    live host on a slow path, or one whose first SYN was dropped, would answer after the probe
    had already called it silence, and silence is what the check *passes* on. Each address
    gets the whole of what is left instead, and the budget bounds how long silence may be
    waited on in total.
    """
    layer = _AnsweringAfter(0.3, ConnectionRefusedError(errno.ECONNREFUSED, "refused"))
    addresses = [f"192.0.2.{n}" for n in range(1, 13)]
    outcome, elapsed = _probe_through(layer, addresses, timeout_s=1)

    assert outcome == DIRECT_ANSWERED, (
        f"an address that answered in 0.3s of a 1 s budget read as {outcome!r}: a name with "
        f"{len(addresses)} addresses is dividing the answer window between them"
    )
    assert layer.dialled == addresses[:1], "it answered on the first address and stopped there"
    assert elapsed < 1.5


def test_an_address_is_reported_unasked_rather_than_dialled_with_no_time_to_answer() -> None:
    """`MIN_DIAL_BUDGET_S`, which is the floor under the window a candidate may be given.

    The loop hands each address what is left of the budget, so an address after one that
    overshot the deadline can be offered a window of microseconds. A `connect` with that
    window comes back a timeout whatever is at the other end, and a timeout reads as silence,
    which is what the check *passes* on -- so the leftover is reported unasked instead.

    It rules out the degenerate window and not a short one: the floor is a constant, and one
    large enough to guarantee an answer would have to be a fraction of the caller's budget or
    it would leave a small budget with nothing dialled at all. What carries that guarantee is
    the order -- the first address gets the whole of what the lookup left.
    """
    class Overshooting:
        """A first address that spends the budget, and a second that must not be dialled."""

        def __init__(self) -> None:
            self.dialled: list[str] = []

        def __call__(self, *_args: object, **_kwargs: object) -> object:
            layer = self

            class Socket:
                def settimeout(self, timeout_s: float) -> None:
                    assert timeout_s >= egress.MIN_DIAL_BUDGET_S, (
                        f"an address was dialled with {timeout_s:.4f}s, under the floor"
                    )

                def connect(self, sockaddr: tuple) -> None:
                    layer.dialled.append(sockaddr[0])
                    # All but a twentieth of the budget, so what is left for the address
                    # after this one is under the floor and over zero -- which is the whole
                    # of the difference between the floor and a bare deadline check.
                    time.sleep(0.95)
                    raise TimeoutError

                def close(self) -> None:
                    pass

            return Socket()

    layer = Overshooting()
    addresses = ["192.0.2.1", "192.0.2.2"]

    def resolves(*_args: object, **_kwargs: object) -> list[tuple]:
        return _candidates(*addresses)

    with mock.patch.object(egress.socket, "getaddrinfo", resolves):
        with mock.patch.object(egress.socket, "socket", layer):
            outcome = probe_direct("example.com", 443, timeout_s=1)

    assert layer.dialled == ["192.0.2.1"], (
        "the second address was dialled with what the first left over, which is a window it "
        "could only time out in -- and a timeout here is the pass"
    )
    assert outcome == DIRECT_UNVERIFIED, "an address not asked leaves the name unsettled"


def test_the_public_name_probe_costs_its_budget_once_over_all_of_a_name_s_addresses() -> None:
    """The bound is the probe's, not the probe's times however many addresses the name has.

    A name on a CDN resolves to a dozen addresses, and the old half gave each one the whole
    timeout in turn, with the lookup in front of them outside the timeout altogether: a
    `check` that said three seconds spent most of a minute. One budget covers all of it now.

    Silence is what spends it, so silence is what runs out of it, and a name only partly
    asked is `DIRECT_UNVERIFIED` -- it fails the check rather than passing it, because the
    addresses that went undialled establish nothing. The container this project ships never
    reaches it: `internal: true` withholds the default route, so the kernel rejects every
    connect immediately and all of them are dialled for nothing. A deployment confined by
    *dropping* egress does reach it, and README says what an operator there reads it as.
    """
    layer = _AnsweringAfter(30.0, None)  # far beyond any budget: nothing ever answers
    addresses = [f"192.0.2.{n}" for n in range(1, 9)]
    outcome, elapsed = _probe_through(layer, addresses, timeout_s=1)

    assert outcome == DIRECT_UNVERIFIED, "the addresses after the first were never asked"
    assert layer.dialled == addresses[:1]
    assert elapsed < 4, (
        f"{len(addresses)} silent addresses cost {elapsed:.2f}s of a 1 s budget: they are "
        "being given a timeout each rather than spending one between them"
    )


ROUTE_HEADER = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
)


def _column(address: str) -> str:
    """An address as /proc/net/route renders it: the `__be32` printed as one host-order word.

    Built rather than written out, because that is exactly what `_address` reverses. A literal
    would be the little-endian rendering, and these fixtures would then be the one thing that
    fails on the big-endian machine the native-order unpacking exists for.
    """
    return f"{int.from_bytes(socket.inet_aton(address), sys.byteorder):08X}"


def _route(iface: str, destination: str, gateway: str, netmask: str, flags: str) -> str:
    columns = (_column(destination), _column(gateway), flags, "0", "0", "0", _column(netmask))
    return iface + "\t" + "\t".join(columns) + "\t0\t0\t0\n"


# A routing table shaped like the dashboard container's: one on-link subnet, no default route,
# which is what `internal: true` produces -- 172.30.0.0/16 on eth0, holding this container at
# 172.30.0.2.
INTERNAL_ROUTE_TABLE = ROUTE_HEADER + _route(
    "eth0", "172.30.0.0", "0.0.0.0", "255.255.0.0", "0001"
)
# The same container given a default route as well, which is what joining a non-internal network
# does: 172.17.0.1 is the gateway it names.
ROUTED_ROUTE_TABLE = (
    ROUTE_HEADER
    + _route("eth0", "0.0.0.0", "172.17.0.1", "0.0.0.0", "0003")
    + _route("eth0", "172.17.0.0", "0.0.0.0", "255.255.0.0", "0001")
)


def test_the_on_link_addresses_are_the_bridge_gateway_internal_true_leaves_behind() -> None:
    """The address `internal: true` does not remove: the first of the container's own subnet,
    which is where Docker puts the bridge's gateway, reachable with no route at all (#37)."""
    assert on_link_addresses(INTERNAL_ROUTE_TABLE) == ["172.30.0.1"]


def test_a_gateway_a_route_names_comes_first_and_is_not_repeated() -> None:
    assert on_link_addresses(ROUTED_ROUTE_TABLE) == ["172.17.0.1"]


def test_the_on_link_addresses_leave_out_loopback() -> None:
    """Which candidates are this project rather than the host is `peer_addresses`' job, so the
    derivation drops only what is nobody's: loopback, and the unspecified address."""
    loopback = ROUTE_HEADER + _route("lo", "127.0.0.0", "0.0.0.0", "255.0.0.0", "0001")
    assert on_link_addresses(loopback) == []


@pytest.mark.parametrize(
    "table",
    [
        "",
        "Iface\tDestination\tGateway \n",
        "header\neth0\tnonsense\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n",
        # /31 and /32 hold no gateway address of their own to derive.
        ROUTE_HEADER + _route("eth0", "172.30.0.0", "0.0.0.0", "255.255.255.254", "0001"),
    ],
)
def test_an_unreadable_routing_table_yields_no_addresses_rather_than_raising(table: str) -> None:
    assert on_link_addresses(table) == []


def test_a_multicast_route_derives_no_candidate_in_either_family() -> None:
    """`224.0.0.0/4` and `ff00::/8` are on-link in a container that has them, and the first
    address of each is a group rather than a host: dialling `224.0.0.1` establishes nothing and
    spends one of the four probes this check will make. One rule for both families, so both are
    driven -- the IPv4 route is the one a reader would assume was covered by the older tests."""
    multicast = ROUTE_HEADER + _route("eth0", "224.0.0.0", "0.0.0.0", "240.0.0.0", "0001")
    assert on_link_addresses(multicast) == []
    assert on_link_addresses(multicast, _route6("eth0", "ff00::", 8, "::")) == []


def test_an_on_link_default_route_derives_nothing() -> None:
    """`ip route add default dev eth0`: a /0 is not a subnet this container is on, and deriving
    its first address would report 0.0.0.1 as checked. The public-name probe covers that case."""
    on_link_default = (
        "header\n"
        "eth0\t00000000\t00000000\t0001\t0\t0\t0\t00000000\t0\t0\t0\n"
    )
    assert on_link_addresses(on_link_default) == []


def _column6(address: str) -> str:
    """An address as /proc/net/ipv6_route renders it: the sixteen bytes as hex, in wire order.

    Built rather than written out, as `_column` is -- and the contrast is the point. There is no
    host order to undo here, because `%pi6` prints the bytes in the order they go on the wire, so
    a fixture that is built from `inet_pton` is the same on either endianness and pins that the
    parser does no unpacking.
    """
    return socket.inet_pton(socket.AF_INET6, address).hex()


def _route6(
    iface: str, destination: str, prefixlen: int, gateway: str, flags: str = "00000001"
) -> str:
    """One line of /proc/net/ipv6_route, which is not shaped like /proc/net/route's.

    No header above it, the device *last* rather than first, and ten columns: destination and
    its prefix length, a source prefix and length, the gateway, then metric, refcount, use,
    flags and the device.

    The device is written the way the kernel writes it -- `%8s`, so right-justified in eight
    columns, and eight spaces where a route has no device at all. That is the shape the parser's
    "the tenth field, if there is one" reading exists for: a line with no device splits into nine
    fields, and reading the flags from a fixed index is what keeps a reject route with no device
    from being derived from.
    """
    return (
        " ".join(
            (
                _column6(destination),
                f"{prefixlen:02x}",
                _column6("::"),
                "00",
                _column6(gateway),
                "00000100",
                "00000001",
                "00000000",
                flags,
            )
        )
        + f" {iface:>8}\n"
    )


# RTF_NONEXTHOP | RTF_REJECT: the `unreachable default` an IPv6-enabled netns holds when it has
# nowhere to send the family, which is in every such container's table and is not a way out.
REJECT_FLAGS = f"{0x00200200:08x}"
# The IPv6 side of a dual-stack `inside` network, as a container joined to one holds it: the ULA
# prefix the compose file would configure, the link-local prefix every interface has, multicast,
# and the kernel's own two entries on `lo`. Nothing here is a way off the container, and the
# only candidate in it is fd00:cafe::1 -- the first address of the container's own prefix, which
# is where Docker puts a bridge's IPv6 gateway.
INTERNAL_IPV6_ROUTE_TABLE = (
    _route6("eth0", "fd00:cafe::", 64, "::")
    + _route6("eth0", "fe80::", 64, "::")
    + _route6("eth0", "ff00::", 8, "::")
    + _route6("lo", "::1", 128, "::")
    + _route6("lo", "::", 0, "::", flags=REJECT_FLAGS)
)
# The same container given an IPv6 default route as well, which is what a dual-stack network
# that is not internal does: fd00:cafe::1 is the gateway it names.
ROUTED_IPV6_ROUTE_TABLE = (
    _route6("eth0", "::", 0, "fd00:cafe::1", flags="00000003") + INTERNAL_IPV6_ROUTE_TABLE
)


def test_the_on_link_addresses_include_the_gateway_of_a_second_family() -> None:
    """A network with `enable_ipv6` has a second gateway address, on-link in the container's own
    prefix and reachable with no route exactly as the first one is (#42). Both tables are read,
    and the IPv4 candidates keep their place at the front.

    What the IPv6 table holds beside that prefix is in the fixture on purpose: `fe80::/64`,
    `ff00::/8` and two `lo` entries are in every IPv6-enabled container, and none of them is an
    address to dial -- so a derivation that took "the first address of each on-link prefix"
    literally would spend the probe cap on three addresses nothing holds.
    """
    assert on_link_addresses(INTERNAL_ROUTE_TABLE, INTERNAL_IPV6_ROUTE_TABLE) == [
        "172.30.0.1",
        "fd00:cafe::1",
    ]


def test_a_gateway_an_ipv6_route_names_comes_first_and_is_not_repeated() -> None:
    """Gateways before derived addresses, across both families, and the same address named by a
    route and derived from a prefix is one candidate."""
    assert on_link_addresses(INTERNAL_ROUTE_TABLE, ROUTED_IPV6_ROUTE_TABLE) == [
        "fd00:cafe::1",
        "172.30.0.1",
    ]


def test_an_ipv6_link_local_gateway_carries_the_device_it_is_reachable_through() -> None:
    """A router-advertised default route names a link-local gateway, and a link-local address
    cannot be dialled without a scope: `connect` to a bare `fe80::1` fails inside this container,
    which would report as "not probed" for an address the table says is reachable. The device is
    in the table, so the candidate carries it."""
    table = _route6("eth0", "::", 0, "fe80::1", flags="00000003")
    assert on_link_addresses(ROUTE_HEADER, table) == ["fe80::1%eth0"]


@pytest.mark.parametrize(
    "table",
    [
        "",
        "not a route table\n",
        # A line that is short of the nine columns the kernel prints.
        "00000000000000000000000000000000 00 eth0\n",
        # Columns that are not hex, and one that is hex of the wrong length.
        _route6("eth0", "fd00:cafe::", 64, "::").replace(_column6("fd00:cafe::"), "z" * 32),
        _route6("eth0", "fd00:cafe::", 64, "::").replace(_column6("fd00:cafe::"), "ff"),
        # A /127 and a /128 hold no separate gateway address to derive, as a /31 and a /32 do
        # not over IPv4.
        _route6("eth0", "fd00:cafe::", 127, "::"),
        _route6("eth0", "fd00:cafe::", 128, "::"),
        # `::/0` on-link: a default route is not a subnet this container is on, and deriving its
        # first address would report ::1 as checked.
        _route6("eth0", "::", 0, "::"),
        # Link-local: built from the interface's MAC rather than handed out, so nothing is at
        # fe80::1. Multicast: a group, not a host. Both are on-link in every such container.
        _route6("eth0", "fe80::", 64, "::"),
        _route6("eth0", "ff00::", 8, "::"),
        # The kernel's own entries, which are on `lo`.
        _route6("lo", "::1", 128, "::"),
        _route6("lo", "fd00:cafe::", 64, "::"),
        # A reject route: this netns saying it has nowhere to send the family, on a real device.
        _route6("eth0", "::", 0, "::", flags=REJECT_FLAGS),
        _route6("eth0", "fd00:cafe::", 64, "::", flags=REJECT_FLAGS),
    ],
)
def test_an_ipv6_table_yields_no_candidate_it_should_not_rather_than_raising(table: str) -> None:
    assert on_link_addresses(ROUTE_HEADER, table) == []


def test_a_route_with_no_device_is_read_from_the_nine_fields_it_prints() -> None:
    """The kernel prints the device as `%8s`, so a route that has none prints eight spaces and
    the line splits into nine fields rather than ten. Both readers of this file branch on that,
    and a regression to a bare `fields[9]` would be an `IndexError` out of `on_link_addresses`
    into `check` -- which catches nothing, so a traceback and no verdict at all rather than a
    FAIL. The flags stay at their own index either way, which is what keeps a reject route with
    no device from being derived from.
    """
    assert on_link_addresses(ROUTE_HEADER, _route6("", "fd00:cafe::", 64, "::")) == [
        "fd00:cafe::1"
    ]
    assert on_link_addresses(ROUTE_HEADER, _route6("", "::", 0, "fd00:cafe::1")) == [
        "fd00:cafe::1"
    ]
    assert on_link_addresses(ROUTE_HEADER, _route6("", "fd00:cafe::", 64, "::", REJECT_FLAGS)) == []
    assert has_default_route(INTERNAL_ROUTE_TABLE, _route6("", "::", 0, "fd00:cafe::1")) is True
    assert has_default_route(INTERNAL_ROUTE_TABLE, _route6("", "::", 0, "::", REJECT_FLAGS)) is False


def test_a_link_local_gateway_the_table_names_no_device_for_is_kept_bare() -> None:
    """A shape the kernel does not print -- every next hop it renders has a device -- and the
    reading is deliberate all the same: the candidate is kept without a scope, `create_connection`
    cannot dial it, and the half reports it as unverified. Dropping it would be this half asking
    one question fewer than it printed a line for, which is the defect it exists to catch.
    """
    assert on_link_addresses(ROUTE_HEADER, _route6("", "::", 0, "fe80::1")) == ["fe80::1"]
    unverified = check_on_link(
        ["fe80::1"], ports=[443], probe=lambda *_a, **_k: ON_LINK_UNVERIFIED, timeout_s=1
    )
    assert [ok for ok, _ in unverified] == [False]
    assert "could not be dialled" in unverified[0][1]


def test_neither_parser_reads_the_other_family_s_table() -> None:
    """Two files are read by path, so which one a parser was handed is worth pinning: a v4 table
    parsed as a v6 one derives hex nonsense, and `check` would dial it."""
    assert on_link_addresses(INTERNAL_IPV6_ROUTE_TABLE) == []
    assert on_link_addresses(ROUTE_HEADER, INTERNAL_ROUTE_TABLE) == []


def test_the_ipv6_table_is_optional_and_not_read_is_not_empty() -> None:
    """`None` is "not read" and derives nothing, which is what a caller with one table gets."""
    assert on_link_addresses(INTERNAL_ROUTE_TABLE, None) == ["172.30.0.1"]
    assert on_link_addresses(INTERNAL_ROUTE_TABLE) == ["172.30.0.1"]


def test_read_ipv6_route_table_tells_a_kernel_without_ipv6_from_one_that_would_not_read(
    tmp_path,
) -> None:
    """Two answers that must not be one value. No file is no IPv6 stack: nothing is on-link over
    a family the netns does not have, so an empty table is honest and the half rests on the
    other family. A file that is there and will not be read leaves the family unknown, and
    `_on_link_results` reports that as unverified rather than passing over it.
    """
    assert read_ipv6_route_table(str(tmp_path / "nonexistent")) == ""
    # There, and not readable as a file: the same OSError any other unreadable path gives.
    assert read_ipv6_route_table(str(tmp_path)) is None


def test_has_default_route_reads_both_families() -> None:
    """A container whose only default route is an IPv6 one has a way off its own subnets, and
    that is what the direct half's fallback asks (#42). The `unreachable default` on `lo` that
    every IPv6-enabled netns holds must not read as one, or the fallback would call every
    correct deployment unverified."""
    assert has_default_route(INTERNAL_ROUTE_TABLE, INTERNAL_IPV6_ROUTE_TABLE) is False
    assert has_default_route(INTERNAL_ROUTE_TABLE, ROUTED_IPV6_ROUTE_TABLE) is True
    assert has_default_route(INTERNAL_ROUTE_TABLE, None) is False
    # A reject route on a real device is not a way out either.
    assert has_default_route(INTERNAL_ROUTE_TABLE, _route6("eth0", "::", 0, "::", REJECT_FLAGS)) \
        is False


def _ipv6_loopback() -> socket.socket:
    """A listening socket on `::1`, or a skip where this machine has no IPv6 loopback.

    `socket.has_ipv6` is a compile-time flag, so it is not the question: a runner can have the
    module and no address to bind.
    """
    if not socket.has_ipv6:  # pragma: no cover -- depends on the interpreter build
        pytest.skip("this interpreter was built without IPv6")
    listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        listener.bind(("::1", 0))
    except OSError as exc:  # pragma: no cover -- depends on the machine
        listener.close()
        pytest.skip(f"no IPv6 loopback address here: {exc.errno}")
    return listener


def test_probe_on_link_dials_an_ipv6_literal_as_ipv6() -> None:
    """The probe needs no family told to it: a literal from the routing table is dialled in the
    family it is written in, and an accept and a refusal are the same two findings as over IPv4.

    Hermetic like the IPv4 case: the only address dialled is a loopback socket this test made.
    """
    with _ipv6_loopback() as listener:
        listener.listen(1)
        port = listener.getsockname()[1]
        assert probe_on_link("::1", port, timeout_s=2) == ON_LINK_ACCEPTED
    # The listener is closed, so the same port is now a refusal from a live host.
    assert probe_on_link("::1", port, timeout_s=2) == ON_LINK_REFUSED


def test_the_on_link_half_reports_an_ipv6_address_as_an_address_and_a_port() -> None:
    """`fd00:cafe::1:443` names neither, and this line is one an operator pastes back."""
    accepted = check_on_link(
        ["fd00:cafe::1"], ports=[443], probe=lambda *_a, **_k: ON_LINK_ACCEPTED, timeout_s=1
    )
    assert [ok for ok, _ in accepted] == [False]
    assert "[fd00:cafe::1]:443 accepted a direct connection" in accepted[0][1]
    refused = check_on_link(
        ["fd00:cafe::1"], ports=[443], probe=lambda *_a, **_k: ON_LINK_REFUSED, timeout_s=1
    )
    assert [ok for ok, _ in refused] == [False]
    assert "fd00:cafe::1 refused a direct connection on port 443" in refused[0][1]
    silent = check_on_link(
        ["fd00:cafe::1"], ports=[443], probe=lambda *_a, **_k: ON_LINK_NO_ANSWER, timeout_s=1
    )
    assert [ok for ok, _ in silent] == [True]


def test_an_ipv6_table_that_will_not_read_is_unverified_beside_what_was_still_dialled(
    monkeypatch,
) -> None:
    """A file that is there and will not be read leaves one family unknown, and the rule here is
    that a probe not made establishes nothing -- so it fails, by name, rather than being passed
    over. The IPv4 candidates are still worth dialling, so the half does both: one failure for
    the family it could not read, and the probes for the family it could.
    """
    dialled: list[str] = []

    def probe(addr: str, _port: int, **_kwargs: object) -> str:
        dialled.append(addr)
        return ON_LINK_NO_ANSWER

    _pin_tables(monkeypatch, INTERNAL_ROUTE_TABLE, None)
    results = egress._on_link_results(None, ports=[443], probe=probe, timeout_s=1)
    assert dialled == ["172.30.0.1"], results
    unverified = [(ok, line) for ok, line in results if IPV6_ROUTE_TABLE_PATH in line]
    assert [ok for ok, _ in unverified] == [False]
    assert "unverified for that family" in unverified[0][1]
    # The IPv4 candidate still got its own line, which is the half not giving up on what it could
    # establish: a family that could not be read is one failure, not a silent whole.
    assert any(line.startswith("172.30.0.1 answered nothing") for _ok, line in results)


def test_a_kernel_without_ipv6_is_not_an_unreadable_table(monkeypatch) -> None:
    """Nothing is on-link over a family the netns does not have, so the half rests on the other
    one and says nothing about IPv6. Reporting unverified there would fail every container the
    stack actually ships on."""
    _pin_tables(monkeypatch, INTERNAL_ROUTE_TABLE, "")
    results = egress._on_link_results(
        None, ports=[443], probe=lambda *_a, **_k: ON_LINK_NO_ANSWER, timeout_s=1
    )
    assert all(ok for ok, _ in results), results
    assert not [line for _ok, line in results if IPV6_ROUTE_TABLE_PATH in line]


def test_resolved_addresses_asks_for_both_families() -> None:
    """A peer's address is matched as a string, so a family left out of the lookup is a peer
    *dialled*: the proxy's own IPv6 address would fail a correct dual-stack deployment (#42).

    The resolver is stubbed rather than given a name to look up: which families this machine's
    `localhost` answers with is the machine's business, and the assertion is about what this
    function asks for.
    """
    asked: list[object] = []

    def getaddrinfo(host: str, port: object, family: int, kind: int, flags: int = 0):
        asked.append((host, family, kind))
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.30.0.1", 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00:cafe::1", 0, 0, 0)),
        ]

    with mock.patch.object(socket, "getaddrinfo", getaddrinfo):
        assert egress.resolved_addresses("egress") == frozenset(
            {"172.30.0.1", "fd00:cafe::1"}
        )
    assert asked == [("egress", socket.AF_UNSPEC, socket.SOCK_STREAM)]


def test_read_route_table_says_when_there_is_none() -> None:
    assert read_route_table("/nonexistent/proc/net/route") is None


def test_own_addresses_is_best_effort_and_holds_addresses(monkeypatch) -> None:
    assert all(isinstance(addr, str) for addr in own_addresses())
    monkeypatch.setattr(socket, "gethostname", lambda: "no-such-host.invalid")
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(socket.gaierror("no"))
    )
    assert own_addresses() == frozenset()


def test_probe_on_link_tells_an_accept_from_a_refusal_from_silence() -> None:
    """A refusal is the finding that matters: an RST comes from a live host, so the address is
    reachable even though nothing was listening on the port asked."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert probe_on_link("127.0.0.1", port, timeout_s=2) == ON_LINK_ACCEPTED
    assert probe_on_link("127.0.0.1", _dead_port(), timeout_s=2) == ON_LINK_REFUSED
    # "Nothing came back" is asserted through a mocked ENETUNREACH rather than by dialling a
    # link-local address for real: what a machine does with 169.254.0.1 is its own business,
    # and a runner that routes it would fail this for the wrong reason.
    with mock.patch.object(
        egress.socket, "create_connection", side_effect=as_oserror(errno.ENETUNREACH)
    ):
        assert probe_on_link("169.254.0.1", 443, timeout_s=0.2) == ON_LINK_NO_ANSWER


def test_the_on_link_half_passes_only_when_nothing_answers() -> None:
    def silent(*_args: object, **_kwargs: object) -> str:
        return ON_LINK_NO_ANSWER

    results = check_on_link(["172.30.0.1"], probe=silent, timeout_s=1)
    assert [ok for ok, _ in results] == [True]
    assert "answered nothing on" in results[0][1]


def test_a_probe_that_never_left_the_container_is_not_reported_as_silence() -> None:
    """The rule this whole check rests on, applied to the socket call itself.

    `ON_LINK_NO_ANSWER` is evidence: the probe went out and nothing came back. A local failure
    -- `EPERM` from a reject rule, `EMFILE` from running out of descriptors -- sent no packet,
    so it establishes nothing about the address, and reporting it as silence would print
    `[ OK ] ... answered nothing` for an address that was never dialled. That is the defect
    this file exists to prevent, one layer further down.
    """
    for failure, expected in (
        (as_oserror(errno.ETIMEDOUT), ON_LINK_NO_ANSWER),
        (as_oserror(errno.ENETUNREACH), ON_LINK_NO_ANSWER),
        (as_oserror(errno.EHOSTUNREACH), ON_LINK_NO_ANSWER),
        (as_oserror(errno.EHOSTDOWN), ON_LINK_NO_ANSWER),
        (TimeoutError(), ON_LINK_NO_ANSWER),
        (as_oserror(errno.EPERM), ON_LINK_UNVERIFIED),
        (as_oserror(errno.EMFILE), ON_LINK_UNVERIFIED),
        (as_oserror(errno.EAFNOSUPPORT), ON_LINK_UNVERIFIED),
        # This container's interface, not the address: never dialled, so never "no answer".
        (as_oserror(errno.ENETDOWN), ON_LINK_UNVERIFIED),
        (socket.gaierror(-2, "Name or service not known"), ON_LINK_UNVERIFIED),
        (ConnectionResetError(errno.ECONNRESET, "reset"), ON_LINK_REFUSED),
    ):
        with mock.patch.object(egress.socket, "create_connection", side_effect=failure):
            assert probe_on_link("172.30.0.1", 443, timeout_s=0.1) == expected, failure


def test_the_on_link_half_fails_an_address_it_could_not_dial() -> None:
    """A probe that did not happen fails, and says so rather than borrowing another verdict."""
    results = check_on_link(
        ["172.30.0.1"], probe=lambda *_a, **_k: ON_LINK_UNVERIFIED, timeout_s=1
    )
    assert [ok for ok, _ in results] == [False]
    assert "could not be dialled" in results[0][1]
    assert "unverified" in results[0][1]


def test_an_address_that_answered_outranks_one_port_that_could_not_be_dialled() -> None:
    """A refusal establishes the address is live; "not probed" establishes nothing. The line an
    operator reads must be the one that found something."""
    answers = {443: ON_LINK_UNVERIFIED, 80: ON_LINK_REFUSED, 22: ON_LINK_UNVERIFIED}
    results = check_on_link(
        ["172.30.0.1"], probe=lambda addr, port, **k: answers[port], timeout_s=1
    )
    assert [ok for ok, _ in results] == [False]
    assert "refused a direct connection on port 80" in results[0][1]


def test_the_on_link_half_names_an_accept_over_a_refusal_on_another_port() -> None:
    """Every port is tried, so a closed 443 does not hide a service on 22 -- both fail, but the
    operator is told which one to go and look at."""
    answers = {443: ON_LINK_REFUSED, 80: ON_LINK_NO_ANSWER, 22: ON_LINK_ACCEPTED}
    results = check_on_link(
        ["172.30.0.1"], probe=lambda addr, port, **k: answers[port], timeout_s=1
    )
    assert [ok for ok, _ in results] == [False]
    assert "172.30.0.1:22 accepted" in results[0][1]


def test_the_on_link_half_probes_no_more_than_its_cap() -> None:
    """A check, not a scan of whatever a surprising routing table held."""
    asked: list[str] = []

    def record(addr: str, port: int, **_kwargs: object) -> str:
        asked.append(addr)
        return ON_LINK_NO_ANSWER

    addresses = [f"10.0.0.{n}" for n in range(1, MAX_ONLINK_PROBES + 1)]
    results = check_on_link(addresses, probe=record, timeout_s=1)
    assert [ok for ok, _ in results] == [True] * MAX_ONLINK_PROBES
    assert set(asked) == set(addresses)
    assert len(asked) == MAX_ONLINK_PROBES * len(PROBE_ONLINK_PORTS)


def test_the_on_link_half_fails_rather_than_passing_over_what_the_cap_left() -> None:
    """The cap bounds the dialling; it may not bound what the check claims. A probe not made
    establishes nothing, so the addresses past the cap are named and the verdict is a failure."""
    asked: list[str] = []

    def record(addr: str, port: int, **_kwargs: object) -> str:
        asked.append(addr)
        return ON_LINK_NO_ANSWER

    addresses = [f"10.0.0.{n}" for n in range(1, MAX_ONLINK_PROBES + 3)]
    results = check_on_link(addresses, probe=record, timeout_s=1)
    failures = [line for ok, line in results if not ok]
    assert len(failures) == 1
    assert "unverified" in failures[0]
    for addr in addresses[MAX_ONLINK_PROBES:]:
        assert addr in failures[0]
        assert addr not in asked


def test_the_proxy_variables_are_both_cases_of_all_three() -> None:
    assert set(PROXY_ENV_NAMES) == {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }


def test_configured_proxy_prefers_the_lower_case_spelling() -> None:
    assert configured_proxy({}) is None
    assert configured_proxy({"HTTPS_PROXY": "  "}) is None
    assert configured_proxy({"HTTPS_PROXY": "http://a:3128"}) == "http://a:3128"
    assert (
        configured_proxy({"HTTPS_PROXY": "http://a:3128", "https_proxy": "http://b:3128"})
        == "http://b:3128"
    )


def test_upstream_targets_are_the_hosts_the_clients_are_configured_for() -> None:
    assert upstream_targets({}) == [("claude.ai", 443), ("chatgpt.com", 443)]
    assert upstream_targets(
        {"CLAUDE_AI_HOST": "https://usage.example.test:8443", "CHATGPT_HOST": "https://c.test/"}
    ) == [("usage.example.test", 8443), ("c.test", 443)]


# The upstream base URLs a *value* can take that urllib will not read a target out of. Each one
# used to come out of `python -m app.egress check` as a traceback (#48): `urlsplit` refuses the
# first outright, and `SplitResult.port` parses lazily so the second raises only once something
# asks -- which `upstream_targets` did, and `check` called it.
UNREADABLE_UPSTREAMS = pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://[::1", id="unclosed-bracket"),
        pytest.param("https://claude.ai:notanumber", id="port-not-a-number"),
        pytest.param("https://claude.ai:-1", id="negative-port"),
        pytest.param("https://claude.ai:99999", id="port-out-of-range"),
        # Syntactically a number, and `urlsplit` returns it as one -- but nothing dials port 0,
        # and `or default` used to read it as 443 and report the upstream as admitted.
        pytest.param("https://claude.ai:0", id="port-zero"),
        pytest.param("https:///no-host", id="no-host"),
    ],
)

# Both variables, every time. `tests/test_payload_contract.py`'s rule is that a case naming a
# shape one provider can have runs for the other too, and these two are the same variable in
# two spellings: a guard added for one and not the other is the drift that rule exists to stop.
UPSTREAM_VARIABLES = pytest.mark.parametrize("variable", ["CLAUDE_AI_HOST", "CHATGPT_HOST"])


@UNREADABLE_UPSTREAMS
def test_an_upstream_url_urllib_will_not_read_yields_no_target_rather_than_raising(
    url: str,
) -> None:
    assert split_upstream_url(url) is None


@UNREADABLE_UPSTREAMS
@UPSTREAM_VARIABLES
def test_upstream_targets_reports_an_unreadable_variable_as_no_target(
    variable: str, url: str
) -> None:
    """`None` in the slot rather than a raise, and in step with `upstream_urls`, so the caller
    still knows which variable the gap belongs to."""
    slot = ["CLAUDE_AI_HOST", "CHATGPT_HOST"].index(variable)
    targets = upstream_targets({variable: url})
    assert len(targets) == 2
    assert targets[slot] is None
    # The other variable keeps its own default target: one bad value costs its own line and no
    # more, rather than emptying the half.
    assert targets[1 - slot] == [("claude.ai", 443), ("chatgpt.com", 443)][1 - slot]


class _CheckRig:
    """A proxy, an allowed upstream and the environment the dashboard container would have."""

    def __init__(self, entries: list[str] | None = None) -> None:
        self.entries = entries

    async def __aenter__(self) -> _CheckRig:
        self.upstream = _Upstream()
        await self.upstream.start()
        entries = self.entries or [f"localhost:{self.upstream.port}"]
        self.server, self.url = await _proxy(entries)
        base = f"https://localhost:{self.upstream.port}"
        self.environ = {"HTTPS_PROXY": self.url, "CLAUDE_AI_HOST": base, "CHATGPT_HOST": base}
        return self

    async def __aexit__(self, *_: object) -> None:
        self.server.close()
        await self.upstream.stop()

    async def run(
        self,
        environ: dict[str, str] | None = None,
        *,
        direct: int | None = None,
        on_link: list[str] | None = None,
        on_link_ports: list[int] | None = None,
        on_link_probe=None,
        direct_probe=None,
    ):
        direct_target = ("example.com", 443) if direct is None else ("127.0.0.1", direct)
        # An on-link address that answers nothing, stubbed rather than dialled: the real
        # gateway of whatever network the suite is running on is not this test's business, and
        # the case has to be reproducible on a developer's machine and on a runner alike.
        def silent(*_args: object, **_kwargs: object) -> str:
            return ON_LINK_NO_ANSWER

        probe = on_link_probe or silent

        # The direct half is stubbed for the same reason: what a real connect to a supposedly
        # unroutable address does is the machine's business, not this test's, and a runner that
        # routes it would fail the suite for the wrong reason. `probe_direct` has its own
        # real-socket tests. A caller asking for a live port wants the answering branch.
        def unrouted(*_args: object, **_kwargs: object) -> str:
            return DIRECT_NO_ROUTE

        return await asyncio.to_thread(
            functools.partial(
                check,
                self.environ if environ is None else environ,
                direct=direct_target,
                direct_probe=direct_probe
                or (probe_direct if direct is not None else unrouted),
                on_link=["172.30.0.1"] if on_link is None else on_link,
                on_link_ports=on_link_ports or [443],
                on_link_probe=probe,
                timeout_s=2,
            )
        )


@asynctest
async def test_the_check_passes_a_confined_container_behind_the_proxy() -> None:
    async with _CheckRig() as rig:
        results = await rig.run()
    assert all(ok for ok, _ in results), results
    # The proxy's own filtering, one line per configured upstream, the on-link address and the
    # public name: both directions off a container, neither standing in for the other.
    assert len(results) == 5


@asynctest
async def test_the_check_fails_without_a_proxy() -> None:
    async with _CheckRig() as rig:
        results = await rig.run({})
    assert [ok for ok, _ in results] == [False]
    assert "no proxy" in results[0][1]


@asynctest
async def test_the_check_fails_a_host_the_allow_list_does_not_admit() -> None:
    async with _CheckRig(entries=["somewhere-else.test"]) as rig:
        results = await rig.run()
    failures = [line for ok, line in results if not ok]
    assert failures and all(ALLOW_ENV in line for line in failures)


@asynctest
async def test_the_check_fails_a_route_round_the_proxy() -> None:
    async with _CheckRig() as rig:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            results = await rig.run(direct=listener.getsockname()[1])
    assert results[-1][0] is False
    assert "route round the proxy" in results[-1][1]


@asynctest
async def test_the_check_fails_an_on_link_address_that_answers(monkeypatch) -> None:
    """The container this check runs in has just reached something that is not the proxy, with
    no route involved. Before #37 the same container passed, because the only thing asked was
    whether a public name routed -- which `internal: true` answers on its own.

    The address dialled here has to be one a test can make answer, so it is loopback -- which is
    also where this rig's proxy listens, and an address `peer_addresses` would account for. It
    is emptied so that the address under test stands for the bridge's gateway and not for
    `egress`.
    """
    monkeypatch.setattr(egress, "peer_addresses", lambda *a, **k: {})
    async with _CheckRig() as rig:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            results = await rig.run(
                on_link=["127.0.0.1"],
                on_link_ports=[listener.getsockname()[1]],
                on_link_probe=probe_on_link,
            )
    failures = [line for ok, line in results if not ok]
    assert len(failures) == 1
    assert "accepted a direct connection" in failures[0]
    # The public-name half still passed, which is exactly how the gap went unseen.
    assert results[-1][0] is True


@asynctest
async def test_the_check_fails_an_on_link_address_that_refuses(monkeypatch) -> None:
    """A refusal is a live host: the address is reachable, and what it happens to be listening
    on is not a bound anybody chose. Loopback stands for the gateway here, as above."""
    monkeypatch.setattr(egress, "peer_addresses", lambda *a, **k: {})
    async with _CheckRig() as rig:
        results = await rig.run(
            on_link=["127.0.0.1"], on_link_ports=[_dead_port()], on_link_probe=probe_on_link
        )
    failures = [line for ok, line in results if not ok]
    assert len(failures) == 1
    assert "refused a direct connection" in failures[0]


@asynctest
async def test_the_check_passes_when_the_subnets_first_address_is_this_containers_own(
    monkeypatch,
) -> None:
    """Where no gateway holds the first address of the subnet, the engine is free to give it to
    a container -- this one. There is then nothing on-link to dial, which is the bound holding,
    not the check failing to look: a container reaching itself proves nothing either way. The
    routing table and this container's addresses are both injected, so the case is the same
    everywhere the suite runs.
    """

    def unexpected(*args: object, **kwargs: object) -> str:
        raise AssertionError(f"dialled its own address: {args} {kwargs}")

    _pin_tables(monkeypatch, INTERNAL_ROUTE_TABLE)
    monkeypatch.setattr(egress, "own_addresses", lambda: frozenset({"172.30.0.1"}))
    async with _CheckRig() as rig:
        results = await asyncio.to_thread(
            functools.partial(
                check,
                rig.environ,
                direct=("example.com", 443),
                direct_probe=lambda *_a, **_k: DIRECT_NO_ROUTE,
                on_link=None,
                on_link_probe=unexpected,
                timeout_s=2,
            )
        )
    assert all(ok for ok, _ in results), results
    assert any(
        line.endswith(f"is on-link by design: {egress.PEER_SELF}") for _ok, line in results
    )


@asynctest
async def test_the_check_accounts_for_the_proxys_address_rather_than_dialling_it(
    monkeypatch,
) -> None:
    """The shape a working deployment actually has on Docker Engine 28.0 or newer.

    `gateway_mode_ipv4: isolated` makes the engine skip allocating a gateway address
    altogether, so the subnet's first address -- the one the host's end of the bridge would
    have held -- is free, and the first container attached takes it. The compose file's
    dependency chain (egress, then codervis, then ingress) makes that `egress`. Dialling it and
    failing on the RST would report the bound broken in precisely the deployment where it is
    whole, so the address is accounted for as the proxy and the line says so. That the proxy is
    there *is* the evidence the option took effect: an engine that ignored it holds the address
    on the bridge instead, the name resolves elsewhere, and the address is dialled.
    """

    def unexpected(*args: object, **kwargs: object) -> str:
        raise AssertionError(f"dialled the proxy's own address: {args} {kwargs}")

    _pin_tables(monkeypatch, INTERNAL_ROUTE_TABLE)
    monkeypatch.setattr(egress, "own_addresses", lambda: frozenset({"172.30.0.2"}))
    monkeypatch.setattr(egress, "resolved_addresses", lambda _host: frozenset({"172.30.0.1"}))
    async with _CheckRig() as rig:
        results = await asyncio.to_thread(
            functools.partial(
                check,
                rig.environ,
                direct=("example.com", 443),
                direct_probe=lambda *_a, **_k: DIRECT_NO_ROUTE,
                on_link=None,
                on_link_probe=unexpected,
                timeout_s=2,
            )
        )
    assert all(ok for ok, _ in results), results
    assert (
        f"172.30.0.1 is on-link by design: {egress.PEER_PROXY.format(proxy=rig.url)}"
        in [line for _ok, line in results]
    )


@asynctest
async def test_the_check_dials_the_gateway_an_engine_that_ignored_the_option_left(
    monkeypatch,
) -> None:
    """The other side of the same case, and the one the issue is about: where the proxy's name
    resolves somewhere else, the subnet's first address is nobody's peer -- it is the host's end
    of the bridge -- so it is dialled, and answering fails the check."""
    _pin_tables(monkeypatch, INTERNAL_ROUTE_TABLE)
    monkeypatch.setattr(egress, "own_addresses", lambda: frozenset({"172.30.0.2"}))
    monkeypatch.setattr(egress, "resolved_addresses", lambda _host: frozenset({"172.30.0.3"}))
    async with _CheckRig() as rig:
        results = await asyncio.to_thread(
            functools.partial(
                check,
                rig.environ,
                direct=("example.com", 443),
                direct_probe=lambda *_a, **_k: DIRECT_NO_ROUTE,
                on_link=None,
                on_link_probe=lambda *_a, **_k: ON_LINK_ACCEPTED,
                timeout_s=2,
            )
        )
    failures = [line for ok, line in results if not ok]
    assert len(failures) == 1
    assert "172.30.0.1:443 accepted a direct connection" in failures[0]


@asynctest
@pytest.mark.parametrize(
    "outcome, passes",
    [(ON_LINK_ACCEPTED, False), (ON_LINK_REFUSED, False), (ON_LINK_NO_ANSWER, True)],
)
async def test_the_check_dials_the_ipv6_gateway_a_dual_stack_network_would_have(
    monkeypatch, outcome: str, passes: bool
) -> None:
    """The issue's first criterion, end to end through `check` (#42): an IPv6 address this
    container can reach on-link fails the check on the same terms as an IPv4 one -- an accept and
    a refusal both fail, silence passes.

    The shape is a dual-stack `inside` network on an engine that honoured `gateway_mode_ipv4` and
    not the IPv6 one, which is what an operator adding `enable_ipv6` to that network without the
    second option would have: the IPv4 candidate is this container's own address, so it is
    accounted for, and `fd00:cafe::1` -- the host's end of the bridge over IPv6 -- is dialled.
    Both tables are injected, so nothing here depends on the machine the suite runs on, and the
    probe is a stub, so no packet goes to an address this test did not name.
    """
    dialled: list[tuple[str, int]] = []

    def probe(addr: str, port: int, **_kwargs: object) -> str:
        dialled.append((addr, port))
        return outcome

    _pin_tables(monkeypatch, INTERNAL_ROUTE_TABLE, INTERNAL_IPV6_ROUTE_TABLE)
    monkeypatch.setattr(egress, "own_addresses", lambda: frozenset({"172.30.0.1"}))
    # The proxy's name resolves nowhere here, so the only address accounted for is this
    # container's: the IPv6 candidate stands for the bridge's gateway and not for `egress`.
    monkeypatch.setattr(egress, "resolved_addresses", lambda _host: frozenset())
    async with _CheckRig() as rig:
        results = await asyncio.to_thread(
            functools.partial(
                check,
                rig.environ,
                direct=("example.com", 443),
                direct_probe=lambda *_a, **_k: DIRECT_NO_ROUTE,
                on_link=None,
                on_link_ports=[443],
                on_link_probe=probe,
                timeout_s=2,
            )
        )
    assert dialled == [("fd00:cafe::1", 443)], results
    line = [(ok, text) for ok, text in results if "fd00:cafe::1" in text]
    assert len(line) == 1, results
    assert line[0][0] is passes, line
    assert all(ok for ok, _ in results) is passes, results
    if outcome == ON_LINK_ACCEPTED:
        # The address and the port as an operator would paste them back.
        assert "[fd00:cafe::1]:443 accepted a direct connection" in line[0][1]
    elif outcome == ON_LINK_REFUSED:
        assert "refused a direct connection on port 443" in line[0][1]


@asynctest
async def test_the_check_accounts_for_the_proxys_ipv6_address_rather_than_dialling_it(
    monkeypatch,
) -> None:
    """The dual-stack shape of a deployment that is whole, which must not fail on its own peers.

    Both families are accounted for the way one was: this container's addresses and the proxy's,
    matched as strings against what the tables yielded. A resolver asked for IPv4 alone would
    leave the proxy's IPv6 address unlabelled, and the check would dial `egress` and fail on the
    RST -- reporting the bound broken in the deployment where it holds.
    """

    def unexpected(*args: object, **kwargs: object) -> str:
        raise AssertionError(f"dialled a peer: {args} {kwargs}")

    _pin_tables(monkeypatch, INTERNAL_ROUTE_TABLE, INTERNAL_IPV6_ROUTE_TABLE)
    monkeypatch.setattr(
        egress, "own_addresses", lambda: frozenset({"172.30.0.2", "fd00:cafe::2"})
    )
    monkeypatch.setattr(
        egress, "resolved_addresses", lambda _host: frozenset({"172.30.0.1", "fd00:cafe::1"})
    )
    async with _CheckRig() as rig:
        results = await asyncio.to_thread(
            functools.partial(
                check,
                rig.environ,
                direct=("example.com", 443),
                direct_probe=lambda *_a, **_k: DIRECT_NO_ROUTE,
                on_link=None,
                on_link_ports=[443],
                on_link_probe=unexpected,
                timeout_s=2,
            )
        )
    assert all(ok for ok, _ in results), results
    printed = [line for _ok, line in results]
    for addr in ("172.30.0.1", "fd00:cafe::1"):
        assert f"{addr} is on-link by design: {egress.PEER_PROXY.format(proxy=rig.url)}" in printed


@asynctest
async def test_a_name_that_will_not_resolve_is_unverified_when_the_ipv6_table_will_not_read(
    monkeypatch,
) -> None:
    """The direct half's fallback reads both tables, so both can be the one it could not read --
    and the line names which file an operator should go and look at. A table that would not be
    read settles nothing about whether this container has a route off its own subnets, whichever
    family it held.
    """
    _pin_tables(monkeypatch, INTERNAL_ROUTE_TABLE, None)
    async with _CheckRig() as rig:
        results = await rig.run(direct_probe=lambda *_a, **_k: DIRECT_NO_DNS)
    line = [(ok, text) for ok, text in results if "example.com" in text]
    assert len(line) == 1, results
    assert line[0][0] is False
    assert f"could not be looked up and {IPV6_ROUTE_TABLE_PATH} could not be read" in line[0][1]
    assert "unverified" in line[0][1]


@asynctest
async def test_an_ipv6_default_route_is_a_way_off_this_container_too(monkeypatch) -> None:
    """The same fallback's other half: a container whose only default route is an IPv6 one has a
    way off its subnets, so a public name that will not resolve establishes nothing (#42). Read
    over IPv4 alone this passed, which is the pass this guards."""
    _pin_tables(monkeypatch, INTERNAL_ROUTE_TABLE, ROUTED_IPV6_ROUTE_TABLE)
    async with _CheckRig() as rig:
        results = await rig.run(direct_probe=lambda *_a, **_k: DIRECT_NO_DNS)
    line = [(ok, text) for ok, text in results if "example.com" in text]
    assert len(line) == 1, results
    assert line[0][0] is False
    assert "this container has a default route" in line[0][1]


@pytest.mark.parametrize("proxy", ["http://egress:3128", "egress:3128", "https://egress:3128"])
def test_the_peers_are_this_container_and_the_proxy(monkeypatch, proxy: str) -> None:
    """Two peers and no more: reaching either is the bound working, and anything else on-link
    is dialled. A proxy name that will not resolve contributes nothing, so its address is
    probed rather than assumed -- which fails the check rather than passing it.

    Both spellings of the variable, because urllib accepts both and the scheme-less one used to
    resolve nothing here while still naming the proxy to dial -- which failed a whole
    deployment on the proxy's own address.
    """
    resolved: list[str] = []

    def resolve(host: str) -> frozenset[str]:
        resolved.append(host)
        return frozenset({"172.30.0.1"})

    monkeypatch.setattr(egress, "own_addresses", lambda: frozenset({"172.30.0.2"}))
    monkeypatch.setattr(egress, "resolved_addresses", resolve)
    assert egress.peer_addresses(proxy) == {
        "172.30.0.2": egress.PEER_SELF,
        "172.30.0.1": egress.PEER_PROXY.format(proxy=proxy),
    }
    assert resolved == ["egress"]

    monkeypatch.setattr(egress, "resolved_addresses", lambda _host: frozenset())
    assert egress.peer_addresses(proxy) == {"172.30.0.2": egress.PEER_SELF}


@pytest.mark.parametrize("proxy", ["[::1", "://"])
def test_a_proxy_variable_that_will_not_parse_is_reported_rather_than_raised(
    proxy: str, monkeypatch
) -> None:
    """`[::1` makes urlsplit raise, which came out of `check` as a traceback once
    `peer_addresses` began parsing the variable too. Both readers are driven, because a raise
    from either is the defect. Nothing here opens a socket or resolves a name that is not this
    host's own: neither value names a proxy host to dial."""
    monkeypatch.setattr(egress, "own_addresses", lambda: frozenset({"172.30.0.2"}))
    assert egress.peer_addresses(proxy) == {"172.30.0.2": egress.PEER_SELF}
    answer = probe_proxy(proxy, "example.com", timeout_s=0.5)
    assert isinstance(answer, str) and "proxy URL" in answer


@asynctest
async def test_the_check_reports_a_proxy_variable_that_will_not_parse(monkeypatch) -> None:
    """End to end, which is where the traceback would have come out: every proxy line fails,
    and the on-link half dials the address it could not account for rather than passing it."""
    dialled: list[str] = []

    def record(addr: str, port: int, **_kwargs: object) -> str:
        dialled.append(addr)
        return ON_LINK_NO_ANSWER

    _pin_tables(monkeypatch, INTERNAL_ROUTE_TABLE)
    monkeypatch.setattr(egress, "own_addresses", lambda: frozenset({"172.30.0.2"}))
    async with _CheckRig() as rig:
        results = await asyncio.to_thread(
            functools.partial(
                check,
                {**rig.environ, "HTTPS_PROXY": "[::1"},
                direct=("example.com", 443),
                direct_probe=lambda *_a, **_k: DIRECT_NO_ROUTE,
                on_link=None,
                on_link_probe=record,
                timeout_s=2,
            )
        )
    failures = [line for ok, line in results if not ok]
    assert failures and all("is not a proxy URL" in line for line in failures)
    assert set(dialled) == {"172.30.0.1"}


# --- what a result line may quote back (#48) ------------------------------------------
#
# `check` is a command README tells operators to run and CI's `Egress bound` job runs into a
# public Actions log, and `http://user:secret@egress:3128` is a valid `HTTPS_PROXY`. So the
# variable is never printed as configured.

# Distinctive enough that a substring search for it cannot match anything the wording itself
# contributes, which is what makes "no line contains it" a real assertion.
PROXY_SECRET = "s3cr3t-egress-password"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # Every spelling urllib takes for a proxy variable, with userinfo and without.
        ("http://egress:3128", "http://egress:3128"),
        ("egress:3128", "egress:3128"),
        ("http://egress", "http://egress"),
        (f"http://user:{PROXY_SECRET}@egress:3128", "http://<userinfo redacted>@egress:3128"),
        # A token as the whole userinfo, with no colon in it: the username half is as much a
        # credential as the password half, so the cut is the whole of it rather than the tail.
        (f"http://{PROXY_SECRET}@egress:3128", "http://<userinfo redacted>@egress:3128"),
        (f"user:{PROXY_SECRET}@egress:3128", "<userinfo redacted>@egress:3128"),
        # A value urllib will not read is a value that still reaches a printed line, so the
        # redaction has to hold without the parser.
        (f"http://user:{PROXY_SECRET}@[::1", "http://<userinfo redacted>@[::1"),
        ("https://[::1", "https://[::1"),
        # A delimiter *inside* the credential. Where the authority ends is deliberately not
        # consulted, because a credential is exactly the part of a URL that may not be well
        # formed: stopping the authority at the first `/` closed the frame before the `@`,
        # found no `@` in it, and handed the whole value back. A base64 secret carries `/`
        # routinely, so this was the common case rather than the exotic one.
        (f"http://user:ab/{PROXY_SECRET}@egress:3128", "http://<userinfo redacted>@egress:3128"),
        (f"http://user:ab?{PROXY_SECRET}@egress:3128", "http://<userinfo redacted>@egress:3128"),
        (f"http://user:ab#{PROXY_SECRET}@egress:3128", "http://<userinfo redacted>@egress:3128"),
        # The cost of that rule, and the direction this function is allowed to be wrong in: an
        # `@` in a path takes the authority with it. No proxy or upstream base URL has such a
        # path, and losing a host from a line is not losing a credential.
        (f"http://user:{PROXY_SECRET}@egress:3128/a@b", "http://<userinfo redacted>@b"),
        ("http://egress:3128/a@b", "http://<userinfo redacted>@b"),
        # Leading whitespace: `urlsplit` strips it, so this is a value urllib reads and a live
        # credential. The scheme survives, rather than being lost to over-redaction.
        (f" http://user:{PROXY_SECRET}@egress:3128", " http://<userinfo redacted>@egress:3128"),
        # The tab survives as `\t`, not as a tab: it still reaches the line, and the line is
        # still one line (#88). The leading-space row above is the control for it -- a space is
        # printable, so it is printed.
        (f"\thttp://user:{PROXY_SECRET}@egress:3128", "\\thttp://<userinfo redacted>@egress:3128"),
        # Both normalisations at once, on a value that carries a credential *and* a forged
        # verdict: neither the secret nor the line break comes out.
        (
            f"http://user:{PROXY_SECRET}@egress:3128\n[ OK ] all good",
            "http://<userinfo redacted>@egress:3128\\n[ OK ] all good",
        ),
        # A value with no userinfo takes the escaping path that returns it otherwise as
        # configured, so that path escapes too.
        ("http://egress:3128\n[ OK ] all good", "http://egress:3128\\n[ OK ] all good"),
        # Where the authority begins, which is the whole of what this depends on being right.
        # Splitting on the first `//` would frame `y` as the authority here, find no `@` in it
        # and hand the value back whole -- the scheme-less spelling is the one that can have a
        # path in front of a `//` with no scheme to mark the authority off.
        (f"user:{PROXY_SECRET}@egress:3128/x//y", "<userinfo redacted>@egress:3128/x//y"),
        (f"user:{PROXY_SECRET}@egress:3128/x://y", "<userinfo redacted>@egress:3128/x://y"),
        (f"//user:{PROXY_SECRET}@egress:3128", "//<userinfo redacted>@egress:3128"),
        (f"HTTP://user:{PROXY_SECRET}@egress:3128", "HTTP://<userinfo redacted>@egress:3128"),
        # A reading that is wrong costs over-redaction and never under: no scheme is recognised
        # here, so the whole value is read as an authority and the scheme goes with the cut.
        (f"mailto:user:{PROXY_SECRET}@egress", "<userinfo redacted>@egress"),
        ("", ""),
        ("egress", "egress"),
    ],
)
def test_without_userinfo_drops_the_credential_and_keeps_the_host_and_port(
    url: str, expected: str
) -> None:
    """Host, port and scheme survive -- the operator has to see which proxy was dialled -- and
    so does the `@`, so an authenticating proxy still reads as one. A URL carrying no userinfo
    comes back exactly as written, which is why README's sample output is unchanged."""
    assert without_userinfo(url) == expected
    assert PROXY_SECRET not in without_userinfo(url)


def test_no_userinfo_survives_without_userinfo_whatever_surrounds_it() -> None:
    """The table above is the readable statement; this is the one that cannot be gamed.

    The credential is placed in userinfo position under every combination of the things that
    decide where the authority starts and ends -- a scheme or none, a leading `//`, a path, a
    query, a fragment, a second `@` further on. A framing bug shows up here as a value handed
    back whole, which is exactly how the first draft of `_authority_of` failed.
    """
    survived = []
    for scheme in ("http://", "https://", "", "//", "HTTP://", "socks5://", " http://", "\thttp://"):
        # The credential is varied too, and with the delimiters in it: a secret that is not
        # well formed is the case the first draft got wrong, and a table that only ever put a
        # tidy credential in userinfo position would not have seen it.
        for user in (
            f"user:{PROXY_SECRET}",
            PROXY_SECRET,
            f"{PROXY_SECRET}:",
            f"user:ab/{PROXY_SECRET}",
            f"user:ab?{PROXY_SECRET}",
            f"user:ab#{PROXY_SECRET}",
            f"user:{PROXY_SECRET}@x",
        ):
            for host in ("egress:3128", "egress", "[::1]:3128", "[::1"):
                for tail in ("", "/", "/x", "/x//y", "/x://y", "?q=1", "#f", "/a@b"):
                    url = f"{scheme}{user}@{host}{tail}"
                    if PROXY_SECRET in without_userinfo(url):
                        survived.append(url)
    assert not survived, survived


@pytest.mark.parametrize(
    "proxy",
    [
        f"http://user:{PROXY_SECRET}@egress:3128",
        f"http://{PROXY_SECRET}@egress:3128",
        f"user:{PROXY_SECRET}@egress:3128",
    ],
)
@pytest.mark.parametrize(
    ("verdict", "answer"),
    [
        # One per branch of the proxy half, because each writes its own line and each used to
        # write the variable into it: the refusal that passes, the tunnel that fails, and a
        # proxy that could not be reached at all.
        ("refused", lambda _p, host, *_a, **_k: (403, "Forbidden") if host == PROBE_DENIED_HOST else (200, "OK")),
        ("tunnelled", lambda *_a, **_k: (200, "OK")),
        ("unreachable", lambda *_a, **_k: "did not answer (TimeoutError)"),
    ],
)
def test_no_result_line_carries_the_proxy_variables_userinfo(
    monkeypatch, proxy: str, verdict: str, answer
) -> None:
    """The acceptance criterion, driven over every line the proxy half can produce.

    Stubbed rather than dialled: what is asserted is the wording, and the wording is the same
    whichever way the probes answered. The peer line is in it too -- `peer_addresses` formats
    the variable into `PEER_PROXY`, which is the fourth site and the one an operator is most
    likely to paste somewhere.
    """
    gateway = "172.30.0.1"
    monkeypatch.setattr(egress, "probe_proxy", answer)
    monkeypatch.setattr(egress, "own_addresses", frozenset)
    monkeypatch.setattr(egress, "resolved_addresses", lambda _host: frozenset({gateway}))

    results = check(
        {"HTTPS_PROXY": proxy},
        direct=("example.com", 443),
        direct_probe=lambda *_a, **_k: DIRECT_NO_ROUTE,
        on_link=[gateway],
        on_link_probe=lambda *_a, **_k: ON_LINK_NO_ANSWER,
        timeout_s=1,
    )
    printed = [egress.format_result(ok, line) for ok, line in results]
    assert printed, "the check produced no lines to assert about"
    assert not [line for line in printed if PROXY_SECRET in line], printed
    # Redacted, not dropped: the proxy half still names the host *and port* that were dialled,
    # or the operator cannot tell which proxy the verdict is about. Asserted as `egress:3128`
    # rather than `egress`, which `egress-probe.invalid` would satisfy on its own.
    assert [line for line in printed if "egress:3128" in line], printed
    assert [line for line in printed if egress.USERINFO_REDACTED in line], printed


@pytest.mark.parametrize(
    "proxy",
    [
        # Neither dials anything or resolves a name: `probe_proxy` returns on the value alone,
        # which is what lets this stay hermetic while driving the real function.
        f"http://user:{PROXY_SECRET}@[::1",
        f"ftp://user:{PROXY_SECRET}@egress:3128",
    ],
)
def test_probe_proxy_redacts_the_variable_it_quotes_back(proxy: str) -> None:
    """Its own two failure strings, which `check` interpolates straight into a printed line.

    They are a fourth and fifth leak site rather than the three in `check`'s own wording, and a
    fix that covered only `check` would have left them: `f"{shown}: {refused}"` prints whatever
    came back here verbatim.
    """
    answer = probe_proxy(proxy, "example.com", timeout_s=0.5)
    assert isinstance(answer, str)
    assert "proxy URL" in answer
    assert PROXY_SECRET not in answer


@asynctest
async def test_no_line_of_a_real_checks_output_carries_the_proxys_userinfo() -> None:
    """End to end against a proxy that really answers, which is where the criterion is claimed.

    The userinfo is inert here -- `egress` requires no authentication and urllib drops it
    before dialling -- so the run is the passing one an operator would have, with the one
    difference the issue is about.
    """
    async with _CheckRig() as rig:
        host_port = rig.url.removeprefix("http://")
        environ = {**rig.environ, "HTTPS_PROXY": f"http://user:{PROXY_SECRET}@{host_port}"}
        results = await rig.run(environ)
    printed = [egress.format_result(ok, line) for ok, line in results]
    assert all(ok for ok, _ in results), printed
    assert not [line for line in printed if PROXY_SECRET in line], printed
    assert [line for line in printed if egress.USERINFO_REDACTED in line], printed


@UPSTREAM_VARIABLES
@pytest.mark.parametrize(
    "url",
    [
        # Both branches that quote an upstream value back: the one that cannot be read, and the
        # one that is readable but not `https://`. README claims the same redaction for these
        # variables as for the proxy, and `check` calls `without_userinfo` on both lines.
        pytest.param(f"https://user:{PROXY_SECRET}@[::1", id="unreadable"),
        pytest.param(f"http://user:{PROXY_SECRET}@usage.example.test", id="not-https"),
        pytest.param(f"https://user:{PROXY_SECRET}@usage.example.test:notaport", id="bad-port"),
        # A stray space in `.env` is an ordinary slip, and `urlsplit` strips it -- so this is a
        # value the live client really uses, carrying a credential that really works.
        pytest.param(f" http://user:{PROXY_SECRET}@usage.example.test", id="leading-space"),
    ],
)
@asynctest
async def test_no_result_line_carries_an_upstream_variables_userinfo(
    variable: str, url: str
) -> None:
    """An upstream base URL can carry userinfo exactly as the proxy variable can, and the lines
    that quote it back are the lines this change adds and rewords."""
    async with _CheckRig() as rig:
        results = await rig.run({**rig.environ, variable: url})
    printed = [egress.format_result(ok, line) for ok, line in results]
    assert not [line for line in printed if PROXY_SECRET in line], printed
    # The variable is still named, and still redacted rather than dropped.
    assert [line for line in printed if line.startswith(f"[FAIL] {variable}=")], printed
    assert [line for line in printed if egress.USERINFO_REDACTED in line], printed


# A proxy variable that parses as the proxy an operator really configured *and* carries a
# forged result line. `urlsplit` removes `\t`, `\r` and `\n` from anywhere in a URL before
# reading it, so this is not a value the parser rejects on its way past -- which is the whole
# of why printing it verbatim was a forgery rather than a mess (#88).
FORGED_LINE = "[ OK ] the proxy refused everything"
# The three the issue names, driven together so one value covers all of them.
CONTROL_CHARACTERS = "\t\r\n"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # What a result line is allowed to carry, stated as itself: printable characters and
        # the space, unchanged. Not a list of the characters that bite -- that list is always
        # one short, and the one somebody adds next is the one that is missing from it.
        ("http://egress:3128", "http://egress:3128"),
        (
            "[ OK ] CLAUDE_AI_HOST: claude.ai:443 admitted",
            "[ OK ] CLAUDE_AI_HOST: claude.ai:443 admitted",
        ),
        ("a b  c", "a b  c"),
        # Non-ASCII that prints is printed: an operator who configured an IDN host has to be
        # able to recognise it in the line.
        ("http://egressó.example:3128", "http://egressó.example:3128"),
        # The three with names, which are the three an operator meets.
        ("a\tb", r"a\tb"),
        ("a\nb", r"a\nb"),
        ("a\rb", r"a\rb"),
        # And the ones without: a terminal escape, the C0 line tabulation `urlsplit` does not
        # drop, DEL, a C1 control, a non-breaking space pasted out of rendered documentation,
        # and a line separator that is a line break to plenty of software that is not a
        # terminal.
        ("a\x1b[2Kb", r"a\x1b[2Kb"),
        ("a\x0bb", r"a\x0bb"),
        ("a\x7fb", r"a\x7fb"),
        ("a\x85b", r"a\x85b"),
        ("a\xa0b", r"a\xa0b"),
        ("a\u2028b", r"a\u2028b"),
        # Right-to-left override, which reorders the rest of the line where it is printed.
        ("a\u202eb", r"a\u202eb"),
        # Above the BMP, where `\uNNNN` cannot say which code point it was: `\ue0041` would be
        # both this tag character and `chr(0xE004)` followed by a `1`, so an astral one gets
        # `\UNNNNNNNN` and the two spellings stay apart.
        ("a" + chr(0xE0041) + "b", r"a\U000e0041b"),
        # The pair it would have collided with: `chr(0xE004)` and then a literal `1`.
        ("a" + chr(0xE004) + "1b", r"a\ue0041b"),
        ("", ""),
    ],
)
def test_escape_controls_prints_what_is_printable_and_writes_out_what_is_not(
    text: str, expected: str
) -> None:
    """The permitted shape, written here rather than read back out of the module."""
    assert escape_controls(text) == expected


# Every code point through the end of the C1 block, and the ones above it that are worth
# naming: the two Unicode separators, an ideographic space, the right-to-left override, a
# non-character, a lone surrogate, and a tag character from the plane the invisible-instruction
# trick uses. The range is the sweep; the tail is what a reader would otherwise wonder about.
SWEPT_CHARACTERS = [chr(code) for code in range(0xA1)] + [
    "\u2028",
    "\u2029",
    "\u3000",
    "\u202e",
    "\ufffe",
    "\ud800",
    chr(0xE0041),
    chr(0x10FFFF),
]


def test_escape_controls_leaves_no_character_a_line_could_break_on() -> None:
    """The table above is the readable statement; this is the one that cannot be gamed.

    Each character in turn, in a URL: what comes back is printable throughout, so a printed
    line ends where `print` puts its newline and nowhere else. It is the same shape as the
    userinfo sweep -- a rule stated as a property over everything, beside a table stating it
    readably.
    """
    escaped = [(char, escape_controls(f"http://egress{char}:3128")) for char in SWEPT_CHARACTERS]
    assert not [(char, line) for char, line in escaped if not line.isprintable()]
    # Escaped, never dropped: the host an operator configured is still in every line.
    assert not [(char, line) for char, line in escaped if "http://egress" not in line]


def test_escape_controls_gives_each_character_a_spelling_of_its_own() -> None:
    """One code point, one escape -- which is what `\\uNNNN` for an astral one would break.

    `\\u` takes exactly four hex digits, so `chr(0xE0041)` written with it is `\\ue0041`, and so
    is `chr(0xE004)` followed by a `1`. Two values an operator could have configured would
    print the same, and a representation of a value may not do that. The sweep above says
    nothing about it, because both spellings are printable.
    """
    spellings = {char: escape_controls(char) for char in SWEPT_CHARACTERS}
    assert len(set(spellings.values())) == len(spellings)
    # And the collision itself, which is what the three widths exist for.
    assert escape_controls(chr(0xE0041)) != escape_controls(chr(0xE004)) + "1"


def test_escape_controls_is_idempotent_so_format_result_may_apply_it_to_an_escaped_url() -> None:
    """`format_result` escapes a whole line that already holds a `without_userinfo` URL.

    That is only safe because a second pass changes nothing -- which is why a backslash is not
    itself escaped. A rule that doubled it would double it twice here, and the line would no
    longer be the value the operator configured.
    """
    for text in ("a\nb", r"a\nb", "http://egress:3128", "a\\b\tc", "a\x1b\u2028b"):
        assert escape_controls(escape_controls(text)) == escape_controls(text)


def test_format_result_is_one_line_whatever_the_line_it_is_given_holds() -> None:
    """The structural half: the count of printed lines is the count of assertions `check` made.

    `without_userinfo` covers the values that are URLs. This covers the line, so a future line
    interpolating something else -- `probe_proxy` names the proxy host as `urlsplit` handed it
    back, controls and all -- cannot put a second line into the output either.
    """
    printed = format_result(True, f"http://egress:3128\n{FORGED_LINE}")
    assert "\n" not in printed
    assert printed == r"[ OK ] http://egress:3128\n[ OK ] the proxy refused everything"


@pytest.mark.parametrize(
    "spoil",
    [
        # **The shape the issue is about**, and the one worth having: the forgery is in the
        # *path*, so `urlsplit` removes the newline, the authority it reads is the rig's own
        # proxy, and the check dials it and passes. Every line is one this command asserted,
        # and one of them used to be two.
        pytest.param(lambda url: f"{url}/\n{FORGED_LINE}", id="in-the-path"),
        # The same, carrying a credential as well, so the two normalisations are seen together
        # on a value that really works.
        pytest.param(
            lambda url: "http://user:{}@{}/\n{}".format(
                PROXY_SECRET, url.removeprefix("http://"), FORGED_LINE
            ),
            id="in-the-path-with-userinfo",
        ),
        # Between the scheme and the authority, which `urlsplit` also removes: another value
        # the check dials the rig's proxy for and passes on, with all three characters in it.
        pytest.param(
            lambda url: url.replace("://", f"://{CONTROL_CHARACTERS}"), id="inside-the-authority"
        ),
        # And the two that do *not* parse: put in the authority, the forged text takes the port
        # with it and every line is a `FAIL` naming the value. They are here because a value
        # the parser turns away is quoted back into a line exactly as one it accepts is, so the
        # property has to hold for both.
        pytest.param(lambda url: f"{url}{CONTROL_CHARACTERS}{FORGED_LINE}", id="trailing"),
        pytest.param(
            lambda url: "http://user:{}@{}\n{}".format(
                PROXY_SECRET, url.removeprefix("http://"), FORGED_LINE
            ),
            id="trailing-with-userinfo",
        ),
    ],
)
@asynctest
async def test_no_control_character_in_the_proxy_variable_can_forge_a_result_line(
    spoil: Callable[[str], str],
) -> None:
    """The acceptance criterion, driven through a real `check` against a proxy that answers.

    One printed line per result. The forged text is still in the output -- it is escaped, not
    dropped, so an operator can still see what their variable holds -- but it is no longer a
    line, so the only text at the start of one is a verdict this command reached.
    """
    async with _CheckRig() as rig:
        host = rig.url.removeprefix("http://").rsplit(":", 1)[0]
        results = await rig.run({**rig.environ, "HTTPS_PROXY": spoil(rig.url)})
    printed = [format_result(ok, line) for ok, line in results]
    # `format_result` writes the prefix, so asserting on it would pass against any output at
    # all. What the criterion rests on is that nothing in a line ends it: the count of lines
    # read is the count of assertions made, and the forgery is inside one rather than beside
    # them.
    assert len("\n".join(printed).splitlines()) == len(results), printed
    assert not [line for line in printed if FORGED_LINE in line.splitlines()], printed
    # Escaped rather than dropped: the proxy is still named, and the newline every one of
    # these values carries is still in the output -- as the two characters `\` and `n`.
    assert [line for line in printed if host in line], printed
    assert [line for line in printed if r"\n" in line], printed
    assert not [line for line in printed if PROXY_SECRET in line], printed


@UPSTREAM_VARIABLES
@pytest.mark.parametrize(
    "url",
    [
        # None of these is a value `check` can read a target out of: `split_upstream_url`
        # refuses a control character outright, because `urlsplit` drops `\t`, `\r` and `\n`
        # while the live client keeps them, and a check that read past one would pass on a
        # configuration the client cannot use. So each is a `FAIL` line quoting the value back
        # -- which is exactly where the forgery would go.
        pytest.param(f"https://usage.example.test\n{FORGED_LINE}", id="newline"),
        pytest.param(f"https://usage.example.test{CONTROL_CHARACTERS}", id="all-three"),
        pytest.param(f"https://usage\r\n.example.test:443/\n{FORGED_LINE}", id="inside-the-host"),
    ],
)
@asynctest
async def test_no_control_character_in_an_upstream_variable_can_forge_a_result_line(
    variable: str, url: str
) -> None:
    """The same criterion for the other pair of variables an operator sets."""
    async with _CheckRig() as rig:
        results = await rig.run({**rig.environ, variable: url})
    printed = [format_result(ok, line) for ok, line in results]
    assert len("\n".join(printed).splitlines()) == len(results), printed
    assert not [line for line in printed if FORGED_LINE in line.splitlines()], printed
    # The variable is still named, and its value still quoted back rather than dropped: the
    # newline each of these carries survives as the two characters `\` and `n`.
    assert [line for line in printed if line.startswith(f"[FAIL] {variable}=")], printed
    assert [line for line in printed if r"\n" in line], printed


def test_upstream_urls_are_read_the_way_the_live_clients_urllib_reads_them() -> None:
    """Leading whitespace only, and the same set `urlsplit` removes.

    The live clients hand the raw variable to `Request`, which lstrips it, so the leading case
    is genuinely the value in force and the check has to agree. The *trailing* case is the
    opposite: `urlsplit` deliberately keeps it, so tidying it away here would have the check
    report `admitted` about a host the client never reaches.
    """
    assert upstream_urls({"CLAUDE_AI_HOST": "  https://usage.example.test"})[0] == (
        "CLAUDE_AI_HOST",
        "https://usage.example.test",
    )
    assert upstream_urls({"CLAUDE_AI_HOST": "\t\n https://usage.example.test"})[0][1] == (
        "https://usage.example.test"
    )
    # `str.lstrip`'s set, not `urlsplit`'s narrower C0-or-space one: `unwrap` is
    # `str(url).strip()`, so a non-breaking space pasted out of rendered documentation is
    # removed by the client, and reporting it here as a URL with no host would fail a
    # configuration that works.
    assert upstream_urls({"CLAUDE_AI_HOST": "\xa0https://usage.example.test"})[0][1] == (
        "https://usage.example.test"
    )
    assert upstream_targets({"CLAUDE_AI_HOST": "\xa0https://usage.example.test"})[0] == (
        "usage.example.test",
        443,
    )
    # Trailing whitespace is part of the host urllib hands the client, so it is part of the
    # host this check asks the proxy about, and the answer is a refusal rather than a pass.
    assert upstream_urls({"CLAUDE_AI_HOST": "https://usage.example.test "})[0][1] == (
        "https://usage.example.test "
    )
    assert upstream_targets({"CLAUDE_AI_HOST": "https://usage.example.test "})[0] == (
        "usage.example.test ",
        443,
    )
    # Neither a whitespace-only nor an empty override falls back to the default. The clients
    # do `os.environ.get(name, default)`, which defaults on absence and not on emptiness, so
    # both leave `Request` with a value it refuses -- and collapsing either to the default
    # would report a pass about `claude.ai`, a host that client will never reach.
    assert upstream_urls({"CLAUDE_AI_HOST": "   "})[0] == ("CLAUDE_AI_HOST", "")
    assert upstream_targets({"CLAUDE_AI_HOST": "   "})[0] is None
    assert upstream_urls({"CLAUDE_AI_HOST": ""})[0] == ("CLAUDE_AI_HOST", "")
    assert upstream_targets({"CLAUDE_AI_HOST": ""})[0] is None
    # Unset is the one genuine "no override" case, and only it takes the default.
    assert upstream_urls({})[0] == ("CLAUDE_AI_HOST", CLAUDE_AI_HOST)
    assert upstream_targets({})[0] == ("claude.ai", 443)
    assert upstream_targets({"CHATGPT_HOST": " https://c.test:8443"})[1] == ("c.test", 8443)


@pytest.mark.parametrize(
    ("url", "urllib_host"),
    [
        # `urlsplit` is the more forgiving reader of the two, in two different ways, and the
        # second value here is what it would have handed over -- the host that would have been
        # probed and reported. Pinning it is what anchors the rule to its reason rather than
        # leaving it a rule someone could delete as arbitrary.
        #
        # It normalises these away, so the host it reads is the clean one, while the client
        # keeps them: `Request` reads the first as the scheme `\x01https`.
        ("\x01https://usage.example.test", "usage.example.test"),
        ("https://usage.example.test\t", "usage.example.test"),
        ("https://usage.\rexample.test", "usage.example.test"),
        ("https://usage.example.test\n", "usage.example.test"),
        # DEL is the opposite case: `urlsplit` keeps it, and hands back a host `http.client`
        # will not dial -- its disallowed set is `[\x00-\x20\x7f]`.
        ("https://usage.example.test\x7f", "usage.example.test\x7f"),
    ],
)
def test_a_control_character_makes_an_upstream_unreadable_rather_than_normalised(
    url: str, urllib_host: str
) -> None:
    assert urlsplit(url).hostname == urllib_host
    assert split_upstream_url(url) is None
    assert upstream_targets({"CLAUDE_AI_HOST": url})[0] is None


@UPSTREAM_VARIABLES
@asynctest
async def test_a_whitespace_only_override_is_a_failed_line_and_not_a_silent_default(
    variable: str,
) -> None:
    """The regression this nearly became: `.strip() or default` turned a value the live client
    refuses into a pass asserting the default host was admitted."""
    async with _CheckRig() as rig:
        results = await rig.run({**rig.environ, variable: "   "})
    failures = [line for ok, line in results if not ok]
    assert [line for line in failures if line.startswith(f"{variable}=")], results
    assert not [line for ok, line in results if ok and line.startswith(f"{variable}: ")], results


@UNREADABLE_UPSTREAMS
@UPSTREAM_VARIABLES
@asynctest
async def test_an_upstream_variable_urllib_will_not_read_is_a_failed_line_not_a_traceback(
    variable: str, url: str
) -> None:
    """The second half of #48: `check` used to raise out of `upstream_targets` for these.

    Both variables, and the other one still gets its own line -- one unreadable value costs its
    own assertion and does not take the other upstream's with it.
    """
    async with _CheckRig() as rig:
        results = await rig.run({**rig.environ, variable: url})
    failures = [line for ok, line in results if not ok]
    assert [line for line in failures if line.startswith(f"{variable}=")], results
    assert [line for line in failures if "the bound is unverified for that upstream" in line]
    # The other upstream was still probed and still passed, so the gap is one line wide.
    other = "CHATGPT_HOST" if variable == "CLAUDE_AI_HOST" else "CLAUDE_AI_HOST"
    assert [line for ok, line in results if ok and line.startswith(f"{other}: ")], results


@UNREADABLE_UPSTREAMS
@UPSTREAM_VARIABLES
def test_the_check_command_exits_nonzero_for_an_upstream_it_cannot_read(
    variable: str, url: str, capsys, monkeypatch
) -> None:
    """Through `main`, because the exit status is the whole of what CI's `Egress bound` job
    reads: a traceback exits non-zero too, and this is what says the command reported instead
    of crashing."""
    monkeypatch.setattr(
        egress,
        "probe_proxy",
        lambda _p, host, *_a, **_k: (403, "Forbidden") if host == PROBE_DENIED_HOST else (200, "OK"),
    )
    monkeypatch.setattr(egress, "own_addresses", frozenset)
    monkeypatch.setattr(egress, "resolved_addresses", lambda _host: frozenset({"172.30.0.1"}))

    # `main` calls `check(os.environ)` with no probes, and the two probe parameters are
    # defaults bound at definition -- patching the module attributes would not reach them. So
    # the real `check` is wrapped rather than replaced: its body, which is what is under test,
    # runs unchanged and only the two things that would dial are supplied.
    real_check = egress.check
    monkeypatch.setattr(
        egress,
        "check",
        lambda environ: real_check(
            environ,
            direct=("example.com", 443),
            direct_probe=lambda *_a, **_k: DIRECT_NO_ROUTE,
            on_link=["172.30.0.1"],
            on_link_probe=lambda *_a, **_k: ON_LINK_NO_ANSWER,
            timeout_s=1,
        ),
    )
    for name in PROXY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://egress:3128")
    monkeypatch.setenv(variable, url)

    assert egress.main(["check"]) == 1
    printed = capsys.readouterr().out.splitlines()
    assert [line for line in printed if line.startswith(f"[FAIL] {variable}=")], printed


def test_port_zero_is_refused_in_both_readers_because_nothing_dials_it() -> None:
    """`0` is falsy, so `parts.port or <default>` read it as the default port -- and that is a
    pass on a deployment that reaches nothing.

    Both readers, because the proxy variable is the one the whole command is about:
    `HTTPS_PROXY=http://egress:0` had `check` probe the proxy that *works* on 3128 and print a
    green line, while the live client's urllib dials port 0 and is refused. `:0080` is not the
    same thing -- it is port 80, which is what the proxy's own `parse_connect_target` makes of
    it too, so the two readers agree on that as well.
    """
    assert urlsplit("http://egress:0").port == 0, "the premise: urllib hands 0 back as a port"
    assert split_proxy_url("http://egress:0") is None
    assert split_upstream_url("https://claude.ai:0") is None
    assert upstream_targets({"CLAUDE_AI_HOST": "https://claude.ai:0"})[0] is None
    assert parse_connect_target("claude.ai:0") is None

    # A port that is merely written oddly is still that port, in every reader.
    assert split_proxy_url("http://egress:0080").port == 80
    assert upstream_targets({"CLAUDE_AI_HOST": "https://claude.ai:0080"})[0] == ("claude.ai", 80)
    assert parse_connect_target("claude.ai:0080") == ("claude.ai", 80)
    # And no port at all still means the scheme's default rather than a refusal.
    assert upstream_targets({"CLAUDE_AI_HOST": "https://claude.ai"})[0] == ("claude.ai", 443)


def test_the_on_link_half_fails_when_it_is_given_no_ports() -> None:
    """A probe not made establishes nothing, ports included."""
    results = check_on_link(["172.30.0.1"], ports=[], probe=_never_probed, timeout_s=1)
    assert [ok for ok, _ in results] == [False]
    assert "unverified" in results[0][1]


def _never_probed(*args: object, **kwargs: object) -> str:
    raise AssertionError(f"probed with no ports: {args} {kwargs}")


def test_a_routing_table_that_hits_the_cap_loses_its_truncated_last_line(tmp_path) -> None:
    """A half-read line still splits into eight fields, and the subnet derived from one is an
    address dialled in place of one that was never read."""
    route = _route("eth0", "172.30.0.0", "0.0.0.0", "255.255.0.0", "0001")
    table = tmp_path / "route"
    table.write_text(ROUTE_HEADER + route * (egress.ROUTE_TABLE_CAP // len(route) + 2))
    read = read_route_table(str(table))
    assert read is not None
    assert len(read) <= egress.ROUTE_TABLE_CAP
    assert read.endswith("\n")
    assert on_link_addresses(read) == ["172.30.0.1"]


@asynctest
async def test_the_check_reads_the_containers_own_routing_table_when_given_none(
    monkeypatch,
) -> None:
    """The default path: no caller-supplied addresses, so the candidates are whatever the
    container's table yields -- here the gateway `internal: true` leaves on the bridge."""
    dialled: list[str] = []

    def record(addr: str, port: int, **_kwargs: object) -> str:
        dialled.append(addr)
        return ON_LINK_NO_ANSWER

    _pin_tables(monkeypatch, INTERNAL_ROUTE_TABLE)
    monkeypatch.setattr(egress, "own_addresses", lambda: frozenset({"172.30.0.2"}))
    async with _CheckRig() as rig:
        results = await asyncio.to_thread(
            functools.partial(
                check,
                rig.environ,
                direct=("example.com", 443),
                direct_probe=lambda *_a, **_k: DIRECT_NO_ROUTE,
                on_link=None,
                on_link_probe=record,
                timeout_s=2,
            )
        )
    assert set(dialled) == {"172.30.0.1"}
    assert all(ok for ok, _ in results), results


@asynctest
async def test_the_check_fails_when_the_routing_table_cannot_be_read(monkeypatch) -> None:
    """Not a platform this can assert the bound on is not the same as a bound that holds."""
    _pin_tables(monkeypatch, None, None)
    async with _CheckRig() as rig:
        results = await asyncio.to_thread(
            functools.partial(
                check, rig.environ, direct=("example.com", 443),
                direct_probe=lambda *_a, **_k: DIRECT_NO_ROUTE, on_link=None, timeout_s=2
            )
        )
    failures = [line for ok, line in results if not ok]
    assert len(failures) == 1
    assert "could not be read" in failures[0]
    assert "unverified" in failures[0]


@asynctest
async def test_the_check_fails_when_it_cannot_tell_what_is_on_link() -> None:
    """A half that probed nothing has established nothing, so it may not print OK."""
    async with _CheckRig() as rig:
        results = await rig.run(on_link=[])
    failures = [line for ok, line in results if not ok]
    assert len(failures) == 1
    assert "unverified" in failures[0]


@asynctest
async def test_the_check_fails_an_http_host_override() -> None:
    async with _CheckRig() as rig:
        environ = {**rig.environ, "CLAUDE_AI_HOST": "http://usage.example.test"}
        results = await rig.run(environ)
    failures = [line for ok, line in results if not ok]
    assert any("HTTPS only" in line for line in failures)
