from __future__ import annotations

import base64
import json
import sqlite3
import urllib.error
from datetime import datetime, timezone

import pytest

from app import codex_quota, cursor_quota, quota


def _fake_jwt(sub: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).rstrip(b"=")
    return "header." + payload.decode() + ".sig"


def _write_state_db(data_dir, token: str | None, membership: str | None = None) -> None:
    db_dir = data_dir / "User" / "globalStorage"
    db_dir.mkdir(parents=True)
    con = sqlite3.connect(db_dir / "state.vscdb")
    con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
    if token is not None:
        con.execute(
            "INSERT INTO ItemTable VALUES (?, ?)", ("cursorAuth/accessToken", token)
        )
    if membership is not None:
        con.execute(
            "INSERT INTO ItemTable VALUES (?, ?)",
            ("cursorAuth/stripeMembershipType", membership),
        )
    con.commit()
    con.close()


def test_claude_window_uses_live_utilization_without_scaling() -> None:
    window = quota._window(
        "five_hour",
        "5-Hour Window",
        {
            "utilization": "42.5",
            "resets_at": "2026-05-20T12:34:56Z",
        },
    )

    assert window.percent == 42.5
    assert window.resets_at == datetime(2026, 5, 20, 12, 34, 56, tzinfo=timezone.utc)


def test_claude_window_rejects_missing_utilization() -> None:
    with pytest.raises(quota.LiveQuotaError, match="missing five_hour.utilization"):
        quota._window("five_hour", "5-Hour Window", {"resets_at": "2026-05-20T12:00:00Z"})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"utilization": "10.5"}, 10.5),
        ({"percent_used": 25}, 25.0),
        ({"used_percent": 33.3}, 33.3),
        ({"percent_left": 70}, 30.0),
        ({"remaining_percent": "12.5"}, 87.5),
    ],
)
def test_codex_window_accepts_alternate_percentage_fields(raw: dict, expected: float) -> None:
    window = codex_quota._window("five_hour", "5-Hour Window", raw)

    assert window.percent == expected


def test_codex_window_parses_epoch_milliseconds_reset() -> None:
    window = codex_quota._window(
        "seven_day",
        "Weekly Window",
        {
            "utilization": 88,
            "reset_time_ms": 1_700_000_000_000,
        },
    )

    assert window.resets_at == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)


def test_codex_client_falls_back_to_alternate_usage_path(tmp_path, monkeypatch) -> None:
    (tmp_path / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "fake-access-token",
                    "account_id": "acct_test",
                },
                "plan_type": "pro",
            }
        ),
        encoding="utf-8",
    )
    calls: list[str] = []
    seen_headers: list[dict[str, str]] = []

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "rate_limit": {
                        "primary_window": {"utilization": 11},
                        "weekly": {"percent_left": 25},
                    }
                }
            ).encode("utf-8")

    def fake_urlopen(req, timeout):
        calls.append(req.full_url)
        seen_headers.append({k.lower(): v for k, v in req.header_items()})
        if req.full_url.endswith(codex_quota.USAGE_PATH):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", hdrs=None, fp=None)
        return FakeResponse()

    monkeypatch.setattr(codex_quota.urllib.request, "urlopen", fake_urlopen)
    client = codex_quota.CodexLiveQuotaClient(
        tmp_path,
        host="https://example.test",
        cache_ttl_seconds=0,
    )

    snapshot = client.get()

    assert calls == [
        "https://example.test/backend-api/wham/usage",
        "https://example.test/backend-api/codex/usage",
    ]
    assert seen_headers[0]["authorization"] == "Bearer fake-access-token"
    assert seen_headers[0]["chatgpt-account-id"] == "acct_test"
    assert snapshot.five_hour.percent == 11.0
    assert snapshot.seven_day.percent == 75.0
    assert snapshot.plan_type == "pro"


def test_cursor_requests_window_uses_request_ratio() -> None:
    window = cursor_quota._requests_window(
        {"gpt-4": {"numRequests": 25, "maxRequestUsage": 500}}, None
    )

    assert window.name == "requests"
    assert window.percent == 5.0
    assert window.detail == "25 / 500 reqs"


def test_cursor_requests_window_handles_null_cap() -> None:
    window = cursor_quota._requests_window(
        {"gpt-4": {"numRequests": 7, "maxRequestUsage": None}}, None
    )

    assert window.percent is None
    assert window.detail == "7 reqs"


def test_cursor_requests_window_rejects_missing_gpt4() -> None:
    with pytest.raises(cursor_quota.CursorLiveQuotaError, match="missing gpt-4"):
        cursor_quota._requests_window({"startOfMonth": "2026-05-06T00:00:00Z"}, None)


def test_cursor_user_id_is_sub_before_pipe() -> None:
    token = _fake_jwt("auth0|user_12345")

    assert cursor_quota._user_id_from_jwt(token) == "auth0"


def test_cursor_invoice_dollars_sums_item_cents() -> None:
    invoice = {"items": [{"cents": 340}, {"cents": 60}, {"description": "no cents"}]}

    assert cursor_quota._invoice_dollars(invoice) == 4.0


def test_cursor_add_month_rolls_over_year_and_clamps_day() -> None:
    start = datetime(2026, 12, 31, 16, 9, tzinfo=timezone.utc)

    assert cursor_quota._add_month(start) == datetime(2027, 1, 31, 16, 9, tzinfo=timezone.utc)


def test_cursor_read_state_reads_token_from_sqlite(tmp_path) -> None:
    _write_state_db(tmp_path, _fake_jwt("u_1|s"), membership="pro")
    client = cursor_quota.CursorLiveQuotaClient(tmp_path)

    token, membership = client._read_state()

    assert membership == "pro"
    assert cursor_quota._user_id_from_jwt(token) == "u_1"


def test_cursor_read_state_reads_uncheckpointed_wal(tmp_path) -> None:
    db_dir = tmp_path / "User" / "globalStorage"
    db_dir.mkdir(parents=True)
    db = db_dir / "state.vscdb"
    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        writer.execute(
            "INSERT INTO ItemTable VALUES (?, ?)",
            ("cursorAuth/accessToken", _fake_jwt("wal_user|s")),
        )
        writer.execute(
            "INSERT INTO ItemTable VALUES (?, ?)",
            ("cursorAuth/stripeMembershipType", "pro"),
        )
        writer.commit()

        assert db.with_name("state.vscdb-wal").exists()

        client = cursor_quota.CursorLiveQuotaClient(tmp_path)
        token, membership = client._read_state()
    finally:
        writer.close()

    assert membership == "pro"
    assert cursor_quota._user_id_from_jwt(token) == "wal_user"


def test_cursor_read_state_snapshots_wal_when_direct_read_fails(tmp_path, monkeypatch) -> None:
    db_dir = tmp_path / "User" / "globalStorage"
    db_dir.mkdir(parents=True)
    db = db_dir / "state.vscdb"
    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        writer.execute(
            "INSERT INTO ItemTable VALUES (?, ?)",
            ("cursorAuth/accessToken", _fake_jwt("snapshot_user|s")),
        )
        writer.execute(
            "INSERT INTO ItemTable VALUES (?, ?)",
            ("cursorAuth/stripeMembershipType", "business"),
        )
        writer.commit()

        original_read = cursor_quota._read_state_db
        calls = []

        def flaky_read_state_db(path, timeout_seconds, *, readonly=True):
            calls.append((path, readonly))
            if path == db and readonly:
                raise sqlite3.OperationalError("unable to open database file")
            return original_read(path, timeout_seconds, readonly=readonly)

        monkeypatch.setattr(cursor_quota, "_read_state_db", flaky_read_state_db)
        client = cursor_quota.CursorLiveQuotaClient(tmp_path)
        token, membership = client._read_state()
    finally:
        writer.close()

    assert calls[0] == (db, True)
    assert calls[1][0].name == "state.vscdb"
    assert calls[1][0] != db
    assert calls[1][1] is False
    assert membership == "business"
    assert cursor_quota._user_id_from_jwt(token) == "snapshot_user"


def test_cursor_fetch_builds_session_cookie_and_windows(tmp_path, monkeypatch) -> None:
    _write_state_db(tmp_path, _fake_jwt("u_1|sess"), membership="pro")
    seen: list[tuple[str, dict]] = []

    class FakeResponse:
        status = 200

        def __init__(self, body: dict) -> None:
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self._body).encode("utf-8")

    def fake_urlopen(req, timeout):
        headers = {k.lower(): v for k, v in req.header_items()}
        seen.append((req.full_url, headers))
        if cursor_quota.USAGE_PATH in req.full_url:
            return FakeResponse(
                {
                    "gpt-4": {"numRequests": 50, "maxRequestUsage": 500},
                    "startOfMonth": "2026-05-06T16:09:14.520Z",
                }
            )
        if cursor_quota.HARD_LIMIT_PATH in req.full_url:
            return FakeResponse({"hardLimit": 20})
        return FakeResponse({"items": [{"cents": 250}]})

    monkeypatch.setattr(cursor_quota.urllib.request, "urlopen", fake_urlopen)
    client = cursor_quota.CursorLiveQuotaClient(
        tmp_path, host="https://example.test", cache_ttl_seconds=0
    )

    snap = client.get()

    assert seen[0][0] == "https://example.test/api/usage?user=u_1"
    assert seen[0][1]["cookie"].startswith("WorkosCursorSessionToken=u_1::")
    assert snap.requests.percent == 10.0
    assert snap.requests.detail == "50 / 500 reqs"
    assert snap.spend.percent == 12.5
    assert snap.spend.detail == "$2.50 / $20.00"
    assert snap.plan_type == "pro"
    assert snap.requests.resets_at == datetime(2026, 6, 6, 16, 9, 14, 520000, tzinfo=timezone.utc)


def test_cursor_spend_window_degrades_when_usage_based_off(tmp_path, monkeypatch) -> None:
    _write_state_db(tmp_path, _fake_jwt("u_1|sess"))

    class FakeResponse:
        status = 200

        def __init__(self, body: dict) -> None:
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self._body).encode("utf-8")

    def fake_urlopen(req, timeout):
        if cursor_quota.USAGE_PATH in req.full_url:
            return FakeResponse(
                {
                    "gpt-4": {"numRequests": 0, "maxRequestUsage": None},
                    "startOfMonth": "2026-05-06T00:00:00Z",
                }
            )
        if cursor_quota.HARD_LIMIT_PATH in req.full_url:
            return FakeResponse({"noUsageBasedAllowed": True})
        raise AssertionError("invoice should not be fetched when usage-based is off")

    monkeypatch.setattr(cursor_quota.urllib.request, "urlopen", fake_urlopen)
    client = cursor_quota.CursorLiveQuotaClient(
        tmp_path, host="https://example.test", cache_ttl_seconds=0
    )

    snap = client.get()

    assert snap.requests.percent is None
    assert snap.requests.detail == "0 reqs"
    assert snap.spend.percent is None
    assert snap.spend.detail == "usage-based off"
