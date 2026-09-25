"""The allow-listing CONNECT proxy that is the dashboard container's only route out.

Hermetic: every connection here is to a loopback socket the test started, so nothing reaches a
name the proxy would have to resolve. The check's on-link half is driven the same way -- a
loopback address standing in for the bridge gateway, and a stub probe where the case under test
is "nothing answers", which must not become a packet to a real subnet. The compose topology
around the proxy is covered by tests/test_compose_topology.py and, end to end, by the CI
`egress` job.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import socket
from urllib.parse import urlsplit

import pytest

from app.codex_quota import CHATGPT_HOST
from app.egress import (
    ALLOW_ENV,
    DEFAULT_ALLOW,
    MAX_CONNECTIONS,
    MAX_ONLINK_PROBES,
    MAX_REQUEST_BYTES,
    MAX_TUNNELS,
    ON_LINK_ACCEPTED,
    ON_LINK_NO_ANSWER,
    ON_LINK_REFUSED,
    PROBE_DENIED_HOST,
    PROBE_ONLINK_PORTS,
    PROXY_ENV_NAMES,
    Proxy,
    Rule,
    allow_rules,
    allowed,
    check,
    check_on_link,
    configured_proxy,
    normalise_host,
    on_link_addresses,
    own_addresses,
    parse_allow,
    parse_connect_target,
    parse_rule,
    probe_on_link,
    probe_proxy,
    read_route_table,
    reachable_directly,
    serve,
    serve_until_stopped,
    split_allow,
    upstream_targets,
)
from app.quota import CLAUDE_AI_HOST


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


def test_reachable_directly_is_false_for_a_port_with_nobody_on_it() -> None:
    assert reachable_directly("127.0.0.1", _dead_port(), timeout_s=1) is False


def test_reachable_directly_is_true_for_one_that_answers() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        assert reachable_directly("127.0.0.1", listener.getsockname()[1], timeout_s=2) is True


# A routing table shaped like the dashboard container's: one on-link subnet, no default route,
# which is what `internal: true` produces. The columns are little-endian hex, as the kernel
# renders them: 172.30.0.0/16 on eth0, holding this container at 172.30.0.2.
INTERNAL_ROUTE_TABLE = """\
Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
eth0\t00001EAC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0
"""
# The same container given a default route as well, which is what joining a non-internal network
# does: 172.17.0.1 is the gateway it names.
ROUTED_ROUTE_TABLE = """\
Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
eth0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\t0\t0\t0
eth0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0
"""


def test_the_on_link_addresses_are_the_bridge_gateway_internal_true_leaves_behind() -> None:
    """The address `internal: true` does not remove: the first of the container's own subnet,
    which is where Docker puts the bridge's gateway, reachable with no route at all (#37)."""
    assert on_link_addresses(INTERNAL_ROUTE_TABLE) == ["172.30.0.1"]
    assert on_link_addresses(INTERNAL_ROUTE_TABLE, own={"172.30.0.2"}) == ["172.30.0.1"]


def test_a_gateway_a_route_names_comes_first_and_is_not_repeated() -> None:
    assert on_link_addresses(ROUTED_ROUTE_TABLE) == ["172.17.0.1"]


def test_the_on_link_addresses_leave_out_this_container_and_loopback() -> None:
    """Reaching itself proves nothing, and a container whose own address is the first of the
    subnet would otherwise fail its own check."""
    assert on_link_addresses(INTERNAL_ROUTE_TABLE, own={"172.30.0.1"}) == []
    loopback = INTERNAL_ROUTE_TABLE.replace(
        "eth0\t00001EAC", "lo\t0000007F"
    )
    assert on_link_addresses(loopback) == []


@pytest.mark.parametrize(
    "table",
    [
        "",
        "Iface\tDestination\tGateway \n",
        "header\neth0\tnonsense\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n",
        # /31 and /32 hold no gateway address of their own to derive.
        "header\neth0\t00001EAC\t00000000\t0001\t0\t0\t0\tFEFFFFFF\t0\t0\t0\n",
    ],
)
def test_an_unreadable_routing_table_yields_no_addresses_rather_than_raising(table: str) -> None:
    assert on_link_addresses(table) == []


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
    # An address with no route to it at all, without leaving the host: a link-local address on
    # no interface here.
    assert probe_on_link("169.254.0.1", 443, timeout_s=0.2) == ON_LINK_NO_ANSWER


def test_the_on_link_half_passes_only_when_nothing_answers() -> None:
    def silent(*_args: object, **_kwargs: object) -> str:
        return ON_LINK_NO_ANSWER

    results = check_on_link(["172.30.0.1"], probe=silent, timeout_s=1)
    assert [ok for ok, _ in results] == [True]
    assert "no host on-link" in results[0][1]


def test_the_on_link_half_names_an_accept_over_a_refusal_on_another_port() -> None:
    """Every port is tried, so a closed 443 does not hide a service on 22 -- both fail, but the
    operator is told which one to go and look at."""
    answers = {443: ON_LINK_REFUSED, 80: ON_LINK_NO_ANSWER, 22: ON_LINK_ACCEPTED}
    results = check_on_link(
        ["172.30.0.1"], probe=lambda _a, port, **k: answers[port], timeout_s=1
    )
    assert [ok for ok, _ in results] == [False]
    assert "172.30.0.1:22 accepted" in results[0][1]


def test_the_on_link_half_probes_no_more_than_its_cap() -> None:
    """A check, not a scan of whatever a surprising routing table held."""
    asked: list[str] = []

    def record(addr: str, _port: int, **_kwargs: object) -> str:
        asked.append(addr)
        return ON_LINK_NO_ANSWER

    addresses = [f"10.0.0.{n}" for n in range(1, MAX_ONLINK_PROBES + 4)]
    results = check_on_link(addresses, probe=record, timeout_s=1)
    assert len(results) == MAX_ONLINK_PROBES
    assert set(asked) == set(addresses[:MAX_ONLINK_PROBES])
    assert len(asked) == MAX_ONLINK_PROBES * len(PROBE_ONLINK_PORTS)


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
    ):
        direct_port = _dead_port() if direct is None else direct
        # An on-link address that answers nothing, stubbed rather than dialled: the real
        # gateway of whatever network the suite is running on is not this test's business, and
        # the case has to be reproducible on a developer's machine and on a runner alike.
        def silent(*_args: object, **_kwargs: object) -> str:
            return ON_LINK_NO_ANSWER

        probe = on_link_probe or silent
        return await asyncio.to_thread(
            functools.partial(
                check,
                self.environ if environ is None else environ,
                direct=("127.0.0.1", direct_port),
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
async def test_the_check_fails_an_on_link_address_that_answers() -> None:
    """The container this check runs in has just reached something that is not the proxy, with
    no route involved. Before #37 the same container passed, because the only thing asked was
    whether a public name routed -- which `internal: true` answers on its own."""
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
async def test_the_check_fails_an_on_link_address_that_refuses() -> None:
    """A refusal is a live host: the address is reachable, and what it happens to be listening
    on is not a bound anybody chose."""
    async with _CheckRig() as rig:
        results = await rig.run(
            on_link=["127.0.0.1"], on_link_ports=[_dead_port()], on_link_probe=probe_on_link
        )
    failures = [line for ok, line in results if not ok]
    assert len(failures) == 1
    assert "refused a direct connection" in failures[0]


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
