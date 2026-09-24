from __future__ import annotations

import asyncio
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import degrade
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
# and printable, or null.
MAX_TEXT_CHARS = 120
_UNPRINTABLE = re.compile(r"[\x00-\x1f\x7f]")

# Each provider's gauges: the DOM-id slot, the display heading, the attribute
# on the client's snapshot, and whether a malformed value isolates to this one
# gauge instead of degrading the whole section.
_CLAUDE_WINDOWS = (
    ("five_hour", "5-Hour Window", "five_hour", False),
    ("seven_day", "Weekly Window", "seven_day", False),
    ("seven_day_fable", "Weekly Window (Fable)", "seven_day_fable", True),
)
_CODEX_WINDOWS = (
    ("five_hour", "5-Hour Window", "five_hour", False),
    ("seven_day", "Weekly Window", "seven_day", False),
)


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
    if not MIN_PERCENT <= parsed <= MAX_PERCENT:
        raise _SchemaError("percent is outside the declared range")
    return round(parsed, PERCENT_DECIMALS)


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


def _degrade_message(exc: Exception, declared: type[Exception]) -> str:
    """The fixed message for a failure — never str(exc) and never a repr.

    source_error is served unauthenticated, and an exception raised while the
    request was being built carries the bearer token, so nothing from the
    exception itself may be quoted. A failure the client declared reports its
    own classification; anything else is an internal error.
    """
    if isinstance(exc, _SchemaError):
        return degrade.message(degrade.SHAPE)
    if isinstance(exc, declared):
        return degrade.message(getattr(exc, "code", None), degrade.UNCLASSIFIED)
    return degrade.message(degrade.INTERNAL)


def _unreported_window(name: str, label: str) -> dict:
    return {
        "name": name,
        "label": label,
        "percent": None,
        "resets_at": None,
        "detail": None,
    }


def _window_dict(name: str, label: str, window: object, *, optional: bool) -> dict:
    if window is None:
        # An omitted upstream window stays live with no value.
        return _unreported_window(name, label)
    try:
        percent = _percent(getattr(window, "percent", None))
    except _SchemaError:
        if not optional:
            raise
        # An optional gauge isolates: a malformed Fable entry shows no value
        # and does not make the provider's section unavailable.
        percent = None
    return {
        "name": name,
        "label": label,
        "percent": percent,
        "resets_at": _iso(getattr(window, "resets_at", None)),
        "detail": _text(getattr(window, "detail", None)),
    }


def _activity_fields(reader: object) -> tuple[str | None, bool]:
    """Each provider's last_activity reading, degrading on its own.

    An activity read is a separate source from the quota call — it walks local
    transcript and session files — so a fault in it means "no reading" and
    never takes the provider's quota section down with it.
    """
    try:
        snapshot = reader.snapshot()
        return (
            _iso(getattr(snapshot, "last_activity", None)),
            bool(getattr(snapshot, "data_root_exists", False)),
        )
    except Exception:
        return None, False


def _provider_section(
    *,
    enabled: bool,
    client: object,
    activity: object,
    windows: tuple[tuple[str, str, str, bool], ...],
    plan_attr: str,
    declared_error: type[Exception],
) -> dict:
    """One provider's whole section: the single boundary for that provider."""
    last_activity, data_root_exists = _activity_fields(activity)
    try:
        snapshot = client.get()
        if snapshot is None:
            raise _SchemaError("client returned no snapshot")
        built = [
            _window_dict(name, label, getattr(snapshot, attr, None), optional=optional)
            for name, label, attr, optional in windows
        ]
        subscription_type = _text(getattr(snapshot, plan_attr, None))
    except Exception as exc:
        return {
            "enabled": enabled,
            "windows": [_unreported_window(name, label) for name, label, _, _ in windows],
            "source": "unavailable",
            "source_error": _degrade_message(exc, declared_error),
            "subscription_type": None,
            "last_activity": last_activity,
            "data_root_exists": data_root_exists,
        }
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
        enabled=CLAUDE_ENABLED,
        client=_live,
        activity=_claude_activity,
        windows=_CLAUDE_WINDOWS,
        plan_attr="subscription_type",
        declared_error=LiveQuotaError,
    )


def _codex_section() -> dict:
    return _provider_section(
        enabled=CODEX_ENABLED,
        client=_codex,
        activity=_codex_activity,
        windows=_CODEX_WINDOWS,
        plan_attr="plan_type",
        declared_error=CodexLiveQuotaError,
    )


def _build_payload() -> dict:
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

    def section(enabled: bool, windows) -> dict:
        return {
            "enabled": enabled,
            "windows": [_unreported_window(name, label) for name, label, _, _ in windows],
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
    literals `NaN`/`Infinity`, and the output is ASCII, so no unencodable
    character can reach a response. /api/usage, the SSE frames and the
    template's initial payload all serve this exact string, so they cannot
    disagree about a value.
    """
    try:
        return json.dumps(payload, allow_nan=False, ensure_ascii=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        # The boundary above is meant to make this unreachable. If it is ever
        # reached, serve a degraded payload instead of a 500.
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
    payload = _build_payload()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "data": payload,
            "payload_json": _payload_script_json(_payload_json(payload)),
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
