from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .claude_activity import ClaudeActivityReader
from .claude_activity import reader_from_env as claude_activity_reader_from_env
from .codex_activity import CodexActivityReader
from .codex_activity import reader_from_env as codex_activity_reader_from_env
from .codex_quota import CodexLiveQuotaClient, CodexLiveQuotaError
from .codex_quota import client_from_env as codex_client_from_env
from .copilot_activity import CopilotActivityReader
from .copilot_activity import reader_from_env as copilot_activity_reader_from_env
from .copilot_quota import (
    CopilotBillingQuotaClient,
    CopilotLiveQuotaClient,
    CopilotLiveQuotaError,
)
from .copilot_quota import client_from_env as copilot_client_from_env
from .cursor_activity import CursorActivityReader
from .cursor_activity import reader_from_env as cursor_activity_reader_from_env
from .cursor_quota import CursorLiveQuotaClient, CursorLiveQuotaError
from .cursor_quota import client_from_env as cursor_client_from_env
from .gemini_activity import GeminiActivityReader
from .gemini_activity import reader_from_env as gemini_activity_reader_from_env
from .gemini_quota import GeminiLiveQuotaClient, GeminiLiveQuotaError
from .gemini_quota import client_from_env as gemini_client_from_env
from .quota import LiveQuotaClient, LiveQuotaError, client_from_env

BASE_DIR = Path(__file__).parent
REFRESH_SECONDS = max(1, int(os.environ.get("REFRESH_INTERVAL_SECONDS", "5")))


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
CURSOR_ENABLED = _enabled("CURSOR_ENABLED")
COPILOT_ENABLED = _enabled("COPILOT_ENABLED")
GEMINI_ENABLED = _enabled("GEMINI_ENABLED")

app = FastAPI(title="Codervis")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

_live: LiveQuotaClient = client_from_env()
_claude_activity: ClaudeActivityReader = claude_activity_reader_from_env()
_codex: CodexLiveQuotaClient = codex_client_from_env()
_codex_activity: CodexActivityReader = codex_activity_reader_from_env()
_cursor: CursorLiveQuotaClient = cursor_client_from_env()
_cursor_activity: CursorActivityReader = cursor_activity_reader_from_env()
_copilot: CopilotLiveQuotaClient | CopilotBillingQuotaClient = copilot_client_from_env()
_copilot_activity: CopilotActivityReader = copilot_activity_reader_from_env()
_gemini: GeminiLiveQuotaClient = gemini_client_from_env()
_gemini_activity: GeminiActivityReader = gemini_activity_reader_from_env()


def _window_dict(name: str, label: str, percent, resets_at, detail=None) -> dict:
    return {
        "name": name,
        "label": label,
        "percent": None if percent is None else round(float(percent), 2),
        "resets_at": resets_at.isoformat() if resets_at else None,
        "detail": detail,
    }


def _window_from(window) -> dict:
    return _window_dict(
        window.name,
        window.label,
        getattr(window, "percent", None),
        getattr(window, "resets_at", None),
        getattr(window, "detail", None),
    )


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


def _cursor_section() -> dict:
    activity_snap = _cursor_activity.snapshot()
    last_activity = (
        activity_snap.last_activity.isoformat() if activity_snap.last_activity else None
    )
    placeholder = [
        _window_dict("requests", "Premium Requests (month)", None, None),
        _window_dict("spend", "Usage-Based Spend (month)", None, None),
    ]
    try:
        snap = _cursor.get()
        return {
            "enabled": CURSOR_ENABLED,
            "windows": [_window_from(snap.requests), _window_from(snap.spend)],
            "source": "live",
            "source_error": None,
            "subscription_type": snap.plan_type,
            "last_activity": last_activity,
            "data_root_exists": activity_snap.data_root_exists,
        }
    except CursorLiveQuotaError as e:
        return {
            "enabled": CURSOR_ENABLED,
            "windows": placeholder,
            "source": "unavailable",
            "source_error": str(e),
            "subscription_type": None,
            "last_activity": last_activity,
            "data_root_exists": activity_snap.data_root_exists,
        }


def _copilot_section() -> dict:
    activity_snap = _copilot_activity.snapshot()
    last_activity = (
        activity_snap.last_activity.isoformat() if activity_snap.last_activity else None
    )
    secondary_label = getattr(_copilot, "secondary_label", "Chat (month)")
    placeholder = [
        _window_dict("premium", "Premium Requests (month)", None, None),
        _window_dict("secondary", secondary_label, None, None),
    ]
    try:
        snap = _copilot.get()
        return {
            "enabled": COPILOT_ENABLED,
            "windows": [_window_from(snap.premium), _window_from(snap.secondary)],
            "source": "live",
            "source_error": None,
            "subscription_type": snap.plan_type,
            "last_activity": last_activity,
            "data_root_exists": activity_snap.data_root_exists,
        }
    except CopilotLiveQuotaError as e:
        return {
            "enabled": COPILOT_ENABLED,
            "windows": placeholder,
            "source": "unavailable",
            "source_error": str(e),
            "subscription_type": None,
            "last_activity": last_activity,
            "data_root_exists": activity_snap.data_root_exists,
        }


def _gemini_section() -> dict:
    activity_snap = _gemini_activity.snapshot()
    last_activity = (
        activity_snap.last_activity.isoformat() if activity_snap.last_activity else None
    )
    placeholder = [
        _window_dict("pro", "Pro Requests (day)", None, None),
        _window_dict("flash", "Flash Requests (day)", None, None),
    ]
    try:
        snap = _gemini.get()
        return {
            "enabled": GEMINI_ENABLED,
            "windows": [_window_from(snap.pro), _window_from(snap.flash)],
            "source": "live",
            "source_error": None,
            "subscription_type": snap.plan_type,
            "last_activity": last_activity,
            "data_root_exists": activity_snap.data_root_exists,
        }
    except GeminiLiveQuotaError as e:
        return {
            "enabled": GEMINI_ENABLED,
            "windows": placeholder,
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
        "cursor": _cursor_section(),
        "copilot": _copilot_section(),
        "gemini": _gemini_section(),
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
        "cursor_data_root_exists": _cursor_activity.data_dir.exists(),
        "cursor_enabled": CURSOR_ENABLED,
        "cursor_credentials_present": _cursor.credentials_path.exists(),
        "copilot_data_root_exists": _copilot_activity.data_dir.exists(),
        "copilot_enabled": COPILOT_ENABLED,
        "copilot_credentials_present": _copilot.credentials_present(),
        "gemini_data_root_exists": _gemini_activity.data_dir.exists(),
        "gemini_enabled": GEMINI_ENABLED,
        "gemini_credentials_present": _gemini.credentials_path.exists(),
    }
