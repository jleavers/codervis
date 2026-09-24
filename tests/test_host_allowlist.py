"""The dashboard serves the hosts its operator named, and nothing else (#15).

There is no login here, so reachability is the whole of the access control, and a `Host`
check is the half of it the service can enforce itself: an instance published on loopback
still answers a page that resolved a name of its own to 127.0.0.1, and the browser counts
that answer as same-origin. These pin the refusal, the names the default accepts, and that
the check sits around the whole app rather than on the routes that existed when it was
written.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

import pytest
from fastapi.testclient import TestClient

from app import main

# Every route the dashboard answers, plus a path it does not: a request refused before
# routing is refused on all of them.
PATHS = ["/", "/api/usage", "/api/stream", "/static/app.js", "/healthz", "/no-such-path"]


@pytest.fixture(autouse=True, scope="module")
def _unset_in_this_shell() -> None:
    """The app read the setting at import, so these cannot be given the default afterwards."""
    if os.environ.get(main.ALLOWED_HOSTS_ENV):
        pytest.skip(f"{main.ALLOWED_HOSTS_ENV} is set in this shell")


def client(host: str) -> TestClient:
    return TestClient(main.app, base_url=f"http://{host}")


@pytest.mark.parametrize("path", PATHS)
def test_a_host_the_operator_did_not_name_is_refused_everywhere(path: str) -> None:
    response = client("attacker.example").get(path)

    assert response.status_code == 403
    # Not 404: the check runs before routing, so a route added later is behind it too.
    assert main.ALLOWED_HOSTS_ENV in response.text
    # The caller's own name is not echoed into a page a browser renders.
    assert "attacker.example" not in response.text


@pytest.mark.parametrize(
    "host",
    ["localhost", "localhost:8765", "LocalHost:8765", "localhost.:8765", "127.0.0.1:8765"],
)
def test_the_default_serves_this_machine(host: str) -> None:
    assert client(host).get("/healthz").status_code == 200


def test_the_default_serves_the_ipv6_loopback_literal() -> None:
    # The brackets and the port are the client's spelling of the same address.
    assert client("[::1]:8765").get("/healthz").status_code == 200


def test_an_empty_host_header_is_refused() -> None:
    response = client("127.0.0.1:8765").get("/healthz", headers={"Host": ""})

    assert response.status_code == 403


def test_a_request_carrying_no_host_header_at_all_is_refused() -> None:
    """HTTP/1.0 has no `Host`, and there is nothing to check against the list."""
    start, body = _first_event("/healthz", headers=[])

    assert start["status"] == 403


def test_two_host_headers_are_refused() -> None:
    """Which one the check reads is exactly the disagreement an attacker is buying."""
    with TestClient(main.app, base_url="http://127.0.0.1:8765") as c:
        request = c.build_request("GET", "/healthz")
        request.headers["Host"] = "127.0.0.1:8765"
        raw = [(k, v) for k, v in request.headers.raw if k.lower() != b"host"]
        raw.extend([(b"host", b"127.0.0.1:8765"), (b"host", b"attacker.example")])
        request.headers = type(request.headers)(raw)

        assert c.send(request).status_code == 403


def test_static_files_are_behind_the_check_too() -> None:
    """A mount is served by its own app, so it has to be covered by the wrapper, not a route."""
    assert client("attacker.example").get("/static/style.css").status_code == 403
    assert client("127.0.0.1:8765").get("/static/style.css").status_code == 200


def test_the_stream_still_streams_for_a_host_that_is_served(monkeypatch) -> None:
    """The check is pure ASGI so that it does not come between SSE and its client.

    Driven as a server would rather than through `TestClient`, which has no way to read
    part of a response that never ends. A wrapper that buffered the body, as
    `BaseHTTPMiddleware` does, would hold the first event back instead of sending it.
    """
    monkeypatch.setattr(main, "_build_payload", lambda: {"server_time": "now"})

    start, body = _first_event("/api/stream", headers=[(b"host", b"127.0.0.1:8765")])

    assert start["status"] == 200
    assert body == b'data: {"server_time": "now"}\n\n'


class _Delivered(Exception):
    """Raised in place of reading the rest of a response that never ends."""


def _first_event(path: str, *, headers: list[tuple[bytes, bytes]]) -> tuple[dict, bytes]:
    """The response start and first body chunk the app sends for one request."""

    async def receive() -> dict:
        nonlocal asked
        if not asked:
            asked = True
            return {"type": "http.request", "body": b"", "more_body": False}
        # Never a disconnect: the client is still reading, which is the case under test.
        await asyncio.Event().wait()

    async def send(message: dict) -> None:
        messages.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            raise _Delivered

    async def drive() -> None:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.1"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8000),
        }
        with contextlib.suppress(_Delivered):
            await main.app(scope, receive, send)

    asked = False
    messages: list[dict] = []
    asyncio.run(asyncio.wait_for(drive(), timeout=10))
    start = next(m for m in messages if m["type"] == "http.response.start")
    chunk = next(m for m in messages if m["type"] == "http.response.body")
    return start, chunk["body"]


def test_the_stream_is_refused_for_a_host_that_is_not() -> None:
    assert client("attacker.example").get("/api/stream").status_code == 403


@pytest.mark.parametrize(
    "setting, expected",
    [
        (None, {"localhost", "127.0.0.1", "::1"}),
        ("", {"localhost", "127.0.0.1", "::1"}),
        ("   ", {"localhost", "127.0.0.1", "::1"}),
        ("dash.example", {"dash.example"}),
        ("a.example, b.example", {"a.example", "b.example"}),
        ("a.example b.example", {"a.example", "b.example"}),
        ("A.Example:8765", {"a.example"}),
        ("[fd00::1]", {"fd00::1"}),
        ("*", {"*"}),
        # An unclosed bracket is no address; it must not read as the name inside it.
        ("[::1", set()),
    ],
)
def test_the_setting_is_read_as_a_set_of_bare_names(setting, expected) -> None:
    assert main.parse_allowed_hosts(setting) == expected


def test_a_subdomain_wildcard_is_dropped_and_reported(caplog) -> None:
    """It matches nothing here, and silently locking the operator out of their own dashboard
    is the worst way to tell them so."""
    with caplog.at_level("WARNING", logger="app.main"):
        allowed = main.parse_allowed_hosts("*.dash.example, dash.example")

    assert allowed == {"dash.example"}
    assert "*.dash.example" in caplog.text


@pytest.mark.parametrize(
    "header, allowed, served",
    [
        ("dash.example", "dash.example", True),
        ("dash.example:8765", "dash.example", True),
        ("dash.example", "dash.example:8765", True),
        ("other.example", "dash.example", False),
        ("sub.dash.example", "dash.example", False),
        ("dash.example.attacker.test", "dash.example", False),
        ("", "dash.example", False),
        (None, "dash.example", False),
        ("anything.example", "*", True),
        (None, "*", True),
        ("192.168.1.5:8765", "192.168.1.5", True),
    ],
)
def test_a_name_matches_exactly_and_a_port_is_ignored(header, allowed, served: bool) -> None:
    assert main.host_allowed(header, main.parse_allowed_hosts(allowed)) is served
