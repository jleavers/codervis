from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .codex_quota import CodexLiveQuotaClient, CodexLiveQuotaError
from .codex_quota import client_from_env as codex_client_from_env
from .quota import LiveQuotaClient, LiveQuotaError, client_from_env
from .usage import UsageReader, reader_from_env

BASE_DIR = Path(__file__).parent
REFRESH_SECONDS = max(1, int(os.environ.get("REFRESH_INTERVAL_SECONDS", "5")))
CODEX_ENABLED = os.environ.get("CODEX_ENABLED", "true").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
    "",
)

app = FastAPI(title="Codervis")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

_live: LiveQuotaClient = client_from_env()
_fallback: UsageReader = reader_from_env()
_codex: CodexLiveQuotaClient | None = codex_client_from_env() if CODEX_ENABLED else None


def _window_dict(name: str, label: str, percent, resets_at) -> dict:
    return {
        "name": name,
        "label": label,
        "percent": None if percent is None else round(float(percent), 2),
        "resets_at": resets_at.isoformat() if resets_at else None,
    }


def _claude_section() -> dict:
    now = datetime.now(timezone.utc)
    fallback_snap = _fallback.snapshot(now)

    source = "live"
    error: str | None = None
    try:
        live = _live.get()
        five_hour = _window_dict(
            "five_hour", "5-Hour Window", live.five_hour.percent, live.five_hour.resets_at
        )
        seven_day = _window_dict(
            "seven_day", "Weekly Window", live.seven_day.percent, live.seven_day.resets_at
        )
        subscription = live.subscription_type
    except LiveQuotaError as e:
        source = "fallback"
        error = str(e)
        f5 = fallback_snap.five_hour
        fw = fallback_snap.weekly
        five_hour = _window_dict("five_hour", "5-Hour Window", f5.percent, f5.resets_at)
        seven_day = _window_dict("seven_day", "Weekly Window", fw.percent, fw.resets_at)
        subscription = None

    return {
        "five_hour": five_hour,
        "seven_day": seven_day,
        "source": source,
        "source_error": error,
        "subscription_type": subscription,
        "last_activity": fallback_snap.last_activity.isoformat()
        if fallback_snap.last_activity
        else None,
        "total_records": fallback_snap.total_records,
        "data_root_exists": fallback_snap.data_root_exists,
    }


def _codex_section() -> dict:
    if _codex is None:
        return {
            "enabled": False,
            "five_hour": _window_dict("five_hour", "5-Hour Window", None, None),
            "seven_day": _window_dict("seven_day", "Weekly Window", None, None),
            "source": "disabled",
            "source_error": None,
            "subscription_type": None,
        }

    try:
        snap = _codex.get()
        return {
            "enabled": True,
            "five_hour": _window_dict(
                "five_hour", "5-Hour Window", snap.five_hour.percent, snap.five_hour.resets_at
            ),
            "seven_day": _window_dict(
                "seven_day", "Weekly Window", snap.seven_day.percent, snap.seven_day.resets_at
            ),
            "source": "live",
            "source_error": None,
            "subscription_type": snap.plan_type,
        }
    except CodexLiveQuotaError as e:
        return {
            "enabled": True,
            "five_hour": _window_dict("five_hour", "5-Hour Window", None, None),
            "seven_day": _window_dict("seven_day", "Weekly Window", None, None),
            "source": "unavailable",
            "source_error": str(e),
            "subscription_type": None,
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
        "index.html",
        {
            "request": request,
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
        "data_root_exists": _fallback.data_dir.exists(),
        "claude_credentials_present": _live.credentials_path.exists(),
        "codex_enabled": _codex is not None,
        "codex_credentials_present": _codex.credentials_path.exists() if _codex else False,
    }
