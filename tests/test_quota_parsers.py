from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timezone

import pytest

from app import codex_quota, quota


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
