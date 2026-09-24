"""The dashboard's network egress, bounded by an allow-listing ``CONNECT`` proxy.

The dashboard holds two long-lived bearer tokens and needs exactly two hosts. This proxy is the
only route off the host its container has, and it admits only the hosts on its allow-list, so
a redirect, a host override or a compromised dependency cannot carry a token anywhere else.
Two halves, and neither is sufficient alone:

* **The network** makes the proxy unavoidable: docker-compose.yml puts the dashboard on an
  ``internal`` network only, which has no default route.
* **The allow-list** makes the route narrow: ``claude.ai`` and ``chatgpt.com``, extended by
  the operator's ``EGRESS_ALLOW``.

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
import logging
import os
import signal
import socket
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

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


def probe_proxy(
    proxy_url: str, host: str, port: int = DEFAULT_TARGET_PORT, *, timeout_s: float = 10.0
) -> tuple[int, str] | str:
    """``CONNECT`` through the proxy: the status it answered, or a string saying why not.

    Closed the moment the status line is read, so an allowed probe costs the named host one
    accepted TCP connection and no bytes.
    """
    parts = urlsplit(proxy_url if "//" in proxy_url else f"//{proxy_url}")
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
    """Whether a TCP connection to ``host`` leaves this container without the proxy.

    Opened and closed at once; nothing is sent. A name that will not resolve, a refusal and a
    timeout all read as "no route", which is what an internal-only container looks like.
    """
    try:
        with socket.create_connection((host, port), timeout_s):
            return True
    except OSError:
        return False


def check(
    environ: Mapping[str, str],
    *,
    direct: tuple[str, int] = (PROBE_DIRECT_HOST, DEFAULT_TARGET_PORT),
    timeout_s: float = 10.0,
) -> list[tuple[bool, str]]:
    """Both halves of the bound, as seen from inside the dashboard's container.

    The proxy must refuse a name that resolves nowhere, admit every host the live clients are
    configured for, and be the only way out.
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
        results.append((True, f"{host}:{port} unreachable directly: no route round the proxy"))
    return results


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
