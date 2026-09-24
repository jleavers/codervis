from __future__ import annotations

import http.client
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from . import degrade


CLAUDE_AI_HOST = "https://claude.ai"
USAGE_PATH = "/api/oauth/usage"

# A credential value is sent as an HTTP header. http.client rejects CR, LF and
# NUL in a header value by raising a ValueError whose message quotes the whole
# value, so a hand-assembled credentials file would otherwise put the bearer
# token into an exception. Reject those characters here, where the value is
# never quoted back.
_ILLEGAL_HEADER_VALUE = re.compile(r"[\r\n\x00]")


@dataclass
class LiveWindow:
    name: str
    label: str
    percent: float
    resets_at: datetime | None


@dataclass
class LiveSnapshot:
    five_hour: LiveWindow
    seven_day: LiveWindow
    seven_day_fable: LiveWindow | None
    subscription_type: str | None
    fetched_at: datetime
    raw: dict


class LiveQuotaError(Exception):
    """A Claude quota failure, tagged with a fixed degrade classification.

    `code` is one of `app.degrade`'s codes. The payload boundary uses it to pick
    the message it serves; the message passed here is for developers reading a
    traceback and is never served or logged.
    """

    def __init__(self, message: str, *, code: str = degrade.UNCLASSIFIED) -> None:
        super().__init__(message)
        self.code = code


class LiveQuotaClient:
    """Calls claude.ai's /api/oauth/usage using the OAuth token Claude Code
    keeps in ~/.claude/.credentials.json (bind-mounted into the container).

    The token is short-lived but Claude Code refreshes it itself; we re-read
    the file on every call so we ride along on the host's refresh cadence.
    A short in-memory result cache avoids hammering the endpoint when many
    SSE clients are connected.

    Parsing stays deliberately tolerant, and every failure it can name becomes
    a LiveQuotaError with a classification. It is not the enforcing boundary,
    though: `app/main.py` treats this client as untrusted and contains whatever
    else it raises or returns.
    """

    def __init__(
        self,
        data_dir: str | Path,
        host: str = CLAUDE_AI_HOST,
        cache_ttl_seconds: float = 30.0,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.host = host.rstrip("/")
        self.cache_ttl_seconds = cache_ttl_seconds
        self.timeout_seconds = timeout_seconds
        self._lock = Lock()
        self._cached: tuple[float, LiveSnapshot] | None = None

    @property
    def credentials_path(self) -> Path:
        return self.data_dir / ".credentials.json"

    def _read_token(self) -> tuple[str, str | None]:
        try:
            raw = self.credentials_path.read_text(encoding="utf-8")
        except (OSError, ValueError) as e:
            raise LiveQuotaError(
                f"cannot read credentials file: {type(e).__name__}", code=degrade.CREDENTIALS
            ) from e
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError) as e:
            raise LiveQuotaError(
                "credentials file is not usable JSON", code=degrade.CREDENTIALS
            ) from e
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        oauth = oauth if isinstance(oauth, dict) else {}
        token = oauth.get("accessToken")
        if not isinstance(token, str) or not token:
            raise LiveQuotaError(
                "no accessToken in credentials file", code=degrade.CREDENTIALS
            )
        if _ILLEGAL_HEADER_VALUE.search(token):
            raise LiveQuotaError(
                "accessToken contains a character that cannot be sent as a header",
                code=degrade.CREDENTIALS,
            )
        subscription = oauth.get("subscriptionType")
        return token, subscription if isinstance(subscription, str) else None

    def _fetch(self) -> LiveSnapshot:
        token, subscription = self._read_token()
        req = urllib.request.Request(
            self.host + USAGE_PATH,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "codervis/0.1 (+local dashboard)",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                if resp.status != 200:
                    raise LiveQuotaError(
                        f"unexpected status {resp.status}", code=degrade.HTTP
                    )
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            code = degrade.AUTH if e.code in (401, 403) else degrade.HTTP
            raise LiveQuotaError(f"HTTP {e.code}", code=code) from e
        except urllib.error.URLError as e:
            raise LiveQuotaError("network error", code=degrade.TRANSPORT) from e
        except ValueError as e:
            # http.client quotes the offending header value, which is the
            # bearer token. Name the type only.
            raise LiveQuotaError(
                f"request could not be sent: {type(e).__name__}", code=degrade.CREDENTIALS
            ) from e
        except (OSError, http.client.HTTPException) as e:
            # A fault raised from getresponse()/read() — a reset connection, a
            # short read, a bad status line — is not a URLError.
            raise LiveQuotaError(
                f"transport error: {type(e).__name__}", code=degrade.TRANSPORT
            ) from e

        try:
            payload = json.loads(body)
        except (ValueError, RecursionError) as e:
            raise LiveQuotaError("response is not usable JSON", code=degrade.SHAPE) from e
        if not isinstance(payload, dict):
            raise LiveQuotaError("response is not a JSON object", code=degrade.SHAPE)

        return LiveSnapshot(
            five_hour=_window("five_hour", "5-Hour Window", payload.get("five_hour")),
            seven_day=_window("seven_day", "Weekly Window", payload.get("seven_day")),
            seven_day_fable=_fable_window(payload),
            subscription_type=subscription,
            fetched_at=datetime.now(timezone.utc),
            raw=payload,
        )

    def get(self) -> LiveSnapshot:
        with self._lock:
            now = time.monotonic()
            if self._cached and (now - self._cached[0]) < self.cache_ttl_seconds:
                return self._cached[1]
            snap = self._fetch()
            self._cached = (now, snap)
            return snap


def _window(name: str, label: str, raw: dict | None) -> LiveWindow:
    if not isinstance(raw, dict):
        raise LiveQuotaError(
            f"missing or invalid {name} usage window", code=degrade.SHAPE
        )
    if "utilization" not in raw:
        raise LiveQuotaError(
            f"missing {name}.utilization in usage response", code=degrade.SHAPE
        )
    percent = _float_field(raw.get("utilization"), f"{name}.utilization")
    resets_raw = raw.get("resets_at")
    resets_at: datetime | None = None
    if isinstance(resets_raw, str):
        v = resets_raw.replace("Z", "+00:00") if resets_raw.endswith("Z") else resets_raw
        try:
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            resets_at = dt.astimezone(timezone.utc)
        except (ValueError, OverflowError, OSError):
            resets_at = None
    return LiveWindow(name=name, label=label, percent=percent, resets_at=resets_at)


def _fable_window(payload: dict) -> LiveWindow | None:
    limits = payload.get("limits")
    if not isinstance(limits, list):
        return None

    for raw in limits:
        if not isinstance(raw, dict) or raw.get("kind") != "weekly_scoped":
            continue
        scope = raw.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        display_name = model.get("display_name") if isinstance(model, dict) else None
        if not isinstance(display_name, str) or "fable" not in display_name.casefold():
            continue
        try:
            return _window(
                "seven_day_fable",
                "Weekly Window (Fable)",
                {
                    "utilization": raw.get("percent"),
                    "resets_at": raw.get("resets_at"),
                },
            )
        except LiveQuotaError:
            continue
    return None


def _float_field(value, field: str) -> float:
    if isinstance(value, bool):
        raise LiveQuotaError(f"{field} is not numeric", code=degrade.SHAPE)
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as e:
        raise LiveQuotaError(f"{field} is not numeric", code=degrade.SHAPE) from e
    if not math.isfinite(parsed):
        raise LiveQuotaError(f"{field} is not finite", code=degrade.SHAPE)
    return parsed


def client_from_env() -> LiveQuotaClient:
    data_dir = os.environ.get("CLAUDE_DATA_DIR", "/data/claude")
    host = os.environ.get("CLAUDE_AI_HOST", CLAUDE_AI_HOST)
    ttl = float(os.environ.get("QUOTA_CACHE_TTL_SECONDS", "30"))
    return LiveQuotaClient(data_dir, host=host, cache_ttl_seconds=ttl)
