"""The relay that publishes the dashboard, whose own container has no route off the host.

Docker ignores a published port on a container whose networks are all ``internal``, so the
dashboard cannot publish its own port without also getting a default route, which would leave
the egress proxy advisory. This relay joins the internal network and an outside one, publishes
the port, and copies bytes to and from the dashboard. It reads nothing, holds no credential and
can reach one address.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from collections.abc import Sequence

from .egress import close, parse_connect_target, reply, response, serve_until_stopped, tunnel

log = logging.getLogger("app.ingress")

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8000
CONNECT_TIMEOUT_S = 10.0
# Each open browser tab holds one SSE connection, so this sits far above any real use while
# still refusing a flood before it runs the process out of descriptors.
MAX_CONNECTIONS = 256


def parse_target(text: str) -> tuple[str, int] | None:
    """``host:port`` of the dashboard, or ``None``."""
    return parse_connect_target(text)


class Relay:
    """Copies each accepted connection to and from one fixed address."""

    def __init__(
        self,
        target_host: str,
        target_port: int,
        *,
        connect_timeout_s: float = CONNECT_TIMEOUT_S,
        max_connections: int = MAX_CONNECTIONS,
    ) -> None:
        self._target = (target_host, target_port)
        self._connect_timeout_s = connect_timeout_s
        self._max_connections = max_connections
        self._recovered_at = max_connections * 3 // 4
        self._accepted = 0
        self._saturated = False

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One client connection. Never raises: a failure costs that connection alone."""
        if self._accepted >= self._max_connections:
            if not self._saturated:
                self._saturated = True
                log.warning(
                    "ingress_connections_exhausted accepted=%d limit=%d",
                    self._accepted,
                    self._max_connections,
                )
            with contextlib.suppress(OSError):
                await reply(writer, response(503, "Service Unavailable", "too many connections\n"))
            await close(writer)
            return
        if self._saturated and self._accepted < self._recovered_at:
            self._saturated = False
        self._accepted += 1
        try:
            await self._serve(reader, writer)
        except OSError as exc:
            log.debug("ingress_connection_failed error=%s: %s", type(exc).__name__, exc)
        except Exception:
            log.exception("ingress_connection_error")
        finally:
            self._accepted -= 1
            await close(writer)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        host, port = self._target
        try:
            async with asyncio.timeout(self._connect_timeout_s):
                target_reader, target_writer = await asyncio.open_connection(host, port)
        except (OSError, TimeoutError) as exc:
            log.warning(
                "ingress_target_failed target=%s:%d error=%s", host, port, type(exc).__name__
            )
            await reply(writer, response(502, "Bad Gateway", "the dashboard is not answering\n"))
            return
        try:
            await tunnel(reader, writer, target_reader, target_writer)
        finally:
            await close(target_writer)


async def serve(
    target_host: str, target_port: int, *, bind: str = DEFAULT_BIND, port: int = DEFAULT_PORT
) -> asyncio.Server:
    """Start the relay and return the server."""
    relay = Relay(target_host, target_port)
    return await asyncio.start_server(relay.handle, bind, port)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.ingress", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--bind", default=DEFAULT_BIND)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--target", required=True, help="the dashboard, as host:port")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    target = parse_target(args.target)
    if target is None:
        print(f"ingress: --target {args.target!r} is not host:port", file=sys.stderr)
        return 1
    if not 0 <= args.port <= 65535:
        print("ingress: --port must be between 0 and 65535", file=sys.stderr)
        return 1
    return asyncio.run(_run(target, bind=args.bind, port=args.port))


async def _run(target: tuple[str, int], *, bind: str, port: int) -> int:
    try:
        server = await serve(*target, bind=bind, port=port)
    except OSError as exc:
        print(f"ingress: cannot listen on {bind}:{port}: {exc}", file=sys.stderr)
        return 1
    log.info("ingress_started bind=%s port=%d target=%s:%d", bind, port, *target)
    return await serve_until_stopped(server)


if __name__ == "__main__":
    sys.exit(main())
