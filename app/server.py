"""The dashboard's own HTTP server, and the bound on what one peer can make it cost.

This is what the image launches, so it is where the bound belongs: this process is the one that
holds both OAuth tokens and does the parsing, and it is reached by more than one route. `ingress`
publishes the port and reads as far as the end of the *first* request head on each connection it
accepts; it can bound nothing after that, and nothing at all on a connection made to
``codervis:8000`` directly, which any process on the bridge can open (#43).

uvicorn's defaults leave both unbounded. `--http auto` picks httptools where it is installed,
which enforces no limit on a request head at all, and neither a connection ceiling nor a timer is
armed until a response has been sent. So this module names three things:

- **h11**, because the head limit is h11's. uvicorn exposes it as
  ``h11_max_incomplete_event_size`` and h11 applies it to every request on a connection, not just
  the first: an oversized or drip-fed second head is cut off with ``400 Bad Request`` and the
  connection closed.
- **the same 16 KiB `ingress` uses**, so the two layers agree on what a head may be. A head over
  it is refused by the relay with 431 before the dashboard is dialled, and by the server with 400
  when it never passed the relay.
- **a connection and task ceiling above `ingress`'s 256**, so a peer that reaches the published
  port cannot exhaust it and an SSE stream per browser tab is never what refuses one. uvicorn
  answers ``503 Service Unavailable`` to a request that arrives while the count is at the ceiling.

What is left unbounded on purpose is *time*: nothing arms a deadline once a head has begun, because
an SSE response lasts as long as the browser tab. A peer that drips a head therefore holds one
counted slot for as long as it likes -- the same cost as an open tab -- and cannot spend more than
16 KiB of memory doing it.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

import uvicorn

APP = "app.main:app"
# The container's own interfaces: the relay is what publishes a port on the host, and
# `DASHBOARD_BIND` is what decides which of the host's addresses that is.
DEFAULT_BIND = "0.0.0.0"
DEFAULT_PORT = 8000

# h11 rather than uvicorn's `auto`: `auto` prefers httptools, which caps a request head at
# nothing. Naming the implementation is part of the bound, not a performance preference.
HTTP_PROTOCOL = "h11"
# The largest complete request head the parser will assemble, on every request of every
# connection. Deliberately the same as `ingress.MAX_REQUEST_HEAD_BYTES`; widening either is a
# change made here and in the documents that state it.
MAX_REQUEST_HEAD_BYTES = 16 * 1024
# Above `ingress.MAX_CONNECTIONS` (256), so the relay runs out of slots before the server does and
# a connection that skipped the relay is still counted. Each open browser tab holds one connection
# and one streaming task, and both count against this.
MAX_CONNECTIONS = 320


def build_config(
    app: object = APP,
    *,
    bind: str = DEFAULT_BIND,
    port: int = DEFAULT_PORT,
    max_request_head_bytes: int = MAX_REQUEST_HEAD_BYTES,
    max_connections: int = MAX_CONNECTIONS,
) -> uvicorn.Config:
    """The server configuration the image runs. Building it imports nothing of the app."""
    return uvicorn.Config(
        app,
        host=bind,
        port=port,
        http=HTTP_PROTOCOL,
        h11_max_incomplete_event_size=max_request_head_bytes,
        limit_concurrency=max_connections,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.server", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--bind", default=DEFAULT_BIND)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        print("server: --port must be between 0 and 65535", file=sys.stderr)
        return 1
    uvicorn.Server(build_config(bind=args.bind, port=args.port)).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
