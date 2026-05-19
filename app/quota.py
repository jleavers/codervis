from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock


CLAUDE_AI_HOST = "https://claude.ai"
USAGE_PATH = "/api/oauth/usage"


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
    subscription_type: str | None
    fetched_at: datetime
    raw: dict


class LiveQuotaError(Exception):
    pass


class LiveQuotaClient:
    """Calls claude.ai's /api/oauth/usage using the OAuth token Claude Code
    keeps in ~/.claude/.credentials.json (bind-mounted into the container).

    The token is short-lived but Claude Code refreshes it itself; we re-read
    the file on every call so we ride along on the host's refresh cadence.
    A short in-memory result cache avoids hammering the endpoint when many
    SSE clients are connected.
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
        except OSError as e:
            raise LiveQuotaError(f"cannot read credentials file: {e}") from e
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise LiveQuotaError(f"credentials file is not JSON: {e}") from e
        oauth = data.get("claudeAiOauth") or {}
        token = oauth.get("accessToken")
        if not token:
            raise LiveQuotaError("no accessToken in credentials file")
        return token, oauth.get("subscriptionType")

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
                    raise LiveQuotaError(f"unexpected status {resp.status}")
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raise LiveQuotaError(f"HTTP {e.code}: {e.reason}") from e
        except urllib.error.URLError as e:
            raise LiveQuotaError(f"network error: {e.reason}") from e

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            raise LiveQuotaError(f"response is not JSON: {e}") from e

        return LiveSnapshot(
            five_hour=_window("five_hour", "5-Hour Window", payload.get("five_hour")),
            seven_day=_window("seven_day", "Weekly Window", payload.get("seven_day")),
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
        raise LiveQuotaError(f"missing or invalid {name} usage window")
    if "utilization" not in raw:
        raise LiveQuotaError(f"missing {name}.utilization in usage response")
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
        except ValueError:
            resets_at = None
    return LiveWindow(name=name, label=label, percent=percent, resets_at=resets_at)


def _float_field(value, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as e:
        raise LiveQuotaError(f"{field} is not numeric") from e


def client_from_env() -> LiveQuotaClient:
    data_dir = os.environ.get("CLAUDE_DATA_DIR", "/data/claude")
    host = os.environ.get("CLAUDE_AI_HOST", CLAUDE_AI_HOST)
    ttl = float(os.environ.get("QUOTA_CACHE_TTL_SECONDS", "30"))
    return LiveQuotaClient(data_dir, host=host, cache_ttl_seconds=ttl)
