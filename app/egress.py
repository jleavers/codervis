"""The dashboard's network egress, bounded by an allow-listing ``CONNECT`` proxy.

The dashboard holds two long-lived bearer tokens and needs exactly two hosts. This proxy is the
only route off the host its container has -- the network below is what makes that true, and
``check`` is what establishes it -- and it admits only the hosts on its allow-list, so a
redirect, a host override or a compromised dependency cannot send a token to a host that is not
on that list.

**That bounds the destination host, and nothing else.** ``CONNECT`` is relayed without being
opened, so which account or tenant a request reaches at ``claude.ai`` or ``chatgpt.com``, and
anything else inside the tunnel, are outside what this can see or limit. Code running in the
dashboard's container can still use a token it holds against the hosts the clients use (#45).

Two halves, and neither is sufficient alone:

* **The network** makes the proxy unavoidable: docker-compose.yml puts the dashboard on an
  ``internal`` network only, which has no default route, and sets that network's gateway mode to
  ``isolated``, so the host holds no address on its bridge either. Both are needed, and the
  second is the one that is easy to miss: ``internal: true`` withholds the default route, while
  the bridge's own gateway address sits in the container's subnet and is reachable with no route
  at all (#37). The gateway-mode option needs Docker Engine 28.0+: 27.x knows the option but
  not the ``isolated`` value and refuses to create the network, and 26.x and older have no case
  for the label and ignore it, leaving that address in place. So ``check`` below, not a
  successful ``docker compose up``, is what tells an operator which of them they have -- and an
  operator who cannot upgrade closes the path with a host firewall rule that drops new inbound
  connections arriving on that bridge's interface, since nothing in the stack ever connects to
  the host over it. README's network section has the remedy for each engine.
* **The allow-list** makes the route narrow: ``claude.ai`` and ``chatgpt.com``, extended by
  the operator's ``EGRESS_ALLOW``.

``check`` asserts the whole bound from inside the dashboard's container, and asserts it by
probing what is reachable rather than by restating the design: the proxy filters by name and
admits the configured upstreams, the on-link addresses it derives are each either a peer in
this compose project or answer nothing, and a public name does not resolve-and-connect. A
refusal on an on-link address is a failure like an accept, because an RST comes from a live
host. Silence is the weaker half of that: it means nothing answered the ports asked, which a
host behind a default-drop rule also produces, so the on-link half is one of three assertions
rather than the only one.

What the on-link half derives is not every address the container could dial: it is the address
a bridge gateway would hold -- the first of each on-link subnet -- and any gateway a route
names (`on_link_addresses`, which says what that misses). A second host address further into
the subnet, or a gateway placed elsewhere by an explicit ``ipam.config.gateway``, is not
probed; the compose file puts neither there, and a change that does has to extend this.

``CONNECT`` is deliberately all of it. The proxy reads the host name from the request line and
never sees a byte of the TLS session it relays, so it holds no certificate authority and never
sees a token. Plain ``http://`` is refused rather than forwarded, so a bearer cannot cross the
proxy in cleartext even when a host override says ``http://``.

Ported from issuebot's ``issuebot.egress``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import errno
import logging
import os
import signal
import socket
import struct
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Network, ip_address
from types import MappingProxyType
from typing import Protocol
from urllib.parse import SplitResult, urlsplit

from .codex_quota import CHATGPT_HOST
from .quota import CLAUDE_AI_HOST

# Named, not __name__: under `python -m app.egress` that would be "__main__".
log = logging.getLogger("app.egress")

# The two hosts the live clients call (app/quota.py, app/codex_quota.py), and nothing else.
DEFAULT_ALLOW: tuple[str, ...] = ("claude.ai", "chatgpt.com")
# The operator's extension, e.g. for a CLAUDE_AI_HOST / CHATGPT_HOST override.
ALLOW_ENV = "EGRESS_ALLOW"
# Both cases of each: curl ignores an upper-case HTTP_PROXY, while other clients read only the
# upper-case spelling, so a deployment that set one case would leave half its tooling unrouted.
PROXY_ENV_NAMES: tuple[str, ...] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)
# The dashboard's upstream base URLs, as the clients read them.
UPSTREAM_ENV: tuple[tuple[str, str], ...] = (
    ("CLAUDE_AI_HOST", CLAUDE_AI_HOST),
    ("CHATGPT_HOST", CHATGPT_HOST),
)
# Reserved by RFC 2606 and resolving nowhere: a proxy that admits it is not filtering by name.
PROBE_DENIED_HOST = "egress-probe.invalid"
# Off the allow-list but answering on 443, so "no route" is the container's doing.
PROBE_DIRECT_HOST = "example.com"
# The container's own routing table, which is where the addresses it can dial without a route
# come from. Linux only, which is what the image is.
ROUTE_TABLE_PATH = "/proc/net/route"
# The ports an on-link address is tried on. Which one answers decides the wording, not the
# verdict: a refusal proves the address is live as surely as an accept does (see
# `probe_on_link`), so one port would be enough to establish reach. Three, because a firewall
# rule that covers a single port must not read as "nothing there", and these are the ports a
# host is likeliest to be listening on.
PROBE_ONLINK_PORTS: tuple[int, ...] = (443, 80, 22)
# How many derived addresses are probed. A container has one or two interfaces; the cap is
# what stops a surprising routing table turning a check into a scan.
MAX_ONLINK_PROBES = 4
DEFAULT_PORT = 3128
# Loopback unless told otherwise; compose passes 0.0.0.0 behind the internal network.
DEFAULT_BIND = "127.0.0.1"
DEFAULT_TARGET_PORT = 443
# A request line plus headers: small enough that a client which never sends the blank line
# costs nothing.
MAX_REQUEST_BYTES = 8 * 1024
# How long a request line may take to arrive. An established tunnel is not bounded here.
REQUEST_TIMEOUT_S = 10.0
# How long the upstream gets to accept a CONNECT. It does not bound an established tunnel,
# and it is no longer "shorter than the clients": a client's own budget is its total deadline
# plus however many per-socket-operation timeouts it spends before the next deadline check
# (quota.py, codex_quota.py), so it can outlast this. A slow-to-connect upstream is therefore
# 502'd here while the client still had budget, which is the right way round -- the client
# turns that into `unavailable` on its own cadence.
UPSTREAM_TIMEOUT_S = 10.0
_RELAY_CHUNK = 64 * 1024
# Tunnels open at once, and connections accepted at once. The second is checked before a byte
# is read, so a peer that connects and says nothing is refused rather than accumulated. Both
# sit far above what the refreshers' two upstream calls need, because a limit tight enough to
# reach is a denial of service for free.
MAX_TUNNELS = 64
MAX_CONNECTIONS = 256
# How long established tunnels get on shutdown, since `Server.wait_closed()` would otherwise
# wait for every one of them until Docker's SIGKILL.
SHUTDOWN_DRAIN_S = 5.0
_HOST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-._")


@dataclass(frozen=True, slots=True)
class Rule:
    """One allow-list entry: a host (or a domain suffix) and the port it may be reached on."""

    name: str
    port: int
    subdomains: bool

    def matches(self, host: str, port: int) -> bool:
        if port != self.port:
            return False
        if host == self.name:
            return True
        return self.subdomains and host.endswith("." + self.name)

    def __str__(self) -> str:
        prefix = "." if self.subdomains else ""
        return f"{prefix}{self.name}:{self.port}"


def split_allow(text: str | None) -> list[str]:
    """The entries in an ``EGRESS_ALLOW`` value: commas or whitespace, either way."""
    if not text:
        return []
    return [entry for entry in text.replace(",", " ").split() if entry]


def parse_rule(entry: str) -> Rule | None:
    """``example.com`` is that host on 443, ``example.com:8443`` names the port, and a leading
    dot (``.example.com``) admits the domain and every name under it."""
    text = entry.strip()
    if not text:
        return None
    subdomains = text.startswith(".")
    if subdomains:
        text = text[1:]
    name, sep, port_text = text.rpartition(":")
    if sep:
        if not port_text.isdigit():
            return None
        port = int(port_text)
        if not 1 <= port <= 65535:
            return None
    else:
        name, port = text, DEFAULT_TARGET_PORT
    host = normalise_host(name)
    if host is None:
        return None
    return Rule(host, port, subdomains)


def parse_allow(entries: Iterable[str]) -> tuple[tuple[Rule, ...], tuple[str, ...]]:
    """The rules the entries make, and a complaint for each that makes none.

    Total: a typo costs its own entry and never the service, because a proxy that refused to
    start would take both panels down rather than the one host the typo was about.
    """
    rules: list[Rule] = []
    complaints: list[str] = []
    for entry in entries:
        rule = parse_rule(entry)
        if rule is None:
            complaints.append(f"{entry!r} is not a host name, or host:port")
        elif rule not in rules:
            rules.append(rule)
    return tuple(rules), tuple(complaints)


def allow_rules(environ: Mapping[str, str]) -> tuple[tuple[Rule, ...], tuple[str, ...]]:
    """The deployment's allow-list: the default, extended by ``EGRESS_ALLOW``."""
    return parse_allow([*DEFAULT_ALLOW, *split_allow(environ.get(ALLOW_ENV))])


def configured_proxy(environ: Mapping[str, str]) -> str | None:
    """The proxy an HTTPS request from this process goes through, lower case first."""
    for name in ("https_proxy", "HTTPS_PROXY"):
        value = environ.get(name, "").strip()
        if value:
            return value
    return None


def upstream_urls(environ: Mapping[str, str]) -> list[tuple[str, str]]:
    """``(variable, base URL)`` for each live client, as the clients themselves read them."""
    return [(name, environ.get(name) or default) for name, default in UPSTREAM_ENV]


def upstream_targets(environ: Mapping[str, str]) -> list[tuple[str, int]]:
    """The ``(host, port)`` each live client connects to."""
    targets = []
    for _, url in upstream_urls(environ):
        parts = urlsplit(url)
        default = 80 if parts.scheme == "http" else DEFAULT_TARGET_PORT
        targets.append((parts.hostname or "", parts.port or default))
    return targets


def normalise_host(host: str) -> str | None:
    """A host name as it will be compared, or ``None`` for anything that is not one.

    Nothing is folded or decoded: a name the proxy cannot spell plainly is a name it refuses,
    which is the safe direction for a comparison that decides whether a connection leaves.
    """
    text = host.strip()
    if text.startswith("[") or text.endswith("]"):
        if not (text.startswith("[") and text.endswith("]")):
            return None
        try:
            return str(ip_address(text[1:-1]).compressed).lower()
        except ValueError:
            return None
    text = text.rstrip(".").lower()
    if not text or len(text) > 253:
        return None
    if not set(text) <= _HOST_CHARS:
        return None
    if ".." in text:
        return None
    return text


def parse_connect_target(target: str) -> tuple[str, int] | None:
    """``host:port`` from a ``CONNECT`` request line, or ``None``. The port is mandatory."""
    text = target.strip()
    if text.startswith("["):
        host_part, sep, port_text = text.partition("]")
        if not sep or not port_text.startswith(":"):
            return None
        host_part += "]"
        port_text = port_text[1:]
    else:
        host_part, sep, port_text = text.rpartition(":")
        if not sep:
            return None
    if not port_text.isdigit():
        return None
    port = int(port_text)
    if not 1 <= port <= 65535:
        return None
    host = normalise_host(host_part)
    if host is None:
        return None
    return host, port


def allowed(host: str, port: int, rules: Sequence[Rule]) -> bool:
    """Whether the allow-list admits this host on this port."""
    return any(rule.matches(host, port) for rule in rules)


def response(status: int, reason: str, body: str = "", *, allow: str | None = None) -> bytes:
    """One complete HTTP/1.1 response, closed after it."""
    payload = body.encode("utf-8", errors="replace")
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n"
    )
    if allow:
        head += f"Allow: {allow}\r\n"
    return head.encode("ascii") + b"\r\n" + payload


class Proxy:
    """A ``CONNECT``-only forward proxy over a fixed allow-list."""

    def __init__(
        self,
        rules: Sequence[Rule],
        *,
        request_timeout_s: float = REQUEST_TIMEOUT_S,
        upstream_timeout_s: float = UPSTREAM_TIMEOUT_S,
        max_tunnels: int = MAX_TUNNELS,
        max_connections: int = MAX_CONNECTIONS,
    ) -> None:
        self._rules = tuple(rules)
        self._request_timeout_s = request_timeout_s
        self._upstream_timeout_s = upstream_timeout_s
        self._max_tunnels = max_tunnels
        self._max_connections = max_connections
        # The level the count must fall back to before another saturation earns a log line.
        self._recovered_at = max_connections * 3 // 4
        self._open = 0
        self._accepted = 0
        self._saturated = False

    @property
    def rules(self) -> tuple[Rule, ...]:
        return self._rules

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One client connection, from its request line to the end of its tunnel.

        Never raises: this is the dashboard's only way out, so a connection that fails in a way
        nobody anticipated costs that connection and not the service.
        """
        if self._accepted >= self._max_connections:
            if not self._saturated:
                # The edge, not every refusal: one line per refusal would be a flood's second
                # payload. Cleared with hysteresis below.
                self._saturated = True
                log.warning(
                    "egress_connections_exhausted accepted=%d limit=%d",
                    self._accepted,
                    self._max_connections,
                )
            with contextlib.suppress(OSError):
                await reply(
                    writer,
                    response(
                        503,
                        "Service Unavailable",
                        f"the proxy is already holding {self._max_connections} connections\n",
                    ),
                )
            await close(writer)
            return
        if self._saturated and self._accepted < self._recovered_at:
            self._saturated = False
        self._accepted += 1
        try:
            await self._serve(reader, writer)
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            log.debug("egress_connection_failed error=%s: %s", type(exc).__name__, exc)
        except Exception:
            log.exception("egress_connection_error")
        finally:
            self._accepted -= 1
            await close(writer)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(self._request_timeout_s):
                head = await reader.readuntil(b"\r\n\r\n")
        except TimeoutError:
            await reply(writer, response(408, "Request Timeout", "no request in time\n"))
            return
        except asyncio.LimitOverrunError:
            await reply(
                writer,
                response(431, "Request Header Fields Too Large", "request head too large\n"),
            )
            return
        except asyncio.IncompleteReadError:
            return
        request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = request_line.split()
        if len(parts) != 3:
            await reply(writer, response(400, "Bad Request", "not a request line\n"))
            return
        method, target, _version = parts
        if method.upper() != "CONNECT":
            log.warning("egress_method_refused method=%s", method.upper()[:16])
            await reply(
                writer,
                response(
                    405,
                    "Method Not Allowed",
                    f"{method.upper()[:16]} is not proxied; this proxy speaks CONNECT only, "
                    "so egress is HTTPS only\n",
                    allow="CONNECT",
                ),
            )
            return
        destination = parse_connect_target(target)
        if destination is None:
            await reply(writer, response(400, "Bad Request", "not a host:port target\n"))
            return
        host, port = destination
        if self._open >= self._max_tunnels:
            log.warning("egress_tunnels_exhausted open=%d limit=%d", self._open, self._max_tunnels)
            await reply(
                writer,
                response(
                    503,
                    "Service Unavailable",
                    f"the proxy is already relaying {self._max_tunnels} connections\n",
                ),
            )
            return
        if not allowed(host, port, self._rules):
            # The healthcheck asks for the reserved probe name every 30 s; that is not an
            # attempt anyone needs to read about.
            level = logging.DEBUG if host == PROBE_DENIED_HOST else logging.WARNING
            log.log(level, "egress_denied host=%s port=%d", host, port)
            await reply(
                writer,
                response(
                    403,
                    "Forbidden",
                    f"{host}:{port} is not on the egress allow-list; add it to "
                    f"{ALLOW_ENV} in the deployment's .env to admit it\n",
                ),
            )
            return
        try:
            async with asyncio.timeout(self._upstream_timeout_s):
                upstream_reader, upstream_writer = await asyncio.open_connection(host, port)
        except (OSError, TimeoutError) as exc:
            log.warning(
                "egress_upstream_failed host=%s port=%d error=%s", host, port, type(exc).__name__
            )
            await reply(writer, response(502, "Bad Gateway", f"cannot reach {host}:{port}\n"))
            return
        log.debug("egress_allowed host=%s port=%d", host, port)
        self._open += 1
        try:
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await tunnel(reader, writer, upstream_reader, upstream_writer)
        finally:
            self._open -= 1
            await close(upstream_writer)


async def reply(writer: asyncio.StreamWriter, payload: bytes) -> None:
    writer.write(payload)
    with contextlib.suppress(OSError):
        await writer.drain()


async def close(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(OSError):
        writer.close()
        await writer.wait_closed()


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(OSError, asyncio.LimitOverrunError):
        while chunk := await reader.read(_RELAY_CHUNK):
            writer.write(chunk)
            await writer.drain()
    with contextlib.suppress(OSError):
        writer.write_eof()


async def tunnel(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    """Copy both ways; the *reply* direction says when the tunnel is over.

    A client that half-closes its side is waiting for an answer, so the request direction
    ending is passed on as ``write_eof`` and does not end the tunnel. When the upstream closes,
    the request direction is cancelled with it.
    """
    to_upstream = asyncio.ensure_future(_relay(client_reader, upstream_writer))
    to_client = asyncio.ensure_future(_relay(upstream_reader, client_writer))
    try:
        await to_client
    finally:
        to_upstream.cancel()
        await asyncio.gather(to_upstream, to_client, return_exceptions=True)


async def serve(
    rules: Sequence[Rule], *, bind: str = DEFAULT_BIND, port: int = DEFAULT_PORT
) -> asyncio.Server:
    """Start the proxy and return the server."""
    proxy = Proxy(rules)
    return await asyncio.start_server(proxy.handle, bind, port, limit=MAX_REQUEST_BYTES)


async def serve_until_stopped(
    server: asyncio.Server,
    *,
    stop: asyncio.Event | None = None,
    drain_s: float = SHUTDOWN_DRAIN_S,
) -> int:
    """Serve until SIGTERM, SIGINT or ``stop``, then give open connections ``drain_s``."""
    stop = asyncio.Event() if stop is None else stop
    loop = asyncio.get_running_loop()
    for signame in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signame, stop.set)
    await stop.wait()
    server.close()
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(drain_s):
            await server.wait_closed()
    return 0


def split_proxy_url(proxy_url: str) -> SplitResult | None:
    """A proxy variable as urllib reads one, ``egress:3128`` and ``http://egress:3128`` alike.

    One spelling for every caller. Where the check parsed the variable twice with two rules,
    the scheme-less form named a proxy to dial and no host to account for on-link, which failed
    a deployment that was whole.

    ``None`` where urllib will not parse it at all (``[::1``, an unclosed bracket), so that it
    is reported by the caller whose job that is rather than raised out of `check`.
    """
    try:
        return urlsplit(proxy_url if "//" in proxy_url else f"//{proxy_url}")
    except ValueError:
        return None


def probe_proxy(
    proxy_url: str, host: str, port: int = DEFAULT_TARGET_PORT, *, timeout_s: float = 10.0
) -> tuple[int, str] | str:
    """``CONNECT`` through the proxy: the status it answered, or a string saying why not.

    Closed the moment the status line is read, so an allowed probe costs the named host one
    accepted TCP connection and no bytes.
    """
    parts = split_proxy_url(proxy_url)
    if parts is None:
        return f"{proxy_url} is not a proxy URL"
    try:
        proxy_host, proxy_port = parts.hostname, parts.port or DEFAULT_PORT
    except ValueError:
        return f"{proxy_url} is not a proxy URL"
    if parts.scheme not in ("", "http") or not proxy_host:
        return f"{proxy_url} is not an http:// proxy URL"
    try:
        with socket.create_connection((proxy_host, proxy_port), timeout_s) as sock:
            sock.settimeout(timeout_s)
            request = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n"
            sock.sendall(request.encode("ascii"))
            line = b""
            while b"\r\n" not in line and len(line) < 512:
                chunk = sock.recv(512)
                if not chunk:
                    break
                line += chunk
    except (OSError, UnicodeEncodeError) as exc:
        return f"{proxy_host}:{proxy_port} did not answer ({type(exc).__name__})"
    status_line = line.split(b"\r\n", 1)[0].decode("latin-1")
    fields = status_line.split(None, 2)
    if len(fields) < 2 or not fields[1].isdigit():
        return f"{proxy_host}:{proxy_port} answered {status_line[:80]!r}, which is not HTTP"
    return int(fields[1]), fields[2] if len(fields) > 2 else ""


def reachable_directly(host: str, port: int = DEFAULT_TARGET_PORT, *, timeout_s: float) -> bool:
    """Whether a TCP connection to a name off this container's own subnets leaves it.

    Opened and closed at once; nothing is sent. A name that will not resolve, a refusal and a
    timeout all read as "no route", which is what an internal-only container looks like. This is
    the off-link half only: it is a public name, so it needs a default route, and a False here
    says nothing about the addresses the container can dial without one. ``probe_on_link`` is
    that half.
    """
    try:
        with socket.create_connection((host, port), timeout_s):
            return True
    except OSError:
        return False


# What an on-link probe found. Two of these mean the address is live, and the wording says
# which, because they call for different reading: something answered, or the host is there with
# nothing on that port. `ON_LINK_UNVERIFIED` is the fourth, and it is not an answer at all: the
# connection never left this container, so the address was not established either way.
ON_LINK_ACCEPTED = "accepted"
ON_LINK_REFUSED = "refused"
ON_LINK_NO_ANSWER = "no answer"
ON_LINK_UNVERIFIED = "not probed"

# Why an on-link address is accounted for rather than dialled (`peer_addresses`). Said once
# here, because the pass line an operator reads is the whole output of this half, and the two
# reasons are not interchangeable: reaching this container proves nothing either way, while the
# proxy is the one peer that *does* lead off the project -- by the route the allow-list bounds.
PEER_SELF = "this container's own address, so reaching it establishes nothing either way"
PEER_PROXY = (
    "the proxy {proxy}, which is the allow-listed way off this project rather than a way "
    "round it"
)

# The errnos that mean the probe was made and nothing came back. Everything else an OSError can
# carry -- EPERM and EACCES from a local rule, EMFILE and ENOBUFS from this container running
# out of something, EAFNOSUPPORT -- means no packet was sent, which is not evidence about the
# address and must not read as one. `TimeoutError` carries no errno and is handled on its own.
NO_ANSWER_ERRNOS = frozenset(
    getattr(errno, name)
    for name in ("ETIMEDOUT", "ENETUNREACH", "EHOSTUNREACH", "ENETDOWN", "EHOSTDOWN")
    if hasattr(errno, name)
)


def _address(column: str) -> str:
    """One of /proc/net/route's address columns.

    The kernel prints each address, which it holds in network order, as one host-order word:
    ``%08X`` of the raw ``__be32``. So the bytes come back by unpacking in *native* order --
    ``"<L"`` would be right only on a little-endian machine, which is most of them and not all.
    """
    return socket.inet_ntoa(struct.pack("=L", int(column, 16)))


def on_link_addresses(route_table: str) -> list[str]:
    """The addresses this container can dial with no route at all, from its own routing table.

    Two kinds, and neither is a constant this module could carry:

    * **A gateway a route names.** On a network with a default route that is the way off the
      host; the probe exists to catch a dashboard that has been given one.
    * **The first address of each on-link subnet.** This is the one ``internal: true`` does not
      remove: Docker gives a bridge network's gateway the first address of its subnet and puts
      it on the host's end of the bridge, so it sits in the container's own subnet and needs no
      route to be reached.

    Every candidate is returned, this container's own address included: which of them are the
    project rather than the host is `peer_addresses`' job, and it says so in a line of its own
    rather than by dropping one.

    Ordered gateways first, deduplicated, and loopback and the unspecified address dropped.

    The derived kind rests on Docker's own convention, and says so because the convention is
    not a guarantee: a network given an explicit ``ipam.config.gateway`` elsewhere in its subnet
    would not be probed. A compose change that does that has to extend this.

    IPv4 only, which is the whole of what reaches this bridge: the compose network sets no
    ``enable_ipv6``, and the topology test refuses one that does without the matching IPv6
    gateway isolation. A network that grows a second family needs ``/proc/net/ipv6_route`` read
    here as well, which is #42.
    """
    named: list[str] = []
    derived: list[str] = []
    for line in route_table.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8 or fields[0] == "lo":
            continue
        try:
            gateway = _address(fields[2])
            destination = _address(fields[1])
            netmask = _address(fields[7])
        except (OSError, ValueError, struct.error):
            continue
        if ip_address(gateway).is_unspecified:
            try:
                subnet = IPv4Network(f"{destination}/{netmask}", strict=False)
            except ValueError:
                continue
            # /31 and /32 hold no separate gateway address to derive, and a /0 is not a subnet
            # this container is on -- an on-link default route (`ip route add default dev eth0`)
            # would otherwise derive 0.0.0.1 and report it as checked.
            if subnet.prefixlen > 30 or subnet.prefixlen == 0:
                continue
            derived.append(str(subnet.network_address + 1))
        else:
            named.append(gateway)
    addresses: list[str] = []
    for addr in named + derived:
        parsed = ip_address(addr)
        if parsed.is_loopback or parsed.is_unspecified or addr in addresses:
            continue
        addresses.append(addr)
    return addresses


ROUTE_TABLE_CAP = 64 * 1024


def read_route_table(path: str = ROUTE_TABLE_PATH) -> str | None:
    """The routing table as the kernel renders it, or None where there is none to read.

    Capped like every other read of something this process does not write. A table that hits the
    cap loses its last line rather than keeping a truncated one: a half-written line can still
    split into eight fields with a short mask, and a subnet derived from that is a candidate
    dialled in place of one that was never read.
    """
    try:
        with open(path, encoding="ascii", errors="replace") as handle:
            table = handle.read(ROUTE_TABLE_CAP + 1)
    except OSError:
        return None
    if len(table) > ROUTE_TABLE_CAP:
        return table[: table.rfind("\n", 0, ROUTE_TABLE_CAP) + 1]
    return table


def resolved_addresses(host: str) -> frozenset[str]:
    """Every IPv4 address a name resolves to, or nothing where it does not resolve.

    Best effort by design: a name that does not resolve costs a candidate its label, so it is
    probed rather than accounted for -- which fails the check rather than passing it.
    """
    try:
        info = socket.getaddrinfo(host, None, socket.AF_INET)
    except (OSError, UnicodeError):
        return frozenset()
    return frozenset(str(entry[4][0]) for entry in info)


def own_addresses() -> frozenset[str]:
    """This container's own addresses, as far as its name resolves to them."""
    return resolved_addresses(socket.gethostname())


def probe_on_link(addr: str, port: int, *, timeout_s: float) -> str:
    """How an on-link address answers a TCP connection. Nothing is sent, and it is closed at once.

    A refusal is not "no route": an RST comes from a live host that chose not to serve this
    port, so it proves the address is reachable just as an accept does, and both are failures
    of the bound. Only silence -- a timeout, or the kernel saying there is no way to the
    address -- means the container cannot reach it.

    Silence is the one answer that is not conclusive: a host holding the address while dropping
    every port asked looks the same from here. That is why this is one of three things `check`
    asserts and not the only one, and why README's fallback is a firewall rule rather than a
    green line.

    A failure that stopped the connection leaving this container is not silence and is not
    reported as it: `EPERM` from a local rule, `EMFILE` from running out of descriptors and the
    like mean no packet was sent, so the address is `ON_LINK_UNVERIFIED` -- a probe not made,
    which fails the check like every other one here.
    """
    try:
        with socket.create_connection((addr, port), timeout_s):
            return ON_LINK_ACCEPTED
    except ConnectionRefusedError:
        return ON_LINK_REFUSED
    except ConnectionResetError:
        # A reset comes from a live host just as a refusal does.
        return ON_LINK_REFUSED
    except TimeoutError:
        # socket.timeout, which carries no errno: the probe went out and nothing came back.
        return ON_LINK_NO_ANSWER
    except OSError as exc:
        return ON_LINK_NO_ANSWER if exc.errno in NO_ANSWER_ERRNOS else ON_LINK_UNVERIFIED


class OnLinkProbe(Protocol):
    """A probe's shape, so the check can be given one and a reader knows what to write.

    Documented, not enforced: nothing in CI type-checks, so a stub whose parameters are spelled
    differently still runs. Every call here is positional in the first two, keyword in the last.
    """

    def __call__(self, addr: str, port: int, *, timeout_s: float) -> str: ...


def check_on_link(
    addresses: Sequence[str],
    *,
    ports: Sequence[int] = PROBE_ONLINK_PORTS,
    probe: OnLinkProbe = probe_on_link,
    timeout_s: float,
) -> list[tuple[bool, str]]:
    """One result per address: reachable at all is a failure, whoever answered.

    More candidates than the cap is itself a failure, and so is being given no ports to dial
    them on. The cap keeps a check from becoming a scan of whatever a surprising routing table
    held, and the rule everywhere here is that a probe not made establishes nothing -- so the
    addresses left undialled are named rather than dropped.
    """
    results: list[tuple[bool, str]] = []
    if not ports:
        return [
            (
                False,
                f"{len(addresses)} on-link address(es) to probe and no ports to probe them on, "
                "so nothing was dialled and the bound is unverified",
            )
        ]
    if len(addresses) > MAX_ONLINK_PROBES:
        skipped = ", ".join(addresses[MAX_ONLINK_PROBES:])
        results.append(
            (
                False,
                f"{len(addresses)} on-link addresses, more than the {MAX_ONLINK_PROBES} this "
                f"check will dial: {skipped} went unprobed, so the bound is unverified for them",
            )
        )
    for addr in addresses[:MAX_ONLINK_PROBES]:
        answers = [
            (port, outcome)
            for port, outcome in ((port, probe(addr, port, timeout_s=timeout_s)) for port in ports)
            if outcome != ON_LINK_NO_ANSWER
        ]
        # Every port is tried rather than stopping at the first answer: a refusal on 443 would
        # otherwise hide a service on 22. An address that answers does so at once, and one that
        # answers nowhere is dialled on every port either way, so this costs a timeout only
        # where a rule covers some ports. Of what came back, an accept is the most useful thing
        # to report and a refusal next -- both establish the address is live -- and "could not
        # be dialled" last, since it establishes nothing and would hide a port that answered.
        priority = (ON_LINK_ACCEPTED, ON_LINK_REFUSED, ON_LINK_UNVERIFIED)
        answered = min(
            answers, key=lambda answer: priority.index(answer[1]), default=None
        )
        if answered is None:
            results.append(
                (
                    True,
                    f"{addr} answered nothing on {_ports(ports)}: nothing holds that address, "
                    "or nothing on it answers the ports probed",
                )
            )
        elif answered[1] == ON_LINK_ACCEPTED:
            results.append(
                (
                    False,
                    f"{addr}:{answered[0]} accepted a direct connection: that address is "
                    "on-link, reachable with no route, and it is neither this container nor "
                    "the proxy -- so egress is not the only way off this container",
                )
            )
        elif answered[1] == ON_LINK_UNVERIFIED:
            results.append(
                (
                    False,
                    f"{addr} could not be dialled on {_ports(ports)}: the connection never left "
                    "this container, so nothing was established about that address and the "
                    "bound is unverified for it",
                )
            )
        else:
            results.append(
                (
                    False,
                    f"{addr} refused a direct connection on port {answered[0]}: a refusal comes "
                    "from a live host, so the address is reachable with no route and only what "
                    "it happens to be listening on bounds where a token can go",
                )
            )
    return results


def _ports(ports: Sequence[int]) -> str:
    return ", ".join(str(port) for port in ports) or "no ports"


def check(
    environ: Mapping[str, str],
    *,
    direct: tuple[str, int] = (PROBE_DIRECT_HOST, DEFAULT_TARGET_PORT),
    on_link: Sequence[str] | None = None,
    on_link_ports: Sequence[int] = PROBE_ONLINK_PORTS,
    on_link_probe: OnLinkProbe = probe_on_link,
    timeout_s: float = 10.0,
) -> list[tuple[bool, str]]:
    """Every assertion the bound rests on, as seen from inside the dashboard's container.

    The proxy must refuse a name that resolves nowhere, admit every host the live clients are
    configured for, and be the only way out. "The only way out" is asserted in both directions
    a container has: the addresses it can dial on-link -- the bridge's own gateway among them,
    which no route is needed to reach -- and a public name, which is what a container that has
    been given a default route can resolve and reach. Neither implies the other, and the
    on-link half is the one `internal: true` does not settle.

    An on-link address that is this container or the proxy is accounted for rather than dialled
    (`peer_addresses`); every other one is dialled, and answering at all fails the check.

    ``on_link`` defaults to whatever the container's routing table yields; a caller passes it
    to probe a set of its own.
    """
    proxy = configured_proxy(environ)
    if proxy is None:
        return [(False, "no proxy configured: HTTPS_PROXY/https_proxy is unset")]
    results: list[tuple[bool, str]] = []
    refused = probe_proxy(proxy, PROBE_DENIED_HOST, timeout_s=timeout_s)
    if isinstance(refused, str):
        results.append((False, f"{proxy}: {refused}"))
    elif refused[0] == 200:
        results.append(
            (False, f"{proxy} tunnelled to {PROBE_DENIED_HOST}: it is not filtering by name")
        )
    else:
        results.append((True, f"{proxy} refused {PROBE_DENIED_HOST} ({refused[0]})"))
    for (name, url), (host, port) in zip(upstream_urls(environ), upstream_targets(environ)):
        if urlsplit(url).scheme != "https":
            results.append(
                (False, f"{name}={url} is not https://, and egress is HTTPS only")
            )
            continue
        admitted = probe_proxy(proxy, host, port, timeout_s=timeout_s)
        if isinstance(admitted, str):
            results.append((False, f"{name}: {admitted}"))
        elif admitted[0] != 200:
            results.append(
                (
                    False,
                    f"{name}: the proxy answered {admitted[0]} for {host}:{port}; "
                    f"add it to {ALLOW_ENV} if the override is intended",
                )
            )
        else:
            results.append((True, f"{name}: {host}:{port} admitted"))
    results.extend(
        _on_link_results(
            on_link,
            ports=on_link_ports,
            probe=on_link_probe,
            timeout_s=min(timeout_s, 2.0),
            peers=peer_addresses(proxy),
        )
    )
    host, port = direct
    if reachable_directly(host, port, timeout_s=min(timeout_s, 3.0)):
        results.append(
            (
                False,
                f"{host}:{port} answered a direct connection: there is a route round the proxy, "
                "so the allow-list bounds only what asks it",
            )
        )
    else:
        results.append(
            (
                True,
                f"{host}:{port} unreachable directly: no route to a public address round the "
                "proxy",
            )
        )
    return results


def peer_addresses(proxy: str) -> dict[str, str]:
    """On-link addresses that are this compose project, and what each one is.

    An address here is not dialled, because reaching it is the bound working rather than a way
    round it, and the line it prints says which peer it was.

    * **This container's own.** Reaching itself establishes nothing either way.
    * **The proxy's.** `egress` is the one peer the dashboard is meant to reach, and on an
      engine that honours ``gateway_mode_ipv4: isolated`` it is *where the gateway would be*:
      no gateway address is allocated for such a network, so the subnet's first address -- the
      one Docker would have given the host's end of the bridge -- falls to the first container
      attached instead, which the compose file's dependency order makes `egress`. Finding the
      proxy there is therefore the evidence the option took effect. An engine that ignores the
      option holds that address on the bridge, the name resolves elsewhere, and the address is
      dialled like any other.
    """
    peers = {addr: PEER_SELF for addr in own_addresses()}
    parts = split_proxy_url(proxy)
    host = parts.hostname if parts else None
    if host:
        for addr in resolved_addresses(host):
            peers.setdefault(addr, PEER_PROXY.format(proxy=proxy))
    return peers


def _on_link_results(
    on_link: Sequence[str] | None,
    *,
    ports: Sequence[int],
    probe: OnLinkProbe,
    timeout_s: float,
    peers: Mapping[str, str] = MappingProxyType({}),
) -> list[tuple[bool, str]]:
    """The on-link half, including the cases where there is nothing left to probe.

    A candidate list that is empty from the start is reported as a failure rather than passed
    over: the invariant is that no on-link address answers, and a check that probed none of them
    has not established it. Saying so is the whole point of this half -- the bound used to read
    as kept because the one thing it asked was a question `internal: true` answers on its own.

    A list emptied by ``peers`` is the other case, and it passes: every address on-link was
    accounted for as this container or the proxy, which is what the bound looks like when it
    holds. What each one was is printed, so a pass is never silent about what it did not dial.
    """
    if on_link is None:
        table = read_route_table()
        if table is None:
            return [
                (
                    False,
                    f"{ROUTE_TABLE_PATH} could not be read, so the addresses this container can "
                    "reach on-link are unknown and the bound is unverified",
                )
            ]
        candidates = on_link_addresses(table)
    else:
        candidates = list(on_link)
    if not candidates:
        source = "the routing table" if on_link is None else "the candidates passed in"
        return [
            (
                False,
                f"no on-link address could be derived from {source}, so the bound is "
                "unverified: a container joined to a network has a subnet to derive one from",
            )
        ]
    accounted = [
        (
            True,
            f"{addr} is on-link by design: {peers[addr]}",
        )
        for addr in candidates
        if addr in peers
    ]
    to_probe = [addr for addr in candidates if addr not in peers]
    if not to_probe:
        return accounted
    return accounted + check_on_link(to_probe, ports=ports, probe=probe, timeout_s=timeout_s)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.egress", description=__doc__.split("\n")[0]
    )
    commands = parser.add_subparsers(dest="command", required=True)
    serve_cmd = commands.add_parser("serve", help="serve the allow-listing CONNECT proxy")
    serve_cmd.add_argument("--bind", default=DEFAULT_BIND)
    serve_cmd.add_argument("--port", type=int, default=DEFAULT_PORT)
    health = commands.add_parser(
        "healthcheck", help="exit 0 if the local proxy refuses a name off its list"
    )
    health.add_argument("--port", type=int, default=DEFAULT_PORT)
    commands.add_parser(
        "check", help="from the dashboard's container: verify the proxy and the network bound"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    if args.command == "healthcheck":
        answer = probe_proxy(f"http://127.0.0.1:{args.port}", PROBE_DENIED_HOST, timeout_s=5)
        return 0 if answer[:1] == (403,) else 1
    if args.command == "check":
        results = check(os.environ)
        for ok, line in results:
            print(f"[{' OK ' if ok else 'FAIL'}] {line}")
        return 0 if all(ok for ok, _ in results) else 1
    if not 0 <= args.port <= 65535:
        print("egress: --port must be between 0 and 65535", file=sys.stderr)
        return 1
    rules, complaints = allow_rules(os.environ)
    for complaint in complaints:
        log.warning("egress_allow_entry_ignored %s", complaint)
    return asyncio.run(_run(rules, bind=args.bind, port=args.port))


async def _run(rules: Sequence[Rule], *, bind: str, port: int) -> int:
    try:
        server = await serve(rules, bind=bind, port=port)
    except OSError as exc:
        print(f"egress: cannot listen on {bind}:{port}: {exc}", file=sys.stderr)
        return 1
    log.info("egress_started bind=%s port=%d allow=%s", bind, port, ",".join(map(str, rules)))
    return await serve_until_stopped(server)


if __name__ == "__main__":
    sys.exit(main())
