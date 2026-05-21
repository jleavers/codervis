from __future__ import annotations

import base64
import calendar
import json
import os
import shutil
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from urllib.parse import quote


CURSOR_HOST = "https://cursor.com"
USAGE_PATH = "/api/usage"
HARD_LIMIT_PATH = "/api/dashboard/get-hard-limit"
MONTHLY_INVOICE_PATH = "/api/dashboard/get-monthly-invoice"

# state.vscdb lives at <data_dir>/User/globalStorage/state.vscdb. We query
# only auth-related ItemTable keys; we never read chat/composer state.
STATE_DB_RELPATH = ("User", "globalStorage", "state.vscdb")
ACCESS_TOKEN_KEY = "cursorAuth/accessToken"
MEMBERSHIP_KEY = "cursorAuth/stripeMembershipType"


@dataclass
class CursorLiveWindow:
    name: str
    label: str
    percent: float | None
    resets_at: datetime | None
    detail: str | None


@dataclass
class CursorLiveSnapshot:
    requests: CursorLiveWindow
    spend: CursorLiveWindow
    plan_type: str | None
    fetched_at: datetime
    raw: dict


class CursorLiveQuotaError(Exception):
    pass


class CursorLiveQuotaClient:
    """Reports Cursor monthly usage using the session token Cursor stores in
    its local SQLite state DB (<data_dir>/User/globalStorage/state.vscdb,
    bind-mounted into the container).

    Unlike Claude/Codex, Cursor keeps its credential in a SQLite ItemTable
    (key ``cursorAuth/accessToken``) rather than a JSON dotfile, authenticates
    with a ``Cookie: WorkosCursorSessionToken=<userId>::<jwt>`` header rather
    than a bearer token, and meters on a monthly billing cycle (premium-request
    count and usage-based dollar spend) rather than 5-hour/weekly windows.

    The token is a JWT; the user id is its ``sub`` claim up to the first ``|``.
    Cursor refreshes the token on the host; we re-read the DB on every call so
    we ride along on that cadence. A short in-memory cache avoids re-reading
    when many SSE clients are connected.

    The endpoints are undocumented. Any failure of the core ``/api/usage`` call
    raises CursorLiveQuotaError so the caller renders an "unavailable" state.
    The usage-based spend window is best-effort: if its extra calls fail (or
    usage-based billing is off, as on free plans) that window degrades to a
    null percent while the section stays live.
    """

    def __init__(
        self,
        data_dir: str | Path,
        host: str = CURSOR_HOST,
        cache_ttl_seconds: float = 30.0,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.host = host.rstrip("/")
        self.cache_ttl_seconds = cache_ttl_seconds
        self.timeout_seconds = timeout_seconds
        self._lock = Lock()
        self._cached: tuple[float, CursorLiveSnapshot] | None = None

    @property
    def credentials_path(self) -> Path:
        return self.data_dir.joinpath(*STATE_DB_RELPATH)

    def _read_state(self) -> tuple[str, str | None]:
        db = self.credentials_path
        if not db.exists():
            raise CursorLiveQuotaError(f"state DB not found: {db}")
        try:
            token, membership = _read_state_db(db, self.timeout_seconds)
        except sqlite3.Error as direct_error:
            try:
                token, membership = _read_state_snapshot(db, self.timeout_seconds)
            except (OSError, sqlite3.Error) as snapshot_error:
                raise CursorLiveQuotaError(
                    "cannot read state DB: "
                    f"{direct_error}; snapshot fallback failed: {snapshot_error}"
                ) from snapshot_error
        if not token:
            raise CursorLiveQuotaError("no cursorAuth/accessToken in state DB")
        return token, membership

    def _fetch(self) -> CursorLiveSnapshot:
        token, membership = self._read_state()
        user_id = _user_id_from_jwt(token)
        cookie = f"WorkosCursorSessionToken={user_id}::{token}"

        usage = self._get_json(
            self.host + USAGE_PATH + "?user=" + quote(user_id, safe=""), cookie
        )
        start_of_month = _parse_iso(usage.get("startOfMonth"))
        resets_at = _add_month(start_of_month) if start_of_month else None

        requests = _requests_window(usage, resets_at)
        spend = self._spend_window(cookie, resets_at)

        return CursorLiveSnapshot(
            requests=requests,
            spend=spend,
            plan_type=membership,
            fetched_at=datetime.now(timezone.utc),
            raw=usage,
        )

    def _spend_window(
        self, cookie: str, resets_at: datetime | None
    ) -> CursorLiveWindow:
        label = "Usage-Based Spend (month)"
        try:
            hard = self._post_json(self.host + HARD_LIMIT_PATH, cookie, {})
            if hard.get("noUsageBasedAllowed"):
                return CursorLiveWindow("spend", label, None, resets_at, "usage-based off")
            hard_limit = hard.get("hardLimit")

            now = datetime.now(timezone.utc)
            invoice = self._post_json(
                self.host + MONTHLY_INVOICE_PATH,
                cookie,
                {"month": now.month, "year": now.year, "includeUsageEvents": False},
            )
            dollars = _invoice_dollars(invoice)
            if isinstance(hard_limit, (int, float)) and hard_limit > 0:
                percent = dollars / float(hard_limit) * 100.0
                detail = f"${dollars:.2f} / ${float(hard_limit):.2f}"
            else:
                percent = None
                detail = f"${dollars:.2f}"
            return CursorLiveWindow("spend", label, percent, resets_at, detail)
        except CursorLiveQuotaError:
            return CursorLiveWindow("spend", label, None, resets_at, "unavailable")

    def _get_json(self, url: str, cookie: str) -> dict:
        return self._request_json(urllib.request.Request(url, headers=_headers(cookie)))

    def _post_json(self, url: str, cookie: str, body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=_headers(cookie), method="POST")
        return self._request_json(req)

    def _request_json(self, req: urllib.request.Request) -> dict:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                if resp.status != 200:
                    raise CursorLiveQuotaError(f"unexpected status {resp.status}")
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raise CursorLiveQuotaError(f"HTTP {e.code}: {e.reason}") from e
        except urllib.error.URLError as e:
            raise CursorLiveQuotaError(f"network error: {e.reason}") from e
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            raise CursorLiveQuotaError(f"response is not JSON: {e}") from e
        if not isinstance(payload, dict):
            raise CursorLiveQuotaError("response is not a JSON object")
        return payload

    def get(self) -> CursorLiveSnapshot:
        with self._lock:
            now = time.monotonic()
            if self._cached and (now - self._cached[0]) < self.cache_ttl_seconds:
                return self._cached[1]
            snap = self._fetch()
            self._cached = (now, snap)
            return snap


def _headers(cookie: str) -> dict[str, str]:
    # Mimic a dashboard request; cursor.com validates Origin/Referer.
    return {
        "Cookie": cookie,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://cursor.com",
        "Referer": "https://cursor.com/dashboard",
        "User-Agent": "codervis/0.1 (+local dashboard)",
    }


def _read_state_db(
    db: Path, timeout_seconds: float, *, readonly: bool = True
) -> tuple[str | None, str | None]:
    if readonly:
        # Open read-only, but do not mark the DB immutable: Cursor keeps this
        # database in WAL mode, and immutable connections ignore uncheckpointed
        # writes in state.vscdb-wal.
        posix = db.resolve().as_posix()
        if not posix.startswith("/"):
            posix = "/" + posix
        uri = "file:" + quote(posix, safe="/") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=timeout_seconds)
    else:
        con = sqlite3.connect(db, timeout=timeout_seconds)
    try:
        token = _query_item(con, ACCESS_TOKEN_KEY)
        membership = _query_item(con, MEMBERSHIP_KEY)
        return token, membership
    finally:
        con.close()


def _read_state_snapshot(db: Path, timeout_seconds: float) -> tuple[str | None, str | None]:
    last_error: OSError | sqlite3.Error | None = None
    for _ in range(2):
        try:
            with tempfile.TemporaryDirectory(prefix="codervis-cursor-state-") as tmp:
                snapshot = Path(tmp) / db.name
                _copy_sqlite_snapshot(db, snapshot)
                return _read_state_db(snapshot, timeout_seconds, readonly=False)
        except (OSError, sqlite3.Error) as e:
            last_error = e
            time.sleep(0.05)
    if last_error is not None:
        raise last_error
    raise sqlite3.OperationalError("snapshot fallback failed")


def _copy_sqlite_snapshot(db: Path, snapshot: Path) -> None:
    shutil.copy2(db, snapshot)
    wal = db.with_name(db.name + "-wal")
    if not wal.exists():
        return
    try:
        shutil.copy2(wal, snapshot.with_name(snapshot.name + "-wal"))
    except FileNotFoundError:
        # Cursor may checkpoint between the exists() check and the copy. In
        # that case the main DB copy is the best available consistent source.
        return


def _query_item(con: sqlite3.Connection, key: str) -> str | None:
    cur = con.execute("SELECT value FROM ItemTable WHERE key = ?", (key,))
    row = cur.fetchone()
    if not row or row[0] is None:
        return None
    value = row[0]
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value)


def _user_id_from_jwt(token: str) -> str:
    parts = token.split(".")
    if len(parts) < 2:
        raise CursorLiveQuotaError("access token is not a JWT")
    seg = parts[1]
    seg += "=" * (-len(seg) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(seg))
    except (ValueError, json.JSONDecodeError) as e:
        raise CursorLiveQuotaError(f"cannot decode access token: {e}") from e
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        raise CursorLiveQuotaError("no sub claim in access token")
    return sub.split("|")[0]


def _requests_window(usage: dict, resets_at: datetime | None) -> CursorLiveWindow:
    gpt4 = usage.get("gpt-4")
    if not isinstance(gpt4, dict):
        raise CursorLiveQuotaError("missing gpt-4 usage window in response")
    num = gpt4.get("numRequests")
    if not isinstance(num, (int, float)):
        raise CursorLiveQuotaError("numRequests is not numeric")
    max_req = gpt4.get("maxRequestUsage")
    label = "Premium Requests (month)"
    if isinstance(max_req, (int, float)) and max_req > 0:
        percent = float(num) / float(max_req) * 100.0
        detail = f"{int(num)} / {int(max_req)} reqs"
    else:
        percent = None
        detail = f"{int(num)} reqs"
    return CursorLiveWindow("requests", label, percent, resets_at, detail)


def _invoice_dollars(invoice: dict) -> float:
    items = invoice.get("items")
    if not isinstance(items, list):
        return 0.0
    cents = 0.0
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("cents"), (int, float)):
            cents += float(item["cents"])
    return cents / 100.0


def _parse_iso(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    v = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _add_month(dt: datetime) -> datetime:
    month = dt.month + 1
    year = dt.year
    if month > 12:
        month = 1
        year += 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def client_from_env() -> CursorLiveQuotaClient:
    data_dir = os.environ.get("CURSOR_DATA_DIR", "/data/cursor")
    host = os.environ.get("CURSOR_HOST", CURSOR_HOST)
    ttl = float(os.environ.get("QUOTA_CACHE_TTL_SECONDS", "30"))
    return CursorLiveQuotaClient(data_dir, host=host, cache_ttl_seconds=ttl)
