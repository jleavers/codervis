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

import asyncio
import contextlib
import errno
import functools
import logging
import re
import socket
import sys
import threading
import time
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
    Proxy,
    Rule,
    allow_rules,
    allowed,
    check,
    check_on_link,
    configured_proxy,
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
    upstream_targets,
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
    other fails. `EAFNOSUPPORT` is a local failure in both, which would fail the check on
    every v4-only deployment of a dual-stack name if such a candidate could arise -- and
    `AI_ADDRCONFIG` on the lookup is what keeps it from arising, rather than an exception to
    the partition.
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

    An AAAA candidate on a container with no IPv6 address fails `EAFNOSUPPORT`, which is a
    local failure and fails the check. `socket.create_connection` did not ask for the flag and
    got away with it by reporting only the last candidate's error; this half reads every
    candidate, so the flag is load-bearing rather than tidy.
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
    # Generously above the budget and far below what the resolver was going to take: the claim
    # is that the probe stopped waiting on its own, not what it costs on a loaded runner.
    assert elapsed < 10, f"the probe outlasted its own budget: {elapsed:.2f}s for a 1 s budget"


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


def test_the_public_name_probe_costs_its_budget_once_over_all_of_a_name_s_addresses() -> None:
    """The bound is the probe's, not the probe's times however many addresses the name has.

    A name on a CDN resolves to a dozen addresses, and the old half gave each one the whole
    timeout in turn, with the lookup in front of them outside the timeout altogether: a
    `check` that said three seconds spent most of a minute. One budget covers all of it now.

    Silence is what spends it, so silence is what runs out of it, and a name only partly
    asked is `DIRECT_UNVERIFIED` -- it fails the check rather than passing it, because the
    addresses that went undialled establish nothing. A confined container never reaches this:
    with no default route the kernel refuses every address at once, for nothing.
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

    def getaddrinfo(host: str, port: object, family: int, kind: int):
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
