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


CHATGPT_HOST = "https://chatgpt.com"
USAGE_PATH = "/backend-api/wham/usage"
USAGE_PATH_ALT = "/backend-api/codex/usage"

# A credential value is sent as an HTTP header. http.client rejects CR, LF and
# NUL in a header value by raising a ValueError whose message quotes the whole
# value, so a hand-assembled auth.json would otherwise put the bearer token or
# the account id into an exception. Reject those characters here, where the
# value is never quoted back.
_ILLEGAL_HEADER_VALUE = re.compile(r"[\r\n\x00]")


@dataclass
class CodexLiveWindow:
    name: str
    label: str
    percent: float | None
    resets_at: datetime | None


@dataclass
class CodexLiveSnapshot:
    five_hour: CodexLiveWindow
    seven_day: CodexLiveWindow
    plan_type: str | None
    fetched_at: datetime
    raw: dict


class CodexLiveQuotaError(Exception):
    """A Codex quota failure, tagged with a fixed degrade classification.

    `code` is one of `app.degrade`'s codes. The payload boundary uses it to pick
    the message it serves; the message passed here is for developers reading a
    traceback and is never served or logged.
    """

    def __init__(self, message: str, *, code: str = degrade.UNCLASSIFIED) -> None:
        super().__init__(message)
        self.code = code


class CodexLiveQuotaClient:
    """Calls chatgpt.com's /backend-api/wham/usage using the OAuth token
    Codex CLI keeps in ~/.codex/auth.json (bind-mounted into the container).

    The Codex CLI refreshes the access token on the host roughly hourly
    using the stored refresh token; we re-read the file on every call so
    we ride along on that cadence. A short in-memory result cache avoids
    hammering the endpoint when many SSE clients are connected.

    The endpoint is undocumented — reverse-engineered from the codex-rs
    backend client. Window positions have changed over time, so duration
    metadata is used to distinguish the 5-hour and weekly limits when it is
    present. A missing window is represented as an unreported gauge. Other
    failures raise CodexLiveQuotaError; the caller is expected to render an
    "unavailable" state rather than synthesizing fake numbers.

    Parsing stays deliberately tolerant, and every failure it can name carries a
    classification. It is not the enforcing boundary, though: `app/main.py`
    treats this client as untrusted and contains whatever else it raises or
    returns.
    """

    def __init__(
        self,
        data_dir: str | Path,
        host: str = CHATGPT_HOST,
        cache_ttl_seconds: float = 30.0,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.host = host.rstrip("/")
        self.cache_ttl_seconds = cache_ttl_seconds
        self.timeout_seconds = timeout_seconds
        self._lock = Lock()
        self._cached: tuple[float, CodexLiveSnapshot] | None = None

    @property
    def credentials_path(self) -> Path:
        return self.data_dir / "auth.json"

    def _read_token(self) -> tuple[str, str | None, str | None]:
        try:
            raw = self.credentials_path.read_text(encoding="utf-8")
        except (OSError, ValueError) as e:
            raise CodexLiveQuotaError(
                f"cannot read credentials file: {type(e).__name__}",
                code=degrade.CREDENTIALS,
            ) from e
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError) as e:
            raise CodexLiveQuotaError(
                "credentials file is not usable JSON", code=degrade.CREDENTIALS
            ) from e
        if not isinstance(data, dict):
            raise CodexLiveQuotaError(
                "credentials file is not a JSON object", code=degrade.CREDENTIALS
            )
        tokens = data.get("tokens")
        tokens = tokens if isinstance(tokens, dict) else {}
        token = tokens.get("access_token")
        if not isinstance(token, str) or not token:
            raise CodexLiveQuotaError(
                "no access_token in credentials file", code=degrade.CREDENTIALS
            )
        if _ILLEGAL_HEADER_VALUE.search(token):
            raise CodexLiveQuotaError(
                "access_token contains a character that cannot be sent as a header",
                code=degrade.CREDENTIALS,
            )
        account = data.get("account")
        account = account if isinstance(account, dict) else {}
        account_id = _header_str(tokens.get("account_id")) or _header_str(
            data.get("account_id")
        )
        plan_type = (
            _plain_str(data.get("plan_type"))
            or _plain_str(tokens.get("plan_type"))
            or _plain_str(account.get("plan_type"))
        )
        return token, account_id, plan_type

    def _fetch(self) -> CodexLiveSnapshot:
        token, account_id, plan_type = self._read_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "User-Agent": "codervis/0.1 (+local dashboard)",
        }
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id

        # Try the primary "wham" path first; some Codex builds expose
        # utilization at /backend-api/codex/usage instead. If the first
        # path returns 401/404 we fall through to the alternate before
        # raising so a single auth/path mismatch doesn't take the panel
        # offline.
        body: str | None = None
        last_err: CodexLiveQuotaError | None = None
        for path in (USAGE_PATH, USAGE_PATH_ALT):
            req = urllib.request.Request(self.host + path, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                    if resp.status != 200:
                        last_err = CodexLiveQuotaError(
                            f"unexpected status {resp.status}", code=degrade.HTTP
                        )
                        continue
                    body = resp.read().decode("utf-8", errors="replace")
                    break
            except urllib.error.HTTPError as e:
                code = degrade.AUTH if e.code in (401, 403) else degrade.HTTP
                last_err = CodexLiveQuotaError(f"HTTP {e.code}", code=code)
                if e.code in (401, 403, 404):
                    continue
                raise last_err from e
            except urllib.error.URLError as e:
                raise CodexLiveQuotaError("network error", code=degrade.TRANSPORT) from e
            except ValueError as e:
                # http.client quotes the offending header value, which is the
                # bearer token or the account id. Name the type only.
                raise CodexLiveQuotaError(
                    f"request could not be sent: {type(e).__name__}",
                    code=degrade.CREDENTIALS,
                ) from e
            except (OSError, http.client.HTTPException) as e:
                # A fault raised from getresponse()/read() — a reset connection,
                # a short read, a bad status line — is not a URLError.
                raise CodexLiveQuotaError(
                    f"transport error: {type(e).__name__}", code=degrade.TRANSPORT
                ) from e
        if body is None:
            raise last_err or CodexLiveQuotaError("no response body", code=degrade.HTTP)

        try:
            payload = json.loads(body)
        except (ValueError, RecursionError) as e:
            raise CodexLiveQuotaError(
                "response is not usable JSON", code=degrade.SHAPE
            ) from e
        if not isinstance(payload, dict):
            raise CodexLiveQuotaError("response is not a JSON object", code=degrade.SHAPE)

        rate_limit = payload.get("rate_limit") if isinstance(payload.get("rate_limit"), dict) else {}
        primary_entry = _pick_window(
            payload,
            rate_limit,
            keys=("primary_window", "five_hour", "five_hour_window"),
        )
        secondary_entry = _pick_window(
            payload,
            rate_limit,
            keys=("secondary_window", "weekly", "seven_day", "weekly_window"),
        )
        primary_key, primary_raw = primary_entry or (None, None)
        _, secondary_raw = secondary_entry or (None, None)
        five_hour_raw, seven_day_raw = _classify_windows(
            primary_raw,
            secondary_raw,
            primary_key=primary_key,
        )
        if five_hour_raw is None and seven_day_raw is None:
            raise CodexLiveQuotaError("missing Codex usage windows", code=degrade.SHAPE)
        rl_plan = _plain_str(rate_limit.get("plan_type"))

        return CodexLiveSnapshot(
            five_hour=_optional_window("five_hour", "5-Hour Window", five_hour_raw),
            seven_day=_optional_window("seven_day", "Weekly Window", seven_day_raw),
            plan_type=plan_type or rl_plan or _plain_str(payload.get("plan_type")),
            fetched_at=datetime.now(timezone.utc),
            raw=payload,
        )

    def get(self) -> CodexLiveSnapshot:
        with self._lock:
            now = time.monotonic()
            if self._cached and (now - self._cached[0]) < self.cache_ttl_seconds:
                return self._cached[1]
            snap = self._fetch()
            self._cached = (now, snap)
            return snap


def _pick_window(*containers: dict, keys: tuple[str, ...]) -> tuple[str, dict] | None:
    for container in containers:
        for k in keys:
            v = container.get(k)
            if isinstance(v, dict):
                return k, v
    return None


def _classify_windows(
    primary: dict | None,
    secondary: dict | None,
    *,
    primary_key: str | None,
) -> tuple[dict | None, dict | None]:
    if (
        primary is not None
        and secondary is None
        and primary_key == "primary_window"
        and _window_duration_seconds(primary) is None
    ):
        return None, primary

    five_hour: dict | None = None
    seven_day: dict | None = None
    unclassified: list[tuple[str, dict]] = []

    for position, raw in (("primary", primary), ("secondary", secondary)):
        if raw is None:
            continue
        duration = _window_duration_seconds(raw)
        if duration == 18_000:
            five_hour = raw
        elif duration == 604_800:
            seven_day = raw
        else:
            unclassified.append((position, raw))

    for position, raw in unclassified:
        if position == "primary" and five_hour is None:
            five_hour = raw
        elif position == "secondary" and seven_day is None:
            seven_day = raw
        elif five_hour is None:
            five_hour = raw
        elif seven_day is None:
            seven_day = raw

    return five_hour, seven_day


def _window_duration_seconds(raw: dict) -> float | None:
    for key in ("limit_window_seconds", "window_duration_seconds"):
        value = raw.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    for key in ("windowDurationMins", "window_duration_mins", "window_minutes"):
        value = raw.get(key)
        if isinstance(value, (int, float)):
            return float(value) * 60
    return None


def _optional_window(name: str, label: str, raw: dict | None) -> CodexLiveWindow:
    if raw is None:
        return CodexLiveWindow(name=name, label=label, percent=None, resets_at=None)
    return _window(name, label, raw)


def _window(name: str, label: str, raw: dict | None) -> CodexLiveWindow:
    if not isinstance(raw, dict):
        raise CodexLiveQuotaError(
            f"missing or invalid {name} usage window", code=degrade.SHAPE
        )

    if "utilization" in raw:
        percent = _float_field(raw.get("utilization"), f"{name}.utilization")
    elif "percent_used" in raw:
        percent = _float_field(raw.get("percent_used"), f"{name}.percent_used")
    elif "used_percent" in raw:
        percent = _float_field(raw.get("used_percent"), f"{name}.used_percent")
    elif "percent_left" in raw:
        percent = 100.0 - _float_field(raw.get("percent_left"), f"{name}.percent_left")
    elif "remaining_percent" in raw:
        percent = 100.0 - _float_field(
            raw.get("remaining_percent"), f"{name}.remaining_percent"
        )
    else:
        raise CodexLiveQuotaError(
            f"missing {name} utilization field in usage response", code=degrade.SHAPE
        )

    resets_at: datetime | None = None
    for key in ("resets_at", "reset_at", "resets", "reset"):
        v = raw.get(key)
        if isinstance(v, str):
            s = v.replace("Z", "+00:00") if v.endswith("Z") else v
            try:
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                resets_at = dt.astimezone(timezone.utc)
                break
            except (ValueError, OverflowError, OSError):
                continue
        elif isinstance(v, (int, float)):
            resets_at = _timestamp_or_relative(v)
            if resets_at is not None:
                break
    if resets_at is None:
        ms = raw.get("reset_time_ms") or raw.get("resets_in_ms")
        if isinstance(ms, (int, float)):
            try:
                if ms > 1e12:
                    resets_at = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
                else:
                    resets_at = datetime.fromtimestamp(
                        time.time() + (ms / 1000.0), tz=timezone.utc
                    )
            except (OverflowError, OSError, ValueError):
                resets_at = None
    if resets_at is None:
        seconds = raw.get("reset_after_seconds") or raw.get("resets_in_seconds")
        if isinstance(seconds, (int, float)):
            resets_at = _relative_seconds(seconds)

    return CodexLiveWindow(name=name, label=label, percent=percent, resets_at=resets_at)


def _float_field(value, field: str) -> float:
    # Kept in step with app/quota.py:_float_field. A bool is an int in Python, so
    # `true` would otherwise read as a live 1.0%, and NaN/Infinity would reach
    # the payload.
    if isinstance(value, bool):
        raise CodexLiveQuotaError(f"{field} is not numeric", code=degrade.SHAPE)
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as e:
        raise CodexLiveQuotaError(f"{field} is not numeric", code=degrade.SHAPE) from e
    if not math.isfinite(parsed):
        raise CodexLiveQuotaError(f"{field} is not finite", code=degrade.SHAPE)
    return parsed


def _header_str(value: object) -> str | None:
    """A credential string safe to send as a header value, or None."""
    text = _plain_str(value)
    if text is None or _ILLEGAL_HEADER_VALUE.search(text):
        return None
    return text


def _plain_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _timestamp_or_relative(value: int | float) -> datetime | None:
    try:
        if value > 1e12:
            return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
        if value > 1e9:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        return _relative_seconds(value)
    except (OverflowError, OSError, ValueError):
        return None


def _relative_seconds(value: int | float) -> datetime | None:
    try:
        return datetime.fromtimestamp(time.time() + float(value), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def client_from_env() -> CodexLiveQuotaClient:
    data_dir = os.environ.get("CODEX_DATA_DIR", "/data/codex")
    host = os.environ.get("CHATGPT_HOST", CHATGPT_HOST)
    ttl = float(os.environ.get("QUOTA_CACHE_TTL_SECONDS", "30"))
    return CodexLiveQuotaClient(data_dir, host=host, cache_ttl_seconds=ttl)
