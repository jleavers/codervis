from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timezone

import pytest

from app import codex_quota, quota


class _JSONResponse:
    """A stand-in for http.client.HTTPResponse, which reads in sized chunks.

    The clients read the body under a byte cap now, so this has to take a size
    argument and return b"" at the end, as the real response does. Serving the
    whole body regardless of the size asked for would hide a cap that does not
    work.
    """

    status = 200

    def __init__(self, body: dict) -> None:
        self._remaining = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._remaining)
        chunk, self._remaining = self._remaining[:size], self._remaining[size:]
        return chunk


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


def test_claude_client_reads_fable_window_from_same_usage_response(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "fake-access-token",
                    "subscriptionType": "max",
                }
            }
        ),
        encoding="utf-8",
    )
    requests = []

    def fake_urlopen(req, timeout):
        requests.append(req)
        return _JSONResponse(
            {
                "five_hour": {
                    "utilization": 12.5,
                    "resets_at": "2026-09-03T12:00:00Z",
                },
                "seven_day": {
                    "utilization": 45,
                    "resets_at": "2026-09-07T08:00:00Z",
                },
                "limits": [
                    {
                        "kind": "session",
                        "group": "session",
                        "percent": 12.5,
                        "resets_at": "2026-09-03T12:00:00Z",
                        "scope": None,
                        "is_active": True,
                    },
                    {
                        "kind": "weekly_all",
                        "group": "weekly",
                        "percent": 45,
                        "resets_at": "2026-09-07T08:00:00Z",
                        "scope": None,
                        "is_active": False,
                    },
                    {
                        "kind": "weekly_scoped",
                        "group": "weekly",
                        "percent": "23.75",
                        "resets_at": "2026-09-07T08:00:01Z",
                        "scope": {
                            "model": {"id": None, "display_name": "Fable"},
                            "surface": None,
                        },
                        "is_active": False,
                    },
                ],
            }
        )

    monkeypatch.setattr(quota.urllib.request, "urlopen", fake_urlopen)
    snapshot = quota.LiveQuotaClient(tmp_path).get()

    assert len(requests) == 1
    assert requests[0].full_url.endswith(quota.USAGE_PATH)
    assert snapshot.seven_day_fable.name == "seven_day_fable"
    assert snapshot.seven_day_fable.label == "Weekly Window (Fable)"
    assert snapshot.seven_day_fable.percent == 23.75
    assert snapshot.seven_day_fable.resets_at == datetime(
        2026, 9, 7, 8, 0, 1, tzinfo=timezone.utc
    )


@pytest.mark.parametrize("malformed_percent", ["not-a-number", "NaN", "Infinity", True])
def test_claude_client_ignores_malformed_optional_fable_window(
    tmp_path, monkeypatch, malformed_percent
) -> None:
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "fake-access-token"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        quota.urllib.request,
        "urlopen",
        lambda req, timeout: _JSONResponse(
            {
                "five_hour": {"utilization": 12.5},
                "seven_day": {"utilization": 45},
                "limits": [
                    {
                        "kind": "weekly_scoped",
                        "percent": malformed_percent,
                        "scope": {"model": {"display_name": "Fable 5"}},
                    }
                ],
            }
        ),
    )

    snapshot = quota.LiveQuotaClient(tmp_path).get()

    assert snapshot.five_hour.percent == 12.5
    assert snapshot.seven_day.percent == 45
    assert snapshot.seven_day_fable is None


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
    window = codex_quota._window("seven_day", "Weekly Window", raw)

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


def test_codex_window_rejects_missing_weekly_window() -> None:
    with pytest.raises(
        codex_quota.CodexLiveQuotaError,
        match="missing or invalid seven_day usage window",
    ):
        codex_quota._window("seven_day", "Weekly Window", None)


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

    def fake_urlopen(req, timeout):
        calls.append(req.full_url)
        seen_headers.append({k.lower(): v for k, v in req.header_items()})
        if req.full_url.endswith(codex_quota.USAGE_PATH):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", hdrs=None, fp=None)
        return _JSONResponse(
            {
                "rate_limit": {
                    "primary_window": {"utilization": 11},
                    "weekly": {"percent_left": 25},
                }
            }
        )

    monkeypatch.setattr(codex_quota.urllib.request, "urlopen", fake_urlopen)
    client = codex_quota.CodexLiveQuotaClient(
        tmp_path,
        host="https://example.test",
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


def test_codex_client_uses_primary_as_weekly_when_secondary_is_null(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "fake-access-token"}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        codex_quota.urllib.request,
        "urlopen",
        lambda req, timeout: _JSONResponse(
            {
                "rate_limit": {
                    "primary_window": {
                        "utilization": 11,
                        "reset_at": 1_700_000_000,
                        "windowDurationMins": 10_080,
                    },
                    "secondary_window": None,
                    "plan_type": "pro",
                }
            }
        ),
    )
    client = codex_quota.CodexLiveQuotaClient(
        tmp_path,
        host="https://example.test",
    )

    snapshot = client.get()

    assert snapshot.five_hour.percent is None
    assert snapshot.five_hour.resets_at is None
    assert snapshot.seven_day.percent == 11.0
    assert snapshot.seven_day.resets_at == datetime.fromtimestamp(
        1_700_000_000, tz=timezone.utc
    )
    assert snapshot.plan_type == "pro"


def test_codex_client_keeps_durationless_primary_as_weekly_when_alone(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "fake-access-token"}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        codex_quota.urllib.request,
        "urlopen",
        lambda req, timeout: _JSONResponse(
            {
                "rate_limit": {
                    "primary_window": {"used_percent": 22},
                    "secondary_window": None,
                    "plan_type": "pro",
                }
            }
        ),
    )
    client = codex_quota.CodexLiveQuotaClient(
        tmp_path,
        host="https://example.test",
    )

    snapshot = client.get()

    assert snapshot.five_hour.percent is None
    assert snapshot.seven_day.percent == 22.0


def test_codex_client_exposes_five_hour_and_weekly_windows(tmp_path, monkeypatch) -> None:
    (tmp_path / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "fake-access-token"}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        codex_quota.urllib.request,
        "urlopen",
        lambda req, timeout: _JSONResponse(
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 37,
                        "limit_window_seconds": 604_800,
                        "reset_at": 1_700_500_000,
                    },
                    "secondary_window": {
                        "used_percent": 12.5,
                        "windowDurationMins": 300,
                        "reset_at": 1_700_000_000,
                    },
                    "plan_type": "pro",
                }
            }
        ),
    )
    client = codex_quota.CodexLiveQuotaClient(
        tmp_path,
        host="https://example.test",
    )

    snapshot = client.get()

    assert snapshot.five_hour.name == "five_hour"
    assert snapshot.five_hour.percent == 12.5
    assert snapshot.five_hour.resets_at == datetime.fromtimestamp(
        1_700_000_000, tz=timezone.utc
    )
    assert snapshot.seven_day.name == "seven_day"
    assert snapshot.seven_day.percent == 37.0
    assert snapshot.seven_day.resets_at == datetime.fromtimestamp(
        1_700_500_000, tz=timezone.utc
    )
