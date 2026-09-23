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

app = FastAPI(title="Codervis")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
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
