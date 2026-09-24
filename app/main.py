from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .claude_activity import ClaudeActivityReader
from .claude_activity import reader_from_env as claude_activity_reader_from_env
from .codex_activity import CodexActivityReader
from .codex_activity import reader_from_env as codex_activity_reader_from_env
from .codex_quota import CodexLiveQuotaClient, CodexLiveQuotaError
from .codex_quota import client_from_env as codex_client_from_env
from .quota import LiveQuotaClient, LiveQuotaError, client_from_env

BASE_DIR = Path(__file__).parent
REFRESH_SECONDS = max(1, int(os.environ.get("REFRESH_INTERVAL_SECONDS", "5")))

# Which clients this dashboard serves. There is no login, so reachability is the whole of its
# access control, and the operator names it rather than inheriting it: DASHBOARD_BIND in the
# compose file decides which host interface the port appears on, and this list decides which
# names a browser may use once it gets there (#15). The two are set together, because an
# address alone is not enough: an instance published on loopback still answers a page that
# resolved its own name to 127.0.0.1, and the browser counts that answer as same-origin
# (DNS rebinding). Entries are exact names; `*` serves any name, and means the operator has
# put something else in front that decides who may ask.
log = logging.getLogger("app.main")

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

app = FastAPI(title="Codervis")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
# Once, around everything: added here rather than per route so that a route added later is
# behind it by default.
app.add_middleware(HostAllowlist, allowed=ALLOWED_HOSTS)
templates = Jinja2Templates(directory=BASE_DIR / "templates")

_live: LiveQuotaClient = client_from_env()
_claude_activity: ClaudeActivityReader = claude_activity_reader_from_env()
_codex: CodexLiveQuotaClient = codex_client_from_env()
_codex_activity: CodexActivityReader = codex_activity_reader_from_env()


def _window_dict(name: str, label: str, percent, resets_at, detail=None) -> dict:
    return {
        "name": name,
        "label": label,
        "percent": None if percent is None else round(float(percent), 2),
        "resets_at": resets_at.isoformat() if resets_at else None,
        "detail": detail,
    }


def _claude_section() -> dict:
    activity_snap = _claude_activity.snapshot()
    last_activity = (
        activity_snap.last_activity.isoformat() if activity_snap.last_activity else None
    )
    try:
        live = _live.get()
        fable = getattr(live, "seven_day_fable", None)
        return {
            "enabled": CLAUDE_ENABLED,
            "windows": [
                _window_dict(
                    "five_hour", "5-Hour Window", live.five_hour.percent, live.five_hour.resets_at
                ),
                _window_dict(
                    "seven_day", "Weekly Window", live.seven_day.percent, live.seven_day.resets_at
                ),
                _window_dict(
                    "seven_day_fable",
                    "Weekly Window (Fable)",
                    getattr(fable, "percent", None),
                    getattr(fable, "resets_at", None),
                ),
            ],
            "source": "live",
            "source_error": None,
            "subscription_type": live.subscription_type,
            "last_activity": last_activity,
            "data_root_exists": activity_snap.data_root_exists,
        }
    except LiveQuotaError as e:
        return {
            "enabled": CLAUDE_ENABLED,
            "windows": [
                _window_dict("five_hour", "5-Hour Window", None, None),
                _window_dict("seven_day", "Weekly Window", None, None),
                _window_dict("seven_day_fable", "Weekly Window (Fable)", None, None),
            ],
            "source": "unavailable",
            "source_error": str(e),
            "subscription_type": None,
            "last_activity": last_activity,
            "data_root_exists": activity_snap.data_root_exists,
        }


def _codex_section() -> dict:
    activity_snap = _codex_activity.snapshot()
    last_activity = (
        activity_snap.last_activity.isoformat() if activity_snap.last_activity else None
    )
    try:
        snap = _codex.get()
        return {
            "enabled": CODEX_ENABLED,
            "windows": [
                _window_dict(
                    "five_hour",
                    "5-Hour Window",
                    snap.five_hour.percent,
                    snap.five_hour.resets_at,
                ),
                _window_dict(
                    "seven_day", "Weekly Window", snap.seven_day.percent, snap.seven_day.resets_at
                ),
            ],
            "source": "live",
            "source_error": None,
            "subscription_type": snap.plan_type,
            "last_activity": last_activity,
            "data_root_exists": activity_snap.data_root_exists,
        }
    except CodexLiveQuotaError as e:
        return {
            "enabled": CODEX_ENABLED,
            "windows": [
                _window_dict("five_hour", "5-Hour Window", None, None),
                _window_dict("seven_day", "Weekly Window", None, None),
            ],
            "source": "unavailable",
            "source_error": str(e),
            "subscription_type": None,
            "last_activity": last_activity,
            "data_root_exists": activity_snap.data_root_exists,
        }


def _build_payload() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "claude": _claude_section(),
        "codex": _codex_section(),
        "server_time": now.isoformat(),
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "data": _build_payload(),
            "refresh_seconds": REFRESH_SECONDS,
        },
    )


@app.get("/api/usage")
async def api_usage() -> JSONResponse:
    return JSONResponse(_build_payload())


@app.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    async def event_gen():
        import json

        while True:
            if await request.is_disconnected():
                return
            yield f"data: {json.dumps(_build_payload())}\n\n"
            await asyncio.sleep(REFRESH_SECONDS)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "ok": True,
        "data_root_exists": _claude_activity.data_dir.exists(),
        "claude_enabled": CLAUDE_ENABLED,
        "claude_credentials_present": _live.credentials_path.exists(),
        "claude_activity_data_root_exists": _claude_activity.data_dir.exists(),
        "codex_data_root_exists": _codex_activity.data_dir.exists(),
        "codex_enabled": CODEX_ENABLED,
        "codex_credentials_present": _codex.credentials_path.exists(),
    }
