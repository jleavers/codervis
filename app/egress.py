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

**The other axis of that container's budget is not here at all**, and this is where to look for
it: what code running there may *read* is what docker-compose.yml bind-mounts, which is the
whole of both agent home trees rather than the seven paths the app reads inside them -- each
credential file sits at its tree's root, and a bind mount of a file follows the inode it was made
from, so it would pin the file a ``logout``/``login``, or a token refresh that renames a new file
over the old one, replaces. README's "How it works" states
both halves together, and its Caveats name what the whole-tree mounts leave readable.

Two halves make that destination bound true, and neither is sufficient alone:

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
this compose project or answer nothing, and a public name does not resolve-and-connect -- or,
where the lookup gave nothing to connect to, whether the resolver declined the name or never
answered, the routing tables name no default route in either family for it to have used. A
refusal on an on-link address is a failure like an accept, because an RST comes from a live
host. Silence is the weaker half of that: it means nothing answered the ports
asked, which a host behind a default-drop rule also produces, so the on-link half is one of
three assertions rather than the only one.

What the on-link half derives is not every address the container could dial: it is the address
a bridge gateway would hold -- the first of each on-link subnet -- and any gateway a route
names, in **either family** (`on_link_addresses`, which says what that misses). Both, because a
network with ``enable_ipv6`` has a second gateway address that is on-link exactly as the first
is, and ``/proc/net/route`` holds IPv4 routes only -- so reading it alone would print this bound
whole while that address sat on the bridge (#42). A second host address further into the subnet,
or a gateway placed elsewhere by an explicit ``ipam.config.gateway``, is not probed; the compose
file puts neither there, and a change that does has to extend this.

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
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Address, IPv6Network, ip_address
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
# The container's own routing tables, which are where the addresses it can dial without a
# route come from. Linux only, which is what the image is. Two files because the first holds
# IPv4 routes only, and a network with `enable_ipv6` puts a second gateway address on-link
# (#42); each is parsed by its own function, since they have nothing in common but their purpose.
ROUTE_TABLE_PATH = "/proc/net/route"
IPV6_ROUTE_TABLE_PATH = "/proc/net/ipv6_route"
# RTF_REJECT, in `/proc/net/ipv6_route`'s flags column. A netns with IPv6 enabled and nowhere
# to send it holds an `unreachable default`, which is the kernel saying so rather than a way
# off this container, so no candidate is derived from one.
RTF_REJECT = 0x0200
# The ports an on-link address is tried on. Which one answers decides the wording, not the
# verdict: a refusal proves the address is live as surely as an accept does (see
# `probe_on_link`), so one port would be enough to establish reach. Three, because a firewall
# rule that covers a single port must not read as "nothing there", and these are the ports a
# host is likeliest to be listening on.
PROBE_ONLINK_PORTS: tuple[int, ...] = (443, 80, 22)
# How many derived addresses are probed. A container has one or two interfaces, and each can
# contribute a candidate per family, so this stack's shape -- one internal network -- derives one
# over IPv4 and one over IPv6 (#42); the cap is what stops a surprising routing table turning a
# check into a scan. A list beyond it is not passed over: `check_on_link` fails and names what
# went unprobed, so growing past this is loud rather than quiet.
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


# --- name resolution, under a deadline ------------------------------------------------
#
# `socket.getaddrinfo` is a blocking call into the platform resolver and takes no timeout. It
# spends the resolver's *own* budget instead -- `/etc/resolv.conf`'s `timeout:`, 5 s by
# default, once per `attempts:` per nameserver listed -- and `socket.create_connection`'s
# timeout does not start until it has returned. So every probe here that dials a *name* rather
# than a literal from the routing table has to put the deadline round the lookup from outside,
# which is what `_resolve_within` is for (#68). `probe_on_link` needs none of this: every
# address it is given is already a literal, and a literal is not a lookup.

# How much of a probe's budget the name lookup may have, with the connection taking what is
# left. A share rather than the whole, because a resolver that is merely slow would otherwise
# spend the budget the connection needs and leave the probe unable to say anything about the
# thing it exists to ask. Half of a ten-second budget is one full resolver attempt, and leaves
# five seconds for the dial.
RESOLVE_BUDGET_SHARE = 0.5
# The least a candidate address may be dialled with. Below this a connect is not a probe: it
# would time out whatever is at the other end. A candidate whose window has fallen this low is
# reported as unasked instead, so that is a property of the loop rather than of how the
# arithmetic happened to land.
MIN_DIAL_BUDGET_S = 0.1
# What one name lookup outside a probe's own budget may cost: `resolved_addresses`, which is
# labelling rather than probing and has no budget of its own to take a share of. One full
# resolver attempt, deliberately generous -- see that function for what being wrong in each
# direction costs.
RESOLVE_TIMEOUT_S = 5.0


def _resolve_within(
    host: str,
    port: int | None,
    *,
    timeout_s: float,
    family: int = socket.AF_UNSPEC,
    kind: int = socket.SOCK_STREAM,
    flags: int = 0,
) -> list[tuple] | None:
    """`getaddrinfo` under a deadline: what the name resolved to, or None inside that time.

    This is the module's one resolver call. A caller that needs to ask differently -- the
    direct probe's `AI_ADDRCONFIG` -- widens these arguments rather than growing a second
    lookup beside it (#51 and #68 each bounded one, and merged as two).

    The deadline is put round the lookup from outside, by joining a thread doing it, because
    there is no timeout to give `getaddrinfo` itself -- see the note above this function for
    why that leaves a caller's own timeout covering the smaller half of what it spends.

    A lookup still running when the join returns is abandoned, not cancelled: there is no way
    to cancel one. The thread is a daemon, so it cannot hold the process open, and the callers
    here are a short-lived command -- at worst one resolver socket outlives the answer by the
    rest of the run. What the caller gets back is the honest answer: nothing was resolved in
    the time it had.

    The lookup's own failure is re-raised in the calling thread rather than swallowed, so a
    name the resolver *declines* still reads as a name that does not resolve. That distinction
    is the whole reason this returns None for the timeout instead of raising: a resolver that
    did not answer established nothing about the name, and a caller that cannot tell the two
    apart would report a claim about DNS that nothing here made.

    **What it re-raises is any `Exception`, so a caller with a string contract catches that
    rather than a list of types it expects.** `getaddrinfo` is a call into the platform
    resolver through a C library, and the interesting ones are `socket.gaierror` and
    `UnicodeError`; but `probe_proxy` promises a `(code, text)` tuple or a string and `check`
    prints what it is handed, so a type nobody listed must not become a traceback out of the
    command. A `BaseException` in the lookup thread is left alone deliberately: it kills that
    thread without an answer, which the join already reports as nothing resolved in time.
    """
    answer: list[tuple[str, object]] = []

    def resolve() -> None:
        try:
            answer.append(("ok", socket.getaddrinfo(host, port, family, kind, flags=flags)))
        except Exception as exc:  # re-raised below, in the thread that asked
            answer.append(("error", exc))

    # Named so that a thread dump during a hung probe says which lookup is outstanding.
    thread = threading.Thread(target=resolve, name=f"egress-resolve-{host}", daemon=True)
    thread.start()
    thread.join(max(timeout_s, 0.0))
    if not answer:
        return None
    kind_of_answer, value = answer[0]
    if kind_of_answer == "error":
        raise value  # type: ignore[misc]
    return list(value)  # type: ignore[arg-type]


def _connect_within(candidates: Sequence[tuple], *, deadline: float) -> socket.socket | OSError:
    """A connected socket to the first candidate that answers, or the failure to report.

    Every address the name has is tried, as `socket.create_connection` does, but against one
    deadline for all of them rather than the timeout each: the point of resolving the name
    here was to bound the total, and handing each candidate the full budget in turn would give
    that back to however many addresses the name happens to have.

    **What is left is shared between the addresses not yet tried, rather than handed to the
    next one whole**, because the two ways of being wrong here are not symmetrical. `egress`
    on a network with `enable_ipv6` resolves to an address per family, and giving the first
    one the whole remainder means a first address that blackholes spends the budget the second
    needed: a reachable proxy reported as `did not answer`, which is the direction
    `resolved_addresses` and README both call the unsafe one -- a whole deployment failing the
    check for nothing. A candidate that answers or refuses quickly costs its siblings nothing,
    since the remainder is recomputed each time round; only one that goes silent spends a
    share, and then the share is what it spends rather than everything.

    `MIN_DIAL_BUDGET_S` is the floor: below it a connect is not a probe, because it would time
    out whatever is at the other end. So a candidate gets its share or that floor, whichever
    is larger, and the loop stops once even the floor is more than is left.

    Failures are returned rather than raised because the caller words its own, and the last
    real one is the one it words -- like `create_connection`, with the same shortcoming, and it
    costs nothing here because this caller reports "did not answer" either way rather than
    drawing a verdict from the errno.

    **Three kinds of failure, reported in the order of what they establish**, because the caller
    renders whichever comes back as the reason an operator goes and looks at something:

    - a **dial** that failed says something about the address, so it wins: a refusal sends an
      operator to a proxy that is not listening, and reporting the budget instead would send
      them to a network that is dropping packets;
    - the **budget** running out says only that the probe stopped asking, which is the honest
      answer when no dial got far enough to say anything;
    - a **socket that could not be created** -- `EAFNOSUPPORT` for an `AF_INET6` candidate on a
      host without IPv6, `EMFILE` from this process -- is last, because no packet was sent and
      the module's own errno note below says such a failure is not evidence about the address.
      It is still reported where it is all there is, since "no address to dial" would be false.
    """
    dial_failure: OSError | None = None
    setup_failure: OSError | None = None
    total = len(candidates)
    for index, (family, socktype, proto, _canonname, sockaddr) in enumerate(candidates):
        remaining = deadline - time.monotonic()
        if remaining < MIN_DIAL_BUDGET_S:
            if dial_failure is not None:
                return dial_failure
            return TimeoutError("the budget ran out before every address was dialled")
        share = max(remaining / (total - index), MIN_DIAL_BUDGET_S)
        try:
            sock = socket.socket(family, socktype, proto)
        except OSError as exc:
            setup_failure = exc
            continue
        try:
            sock.settimeout(share)
            sock.connect(sockaddr)
        except OSError as exc:
            sock.close()
            dial_failure = exc
            continue
        return sock
    # Every candidate was tried. Checked against None rather than for truthiness, because an
    # exception class is free to define `__bool__` and a falsy failure is still a failure.
    if dial_failure is not None:
        return dial_failure
    if setup_failure is not None:
        return setup_failure
    return OSError("no address to dial")


def _arm_until(sock: socket.socket, deadline: float) -> None:
    """Give `sock` whatever is left of the deadline, or refuse the operation if nothing is.

    Called before every blocking operation rather than once, because a socket timeout is
    *per operation*: a peer answering one byte at a time renews it for as long as it keeps
    answering, and a caller reading a 512-byte status line renews it once a byte. Only a
    deadline checked between the operations bounds the total.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("the probe's budget ran out")
    sock.settimeout(remaining)


def probe_proxy(
    proxy_url: str, host: str, port: int = DEFAULT_TARGET_PORT, *, timeout_s: float = 10.0
) -> tuple[int, str] | str:
    """``CONNECT`` through the proxy: the status it answered, or a string saying why not.

    Closed the moment the status line is read, so an allowed probe costs the named host one
    accepted TCP connection and no bytes.

    **``timeout_s`` is the whole of what this costs, the proxy's own name lookup included**
    (#68). The proxy is configured as a name -- `egress`, answered by Docker's embedded DNS --
    and `socket.create_connection`'s timeout does not start until that name has been resolved,
    so this used to spend the resolver's budget before its own began, and `check` calls it once
    per configured upstream plus once for the denied probe. Three unbounded waits on the
    default configuration, before the direct half was reached. Now the lookup gets
    `RESOLVE_BUDGET_SHARE` of the budget, the candidate addresses share what is left of it, and
    the status line is read against the same deadline rather than against a per-operation
    timeout a trickling sender could renew 512 times over.

    **A resolver that did not answer produces its own failure string**, and it is
    ``{proxy_host}:{proxy_port} did not answer (the resolver did not answer)`` whenever the
    lookup's share is the shorter of the two budgets, which on the default it is. It is worded
    apart from the `({ExcType})` form deliberately: everything else that reaches that form is
    the proxy's own address failing to answer, which is a proxy an operator goes and looks at,
    while this one is the container's resolver and is nothing to do with the proxy at all. A
    resolver that answers and *declines* the name is not this case -- that is a `gaierror`, and
    it stays in the `({ExcType})` form, because the resolver did answer.

    That qualification is load-bearing rather than decorative. On the default 10 s the share is
    5 s, and `/etc/resolv.conf`'s own default is 5 s per attempt times two attempts per
    nameserver, so the share is the shorter and the string above is what appears. A
    container configured with `options timeout:2 attempts:1` gives up before the share does and
    raises `EAI_AGAIN` instead, and then the same unreachable resolver reads as `(gaierror)`,
    because that is what it handed back. Both are bounded, which is what this docstring
    promises; which of the two strings appears depends on whose budget runs out first, and only
    the resolver's own configuration decides that.
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
    deadline = time.monotonic() + timeout_s
    try:
        candidates = _resolve_within(
            proxy_host, proxy_port, timeout_s=timeout_s * RESOLVE_BUDGET_SHARE
        )
    except Exception as exc:
        # The lookup answered with something other than addresses: the resolver refusing the
        # name (`gaierror`), an IDNA encoding `getaddrinfo` will not take, or -- `RuntimeError`
        # -- no thread to run it in, from a process already at its limit. None of them is a
        # proxy this probe can reach, and the type name says which without quoting a message.
        # Caught whole rather than as those three, because this function contracts to *return*
        # a string or a tuple: a type nobody listed would otherwise leave `check` with a
        # traceback instead of a failed line, which is the one thing it must not do.
        return f"{proxy_host}:{proxy_port} did not answer ({type(exc).__name__})"
    if candidates is None:
        return f"{proxy_host}:{proxy_port} did not answer (the resolver did not answer)"
    dialled = _connect_within(candidates, deadline=deadline)
    if isinstance(dialled, OSError):
        return f"{proxy_host}:{proxy_port} did not answer ({type(dialled).__name__})"
    try:
        with dialled as sock:
            request = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n"
            _arm_until(sock, deadline)
            sock.sendall(request.encode("ascii"))
            line = b""
            while b"\r\n" not in line and len(line) < 512:
                _arm_until(sock, deadline)
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


# The errnos that mean a probe was made and nothing came back, shared by both halves below.
# Everything else an OSError can carry -- EPERM and EACCES from a local rule, EMFILE and
# ENOBUFS from this container running out of something, EAFNOSUPPORT -- means no packet was
# sent, which is not evidence about the address and must not read as some. `ENETDOWN` is
# deliberately absent for the same reason: an interface that is down is this container's
# condition, not the address's. `TimeoutError` carries no errno and is handled on its own, and
# `socket.gaierror` is an OSError whose errno is a negative `EAI_*`, so a name that will not
# resolve never lands here by accident.
NO_ANSWER_ERRNOS = frozenset(
    getattr(errno, name)
    for name in ("ETIMEDOUT", "ENETUNREACH", "EHOSTUNREACH", "EHOSTDOWN")
    if hasattr(errno, name)
)


# The two `EAI_*` codes that are the resolver *answering*: this name has no address here, or
# none of the kind asked for. Everything else `getaddrinfo` raises is the resolver not
# answering -- `EAI_AGAIN` for a lookup that timed out or was told to come back later,
# `EAI_FAIL` for a server that failed it, `EAI_SYSTEM` for a local error -- and that is a
# different thing to report: "the name does not resolve" is a claim about DNS, and only these
# two establish it.
NO_SUCH_NAME_ERRORS = frozenset(
    getattr(socket, name) for name in ("EAI_NONAME", "EAI_NODATA") if hasattr(socket, name)
)


# How the public name answered. Two of these are what an internal-only container looks like;
# the other three are failures, and they are failures for different reasons.
DIRECT_ANSWERED = "answered"
DIRECT_NO_ROUTE = "no route"
DIRECT_NO_DNS = "does not resolve"
DIRECT_NO_RESOLVER = "resolver did not answer"
DIRECT_UNVERIFIED = "not probed"

# How much of the direct probe's budget name resolution may have, with the dials taking what
# is left. A share rather than the whole, because a resolver that is merely slow would
# otherwise spend the budget the dials need and leave the probe unable to settle the name at
# all -- `DIRECT_UNVERIFIED`, which fails the check. It is the dials that establish the thing
# being asserted, so what they are guaranteed is what this number is for. `check` sizes the
# budget it passes so that half of it is a share worth having on each side.
RESOLVE_BUDGET_SHARE = 0.5
# The least a candidate may be dialled with. It rules out the degenerate window -- the
# microsecond left over when the address before it overshot the deadline -- which `connect`
# would spend in `select` and come back from as a timeout, reading as silence, which is the
# *pass*. It is not a window an address can be relied on to answer in: the module puts that
# figure an order of magnitude higher (`probe_direct`, on Linux's first SYN retransmit), and
# a floor that size cannot live here, because it would have to be a fraction of the caller's
# budget rather than a constant to avoid leaving a small budget with nothing dialled at all.
# What carries the real guarantee is the order: the *first* address gets the whole of what
# the lookup left, which at `check`'s budget is five seconds.
MIN_DIAL_BUDGET_S = 0.1


def _direct_errno_outcome(err: int | None) -> str:
    """What one candidate's failed connection establishes, by the errno it failed with.

    Exactly the partition `probe_on_link` draws, off the same set and for the same reason:
    silence -- a timeout, or the kernel saying there is no way to the address -- is the only
    failure that says the container could not reach it. Anything else stopped the connection
    inside this container (`EPERM` from a local rule, `EMFILE` from running out of descriptors,
    `ENETDOWN` from an interface that is down), so the address was not established either way
    and the candidate is `DIRECT_UNVERIFIED`. The two halves must not answer one errno in
    opposite directions, since one of those directions is a pass.

    `EAFNOSUPPORT` is `DIRECT_UNVERIFIED` here like everywhere else, and `AI_ADDRCONFIG` on
    the lookup is what keeps that from failing a container with IPv6 off kernel-wide on a
    dual-stack name: an address in a family this container holds no address in is never a
    candidate to begin with.

    A refusal never reaches here: it is reach, and its callers catch it before this is asked.
    """
    return DIRECT_NO_ROUTE if err in NO_ANSWER_ERRNOS else DIRECT_UNVERIFIED


def _resolve_direct(host: str, port: int, *, timeout_s: float) -> list[tuple] | None:
    """`getaddrinfo` for `host`, under a deadline, or None where it did not answer inside one.

    The lookup and its deadline are `_resolve_within`'s, which says why there is no timeout to
    give `getaddrinfo` and what an abandoned lookup costs (#51). This asks it one thing more.

    `AI_ADDRCONFIG` because a candidate in a family this container holds no address in is not
    an address it could ever have reached, so asking about it can only cost the probe an
    answer. What it costs depends on how the family is missing, and only one of the two is
    harmless: with the IPv6 module loaded but no address in it, `connect` fails `ENETUNREACH`,
    which this half already reads as no route; with IPv6 off kernel-wide (`ipv6.disable=1`, or
    the module absent) the socket cannot be opened at all and the dial fails `EAFNOSUPPORT`,
    which is a local failure and fails the whole check. `socket.create_connection` did not ask
    for the flag, and got away with it by reporting only the last candidate's error.
    """
    return _resolve_within(host, port, timeout_s=timeout_s, flags=socket.AI_ADDRCONFIG)


def _dial_direct(
    family: int, socktype: int, proto: int, sockaddr: tuple, *, timeout_s: float
) -> str:
    """How one resolved address of the public name answers. Nothing is sent; it closes at once.

    One address, because `probe_direct` classifies every address the name has rather than
    whichever one an error came back from last -- see there for why that matters.
    """
    try:
        sock = socket.socket(family, socktype, proto)
    except OSError as exc:
        return _direct_errno_outcome(exc.errno)
    try:
        sock.settimeout(timeout_s)
        sock.connect(sockaddr)
    except (ConnectionRefusedError, ConnectionResetError):
        return DIRECT_ANSWERED
    except TimeoutError:
        # socket.timeout, which carries no errno: the probe went out and nothing came back.
        return DIRECT_NO_ROUTE
    except OSError as exc:
        return _direct_errno_outcome(exc.errno)
    finally:
        sock.close()
    return DIRECT_ANSWERED


def probe_direct(host: str, port: int = DEFAULT_TARGET_PORT, *, timeout_s: float) -> str:
    """How a name off this container's own subnets answers a TCP connection.

    Opened and closed at once; nothing is sent. This is the off-link half only: it is a public
    name, so reaching it needs a default route, and nothing here says anything about the
    addresses the container can dial without one -- ``probe_on_link`` is that half.

    The partition is the same rule that half applies, because this one used to break it: a
    refusal, a name that would not resolve and a timeout all read as "no route", and a refusal
    is not that. An RST comes from a live host, so packets left the container and something
    answered -- a route round the proxy, reported as one. Anything that stopped the connection
    leaving this container is `DIRECT_UNVERIFIED`, because a probe not made establishes nothing.

    A name that does not resolve is its own outcome rather than either verdict. An internal
    network's resolver declines public names, so it is what a confined container looks like --
    but DNS failing establishes nothing about IP routing on its own, and `check` settles it
    against the routing table rather than inferring a route from a lookup. A resolver that
    does not answer *at all* is a fifth outcome and not that one, because "the name did not
    resolve" would be a claim about DNS nothing here established; `check` settles it against
    the same tables, which answer whatever the resolver did.

    **The name is resolved here rather than by `socket.create_connection`, and every address
    it has is dialled.** Both halves of that are the point (#51):

    * `create_connection`'s timeout starts *after* the name is resolved, so the old direct half
      cost a resolver's own budget -- seconds, several times over, where a container's resolver
      is unreachable -- before this one began, and nothing bounded the total. `timeout_s` is a
      deadline over both halves now: `RESOLVE_BUDGET_SHARE` of it is what the lookup gets, and
      the dials share what is left of it, so the whole probe costs its budget and no more.
    * `create_connection` defaults to `all_errors=False` and raises the *last* candidate's
      error, so a name whose first address refuses the connection -- which is reach, an RST
      from a live host -- and whose last is unreachable reported "no route" and passed the
      check. Every candidate is classified here, and a refusal from any of them is reach. It
      is the same conflation #37 took out of this function's verdicts, in the corner its
      verdicts were right about but its candidate handling was not.

    Where the candidates disagree otherwise, the least-established answer wins: reach if
    anything was reached, `DIRECT_UNVERIFIED` if any candidate's connection never left this
    container, and "no route" only where every address of the name was really dialled and
    nothing came back. A candidate left undialled because the budget ran out is unverified for
    the same reason -- a probe not made establishes nothing.

    **Each candidate gets the whole of what is left, not a share of it.** Dividing the budget
    by the number of addresses looks like the way to bound the total, and it is how this was
    first written, but a name on a CDN has a dozen: it would give each one two hundred
    milliseconds, which is under Linux's own first SYN retransmit, so a live host on a slow
    path -- or one whose first SYN was dropped -- would answer after the probe had already
    called it silence, and silence is the *pass*. The budget bounds how long silence may be
    waited on in total instead. What that costs is the other end of it: where the addresses
    really are silent, the budget goes on the first of them and the rest go undialled, which
    is `DIRECT_UNVERIFIED` and fails the check rather than passing it on a name only partly
    asked. The container this project ships does not reach that case: `internal: true`
    withholds the default route, so the kernel refuses every address at once and for nothing.
    A deployment confined by *dropping* egress rather than by withholding the route does reach
    it, and it is the one configuration this change moves from a pass to a failure -- before,
    each address got the whole timeout in turn and the last one's `TimeoutError` read as "no
    route". It fails closed, and README says what an operator there reads the line as.
    """
    deadline = time.monotonic() + timeout_s
    try:
        candidates = _resolve_direct(host, port, timeout_s=timeout_s * RESOLVE_BUDGET_SHARE)
    except socket.gaierror as exc:
        # Only the resolver's own "no such name" is a name that does not resolve. A lookup it
        # could not complete did not establish that, and says so in its own words.
        return DIRECT_NO_DNS if exc.errno in NO_SUCH_NAME_ERRORS else DIRECT_NO_RESOLVER
    except (OSError, RuntimeError, UnicodeError):
        # A lookup that could not be made at all: an IDNA encoding `getaddrinfo` will not take,
        # or no thread to run it in (`RuntimeError`, from a process at its limit). Nothing was
        # established, and this is the module's answer for that.
        return DIRECT_UNVERIFIED
    if candidates is None:
        return DIRECT_NO_RESOLVER
    if not candidates:
        # getaddrinfo does not return an empty list -- it raises instead -- but a name with no
        # address dialled nothing, and that is not silence.
        return DIRECT_UNVERIFIED
    outcome = DIRECT_NO_ROUTE
    for family, socktype, proto, _canonname, sockaddr in candidates:
        remaining = deadline - time.monotonic()
        if remaining < MIN_DIAL_BUDGET_S:
            # The budget went on the addresses before this one. Whatever they said, the rest
            # were not asked, and the name was not settled.
            return DIRECT_UNVERIFIED
        dialled = _dial_direct(family, socktype, proto, sockaddr, timeout_s=remaining)
        if dialled == DIRECT_ANSWERED:
            return DIRECT_ANSWERED
        if dialled == DIRECT_UNVERIFIED:
            outcome = DIRECT_UNVERIFIED
    return outcome


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


def _address(column: str) -> str:
    """One of /proc/net/route's address columns.

    The kernel prints each address, which it holds in network order, as one host-order word:
    ``%08X`` of the raw ``__be32``. So the bytes come back by unpacking in *native* order --
    ``"<L"`` would be right only on a little-endian machine, which is most of them and not all.
    """
    return socket.inet_ntoa(struct.pack("=L", int(column, 16)))


def _ipv6_address(column: str) -> IPv6Address:
    """One of /proc/net/ipv6_route's address columns.

    Nothing like `_address`: these are printed with ``%pi6``, which is the sixteen bytes in the
    order they go on the wire, so they are read big-endian and there is no host-order word to
    undo. `bytes.fromhex` rather than `int(column, 16)` because the column is thirty-two hex
    digits or it is not an address, and `int` would accept a sign and an underscore in it.
    """
    raw = bytes.fromhex(column)
    if len(raw) != 16:
        raise ValueError(f"an IPv6 column is 16 bytes, not {len(raw)}")
    return IPv6Address(raw)


def on_link_addresses(route_table: str, ipv6_route_table: str | None = None) -> list[str]:
    """The addresses this container can dial with no route at all, from its own routing tables.

    Two kinds, and neither is a constant this module could carry:

    * **A gateway a route names.** On a network with a default route that is the way off the
      host; the probe exists to catch a dashboard that has been given one.
    * **The first address of each on-link subnet.** This is the one ``internal: true`` does not
      remove: Docker gives a bridge network's gateway the first address of its subnet and puts
      it on the host's end of the bridge, so it sits in the container's own subnet and needs no
      route to be reached.

    **Both families**, from a table each (#42). A network with ``enable_ipv6`` has a second
    gateway address, on-link in the container's own IPv6 prefix and reachable with no route
    exactly as the first one is, and ``/proc/net/route`` holds IPv4 routes only -- so a half
    that read it alone would print the bound whole while that address sat on the bridge, which
    is the shape of defect this whole check exists to catch. ``ipv6_route_table`` is optional
    and ``None`` means "not read": a caller with one table passes one, and a kernel with no
    IPv6 at all has no second table to give (`read_ipv6_route_table`). The two files share
    nothing but their purpose -- no header line, the device last rather than first, prefixes in
    hex with a length beside them -- so each has its own parser and this combines what they
    yield.

    Every candidate is returned, this container's own address included: which of them are the
    project rather than the host is `peer_addresses`' job, and it says so in a line of its own
    rather than by dropping one.

    Ordered gateways first and derived after, IPv4 before IPv6 within each kind, deduplicated,
    and what is nobody's address in either family dropped: loopback, the unspecified address,
    and multicast -- a route to ``224.0.0.0/4`` or ``ff00::/8`` is on-link, and the first
    address of each is a group rather than a host to dial.

    The derived kind rests on Docker's own convention, and says so because the convention is
    not a guarantee: a network given an explicit ``ipam.config.gateway`` elsewhere in its subnet
    would not be probed. A compose change that does that has to extend this.
    """
    named, derived = _ipv4_candidates(route_table)
    if ipv6_route_table is not None:
        named6, derived6 = _ipv6_candidates(ipv6_route_table)
        named += named6
        derived += derived6
    addresses: list[str] = []
    for addr in named + derived:
        parsed = ip_address(addr)
        if parsed.is_loopback or parsed.is_unspecified or parsed.is_multicast:
            continue
        if addr in addresses:
            continue
        addresses.append(addr)
    return addresses


def _ipv4_candidates(route_table: str) -> tuple[list[str], list[str]]:
    """`/proc/net/route`'s gateways and on-link subnets, in that order, as two lists.

    A header line first, the device in the first column, and every address a host-order word
    (`_address`). Filtering what is nobody's is `on_link_addresses`' job, since that rule is
    the same for both families.
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
    return named, derived


def _ipv6_candidates(route_table: str) -> tuple[list[str], list[str]]:
    """The same two kinds from `/proc/net/ipv6_route`, which is a different file in every detail.

    No header line, so every line is read. The device is the *last* column and is empty on a
    route that has none. A prefix is thirty-two hex digits with its length in hex beside it
    (`_ipv6_address`), and a route with no gateway prints one of zeros -- which is how a gateway
    a route names is told from an on-link prefix here, without reading RTF_GATEWAY.

    What it skips, and why none of them is an address this check should dial:

    * **`lo`**, as in the IPv4 reading. It is also where the kernel's own ``::1/128`` and its
      ``unreachable default`` sit.
    * **A reject route.** `RTF_REJECT` is this netns saying it has nowhere to send the family.
    * **A /0, and a /127 or /128**, exactly as over IPv4: a default route is not a subnet this
      container is on, and a prefix that long holds no separate address to derive.
    * **A link-local prefix.** Its first address is derived from nothing: a link-local address
      is not handed out by Docker's IPAM but built from the interface's own MAC, so nothing sits
      at ``fe80::1``, while every container with IPv6 at all has ``fe80::/64`` on-link -- so
      deriving from it would spend the probe cap on an address that answers nowhere. A gateway a
      route *names* in ``fe80::/10`` is a different thing and is kept, with the device it is
      reachable through appended, since a link-local address cannot be dialled without one.
    """
    named: list[str] = []
    derived: list[str] = []
    for line in route_table.splitlines():
        fields = line.split()
        if len(fields) < 9:
            continue
        device = fields[9] if len(fields) > 9 else ""
        if device == "lo":
            continue
        try:
            destination = _ipv6_address(fields[0])
            prefixlen = int(fields[1], 16)
            gateway = _ipv6_address(fields[4])
            flags = int(fields[8], 16)
        except ValueError:
            continue
        if flags & RTF_REJECT:
            continue
        if gateway.is_unspecified:
            try:
                subnet = IPv6Network((destination, prefixlen), strict=False)
            except ValueError:
                continue
            if subnet.prefixlen > 126 or subnet.prefixlen == 0:
                continue
            if subnet.network_address.is_link_local:
                continue
            derived.append(str(subnet.network_address + 1))
        elif gateway.is_link_local and device:
            # Without a scope this could not be dialled at all: `connect` to a bare link-local
            # address fails locally, which would report as "not probed" for an address the
            # table says is reachable through a device it names. Where the table names no device
            # for one -- which no next hop the kernel prints does, since every one of them has
            # a device -- the candidate falls through bare and is reported as unverified rather
            # than dropped, because a candidate dropped is a question this half did not ask.
            named.append(f"{gateway}%{device}")
        else:
            named.append(str(gateway))
    return named, derived


def has_default_route(route_table: str, ipv6_route_table: str | None = None) -> bool:
    """Whether the tables name a default route -- `0.0.0.0/0`, or `::/0`.

    That, and not "a route off this container's subnets" in general, is the question: it is
    what `internal: true` withholds, and it is the evidence the direct half falls back on when
    a public name will not resolve. A lookup that fails says nothing about routing -- an
    internal network's resolver declines public names, and so does a broken resolver on a
    container that has a way off the host. The table tells the two apart without asking
    anything of the network.

    Both families, for the reason the on-link half reads both (#42): a container whose only
    default route is an IPv6 one has a way off its own subnets, and reading `/proc/net/route`
    alone would answer `False` for it -- which is the pass this guards. ``ipv6_route_table`` is
    optional and ``None`` means "not read"; a caller that could not read it must not treat this
    answer as settled.

    What it therefore answers `False` to while a route off-subnet exists: a split default
    (`0.0.0.0/1` plus `128.0.0.0/1`, the VPN idiom), and a route to some other subnet via a
    named gateway. Neither is silent here: `on_link_addresses` returns every gateway a route
    names, so the on-link half dials it. This is the narrower question, deliberately, because
    the pass it guards should rest on the one condition the compose file sets.

    The table it is given is `read_route_table`'s, which stops at `ROUTE_TABLE_CAP`. A table
    long enough to lose its default route to that cap would answer `False` on evidence that was
    never read -- 64 KiB is some five hundred routes, and a container attached to one or two
    networks has a handful, so this is out of reach rather than guarded against.
    """
    for line in route_table.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8 or fields[0] == "lo":
            continue
        try:
            destination = int(fields[1], 16)
            netmask = int(fields[7], 16)
        except ValueError:
            continue
        if destination == 0 and netmask == 0:
            return True
    return _has_ipv6_default_route(ipv6_route_table) if ipv6_route_table is not None else False


def _has_ipv6_default_route(route_table: str) -> bool:
    """`::/0` in `/proc/net/ipv6_route`, read the way `_ipv6_candidates` reads that file.

    A netns with IPv6 enabled and nowhere to send it holds an `unreachable default` on `lo`,
    which is a default route in shape and the absence of one in fact -- so both of that
    function's skips apply here, or every IPv6-enabled container would report a way off itself.
    """
    for line in route_table.splitlines():
        fields = line.split()
        if len(fields) < 9:
            continue
        if (fields[9] if len(fields) > 9 else "") == "lo":
            continue
        try:
            destination = _ipv6_address(fields[0])
            prefixlen = int(fields[1], 16)
            flags = int(fields[8], 16)
        except ValueError:
            continue
        if flags & RTF_REJECT:
            continue
        if destination.is_unspecified and prefixlen == 0:
            return True
    return False


ROUTE_TABLE_CAP = 64 * 1024


def read_route_table(path: str = ROUTE_TABLE_PATH) -> str | None:
    """The routing table as the kernel renders it, or None where there is none to read.

    Either table, by path -- the format is the parsers' business and the cap is the same.

    Capped like every other read of something this process does not write. A table that hits the
    cap loses its last line rather than keeping a truncated one: a half-written line can still
    split into enough fields to parse, with a short mask or a short prefix length, and a subnet
    derived from that is a candidate dialled in place of one that was never read.
    """
    try:
        with open(path, encoding="ascii", errors="replace") as handle:
            table = handle.read(ROUTE_TABLE_CAP + 1)
    except OSError:
        return None
    if len(table) > ROUTE_TABLE_CAP:
        return table[: table.rfind("\n", 0, ROUTE_TABLE_CAP) + 1]
    return table


def read_ipv6_route_table(path: str = IPV6_ROUTE_TABLE_PATH) -> str | None:
    """The IPv6 table: `""` where this kernel has no IPv6, `None` where it would not be read.

    Those are two different answers and must not be one value. A netns with no IPv6 stack has no
    file here, no IPv6 address, and no way to dial one -- nothing is on-link over a family that
    is not there, so an empty table is the honest reading of an absent file and the half rests
    on what the other family establishes. A file that *is* there and will not be read leaves
    this family unknown, which its caller reports as unverified rather than passing over, since
    the rule everywhere here is that a probe not made establishes nothing.
    """
    if not os.path.exists(path):
        return ""
    return read_route_table(path)


def resolved_addresses(host: str, *, timeout_s: float = RESOLVE_TIMEOUT_S) -> frozenset[str]:
    """Every address a name resolves to, in either family, or nothing where it does not resolve.

    Both families, because this is what labels an on-link candidate as a peer, and the label is
    a string match: a peer's IPv6 address left out here is a peer *dialled*, which fails a
    correct dual-stack deployment on the proxy's own address (#42).

    Best effort by design: a name that does not resolve costs a candidate its label, so it is
    probed rather than accounted for -- which fails the check rather than passing it.

    **``timeout_s`` bounds the lookup, and a resolver that does not answer inside it is that
    same best-effort cost** (#68). This is labelling rather than probing, so it has no budget of
    its own to take a share of and carries `RESOLVE_TIMEOUT_S` instead -- one full attempt at
    `/etc/resolv.conf`'s default `timeout:`, which is deliberately the generous end. Being wrong
    in the two directions costs different things, and only one of them is safe: too long and the
    check is slow, which is what this bound is for; too short and the *proxy's* own label is
    lost on a resolver that was merely slow, the proxy's address is dialled, the proxy answers,
    and a whole deployment fails the check for nothing. So it errs towards the slow side, and
    `check`'s cost is what README states.

    The budget is per name rather than shared across a caller's names for the same reason:
    `peer_addresses` looks up two, and the second is the proxy's -- the one whose label decides
    a pass. A shared budget would let this container's own hostname spend the proxy's.
    """
    try:
        info = _resolve_within(host, None, timeout_s=timeout_s)
    except Exception:
        # Whole, for the reason `probe_proxy`'s own handler says: this is best effort and its
        # callers have no failure path, so a type nobody listed must cost a label rather than
        # end the run. The exception is not reported anywhere, so nothing of it is quoted.
        return frozenset()
    if info is None:
        # The resolver did not answer in the time it had. Nothing was resolved, which is what
        # `frozenset()` says, and the caller's own docstring says what an unlabelled candidate
        # then costs: it is dialled, and the check fails rather than passing.
        return frozenset()
    return frozenset(str(entry[4][0]) for entry in info)


def own_addresses(*, timeout_s: float = RESOLVE_TIMEOUT_S) -> frozenset[str]:
    """This container's own addresses, as far as its name resolves to them, in ``timeout_s``."""
    return resolved_addresses(socket.gethostname(), timeout_s=timeout_s)


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

    Either family, and there is no family argument because there is nothing for one to decide:
    every address here is a literal from the routing table, and `socket.create_connection` dials
    a literal in the family it is written in -- an IPv6 one over `AF_INET6`, a scoped one
    (`fe80::1%eth0`) out of the device named in it.
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


class DirectProbe(Protocol):
    """The public-name probe's shape, so `check` can be handed one that dials nothing.

    The tests need it: a real connect to a supposedly unroutable address is whatever the
    machine running the suite does with it, which is not reproducible between a developer's
    laptop and a runner. `probe_direct` keeps its own real-socket tests.
    """

    def __call__(self, host: str, port: int, *, timeout_s: float) -> str: ...


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
        priority = (ON_LINK_ACCEPTED, ON_LINK_REFUSED)

        # An outcome this does not know sorts last and is reported as unverified, rather than
        # raising out of a check whose other assertions have already run.
        def rank(answer: tuple[int, str]) -> int:
            return priority.index(answer[1]) if answer[1] in priority else len(priority)

        answered = min(answers, key=rank, default=None)
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
                    f"{_target(addr, answered[0])} accepted a direct connection: that address "
                    "is on-link, reachable with no route, and it is neither this container nor "
                    "the proxy -- so egress is not the only way off this container",
                )
            )
        elif answered[1] == ON_LINK_REFUSED:
            results.append(
                (
                    False,
                    f"{addr} refused a direct connection on port {answered[0]}: a refusal comes "
                    "from a live host, so the address is reachable with no route and only what "
                    "it happens to be listening on bounds where a token can go",
                )
            )
        else:
            results.append(
                (
                    False,
                    f"{addr} could not be dialled on {_ports(ports)}: no answer came back "
                    "and no probe was established as having been made, so nothing is known "
                    "about that address and the bound is unverified for it",
                )
            )
    return results


def _ports(ports: Sequence[int]) -> str:
    return ", ".join(str(port) for port in ports) or "no ports"


def _target(addr: str, port: int) -> str:
    """An address and port as an operator would paste them back: an IPv6 literal in brackets.

    `fd00:cafe::1:443` names neither the address nor the port, and the line it appears in is the
    one a reader takes to `docker network inspect`.

    Only where an address and a port are printed together. The refusal and no-answer lines name
    the port in words ("on port 443", "on 443, 80, 22"), so the address in them is already
    unambiguous and bracketing it would be noise.
    """
    return f"[{addr}]:{port}" if ":" in addr else f"{addr}:{port}"


def format_result(ok: bool, line: str) -> str:
    """One line of `check`'s output, as an operator reads it.

    Shared with the test that compares README's sample output against what `check` produces, so
    the documented output and the real one cannot drift through the prefix either.
    """
    return f"[{' OK ' if ok else 'FAIL'}] {line}"


def check(
    environ: Mapping[str, str],
    *,
    direct: tuple[str, int] = (PROBE_DIRECT_HOST, DEFAULT_TARGET_PORT),
    on_link: Sequence[str] | None = None,
    on_link_ports: Sequence[int] = PROBE_ONLINK_PORTS,
    on_link_probe: OnLinkProbe = probe_on_link,
    direct_probe: DirectProbe = probe_direct,
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
    (`peer_addresses`); every other one is dialled, and answering at all fails the check. So
    does a probe that could not be made: an unreadable routing table, a list longer than the
    cap, a connection that never left this container. Both halves report that separately from
    "nothing answered", because only one of the two is evidence. The public name is the same
    rule: a name that will not resolve is settled against the routing table, since a failed
    lookup on its own says nothing about whether packets can leave.

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
    # The whole of what this half costs, resolution included: `probe_direct` holds the deadline
    # over both its lookup and its dials, so this is what it costs `check` however many
    # addresses the name has and however slow the resolver is. It used to be capped at three
    # seconds, which was a cap on the dial alone and bought nothing -- the lookup in front of
    # it was unbounded. Now that the number is the total, the half is given the same budget as
    # the rest of the check: half of it covers one full resolver attempt (`/etc/resolv.conf`'s
    # `timeout:` is 5 s), and half leaves an address long enough to answer a retransmitted SYN.
    answered = direct_probe(host, port, timeout_s=timeout_s)
    if answered == DIRECT_ANSWERED:
        results.append(
            (
                False,
                f"{host}:{port} answered a direct connection: there is a route round the proxy, "
                "so the allow-list bounds only what asks it",
            )
        )
    elif answered == DIRECT_NO_ROUTE:
        results.append(
            (
                True,
                f"{host}:{port} unreachable directly: no route to a public address round the "
                "proxy",
            )
        )
    elif answered in (DIRECT_NO_DNS, DIRECT_NO_RESOLVER):
        # A lookup that failed is not a routing fact. What settles it is the table: a container
        # with no default route cannot reach an off-link address whether it resolved one or
        # not, and a container that has one was not established either way by a failed lookup.
        # Both tables, because a default route in either family is a way off this container's
        # subnets (#42), and a table that would not be read settles nothing. The line names one
        # file, since that is what an operator goes and looks at: the IPv4 table where it was
        # the unreadable one or both were, and the IPv6 table where that was the only one.
        #
        # A resolver that never answered is settled here too, and by the same tables: they say
        # what they say whether the lookup was declined or never returned. Only the passing
        # line tells the two apart, because that one is where "does not resolve" would be a
        # claim about DNS the probe did not establish -- and the difference is the operator's
        # to see, since one of the two is a resolver an operator has to go and fix, and it is
        # what the public-name line spends most of its budget on when it happens (#51).
        lookup = (
            "does not resolve here"
            if answered == DIRECT_NO_DNS
            else "could not be looked up, because the resolver did not answer"
        )
        table = read_route_table()
        ipv6_table = read_ipv6_route_table()
        unread = ROUTE_TABLE_PATH if table is None else None
        if unread is None and ipv6_table is None:
            unread = IPV6_ROUTE_TABLE_PATH
        if unread is not None:
            results.append(
                (
                    False,
                    f"{host} could not be looked up and {unread} could not be read, "
                    "so neither way of telling whether this container has a route off it was "
                    "available and the bound is unverified",
                )
            )
        elif not has_default_route(table, ipv6_table):
            results.append(
                (
                    True,
                    f"{host} {lookup}, and the routing table names no default "
                    "route: there is no route round the proxy to take",
                )
            )
        else:
            results.append(
                (
                    False,
                    f"{host} could not be looked up, and this container has a default route: "
                    "it has a way off its own subnets, and whether that reaches round the "
                    "proxy is unverified",
                )
            )
    else:
        results.append(
            (
                False,
                f"{host}:{port} was not settled: a connection never left this container, "
                "the lookup could not be made, or the budget ran out with addresses of "
                f"{host} still undialled -- so whether a public name routes round the proxy "
                "is unverified",
            )
        )
    return results


def peer_addresses(proxy: str) -> dict[str, str]:
    """On-link addresses that are this compose project, and what each one is.

    An address here is not dialled, because reaching it is the bound working rather than a way
    round it, and the line it prints says which peer it was.

    * **This container's own.** Reaching itself establishes nothing either way.
    This is also the one input that can switch the half off: an operator who pointed
    ``HTTPS_PROXY`` at a proxy on the host would exempt that host address from being dialled.
    The compose file points it at `egress`, and nothing here reads a proxy the operator did not
    set; it is named so that a reader knows the exemption follows the variable.

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

    An IPv6 table that is there and will not be read is a third case, and it is a failure of its
    own rather than a reason to report nothing: the IPv4 candidates are still worth dialling, and
    the family that could not be read is named as unverified beside them (#42). A kernel with no
    IPv6 is not that case -- there is no table, and nothing is on-link over a family that is not
    there.

    The mirror of that is deliberately *not* symmetric: where `/proc/net/route` is the file that
    will not be read, this returns on the spot and does not go looking at the second table. The
    verdict is the same either way -- a half that could not enumerate what is on-link is
    unverified, and no address it went on to dial could turn that into a pass -- and the one
    thing an operator does about it is the same too: find out why the kernel's own routing table
    is unreadable in this container. Probing a second family underneath that answer would add
    lines to a report whose first line already says the enumeration failed.
    """
    unreadable: list[tuple[bool, str]] = []
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
        ipv6_table = read_ipv6_route_table()
        if ipv6_table is None:
            unreadable.append(
                (
                    False,
                    f"{IPV6_ROUTE_TABLE_PATH} is there and could not be read, so the addresses "
                    "this container can reach on-link over IPv6 are unknown and the bound is "
                    "unverified for that family",
                )
            )
        candidates = on_link_addresses(table, ipv6_table)
    else:
        candidates = list(on_link)
    if not candidates:
        source = "the routing tables" if on_link is None else "the candidates passed in"
        return unreadable + [
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
        return unreadable + accounted
    return (
        unreadable
        + accounted
        + check_on_link(to_probe, ports=ports, probe=probe, timeout_s=timeout_s)
    )


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
            print(format_result(ok, line))
        # `all([])` is True, so "no results" would exit 0 -- the one shape that turns CI's
        # gate into a no-op. A check that asserted nothing has not established the bound.
        return 0 if results and all(ok for ok, _ in results) else 1
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
