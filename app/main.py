from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .budget import env_float
from .claude_activity import ClaudeActivityReader
from .claude_activity import reader_from_env as claude_activity_reader_from_env
from .codex_activity import CodexActivityReader
from .codex_activity import reader_from_env as codex_activity_reader_from_env
from .codex_quota import CodexLiveQuotaClient, CodexLiveQuotaError
from .codex_quota import client_from_env as codex_client_from_env
from .quota import LiveQuotaClient, LiveQuotaError, client_from_env
from .refresh import SourceRefresher, SourceSnapshot, wait_for_first_publish

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
_claude_quota_source: SourceRefresher = SourceRefresher(
    "claude-quota", lambda: _live.get(), QUOTA_REFRESH_SECONDS
)
_claude_activity_source: SourceRefresher = SourceRefresher(
    "claude-activity", lambda: _claude_activity.snapshot(), CLAUDE_ACTIVITY_REFRESH_SECONDS
)
_codex_quota_source: SourceRefresher = SourceRefresher(
    "codex-quota", lambda: _codex.get(), QUOTA_REFRESH_SECONDS
)
_codex_activity_source: SourceRefresher = SourceRefresher(
    "codex-activity", lambda: _codex_activity.snapshot(), CODEX_ACTIVITY_REFRESH_SECONDS
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
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _source_error(snapshot: SourceSnapshot, expected: type[Exception]) -> str:
    """The `source_error` string for a source that is not live.

    The client's own error type is surfaced as before. Anything else is named
    by type only: the refresher has to catch every exception (letting one
    escape would freeze the source), and an exception raised while an upstream
    request is being built can carry the bearer token in its message.
    """
    err = snapshot.error
    if err is None:
        return "waiting for the first refresh"
    if isinstance(err, expected):
        return str(err)
    return f"unexpected {type(err).__name__} while refreshing"


def _activity_fields(snapshot: SourceSnapshot) -> tuple[str | None, bool]:
    """`last_activity` and `data_root_exists` from a published activity scan.

    A failed scan degrades to "no activity known" on its own; it does not take
    the provider's quota section with it.
    """
    scan = snapshot.value if snapshot.ok else None
    if scan is None:
        return None, False
    last = scan.last_activity
    return (last.isoformat() if last else None), bool(scan.data_root_exists)


def _window_dict(name: str, label: str, percent, resets_at, detail=None) -> dict:
    return {
        "name": name,
        "label": label,
        "percent": None if percent is None else round(float(percent), 2),
        "resets_at": resets_at.isoformat() if resets_at else None,
        "detail": detail,
    }


def _claude_section() -> dict:
    last_activity, data_root_exists = _activity_fields(_claude_activity_source.snapshot())
    quota = _claude_quota_source.snapshot()
    if quota.ok:
        live = quota.value
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
            "data_root_exists": data_root_exists,
        }
    return {
        "enabled": CLAUDE_ENABLED,
        "windows": [
            _window_dict("five_hour", "5-Hour Window", None, None),
            _window_dict("seven_day", "Weekly Window", None, None),
            _window_dict("seven_day_fable", "Weekly Window (Fable)", None, None),
        ],
        "source": "unavailable",
        "source_error": _source_error(quota, LiveQuotaError),
        "subscription_type": None,
        "last_activity": last_activity,
        "data_root_exists": data_root_exists,
    }


def _codex_section() -> dict:
    last_activity, data_root_exists = _activity_fields(_codex_activity_source.snapshot())
    quota = _codex_quota_source.snapshot()
    if quota.ok:
        snap = quota.value
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
            "data_root_exists": data_root_exists,
        }
    return {
        "enabled": CODEX_ENABLED,
        "windows": [
            _window_dict("five_hour", "5-Hour Window", None, None),
            _window_dict("seven_day", "Weekly Window", None, None),
        ],
        "source": "unavailable",
        "source_error": _source_error(quota, CodexLiveQuotaError),
        "subscription_type": None,
        "last_activity": last_activity,
        "data_root_exists": data_root_exists,
    }


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
