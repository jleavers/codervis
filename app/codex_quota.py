from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .budget import (
    BudgetExceeded,
    deadline_in,
    env_float,
    env_int,
    read_capped,
    read_text_capped,
    remaining,
)


CHATGPT_HOST = "https://chatgpt.com"
USAGE_PATH = "/backend-api/wham/usage"
USAGE_PATH_ALT = "/backend-api/codex/usage"


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
    pass


class CodexLiveQuotaClient:
    """Calls chatgpt.com's /backend-api/wham/usage using the OAuth token
    Codex CLI keeps in ~/.codex/auth.json (bind-mounted into the container).

    The Codex CLI refreshes the access token on the host roughly hourly
    using the stored refresh token; we re-read the file on every call so
    we ride along on that cadence.

    ``get()`` always fetches; the refresher in ``app.main`` owns the cadence
    and caches the outcome, failure included. ``timeout_seconds`` is urllib's
    per-socket-operation timeout, so only ``total_deadline_seconds`` bounds the
    call against a sender that keeps trickling -- and it spans *both* candidate
    paths, which is why trying the second one can no longer double the stall.
    ``max_response_bytes`` bounds what a body may cost in memory.

    The endpoint is undocumented — reverse-engineered from the codex-rs
    backend client. Window positions have changed over time, so duration
    metadata is used to distinguish the 5-hour and weekly limits when it is
    present. A missing window is represented as an unreported gauge. Other
    failures raise CodexLiveQuotaError; the caller is expected to render an
    "unavailable" state rather than synthesizing fake numbers.
    """

    def __init__(
        self,
        data_dir: str | Path,
        host: str = CHATGPT_HOST,
        timeout_seconds: float = 8.0,
        total_deadline_seconds: float = 10.0,
        max_response_bytes: int = 1024 * 1024,
        max_credentials_bytes: int = 1024 * 1024,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.host = host.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.total_deadline_seconds = total_deadline_seconds
        self.max_response_bytes = max_response_bytes
        self.max_credentials_bytes = max_credentials_bytes

    @property
    def credentials_path(self) -> Path:
        return self.data_dir / "auth.json"

    def _read_token(self) -> tuple[str, str | None, str | None]:
        try:
            raw = read_text_capped(
                self.credentials_path, max_bytes=self.max_credentials_bytes
            )
        except BudgetExceeded as e:
            raise CodexLiveQuotaError(f"credentials read budget: {e}") from e
        except OSError as e:
            raise CodexLiveQuotaError(f"cannot read credentials file: {e}") from e
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise CodexLiveQuotaError(f"credentials file is not JSON: {e}") from e
        tokens = data.get("tokens") or {}
        token = tokens.get("access_token")
        if not token:
            raise CodexLiveQuotaError("no access_token in credentials file")
        account_id = tokens.get("account_id") or data.get("account_id")
        plan_type = (
            data.get("plan_type")
            or tokens.get("plan_type")
            or (data.get("account") or {}).get("plan_type")
        )
        return token, account_id, plan_type

    def _fetch(self) -> CodexLiveSnapshot:
        deadline = deadline_in(self.total_deadline_seconds)
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
        #
        # One deadline covers both attempts, so falling through to the
        # alternate path cannot add a second full timeout to the stall.
        body: str | None = None
        last_err: CodexLiveQuotaError | None = None
        for path in (USAGE_PATH, USAGE_PATH_ALT):
            req = urllib.request.Request(self.host + path, headers=headers)
            try:
                timeout = remaining(deadline, self.timeout_seconds)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if resp.status != 200:
                        last_err = CodexLiveQuotaError(f"unexpected status {resp.status}")
                        continue
                    raw_body = read_capped(
                        resp, max_bytes=self.max_response_bytes, deadline=deadline
                    )
                body = raw_body.decode("utf-8", errors="replace")
                break
            except BudgetExceeded as e:
                raise CodexLiveQuotaError(f"upstream read budget: {e}") from e
            except urllib.error.HTTPError as e:
                last_err = CodexLiveQuotaError(f"HTTP {e.code}: {e.reason}")
                if e.code in (401, 403, 404):
                    continue
                raise last_err from e
            except urllib.error.URLError as e:
                raise CodexLiveQuotaError(f"network error: {e.reason}") from e
        if body is None:
            raise last_err or CodexLiveQuotaError("no response body")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            raise CodexLiveQuotaError(f"response is not JSON: {e}") from e

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
            raise CodexLiveQuotaError("missing Codex usage windows")
        rl_plan = rate_limit.get("plan_type")

        return CodexLiveSnapshot(
            five_hour=_optional_window("five_hour", "5-Hour Window", five_hour_raw),
            seven_day=_optional_window("seven_day", "Weekly Window", seven_day_raw),
            plan_type=plan_type or rl_plan or payload.get("plan_type"),
            fetched_at=datetime.now(timezone.utc),
            raw=payload,
        )

    def get(self) -> CodexLiveSnapshot:
        """One bounded upstream call. Called only from this source's refresher."""
        return self._fetch()


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
        raise CodexLiveQuotaError(f"missing or invalid {name} usage window")

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
        raise CodexLiveQuotaError(f"missing {name} utilization field in usage response")

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
            except ValueError:
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
    try:
        return float(value)
    except (TypeError, ValueError) as e:
        raise CodexLiveQuotaError(f"{field} is not numeric") from e


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
    return CodexLiveQuotaClient(
        data_dir,
        host=host,
        timeout_seconds=env_float("QUOTA_TIMEOUT_SECONDS", 8.0),
        total_deadline_seconds=env_float("QUOTA_TOTAL_DEADLINE_SECONDS", 10.0),
        max_response_bytes=env_int("QUOTA_MAX_RESPONSE_BYTES", 1024 * 1024),
        max_credentials_bytes=env_int("CREDENTIALS_MAX_BYTES", 1024 * 1024),
    )
