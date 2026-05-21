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
from urllib.parse import quote


GITHUB_API_HOST = "https://api.github.com"
USER_PATH = "/copilot_internal/user"
SELF_PATH = "/user"
PREMIUM_USAGE_PATH = "/users/{user}/settings/billing/premium_request/usage"

# Monthly premium-request allowances by plan. The billing REST API reports
# consumption but not the allowance, so we supply the denominator. Override
# with COPILOT_PREMIUM_ALLOWANCE if your plan/cap differs.
PLAN_ALLOWANCES = {
    "free": 50,
    "pro": 300,
    "pro+": 1500,
    "proplus": 1500,
    "business": 300,
    "enterprise": 1000,
}

# Candidate credential files inside the data dir, newest format first. The
# Copilot editor integrations keep a GitHub OAuth token here; the file is a
# JSON object keyed by host (e.g. "github.com" or "github.com:Iv1.<appid>")
# whose value carries an "oauth_token".
CRED_FILES = ("apps.json", "hosts.json")


@dataclass
class CopilotLiveWindow:
    name: str
    label: str
    percent: float | None
    resets_at: datetime | None
    detail: str | None


@dataclass
class CopilotLiveSnapshot:
    premium: CopilotLiveWindow
    secondary: CopilotLiveWindow
    plan_type: str | None
    fetched_at: datetime
    raw: dict


class CopilotLiveQuotaError(Exception):
    pass


class CopilotLiveQuotaClient:
    """Reports GitHub Copilot monthly quota using the OAuth token the Copilot
    editor/CLI integration stores locally (``apps.json`` / ``hosts.json`` under
    ``~/.config/github-copilot``, bind-mounted into the container).

    Like Claude/Codex this rides along on a token the agent refreshes itself
    and reads percentages straight from the server, but like Cursor it meters
    on a monthly billing cycle. It calls the same undocumented endpoint VS
    Code's status-bar usage indicator uses::

        GET https://api.github.com/copilot_internal/user
        Authorization: token <oauth_token>

    whose ``quota_snapshots`` object carries one entry per quota kind
    (``premium_interactions``, ``chat``, ``completions``), each with
    ``entitlement`` / ``remaining`` / ``percent_remaining`` / ``unlimited``,
    plus a top-level ``quota_reset_date``.

    The endpoint is undocumented and unversioned (internal Microsoft↔GitHub
    integration). Any failure of the core call — or a response without a
    ``premium_interactions`` snapshot — raises CopilotLiveQuotaError so the
    caller renders an "unavailable" state. The second (chat) window is
    best-effort: if its snapshot is missing it degrades to a null percent
    while the section stays live. On paid plans chat/completions are
    ``unlimited``, so that gauge legitimately shows no percentage.
    """

    secondary_label = "Chat (month)"

    def __init__(
        self,
        data_dir: str | Path,
        host: str = GITHUB_API_HOST,
        cache_ttl_seconds: float = 30.0,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.host = host.rstrip("/")
        self.cache_ttl_seconds = cache_ttl_seconds
        self.timeout_seconds = timeout_seconds
        self._lock = Lock()
        self._cached: tuple[float, CopilotLiveSnapshot] | None = None

    @property
    def credentials_path(self) -> Path:
        for name in CRED_FILES:
            path = self.data_dir / name
            if path.exists():
                return path
        return self.data_dir / CRED_FILES[0]

    def credentials_present(self) -> bool:
        return any((self.data_dir / name).exists() for name in CRED_FILES)

    def _read_token(self) -> str:
        last_err: CopilotLiveQuotaError | None = None
        found_file = False
        for name in CRED_FILES:
            path = self.data_dir / name
            if not path.exists():
                continue
            found_file = True
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as e:
                last_err = CopilotLiveQuotaError(f"cannot read {name}: {e}")
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                last_err = CopilotLiveQuotaError(f"{name} is not JSON: {e}")
                continue
            token = _extract_oauth_token(data)
            if token:
                return token
            last_err = CopilotLiveQuotaError(f"no github.com oauth_token in {name}")
        if last_err is not None:
            raise last_err
        if not found_file:
            raise CopilotLiveQuotaError(
                f"no Copilot credential file in {self.data_dir} "
                f"(looked for {', '.join(CRED_FILES)})"
            )
        raise CopilotLiveQuotaError("no usable Copilot credential found")

    def _fetch(self) -> CopilotLiveSnapshot:
        token = self._read_token()
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/json",
            "User-Agent": "codervis/0.1 (+local dashboard)",
            # The copilot_internal endpoints expect an editor identity.
            "Editor-Version": "codervis/0.1",
            "Editor-Plugin-Version": "codervis/0.1",
        }
        req = urllib.request.Request(self.host + USER_PATH, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                if resp.status != 200:
                    raise CopilotLiveQuotaError(f"unexpected status {resp.status}")
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raise CopilotLiveQuotaError(f"HTTP {e.code}: {e.reason}") from e
        except urllib.error.URLError as e:
            raise CopilotLiveQuotaError(f"network error: {e.reason}") from e

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            raise CopilotLiveQuotaError(f"response is not JSON: {e}") from e
        if not isinstance(payload, dict):
            raise CopilotLiveQuotaError("response is not a JSON object")

        snapshots = payload.get("quota_snapshots")
        if not isinstance(snapshots, dict):
            raise CopilotLiveQuotaError("no quota_snapshots in response")

        resets_at = _parse_reset(payload.get("quota_reset_date"))

        premium = _quota_window(
            "premium",
            "Premium Requests (month)",
            snapshots.get("premium_interactions"),
            resets_at,
            unit="reqs",
            require_percent=True,
        )
        secondary = _best_effort_window(
            "secondary", self.secondary_label, snapshots.get("chat"), resets_at
        )

        plan = payload.get("copilot_plan") or payload.get("access_type_sku")
        return CopilotLiveSnapshot(
            premium=premium,
            secondary=secondary,
            plan_type=plan if isinstance(plan, str) else None,
            fetched_at=datetime.now(timezone.utc),
            raw=payload,
        )

    def get(self) -> CopilotLiveSnapshot:
        with self._lock:
            now = time.monotonic()
            if self._cached and (now - self._cached[0]) < self.cache_ttl_seconds:
                return self._cached[1]
            snap = self._fetch()
            self._cached = (now, snap)
            return snap


class CopilotBillingQuotaClient:
    """Reports GitHub Copilot monthly usage via GitHub's *documented* billing
    REST API, for setups where the OAuth token lives only in an editor's
    encrypted secret store (e.g. VS Code) and no ``apps.json``/``hosts.json``
    file exists for `CopilotLiveQuotaClient` to read.

    It calls
    ``GET /users/{user}/settings/billing/premium_request/usage`` with a
    fine-grained PAT carrying the ``Plan`` (read) permission. Unlike the
    internal endpoint, this report gives consumption only — no allowance — so
    the premium gauge's denominator comes from the plan cap (``COPILOT_PLAN`` /
    ``COPILOT_PREMIUM_ALLOWANCE``). The second gauge shows usage-based dollar
    spend (the report's net amount), as a percentage of ``COPILOT_SPEND_BUDGET``
    when one is set, otherwise just the dollar figure.

    Caveats baked into the contract: this endpoint returns nothing for Copilot
    licences billed through an organization or enterprise — that surfaces as
    ``CopilotLiveQuotaError`` → ``unavailable`` like any other failure.
    """

    secondary_label = "Usage-Based Spend (month)"

    def __init__(
        self,
        token: str | None = None,
        token_file: str | Path | None = None,
        username: str | None = None,
        plan: str = "pro",
        allowance: int | None = None,
        spend_budget: float | None = None,
        host: str = GITHUB_API_HOST,
        cache_ttl_seconds: float = 30.0,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.token = token or None
        self.token_file = Path(token_file) if token_file else None
        self.plan = plan
        self.allowance = allowance if allowance else PLAN_ALLOWANCES.get(plan.lower(), 300)
        self.spend_budget = spend_budget
        self.host = host.rstrip("/")
        self.cache_ttl_seconds = cache_ttl_seconds
        self.timeout_seconds = timeout_seconds
        self._username = username or None
        self._lock = Lock()
        self._cached: tuple[float, CopilotLiveSnapshot] | None = None

    @property
    def credentials_path(self) -> Path:
        return self.token_file if self.token_file else Path("env:COPILOT_GITHUB_TOKEN")

    def credentials_present(self) -> bool:
        return self._resolve_token(soft=True) is not None

    def _resolve_token(self, *, soft: bool = False) -> str | None:
        if self.token_file is not None:
            try:
                token = self.token_file.read_text(encoding="utf-8").strip()
            except OSError as e:
                if soft:
                    return None
                raise CopilotLiveQuotaError(
                    f"cannot read token file {self.token_file}: {e}"
                ) from e
            if token:
                return token
            if soft:
                return None
            raise CopilotLiveQuotaError(f"token file {self.token_file} is empty")
        if self.token:
            return self.token
        if soft:
            return None
        raise CopilotLiveQuotaError(
            "no Copilot PAT configured (set COPILOT_GITHUB_TOKEN or COPILOT_TOKEN_FILE)"
        )

    def _resolve_username(self, token: str) -> str:
        if self._username:
            return self._username
        data = self._get_json(self.host + SELF_PATH, token)
        login = data.get("login")
        if not isinstance(login, str) or not login:
            raise CopilotLiveQuotaError("cannot determine GitHub username from token")
        self._username = login
        return login

    def _fetch(self) -> CopilotLiveSnapshot:
        token = self._resolve_token()
        user = self._resolve_username(token)
        now = datetime.now(timezone.utc)
        url = (
            self.host
            + PREMIUM_USAGE_PATH.format(user=quote(user, safe=""))
            + f"?year={now.year}&month={now.month}"
        )
        data = self._get_json(url, token)

        used, net = _sum_usage_items(data.get("usageItems"))
        resets_at = _start_of_next_month(now)

        if self.allowance > 0:
            percent = used / float(self.allowance) * 100.0
            detail = f"{int(round(used))} / {int(self.allowance)} reqs"
        else:
            percent = None
            detail = f"{int(round(used))} reqs"
        premium = CopilotLiveWindow(
            "premium", "Premium Requests (month)", percent, resets_at, detail
        )

        if self.spend_budget and self.spend_budget > 0:
            spend_pct = net / float(self.spend_budget) * 100.0
            spend_detail = f"${net:.2f} / ${self.spend_budget:.2f}"
        else:
            spend_pct = None
            spend_detail = f"${net:.2f}"
        secondary = CopilotLiveWindow(
            "secondary", self.secondary_label, spend_pct, resets_at, spend_detail
        )

        return CopilotLiveSnapshot(
            premium=premium,
            secondary=secondary,
            plan_type=self.plan,
            fetched_at=now,
            raw=data,
        )

    def _get_json(self, url: str, token: str) -> dict:
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "codervis/0.1 (+local dashboard)",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                if resp.status != 200:
                    raise CopilotLiveQuotaError(f"unexpected status {resp.status}")
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            hint = ""
            if e.code in (403, 404):
                hint = " (PAT needs 'Plan' read permission; org/enterprise-managed licences are not reported here)"
            raise CopilotLiveQuotaError(f"HTTP {e.code}: {e.reason}{hint}") from e
        except urllib.error.URLError as e:
            raise CopilotLiveQuotaError(f"network error: {e.reason}") from e
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            raise CopilotLiveQuotaError(f"response is not JSON: {e}") from e
        if not isinstance(payload, dict):
            raise CopilotLiveQuotaError("response is not a JSON object")
        return payload

    def get(self) -> CopilotLiveSnapshot:
        with self._lock:
            now = time.monotonic()
            if self._cached and (now - self._cached[0]) < self.cache_ttl_seconds:
                return self._cached[1]
            snap = self._fetch()
            self._cached = (now, snap)
            return snap


def _sum_usage_items(items) -> tuple[float, float]:
    """Returns (total premium requests used, total net dollar amount)."""
    if not isinstance(items, list):
        raise CopilotLiveQuotaError("missing usageItems array in billing response")

    used = 0.0
    net = 0.0
    for item in items:
        if not isinstance(item, dict):
            continue
        qty = item.get("grossQuantity")
        if not isinstance(qty, (int, float)):
            qty = item.get("netQuantity")
        if isinstance(qty, (int, float)):
            used += float(qty)
        amount = item.get("netAmount")
        if isinstance(amount, (int, float)):
            net += float(amount)
    return used, net


def _start_of_next_month(now: datetime) -> datetime:
    # Premium allowances reset on the 1st of each month at 00:00 UTC.
    year, month = now.year, now.month + 1
    if month > 12:
        month = 1
        year += 1
    return datetime(year, month, 1, tzinfo=timezone.utc)


def _extract_oauth_token(data) -> str | None:
    if not isinstance(data, dict):
        return None
    fallback: str | None = None
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        token = value.get("oauth_token")
        if not isinstance(token, str) or not token:
            continue
        if isinstance(key, str) and "github.com" in key:
            return token
        fallback = fallback or token
    return fallback


def _quota_window(
    name: str,
    label: str,
    snap,
    resets_at: datetime | None,
    *,
    unit: str = "",
    require_percent: bool = False,
) -> CopilotLiveWindow:
    if not isinstance(snap, dict):
        raise CopilotLiveQuotaError(f"missing {name} quota snapshot")

    overage = snap.get("overage_count")
    overage = int(overage) if isinstance(overage, (int, float)) and overage > 0 else 0

    if snap.get("unlimited"):
        detail = "unlimited" if not overage else f"unlimited (+{overage} overage)"
        return CopilotLiveWindow(name, label, None, resets_at, detail)

    entitlement = snap.get("entitlement")
    remaining = snap.get("remaining")
    if not isinstance(remaining, (int, float)):
        remaining = snap.get("quota_remaining")
    percent_remaining = snap.get("percent_remaining")

    percent: float | None = None
    if isinstance(percent_remaining, (int, float)):
        percent = 100.0 - float(percent_remaining)
    elif (
        isinstance(entitlement, (int, float))
        and entitlement > 0
        and isinstance(remaining, (int, float))
    ):
        percent = (1.0 - float(remaining) / float(entitlement)) * 100.0
    if percent is not None and percent < 0:
        percent = 0.0
    if percent is None and require_percent:
        raise CopilotLiveQuotaError(f"cannot parse {name} quota usage")

    suffix = f" {unit}" if unit else ""
    if isinstance(entitlement, (int, float)) and entitlement > 0:
        if isinstance(remaining, (int, float)):
            used = float(entitlement) - float(remaining)
        elif percent is not None:
            used = float(entitlement) * percent / 100.0
        else:
            used = 0.0
        detail = f"{int(round(used))} / {int(entitlement)}{suffix}"
        if overage:
            detail += f" (+{overage} overage)"
    elif isinstance(remaining, (int, float)):
        detail = f"{int(remaining)} remaining"
    else:
        detail = None

    return CopilotLiveWindow(name, label, percent, resets_at, detail)


def _best_effort_window(
    name: str, label: str, snap, resets_at: datetime | None
) -> CopilotLiveWindow:
    try:
        return _quota_window(name, label, snap, resets_at)
    except CopilotLiveQuotaError:
        return CopilotLiveWindow(name, label, None, resets_at, "unavailable")


def _parse_reset(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    s = value.strip()
    s = s.replace("Z", "+00:00") if s.endswith("Z") else s
    try:
        if len(s) == 10:  # date only, e.g. "2026-06-01"
            return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def client_from_env() -> CopilotLiveQuotaClient | CopilotBillingQuotaClient:
    """Picks the Copilot client based on configuration.

    If a PAT is configured (``COPILOT_GITHUB_TOKEN`` or ``COPILOT_TOKEN_FILE``)
    we use the documented billing REST API — needed when the OAuth token lives
    only in an editor's secret store (e.g. VS Code). Otherwise we fall back to
    reading the editor/CLI token file (``apps.json``/``hosts.json``) and the
    internal ``copilot_internal/user`` endpoint.
    """
    host = os.environ.get("GITHUB_API_HOST", GITHUB_API_HOST)
    ttl = float(os.environ.get("QUOTA_CACHE_TTL_SECONDS", "30"))

    token = os.environ.get("COPILOT_GITHUB_TOKEN") or None
    token_file = os.environ.get("COPILOT_TOKEN_FILE") or None
    if token or token_file:
        return CopilotBillingQuotaClient(
            token=token,
            token_file=token_file,
            username=os.environ.get("COPILOT_GITHUB_USER") or None,
            plan=os.environ.get("COPILOT_PLAN", "pro"),
            allowance=_int_env("COPILOT_PREMIUM_ALLOWANCE"),
            spend_budget=_float_env("COPILOT_SPEND_BUDGET"),
            host=host,
            cache_ttl_seconds=ttl,
        )

    data_dir = os.environ.get("COPILOT_DATA_DIR", "/data/copilot")
    return CopilotLiveQuotaClient(data_dir, host=host, cache_ttl_seconds=ttl)


def _int_env(name: str) -> int | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _float_env(name: str) -> float | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
