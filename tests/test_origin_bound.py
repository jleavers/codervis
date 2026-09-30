"""What may run in the dashboard's origin, and what the dashboard offers to load (#104).

Two things reached this origin by name rather than by content. FastAPI registered
`/openapi.json`, `/docs`, `/docs/oauth2-redirect` and `/redoc` by default although nothing
here uses them, and the two HTML ones load `swagger-ui-dist@5` and `redoc@2` from a CDN with
no integrity attribute. And no response set a Content-Security-Policy, so a script that got
in from anywhere ran with `/api/usage` and `/api/stream` -- usage, plan tier, `last_activity`
-- readable and sendable anywhere.

Both halves are pinned here as *allow-lists written in this file*: the routes the app serves,
and the directives and sources the policy is made of. A check that merely looked for the
absence of `jsdelivr`, or asserted `main.CSP_DIRECTIVES == main.CSP_DIRECTIVES`, would pass
over the next origin somebody adds -- which is the shape `AGENTS.md` forbids and #78 is about.
"""

from __future__ import annotations

import os
import re

import pytest
from fastapi.testclient import TestClient

from app import main

LOOPBACK = "http://127.0.0.1:8765"

#: The paths the dashboard answers, stated here rather than read back off the app. A route
#: added later has to be added here too, which is the point: `/docs` was registered by a
#: default nobody chose.
SERVED_PATHS = frozenset({"/", "/api/usage", "/api/stream", "/healthz", "/static"})

#: The four FastAPI registers unless told not to. Each loads, or describes, code this project
#: does not ship and does not use.
UNDOCUMENTED_ROUTES = ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")

#: The policy, restated. `AGENTS.md`: state the value here and assert the module still equals
#: it, so that widening the policy is one line a reviewer reads rather than nothing at all.
POLICY = (
    ("default-src", ("'none'",)),
    ("script-src", ("'self'",)),
    ("style-src", ("'self'",)),
    ("img-src", ("'self'",)),
    ("connect-src", ("'self'",)),
    ("base-uri", ("'none'",)),
    ("form-action", ("'none'",)),
    ("frame-ancestors", ("'none'",)),
)

#: The only source expressions this origin may name. Not a list of CDNs to refuse: a
#: deny-list would be a list of the hosts somebody already thought of, and the one that gets
#: added is the fourth. A nonce is admitted by shape, since its value is per response.
PERMITTED_SOURCES = frozenset({"'none'", "'self'"})
NONCE_SOURCE = re.compile(r"^'nonce-[A-Za-z0-9+/_-]+={0,2}'$")

#: Every path a response is asked for below, plus one the app does not route: the policy is
#: set around the whole app, so a 404 carries it as much as a page does.
PATHS = ["/", "/api/usage", "/static/app.js", "/static/style.css", "/healthz", "/no-such-path"]


@pytest.fixture(autouse=True, scope="module")
def _unset_in_this_shell() -> None:
    """The app read the host setting at import, so it cannot be given the default here."""
    if os.environ.get(main.ALLOWED_HOSTS_ENV):
        pytest.skip(f"{main.ALLOWED_HOSTS_ENV} is set in this shell")


def client(base_url: str = LOOPBACK) -> TestClient:
    """A client speaking as a browser on this machine does; `testserver` is not a served host."""
    return TestClient(main.app, base_url=base_url)


def policy_of(response) -> dict[str, tuple[str, ...]]:
    """One response's policy, parsed back into `{directive: sources}`."""
    header = response.headers["content-security-policy"]
    directives: dict[str, tuple[str, ...]] = {}
    for part in header.split(";"):
        name, *sources = part.split()
        assert name not in directives, f"{name} appears twice in the policy"
        directives[name] = tuple(sources)
    return directives


# ─── the routes that are no longer served ────────────────────────────────────


def test_the_app_registers_exactly_the_paths_named_here() -> None:
    """The list, not the four names: a fifth default would be as unasked-for as the four."""
    registered = {getattr(route, "path", None) for route in main.app.routes}
    assert registered == SERVED_PATHS, (
        "the dashboard registers a path this file does not name. Add it here on purpose, or "
        "take it off the app."
    )


@pytest.mark.parametrize("path", UNDOCUMENTED_ROUTES)
def test_no_schema_or_documentation_route_is_served(path: str) -> None:
    """404, and not 200-with-nothing-useful: the route has to be gone, not empty."""
    assert client().get(path).status_code == 404


def test_the_app_publishes_no_schema_at_all() -> None:
    """`openapi_url=None` is what unregisters `/openapi.json`; the three switches are separate.

    Asserted on the app's own settings as well as on the responses above, because a route
    could be shadowed by something else answering 404 first and this says which mechanism is
    in force.
    """
    assert main.app.docs_url is None
    assert main.app.redoc_url is None
    assert main.app.openapi_url is None


def test_the_page_offers_no_origin_but_this_one() -> None:
    """Nothing the browser is told to fetch is off this origin, policy or no policy.

    The policy is what stops a load; this is what says the page never asks for one. Both,
    because an operator on a browser that ignored the header still gets a page that loads
    three files of this project's own.
    """
    body = client().get("/").text
    for match in re.finditer(r'(?:src|href)\s*=\s*"([^"]*)"', body):
        url = match.group(1)
        assert not url.startswith(("http://", "https://", "//")), (
            f"the page loads {url}, which is not this origin"
        )


# ─── the policy ──────────────────────────────────────────────────────────────


def test_the_policy_is_the_one_stated_here() -> None:
    assert main.CSP_DIRECTIVES == POLICY
    assert main.CSP_NONCED_DIRECTIVE == "script-src"


@pytest.mark.parametrize("path", PATHS)
def test_every_response_this_origin_makes_carries_the_policy(path: str) -> None:
    response = client().get(path)

    directives = policy_of(response)
    assert set(directives) == {name for name, _ in POLICY}


def test_the_refusal_of_an_unserved_host_carries_it_too() -> None:
    """The layer is outside `HostAllowlist`, so what bounds the origin does not depend on
    which layer answered."""
    response = client("http://attacker.example").get("/")

    assert response.status_code == 403
    assert set(policy_of(response)) == {name for name, _ in POLICY}


@pytest.mark.parametrize("path", PATHS)
def test_no_response_names_a_source_that_is_not_this_origin(path: str) -> None:
    """The check that bites when somebody adds a CDN back, on the header a browser reads."""
    for name, sources in policy_of(client().get(path)).items():
        for source in sources:
            assert source in PERMITTED_SOURCES or NONCE_SOURCE.match(source), (
                f"{name} names {source}, which is neither this origin nor a nonce"
            )


@pytest.mark.parametrize("path", PATHS)
def test_each_directive_carries_exactly_the_sources_stated_here(path: str) -> None:
    """Per directive, so that `connect-src` gaining a host is not hidden by `script-src`."""
    directives = policy_of(client().get(path))
    for name, sources in POLICY:
        served = directives[name]
        if name == main.CSP_NONCED_DIRECTIVE:
            assert served[:-1] == sources
            assert NONCE_SOURCE.match(served[-1]), served[-1]
        else:
            assert served == sources


def test_the_policy_layer_wraps_the_whole_app_and_is_outside_the_host_check() -> None:
    """Pure ASGI and around everything, for the reasons `HostAllowlist` is: a per-route
    dependency would miss `/static`, and `BaseHTTPMiddleware` would buffer the SSE stream."""
    stack = [layer.cls for layer in main.app.user_middleware]
    assert stack == [main.ContentSecurityPolicy, main.HostAllowlist]


def test_a_streaming_response_is_not_buffered_by_the_layer() -> None:
    """The layer touches `http.response.start` and nothing else.

    `/api/stream` never ends, so this asks the layer directly rather than through a client:
    the body messages it passed on must be the ones it was given, one at a time.
    """
    import asyncio

    sent: list[dict] = []

    async def streaming_app(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for chunk in (b"first", b"second"):
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def send(message) -> None:
        sent.append(message)

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    layer = main.ContentSecurityPolicy(streaming_app)
    asyncio.run(layer({"type": "http", "headers": []}, receive, send))

    bodies = [message for message in sent if message["type"] == "http.response.body"]
    assert [message["body"] for message in bodies] == [b"first", b"second", b""]
    assert [message["more_body"] for message in bodies] == [True, True, False]


def test_a_policy_an_inner_layer_set_is_replaced_and_not_added_to() -> None:
    """Two policy headers are intersected by the browser, so what is in force would depend on
    which layer spoke last. One header, always this one."""
    import asyncio

    sent: list[dict] = []

    async def opinionated_app(scope, receive, send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-security-policy", b"default-src *")],
            }
        )
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def send(message) -> None:
        sent.append(message)

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(
        main.ContentSecurityPolicy(opinionated_app)({"type": "http", "headers": []}, receive, send)
    )

    headers = [value for name, value in sent[0]["headers"] if name == b"content-security-policy"]
    assert len(headers) == 1
    assert b"default-src *" not in headers[0]


# ─── the nonce ───────────────────────────────────────────────────────────────


def test_the_inline_block_carries_the_nonce_from_that_responses_own_header() -> None:
    """The initial payload is the origin's one inline script, and a nonce is what admits it
    without `'unsafe-inline'` -- which would admit an injected `onerror=` too (#78)."""
    response = client().get("/")

    nonce = policy_of(response)["script-src"][-1].removeprefix("'nonce-").removesuffix("'")
    assert nonce
    assert f'<script nonce="{nonce}">' in response.text


def test_no_two_responses_share_a_nonce() -> None:
    """A nonce reused across responses is a nonce an attacker can read off an earlier one."""
    nonces = {policy_of(client().get("/")).get("script-src")[-1] for _ in range(5)}
    assert len(nonces) == 5


def test_the_nonce_is_the_only_inline_script_the_page_has() -> None:
    """One nonced block, and every other script on the page loaded from this origin.

    A second inline block would need a second nonce, and a nonce handed to a block whose
    contents somebody else influences is how a nonce policy is lost.
    """
    body = client().get("/").text
    inline = re.findall(r"<script(?![^>]*\ssrc=)[^>]*>", body)
    assert len(inline) == 1, inline
    assert "nonce=" in inline[0]


def test_the_nonce_reaches_the_handler_through_the_scope() -> None:
    """The one coupling between the layer and a handler, named so it cannot drift."""
    assert main.CSP_NONCE_SCOPE_KEY == "codervis.csp_nonce"
    assert main.CSP_NONCE_BYTES >= 16, "a CSP nonce needs at least 128 bits of entropy"
