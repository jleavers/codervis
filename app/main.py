from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import degrade
from .budget import env_float
from .claude_activity import ClaudeActivityReader
from .claude_activity import reader_from_env as claude_activity_reader_from_env
from .codex_activity import CodexActivityReader
from .codex_activity import reader_from_env as codex_activity_reader_from_env
from .codex_quota import CodexLiveQuotaClient, CodexLiveQuotaError
from .codex_quota import client_from_env as codex_client_from_env
from .quota import LiveQuotaClient, LiveQuotaError, client_from_env
from .refresh import (
    SourceRefresher,
    SourceStale,
    stale_after,
    wait_for_first_publish,
)

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
REFRESH_SECONDS = max(1, int(os.environ.get("REFRESH_INTERVAL_SECONDS", "5")))

# How often each source is read, in its own background thread. This is the
# *only* thing that decides how often a credential is read or an upstream call
# is made: no number of requests, SSE connections or ticks can add one.
# QUOTA_CACHE_TTL_SECONDS is honoured as the old name for the same number,
# because that is what it always meant -- the minimum gap between fetches.
QUOTA_REFRESH_SECONDS = env_float(
    "QUOTA_REFRESH_INTERVAL_SECONDS", 30.0, fallback="QUOTA_CACHE_TTL_SECONDS"
)
CLAUDE_ACTIVITY_REFRESH_SECONDS = env_float(
    "CLAUDE_ACTIVITY_REFRESH_INTERVAL_SECONDS",
    5.0,
    fallback="CLAUDE_ACTIVITY_CACHE_TTL_SECONDS",
)
CODEX_ACTIVITY_REFRESH_SECONDS = env_float(
    "CODEX_ACTIVITY_REFRESH_INTERVAL_SECONDS",
    5.0,
    fallback="CODEX_ACTIVITY_CACHE_TTL_SECONDS",
)
# A bounded wait at startup only, so the first page load is served from real
# data rather than the placeholder. It happens once and does not scale with
# requests; if a source is slower than this the app starts anyway and that
# provider shows unavailable until its first refresh lands.
STARTUP_REFRESH_WAIT_SECONDS = env_float("STARTUP_REFRESH_WAIT_SECONDS", 2.0)

# Which clients this dashboard serves. There is no login, so reachability is the whole of its
# access control, and the operator names it rather than inheriting it: DASHBOARD_BIND in the
# compose file decides which host interface the port appears on, and this list decides which
# names a browser may use once it gets there (#15). The two are set together, because an
# address alone is not enough: an instance published on loopback still answers a page that
# resolved its own name to 127.0.0.1, and the browser counts that answer as same-origin
# (DNS rebinding). Entries are exact names; `*` serves any name, and means the operator has
# put something else in front that decides who may ask.
ALLOWED_HOSTS_ENV = "DASHBOARD_ALLOWED_HOSTS"
DEFAULT_ALLOWED_HOSTS = "localhost,127.0.0.1,::1"
ANY_HOST = "*"


def _normalise_host(value: str) -> str:
    """A `Host` value or allow-list entry as a bare name: lowercase, no port, no brackets.

    An address the caller spelled in a way no client would (an unclosed bracket) normalises
    to the empty string, which matches nothing, rather than to whatever is inside it.
    """
    host = value.strip().lower()
    if host.startswith("["):
        # [::1] or [::1]:8765 -- the brackets are what tell a port from the address.
        address, closed, _ = host.partition("]")
        host = address[1:] if closed else ""
    elif host.count(":") == 1:
        # name:port. A bare IPv6 literal has more colons and no port, so it is left whole.
        host = host.partition(":")[0]
    return host.rstrip(".")


def parse_allowed_hosts(text: str | None) -> frozenset[str]:
    """The setting as a set of names. Unset or empty means the loopback default."""
    entries = (text or "").replace(",", " ").split()
    if not entries:
        entries = DEFAULT_ALLOWED_HOSTS.replace(",", " ").split()
    names = set()
    for entry in entries:
        name = _normalise_host(entry)
        if ANY_HOST in name and name != ANY_HOST:
            # `*.example.com` is what several other servers spell a subdomain wildcard, and
            # it would sit here matching nothing at all: an operator locked out of their own
            # dashboard by a setting that reads as though it should work. Say so. The lock-out
            # stays -- the entry is dropped rather than widened into something unasked for.
            log.warning(
                "host_allowlist_entry_ignored entry=%r: names are matched whole, and only "
                "%r on its own serves every name",
                entry,
                ANY_HOST,
            )
            continue
        if name:
            names.add(name)
    return frozenset(names)


def host_allowed(header: str | None, allowed: frozenset[str]) -> bool:
    """Whether a request carrying this `Host` is one the operator asked to serve."""
    if ANY_HOST in allowed:
        return True
    if not header:
        return False
    return _normalise_host(header) in allowed


class HostAllowlist:
    """Refuses every request whose `Host` the operator did not name.

    Wrapped around the whole application rather than applied per route, so that `/static`
    and `/healthz` are covered along with the API. Pure ASGI, like Starlette's own
    `TrustedHostMiddleware`, because `BaseHTTPMiddleware` would come between the SSE response
    in `stream()` and its client. This is not that middleware because the differences are the
    point here: a name matches whole and never by subdomain, a bracketed IPv6 address is
    understood, two `Host` headers are refused rather than resolved, and the answer is 403
    with the setting to change, not a bare 400.
    """

    def __init__(self, app, allowed: frozenset[str]) -> None:
        self.app = app
        self.allowed = allowed

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket"):
            sent = [value for name, value in scope["headers"] if name == b"host"]
            # Exactly one, or none: two `Host` headers are a request two hops need not agree
            # about, so there is no value here to check.
            header = sent[0].decode("latin-1") if len(sent) == 1 else None
            if not host_allowed(header, self.allowed):
                await self._refuse(scope, receive, send)
                return
        await self.app(scope, receive, send)

    async def _refuse(self, scope, receive, send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        # The refused name is not echoed back: it is the caller's own text, and this body
        # reaches a browser.
        response = PlainTextResponse(
            f"Host not served by this dashboard. Add it to {ALLOWED_HOSTS_ENV}.\n",
            status_code=403,
        )
        await response(scope, receive, send)


ALLOWED_HOSTS = parse_allowed_hosts(os.environ.get(ALLOWED_HOSTS_ENV))


def _enabled(name: str) -> bool:
    return os.environ.get(name, "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
        "",
    )


CLAUDE_ENABLED = _enabled("CLAUDE_ENABLED")
CODEX_ENABLED = _enabled("CODEX_ENABLED")

_live: LiveQuotaClient = client_from_env()
_claude_activity: ClaudeActivityReader = claude_activity_reader_from_env()
_codex: CodexLiveQuotaClient = codex_client_from_env()
_codex_activity: CodexActivityReader = codex_activity_reader_from_env()

# One refresher per source. The fetch is a lambda rather than a bound method so
# that it resolves the module global at call time: that keeps the sources
# replaceable (tests swap a stub in and call refresh_once()) without the
# refresher holding a stale client.

def _quota_read_seconds(client) -> float:
    """What the *network* part of a healthy quota fetch may cost.

    The total deadline is checked between reads, so a read already in flight
    when it passes still runs to its own per-operation timeout -- hence the
    second term. It is not a hard ceiling: urllib applies that timeout per
    socket operation, so a drip-fed status line or header can spend several
    before `read_capped()` first looks at the deadline. That is not a *healthy*
    fetch, and letting it go stale is the wanted outcome.

    The credential read is deliberately excluded: it has no deadline to add,
    and it is the read this limit exists to catch hanging.
    """
    return client.total_deadline_seconds + client.timeout_seconds


# Each source's staleness limit is built from *its* read budgets, not from the
# cadence alone, because those budgets are operator knobs: raising a deadline
# past two intervals would otherwise start reporting a working source
# `unavailable`, and the staleness limit is the one number here with no knob
# of its own.
_claude_quota_source: SourceRefresher = SourceRefresher(
    "claude-quota",
    lambda: _live.get(),
    QUOTA_REFRESH_SECONDS,
    stale_after_seconds=stale_after(QUOTA_REFRESH_SECONDS, _quota_read_seconds(_live)),
)
_claude_activity_source: SourceRefresher = SourceRefresher(
    "claude-activity",
    lambda: _claude_activity.snapshot(),
    CLAUDE_ACTIVITY_REFRESH_SECONDS,
    stale_after_seconds=stale_after(
        CLAUDE_ACTIVITY_REFRESH_SECONDS, _claude_activity.scan_deadline_seconds
    ),
)
_codex_quota_source: SourceRefresher = SourceRefresher(
    "codex-quota",
    lambda: _codex.get(),
    QUOTA_REFRESH_SECONDS,
    stale_after_seconds=stale_after(QUOTA_REFRESH_SECONDS, _quota_read_seconds(_codex)),
)
_codex_activity_source: SourceRefresher = SourceRefresher(
    "codex-activity",
    lambda: _codex_activity.snapshot(),
    CODEX_ACTIVITY_REFRESH_SECONDS,
    stale_after_seconds=stale_after(
        CODEX_ACTIVITY_REFRESH_SECONDS, _codex_activity.scan_deadline_seconds
    ),
)
_SOURCES: tuple[SourceRefresher, ...] = (
    _claude_quota_source,
    _claude_activity_source,
    _codex_quota_source,
    _codex_activity_source,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Every payload-feeding read lives and dies with the app, not with a request."""
    for source in _SOURCES:
        source.start()
    await asyncio.to_thread(
        wait_for_first_publish, _SOURCES, STARTUP_REFRESH_WAIT_SECONDS
    )
    try:
        yield
    finally:
        for source in _SOURCES:
            source.stop()


app = FastAPI(title="Codervis", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
# Once, around everything: added here rather than per route so that a route added later is
# behind it by default.
app.add_middleware(HostAllowlist, allowed=ALLOWED_HOSTS)
templates = Jinja2Templates(directory=BASE_DIR / "templates")


# ─── The payload schema ──────────────────────────────────────────────────────
#
# One schema, shared by both providers, for every value that reaches the
# payload. The quota clients read undocumented endpoints and the activity
# readers read files another program writes, so everything they hand back is
# checked against this before it is served.

# A window percentage is a finite float in [0, 100] or null. The live APIs
# already report that scale; nothing here multiplies by 100.
MIN_PERCENT = 0.0
MAX_PERCENT = 100.0
PERCENT_DECIMALS = 2

# A date in the payload is an ISO-8601 string inside this range, or null. A
# value outside it can only be upstream noise or a clock fault.
MIN_DATE = datetime(1970, 1, 1, tzinfo=timezone.utc)
MAX_DATE = datetime(2100, 1, 1, tzinfo=timezone.utc)

# A free-form string in the payload (a plan name, a window detail) is bounded
# and printable, or null. The character class covers C0 and C1 controls and the
# two Unicode line terminators: a line terminator would end a JavaScript line
# inside the <script> block, and a control character can forge a line in
# whatever reads the payload.
MAX_TEXT_CHARS = 120
_UNPRINTABLE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")


class _WindowSpec(NamedTuple):
    """One gauge: where it goes, what it is called, and where its value is."""

    slot: str  # the gauge's DOM-id slot, and its `name` in the payload
    heading: str  # the display heading
    attribute: str  # the attribute to read on the client's snapshot
    # An optional gauge isolates: a malformed value shows no value there and
    # does not make the provider's section unavailable.
    optional: bool


_CLAUDE_WINDOWS = (
    _WindowSpec("five_hour", "5-Hour Window", "five_hour", optional=False),
    _WindowSpec("seven_day", "Weekly Window", "seven_day", optional=False),
    _WindowSpec(
        "seven_day_fable", "Weekly Window (Fable)", "seven_day_fable", optional=True
    ),
)
_CODEX_WINDOWS = (
    _WindowSpec("five_hour", "5-Hour Window", "five_hour", optional=False),
    _WindowSpec("seven_day", "Weekly Window", "seven_day", optional=False),
)


def _recorded(error: Exception | None, pending: str) -> Exception:
    """The failure a refresher recorded, ready to raise here.

    Its traceback is cleared first. The same exception object is re-raised on
    every payload build for as long as its snapshot stands, and each raise
    appends a frame to the *same* object -- so the chain would grow without
    bound in precisely the case this design exists to survive: a source that
    published a failure and then wedged is never republished (`current()`
    leaves an already-failed snapshot alone), so nothing ever replaces it.
    Nothing reads the traceback: the log records the type name and the
    classification comes from `isinstance`.

    `is not None` rather than `or`, because the snapshot's own flag is `ok`;
    whether an exception happens to be truthy is beside the point.
    """
    if error is not None:
        return error.with_traceback(None)
    return _NotPublished(pending)


class _NotPublished(Exception):
    """A source has not published an outcome yet.

    Its refresher is started by the lifespan and the app waits briefly for a
    first publish, so this is the gap before that lands (or a source slower
    than the wait). It is "no data yet", not a fault in what upstream sent.
    """


class _SchemaError(Exception):
    """A value a client returned is outside the schema declared above."""


# ─── The boundary ────────────────────────────────────────────────────────────
#
# Every independently sourced part of the payload — each provider's quota
# section, the optional Fable gauge, each provider's last_activity — is
# produced here and nowhere else. The clients and readers are treated as
# untrusted: everything they raise is caught, everything they return is
# checked, and the result is either a value that matches the schema or that
# part's own degraded state. No exception, out-of-schema value or exception
# text gets past this point, so _build_payload(), the serializer and the log
# see only values the schema allows.


def _percent(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        # A bool is an int in Python, so `true` would read as a live 1.0%.
        raise _SchemaError("percent is not a number")
    try:
        parsed = float(value)
    except (OverflowError, ValueError) as e:
        raise _SchemaError("percent is not representable as a float") from e
    if not math.isfinite(parsed):
        raise _SchemaError("percent is not finite")
    # Range-check the value that is actually served. Rounding first keeps a
    # float artefact a hair outside the range — 100.0 - -1e-9 upstream, or
    # 100.004 — from blacking the whole card out for a value that renders as a
    # legal 100.0, while a genuinely out-of-range 600.0 is still refused.
    # `+ 0.0` normalises -0.0 to 0.0. round(-0.0001, 2) is -0.0, which passes
    # the range check (-0.0 == 0.0) but renders differently on each side: the
    # template's "%.1f" gives "-0.0%" while JavaScript's toFixed(1) gives
    # "0.0", so the first paint and the first SSE update would disagree.
    served = round(parsed, PERCENT_DECIMALS) + 0.0
    if not MIN_PERCENT <= served <= MAX_PERCENT:
        raise _SchemaError("percent is outside the declared range")
    return served


def _iso(value: object) -> str | None:
    """An in-range datetime as an ISO-8601 string; anything else as null."""
    if not isinstance(value, datetime):
        return None
    try:
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        if not MIN_DATE <= moment <= MAX_DATE:
            return None
        return moment.isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _text(value: object) -> str | None:
    """A bounded, printable, encodable string; anything else as null."""
    if not isinstance(value, str) or not value:
        return None
    # A lone surrogate cannot be encoded as UTF-8 at all, and a control
    # character can forge a line in whatever reads the payload. Neither can be
    # part of a real plan name.
    cleaned = value.encode("utf-8", "replace").decode("utf-8", "replace")
    cleaned = _UNPRINTABLE.sub(" ", cleaned)[:MAX_TEXT_CHARS].strip()
    return cleaned or None


def _degrade_code(exc: Exception, declared: type[Exception]) -> str:
    """The fixed classification for a failure — never str(exc), never a repr.

    source_error is served unauthenticated, and an exception raised while the
    request was being built carries the bearer token, so nothing from the
    exception itself may be quoted. A failure the client declared reports its
    own classification; anything else is an internal error.
    """
    if isinstance(exc, _SchemaError):
        return degrade.SHAPE
    if isinstance(exc, SourceStale):
        return degrade.STALE
    if isinstance(exc, _NotPublished):
        return degrade.UNCLASSIFIED
    if isinstance(exc, declared):
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code in degrade.SERVABLE:
            return code
        return degrade.UNCLASSIFIED
    return degrade.INTERNAL


# The last classification logged for each source, so a source that stays broken
# is reported once rather than on every SSE frame. A dashboard is allowed to
# degrade visibly, and a browser asks for a fresh payload every few seconds.
_logged_degrade: dict[str, str] = {}


def _log_degraded(source: str, code: str, exc: Exception) -> None:
    """Report a degraded source, carrying no text the exception supplied.

    The classification and the exception's type name are enough to tell a
    configuration problem from a dashboard bug. The exception's own message is
    not logged, and neither is a traceback: the exception raised for a malformed
    header quotes the whole header value, which is the bearer token.
    """
    signature = f"{code}/{type(exc).__name__}"
    if _logged_degrade.get(source) == signature:
        return
    _logged_degrade[source] = signature
    log.warning(
        "degraded source=%s classification=%s exception=%s",
        source,
        code,
        type(exc).__name__,
    )


def _log_recovered(source: str) -> None:
    if _logged_degrade.pop(source, None) is not None:
        log.info("recovered source=%s", source)


def _unreported_window(spec: _WindowSpec) -> dict:
    return {
        "name": spec.slot,
        "label": spec.heading,
        "percent": None,
        "resets_at": None,
        "detail": None,
    }


def _window_dict(spec: _WindowSpec, window: object) -> dict:
    if window is None:
        # An omitted upstream window stays live with no value.
        return _unreported_window(spec)
    try:
        percent = _percent(getattr(window, "percent", None))
    except _SchemaError:
        if not spec.optional:
            raise
        # A malformed Fable entry shows no value and does not make the Claude
        # section unavailable.
        percent = None
    return {
        "name": spec.slot,
        "label": spec.heading,
        "percent": percent,
        "resets_at": _iso(getattr(window, "resets_at", None)),
        "detail": _text(getattr(window, "detail", None)),
    }


def _activity_fields(provider: str, source: SourceRefresher) -> tuple[str | None, bool]:
    """Each provider's last_activity reading, degrading on its own.

    An activity read is a separate source from the quota call — it walks local
    transcript and session files — so a fault in it means "no reading" and
    never takes the provider's quota section down with it.

    The scan itself ran in the refresher, off the event loop; this only reads
    what it published. A scan that failed, or one that stopped being refreshed,
    arrives here as a not-ok snapshot and reads as "no reading" — the same
    answer, reached without doing any I/O in a request.
    """
    published = source.current()
    try:
        if not published.ok:
            raise _recorded(published.error, "activity source has not published")
        snapshot = published.value
        fields = (
            _iso(getattr(snapshot, "last_activity", None)),
            bool(getattr(snapshot, "data_root_exists", False)),
        )
    except Exception as exc:
        _log_degraded(f"{provider}.last_activity", degrade.ACTIVITY, exc)
        return None, False
    _log_recovered(f"{provider}.last_activity")
    return fields


def _provider_section(
    *,
    provider: str,
    enabled: bool,
    source: SourceRefresher,
    activity_source: SourceRefresher,
    windows: tuple[_WindowSpec, ...],
    plan_attr: str,
    declared_error: type[Exception],
) -> dict:
    """One provider's whole section: the single boundary for that provider.

    It does no I/O. The quota call ran in `source`'s refresher, off the event
    loop and on its own cadence, and what arrives here is the outcome it
    published — success or failure. A recorded failure is re-raised into the
    same `except` a live call used to land in, so every failure is classified,
    logged and reported exactly as before, whichever request happens to be
    looking.
    """
    last_activity, data_root_exists = _activity_fields(provider, activity_source)
    published = source.current()
    try:
        if not published.ok:
            raise _recorded(published.error, "source has not published")
        snapshot = published.value
        if snapshot is None:
            raise _SchemaError("client returned no snapshot")
        built = [
            _window_dict(spec, getattr(snapshot, spec.attribute, None))
            for spec in windows
        ]
        subscription_type = _text(getattr(snapshot, plan_attr, None))
    except Exception as exc:
        code = _degrade_code(exc, declared_error)
        _log_degraded(f"{provider}.quota", code, exc)
        return {
            "enabled": enabled,
            "windows": [_unreported_window(spec) for spec in windows],
            "source": "unavailable",
            "source_error": degrade.message(code),
            "subscription_type": None,
            "last_activity": last_activity,
            "data_root_exists": data_root_exists,
        }
    _log_recovered(f"{provider}.quota")
    return {
        "enabled": enabled,
        "windows": built,
        "source": "live",
        "source_error": None,
        "subscription_type": subscription_type,
        "last_activity": last_activity,
        "data_root_exists": data_root_exists,
    }


def _claude_section() -> dict:
    return _provider_section(
        provider="claude",
        enabled=CLAUDE_ENABLED,
        source=_claude_quota_source,
        activity_source=_claude_activity_source,
        windows=_CLAUDE_WINDOWS,
        plan_attr="subscription_type",
        declared_error=LiveQuotaError,
    )


def _codex_section() -> dict:
    return _provider_section(
        provider="codex",
        enabled=CODEX_ENABLED,
        source=_codex_quota_source,
        activity_source=_codex_activity_source,
        windows=_CODEX_WINDOWS,
        plan_attr="plan_type",
        declared_error=CodexLiveQuotaError,
    )


def _build_payload() -> dict:
    """Assemble the payload from published snapshots only. Does no I/O.

    This is what makes the number of requests, and the number of SSE
    connections, irrelevant to how much upstream traffic the dashboard sends.
    """
    now = datetime.now(timezone.utc)
    return {
        "claude": _claude_section(),
        "codex": _codex_section(),
        "server_time": now.isoformat(),
    }


# ─── One serialized form ─────────────────────────────────────────────────────


def _internal_error_payload() -> dict:
    """A fully degraded payload built from literals only."""
    message = degrade.message(degrade.INTERNAL)

    def section(enabled: bool, windows: tuple[_WindowSpec, ...]) -> dict:
        return {
            "enabled": enabled,
            "windows": [_unreported_window(spec) for spec in windows],
            "source": "unavailable",
            "source_error": message,
            "subscription_type": None,
            "last_activity": None,
            "data_root_exists": False,
        }

    return {
        "claude": section(CLAUDE_ENABLED, _CLAUDE_WINDOWS),
        "codex": section(CODEX_ENABLED, _CODEX_WINDOWS),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


def _payload_json(payload: dict) -> str:
    """The payload's one serialized form, shared by all three consumers.

    Strict: NaN and Infinity are refused rather than written as the JavaScript
    literals `NaN`/`Infinity`, the output is ASCII, and a payload that cannot be
    encoded as UTF-8 at all is refused outright. /api/usage, the SSE frames and the
    template's initial payload all serve this exact string, so they cannot
    disagree about a value.
    """
    try:
        serialized = json.dumps(
            payload, allow_nan=False, ensure_ascii=True, separators=(",", ":")
        )
        # `ensure_ascii` escapes a lone surrogate to \udXXX rather than refusing
        # it, and json.loads turns that back into one, so the ASCII form alone
        # does not prove the payload can be encoded. Prove it, so that every
        # consumer of this payload — including the template, which parses this
        # string back into its render context — is working with a document that
        # can reach a response.
        json.dumps(payload, allow_nan=False, ensure_ascii=False).encode("utf-8")
        _log_recovered("payload")
        return serialized
    except (TypeError, ValueError, RecursionError) as exc:
        # The boundary above is meant to make this unreachable. If it is ever
        # reached, serve a degraded payload instead of a 500 — but say so, or
        # both cards read "internal error" with nothing in the log to tell a
        # serializer fault from a genuine dual-provider outage. Type name only,
        # for the same reason the boundary logs no message.
        _log_degraded("payload", degrade.INTERNAL, exc)
        return json.dumps(
            _internal_error_payload(),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
        )


# `</script>` inside a JSON string would end the block early; the payload is
# ASCII by then, so these three characters are the whole risk.
_SCRIPT_UNSAFE = str.maketrans({"<": "\\u003c", ">": "\\u003e", "&": "\\u0026"})


def _payload_script_json(payload_json: str) -> str:
    """The same serialized form, safe to embed in a <script> block."""
    return payload_json.translate(_SCRIPT_UNSAFE)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    payload_json = _payload_json(_build_payload())
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            # The server-rendered gauges and the payload the browser picks up
            # are the same document, so the first paint cannot disagree with
            # the first SSE frame.
            "data": json.loads(payload_json),
            "payload_json": _payload_script_json(payload_json),
            "refresh_seconds": REFRESH_SECONDS,
        },
    )


@app.get("/api/usage")
async def api_usage() -> Response:
    return Response(
        content=_payload_json(_build_payload()),
        media_type="application/json",
    )


@app.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    async def event_gen():
        while True:
            if await request.is_disconnected():
                return
            yield f"data: {_payload_json(_build_payload())}\n\n"
            await asyncio.sleep(REFRESH_SECONDS)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


def _data_root_flags() -> dict:
    """The `stat()`s behind `/healthz`. Blocking, so it is called off the loop."""
    claude_root = _claude_activity.data_dir.exists()
    return {
        "data_root_exists": claude_root,
        "claude_enabled": CLAUDE_ENABLED,
        "claude_credentials_present": _live.credentials_path.exists(),
        "claude_activity_data_root_exists": claude_root,
        "codex_data_root_exists": _codex_activity.data_dir.exists(),
        "codex_enabled": CODEX_ENABLED,
        "codex_credentials_present": _codex.credentials_path.exists(),
    }


@app.get("/healthz")
async def healthz() -> dict:
    """Whether the data this dashboard needs is where it was told to look.

    The four `exists()` calls are the one place left that touches the bind
    mounts from a request. They go to a thread because they are `stat()`s on
    `/data/claude` and `/data/codex`, the mounts everything else about this
    app now reads only in a refresher: a hung mount would otherwise block the
    event loop here and stall every other route -- including `/api/usage`,
    which does no I/O of its own. A thread cannot cancel a wedged `stat()`
    either, but it wedges alone.
    """
    return {"ok": True, **await asyncio.to_thread(_data_root_flags)}
