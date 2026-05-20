from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import main
from app.claude_activity import ClaudeActivitySnapshot
from app.codex_activity import CodexActivitySnapshot


class ActivityStub:
    def __init__(self, snapshot, data_dir: Path | None = None) -> None:
        self._snapshot = snapshot
        self.data_dir = data_dir or Path(".")

    def snapshot(self):
        return self._snapshot


class QuotaClientStub:
    credentials_path = Path("unused")

    def __init__(self, snapshot=None, error: Exception | None = None) -> None:
        self._snapshot = snapshot
        self._error = error

    def get(self):
        if self._error is not None:
            raise self._error
        return self._snapshot


def _window(percent: float, resets_at: datetime | None = None):
    return SimpleNamespace(percent=percent, resets_at=resets_at)


def test_api_usage_returns_live_payload_without_scaling(monkeypatch) -> None:
    reset = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    activity = datetime(2026, 5, 20, 9, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(
        main,
        "_claude_activity",
        ActivityStub(ClaudeActivitySnapshot(last_activity=activity, data_root_exists=True)),
    )
    monkeypatch.setattr(
        main,
        "_live",
        QuotaClientStub(
            SimpleNamespace(
                five_hour=_window(12.345, reset),
                seven_day=_window(67.891),
                subscription_type="max",
            )
        ),
    )
    monkeypatch.setattr(
        main,
        "_codex_activity",
        ActivityStub(CodexActivitySnapshot(last_activity=None, data_root_exists=True)),
    )
    monkeypatch.setattr(
        main,
        "_codex",
        QuotaClientStub(
            SimpleNamespace(
                five_hour=_window(33.333),
                seven_day=_window(88.888, reset),
                plan_type="pro",
            )
        ),
    )

    response = TestClient(main.app).get("/api/usage")

    assert response.status_code == 200
    data = response.json()
    assert data["claude"]["source"] == "live"
    assert data["claude"]["five_hour"]["percent"] == 12.35
    assert data["claude"]["seven_day"]["percent"] == 67.89
    assert data["claude"]["five_hour"]["resets_at"] == "2026-05-20T12:00:00+00:00"
    assert data["claude"]["subscription_type"] == "max"
    assert data["claude"]["last_activity"] == "2026-05-20T09:30:00+00:00"
    assert data["codex"]["source"] == "live"
    assert data["codex"]["enabled"] is True
    assert data["codex"]["five_hour"]["percent"] == 33.33
    assert data["codex"]["seven_day"]["percent"] == 88.89
    assert data["codex"]["subscription_type"] == "pro"


def test_payload_contains_unavailable_states_on_live_errors(monkeypatch) -> None:
    monkeypatch.setattr(
        main,
        "_claude_activity",
        ActivityStub(ClaudeActivitySnapshot(last_activity=None, data_root_exists=False)),
    )
    monkeypatch.setattr(
        main,
        "_live",
        QuotaClientStub(error=main.LiveQuotaError("claude upstream changed")),
    )
    monkeypatch.setattr(
        main,
        "_codex_activity",
        ActivityStub(CodexActivitySnapshot(last_activity=None, data_root_exists=True)),
    )
    monkeypatch.setattr(
        main,
        "_codex",
        QuotaClientStub(error=main.CodexLiveQuotaError("codex upstream changed")),
    )

    data = main._build_payload()

    assert data["claude"]["source"] == "unavailable"
    assert data["claude"]["source_error"] == "claude upstream changed"
    assert data["claude"]["five_hour"]["percent"] is None
    assert data["claude"]["seven_day"]["percent"] is None
    assert data["codex"]["source"] == "unavailable"
    assert data["codex"]["source_error"] == "codex upstream changed"
    assert data["codex"]["five_hour"]["percent"] is None
    assert data["codex"]["seven_day"]["percent"] is None


def test_codex_section_reports_disabled_when_client_is_absent(monkeypatch) -> None:
    monkeypatch.setattr(
        main,
        "_codex_activity",
        ActivityStub(CodexActivitySnapshot(last_activity=None, data_root_exists=False)),
    )
    monkeypatch.setattr(main, "_codex", None)

    data = main._codex_section()

    assert data["enabled"] is False
    assert data["source"] == "disabled"
    assert data["five_hour"]["percent"] is None
    assert data["seven_day"]["percent"] is None
    assert data["data_root_exists"] is False
