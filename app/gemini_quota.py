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


CODE_ASSIST_HOST = "https://daily-cloudcode-pa.googleapis.com"
LOAD_CODE_ASSIST_METHOD = "loadCodeAssist"
RETRIEVE_USER_QUOTA_METHOD = "retrieveUserQuota"


@dataclass
class GeminiLiveWindow:
    name: str
    label: str
    percent: float | None
    resets_at: datetime | None
    detail: str | None = None


@dataclass
class GeminiLiveSnapshot:
    pro: GeminiLiveWindow
    flash: GeminiLiveWindow
    plan_type: str | None
    fetched_at: datetime
    raw: dict


class GeminiLiveQuotaError(Exception):
    pass


class GeminiLiveQuotaClient:
    """Reports Antigravity/Gemini Code Assist daily request quota.

    Antigravity CLI stores an OAuth token under ~/.gemini/antigravity-cli.
    We read that token file on every call, ask Cloud Code for the companion
    project, then fetch request quota buckets for that project.

    The endpoints are internal and can change without notice. Any upstream,
    auth, parse, or file-read failure raises GeminiLiveQuotaError so callers
    render an unavailable state instead of inventing quota usage.
    """

    def __init__(
        self,
        data_dir: str | Path,
        host: str = CODE_ASSIST_HOST,
        token_file: str | Path | None = None,
        cache_ttl_seconds: float = 30.0,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.host = host.rstrip("/")
        self._token_file = Path(token_file) if token_file else None
        self.cache_ttl_seconds = cache_ttl_seconds
        self.timeout_seconds = timeout_seconds
        self._lock = Lock()
        self._cached: tuple[float, GeminiLiveSnapshot] | None = None

    @property
    def credentials_path(self) -> Path:
        if self._token_file is not None:
            return self._token_file
        return self.data_dir / "antigravity-cli" / "antigravity-oauth-token"

    def _read_token(self) -> str:
        try:
            raw = self.credentials_path.read_text(encoding="utf-8")
        except OSError as e:
            raise GeminiLiveQuotaError(f"cannot read token file: {e}") from e
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise GeminiLiveQuotaError(f"token file is not JSON: {e}") from e

        if not isinstance(data, dict):
            raise GeminiLiveQuotaError("token file is not a JSON object")

        token_blob = data.get("token")
        token_data = token_blob if isinstance(token_blob, dict) else data
        token = token_data.get("access_token") if isinstance(token_data, dict) else None
        if not token:
            raise GeminiLiveQuotaError("no access_token in token file")
        return token

    def _post_json(self, method: str, token: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.host}/v1internal:{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "codervis/0.1 (+local dashboard)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                if resp.status != 200:
                    raise GeminiLiveQuotaError(f"unexpected status {resp.status}")
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raise GeminiLiveQuotaError(f"HTTP {e.code}: {e.reason}") from e
        except urllib.error.URLError as e:
            raise GeminiLiveQuotaError(f"network error: {e.reason}") from e

        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise GeminiLiveQuotaError(f"response is not JSON: {e}") from e
        if not isinstance(data, dict):
            raise GeminiLiveQuotaError("response is not a JSON object")
        return data

    def _fetch(self) -> GeminiLiveSnapshot:
        token = self._read_token()
        load = self._post_json(
            LOAD_CODE_ASSIST_METHOD,
            token,
            {
                "metadata": {
                    "ideType": "IDE_UNSPECIFIED",
                    "platform": "PLATFORM_UNSPECIFIED",
                    "pluginType": "GEMINI",
                },
                "mode": "HEALTH_CHECK",
            },
        )
        project = _project_id(load)
        if not project:
            raise GeminiLiveQuotaError("no cloudaicompanionProject in response")

        quota = self._post_json(RETRIEVE_USER_QUOTA_METHOD, token, {"project": project})
        buckets = _request_buckets(quota)
        pro = _bucket_window(
            "pro",
            "Pro Requests (day)",
            buckets,
            lambda model: "pro" in model.lower(),
        )
        flash = _bucket_window(
            "flash",
            "Flash Requests (day)",
            buckets,
            lambda model: "flash" in model.lower(),
        )
        if pro.percent is None and flash.percent is None:
            raise GeminiLiveQuotaError("no pro or flash request quota buckets")

        return GeminiLiveSnapshot(
            pro=pro,
            flash=flash,
            plan_type=_plan_type(load),
            fetched_at=datetime.now(timezone.utc),
            raw={"loadCodeAssist": load, "retrieveUserQuota": quota},
        )

    def get(self) -> GeminiLiveSnapshot:
        with self._lock:
            now = time.monotonic()
            if self._cached and (now - self._cached[0]) < self.cache_ttl_seconds:
                return self._cached[1]
            snap = self._fetch()
            self._cached = (now, snap)
            return snap


def _project_id(payload: dict) -> str | None:
    for candidate in (
        payload.get("cloudaicompanionProject"),
        _nested(payload, "currentTier", "cloudaicompanionProject"),
        _nested(payload, "paidTier", "cloudaicompanionProject"),
    ):
        if isinstance(candidate, str) and candidate:
            return candidate
        if isinstance(candidate, dict):
            value = candidate.get("id") or candidate.get("name")
            if isinstance(value, str) and value:
                return value
    return None


def _plan_type(payload: dict) -> str | None:
    for container in (payload.get("paidTier"), payload.get("currentTier")):
        if isinstance(container, dict):
            for key in ("name", "id"):
                value = container.get(key)
                if isinstance(value, str) and value:
                    return value
    return None


def _nested(payload: dict, *keys: str):
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _request_buckets(payload: dict) -> list[dict]:
    buckets = payload.get("buckets")
    if not isinstance(buckets, list):
        raise GeminiLiveQuotaError("missing buckets array in quota response")

    request_buckets: list[dict] = []
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        token_type = _str_field(bucket, "tokenType", "token_type")
        if token_type and token_type.upper() != "REQUESTS":
            continue
        if _model_id(bucket):
            request_buckets.append(bucket)
    if not request_buckets:
        raise GeminiLiveQuotaError("no request quota buckets in response")
    return request_buckets


def _bucket_window(
    name: str,
    label: str,
    buckets: list[dict],
    predicate,
) -> GeminiLiveWindow:
    candidates: list[tuple[float, int, str, GeminiLiveWindow]] = []
    for bucket in buckets:
        model = _model_id(bucket)
        if not model or not predicate(model):
            continue
        try:
            fraction = _remaining_fraction(bucket)
        except GeminiLiveQuotaError:
            continue
        window = GeminiLiveWindow(
            name=name,
            label=label,
            percent=(1.0 - fraction) * 100.0,
            resets_at=_reset_time(bucket),
            detail=_detail(model, fraction, bucket),
        )
        candidates.append((fraction, -_model_priority(model), model, window))

    if not candidates:
        return GeminiLiveWindow(name=name, label=label, percent=None, resets_at=None, detail="unavailable")
    return sorted(candidates, key=lambda item: item[:3])[0][3]


def _model_id(bucket: dict) -> str | None:
    return _str_field(bucket, "modelId", "model_id", "model")


def _str_field(bucket: dict, *keys: str) -> str | None:
    for key in keys:
        value = bucket.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _remaining_fraction(bucket: dict) -> float:
    value = bucket.get("remainingFraction")
    if value is None:
        value = bucket.get("remaining_fraction")
    try:
        fraction = float(value)
    except (TypeError, ValueError) as e:
        raise GeminiLiveQuotaError("remainingFraction is not numeric") from e

    if 1.0 < fraction <= 100.0:
        fraction = fraction / 100.0
    if not 0.0 <= fraction <= 1.0:
        raise GeminiLiveQuotaError("remainingFraction is outside 0-1 range")
    return fraction


def _reset_time(bucket: dict) -> datetime | None:
    value = _str_field(bucket, "resetTime", "reset_time", "resetAt", "resets_at")
    if not value:
        return None
    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _detail(model: str, remaining_fraction: float, bucket: dict) -> str:
    remaining = bucket.get("remainingAmount") or bucket.get("remaining_amount")
    if isinstance(remaining, (int, float)):
        return f"{model}: {remaining:g} remaining"
    remaining_pct = remaining_fraction * 100.0
    if remaining_pct.is_integer():
        formatted = f"{remaining_pct:.0f}%"
    else:
        formatted = f"{remaining_pct:.1f}%"
    return f"{model}: {formatted} left"


def _model_priority(model: str) -> int:
    lowered = model.lower()
    score = 0
    if "3.1" in lowered:
        score += 40
    elif "3-" in lowered or "3." in lowered:
        score += 30
    elif "2.5" in lowered:
        score += 20
    if "pro" in lowered:
        score += 10
    if "flash" in lowered and "lite" not in lowered:
        score += 8
    elif "flash" in lowered:
        score += 4
    if "preview" in lowered:
        score += 1
    return score


def client_from_env() -> GeminiLiveQuotaClient:
    data_dir = os.environ.get("GEMINI_DATA_DIR", "/data/gemini")
    host = os.environ.get("GEMINI_CODE_ASSIST_HOST", CODE_ASSIST_HOST)
    token_file = os.environ.get("GEMINI_TOKEN_FILE") or None
    ttl = float(os.environ.get("QUOTA_CACHE_TTL_SECONDS", "30"))
    return GeminiLiveQuotaClient(
        data_dir,
        host=host,
        token_file=token_file,
        cache_ttl_seconds=ttl,
    )
