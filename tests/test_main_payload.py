from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import main
from app.claude_activity import ClaudeActivitySnapshot
from app.codex_activity import CodexActivitySnapshot
from app.copilot_activity import CopilotActivitySnapshot
from app.cursor_activity import CursorActivitySnapshot
from app.gemini_activity import GeminiActivitySnapshot


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
        self.calls = 0

    def get(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._snapshot

    def credentials_present(self) -> bool:
        return self.credentials_path.exists()


def _window(percent: float, resets_at: datetime | None = None):
    return SimpleNamespace(percent=percent, resets_at=resets_at)


def _named_window(name, label, percent, resets_at=None, detail=None):
    return SimpleNamespace(
        name=name, label=label, percent=percent, resets_at=resets_at, detail=detail
    )


def _win(section: dict, name: str) -> dict:
    return next(w for w in section["windows"] if w["name"] == name)


def _stub_cursor(monkeypatch, *, snapshot=None, error=None, data_root_exists=True):
    monkeypatch.setattr(
        main,
        "_cursor_activity",
        ActivityStub(
            CursorActivitySnapshot(last_activity=None, data_root_exists=data_root_exists)
        ),
    )
    if snapshot is None and error is None:
        monkeypatch.setattr(main, "_cursor", None)
    else:
        monkeypatch.setattr(main, "_cursor", QuotaClientStub(snapshot, error))


def _stub_copilot(monkeypatch, *, snapshot=None, error=None, data_root_exists=True):
    monkeypatch.setattr(
        main,
        "_copilot_activity",
        ActivityStub(
            CopilotActivitySnapshot(last_activity=None, data_root_exists=data_root_exists)
        ),
    )
    if snapshot is None and error is None:
        monkeypatch.setattr(main, "_copilot", None)
    else:
        monkeypatch.setattr(main, "_copilot", QuotaClientStub(snapshot, error))


def _stub_gemini(monkeypatch, *, snapshot=None, error=None, data_root_exists=True):
    monkeypatch.setattr(
        main,
        "_gemini_activity",
        ActivityStub(
            GeminiActivitySnapshot(last_activity=None, data_root_exists=data_root_exists)
        ),
    )
    if snapshot is None and error is None:
        monkeypatch.setattr(main, "_gemini", None)
    else:
        monkeypatch.setattr(main, "_gemini", QuotaClientStub(snapshot, error))


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
    _stub_cursor(
        monkeypatch,
        snapshot=SimpleNamespace(
            requests=_named_window(
                "requests", "Premium Requests (month)", 2.4, reset, "12 / 500 reqs"
            ),
            spend=_named_window(
                "spend", "Usage-Based Spend (month)", 17.0, reset, "$3.40 / $20.00"
            ),
            plan_type="pro",
        ),
    )
    _stub_copilot(
        monkeypatch,
        snapshot=SimpleNamespace(
            premium=_named_window(
                "premium", "Premium Requests (month)", 11.5, reset, "34 / 300 reqs"
            ),
            secondary=_named_window(
                "secondary", "Chat (month)", None, reset, "unlimited"
            ),
            plan_type="individual",
        ),
    )
    _stub_gemini(
        monkeypatch,
        snapshot=SimpleNamespace(
            pro=_named_window(
                "pro", "Pro Requests (day)", 5.0, reset, "gemini-3.1-pro-preview: 95% left"
            ),
            flash=_named_window(
                "flash", "Flash Requests (day)", 15.0, reset, "gemini-3-flash-preview: 85% left"
            ),
            plan_type="Gemini Code Assist in Google One AI Pro",
        ),
    )

    response = TestClient(main.app).get("/api/usage")

    assert response.status_code == 200
    data = response.json()
    assert data["claude"]["source"] == "live"
    assert data["claude"]["enabled"] is True
    assert _win(data["claude"], "five_hour")["percent"] == 12.35
    assert _win(data["claude"], "seven_day")["percent"] == 67.89
    assert _win(data["claude"], "five_hour")["resets_at"] == "2026-05-20T12:00:00+00:00"
    assert data["claude"]["subscription_type"] == "max"
    assert data["claude"]["last_activity"] == "2026-05-20T09:30:00+00:00"
    assert data["codex"]["source"] == "live"
    assert data["codex"]["enabled"] is True
    assert _win(data["codex"], "five_hour")["percent"] == 33.33
    assert _win(data["codex"], "seven_day")["percent"] == 88.89
    assert data["codex"]["subscription_type"] == "pro"
    assert data["cursor"]["source"] == "live"
    assert data["cursor"]["enabled"] is True
    assert _win(data["cursor"], "requests")["percent"] == 2.4
    assert _win(data["cursor"], "requests")["detail"] == "12 / 500 reqs"
    assert _win(data["cursor"], "spend")["percent"] == 17.0
    assert _win(data["cursor"], "spend")["detail"] == "$3.40 / $20.00"
    assert data["cursor"]["subscription_type"] == "pro"
    assert data["copilot"]["source"] == "live"
    assert data["copilot"]["enabled"] is True
    assert _win(data["copilot"], "premium")["percent"] == 11.5
    assert _win(data["copilot"], "premium")["detail"] == "34 / 300 reqs"
    assert _win(data["copilot"], "secondary")["percent"] is None
    assert _win(data["copilot"], "secondary")["detail"] == "unlimited"
    assert data["copilot"]["subscription_type"] == "individual"
    assert data["gemini"]["source"] == "live"
    assert data["gemini"]["enabled"] is True
    assert _win(data["gemini"], "pro")["percent"] == 5.0
    assert _win(data["gemini"], "pro")["detail"] == "gemini-3.1-pro-preview: 95% left"
    assert _win(data["gemini"], "flash")["percent"] == 15.0
    assert _win(data["gemini"], "flash")["detail"] == "gemini-3-flash-preview: 85% left"
    assert data["gemini"]["subscription_type"] == "Gemini Code Assist in Google One AI Pro"


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
    _stub_cursor(monkeypatch, error=main.CursorLiveQuotaError("cursor upstream changed"))
    _stub_copilot(monkeypatch, error=main.CopilotLiveQuotaError("copilot upstream changed"))
    _stub_gemini(monkeypatch, error=main.GeminiLiveQuotaError("gemini upstream changed"))

    data = main._build_payload()

    assert data["claude"]["source"] == "unavailable"
    assert data["claude"]["source_error"] == "claude upstream changed"
    assert _win(data["claude"], "five_hour")["percent"] is None
    assert _win(data["claude"], "seven_day")["percent"] is None
    assert data["codex"]["source"] == "unavailable"
    assert data["codex"]["source_error"] == "codex upstream changed"
    assert _win(data["codex"], "five_hour")["percent"] is None
    assert _win(data["codex"], "seven_day")["percent"] is None
    assert data["cursor"]["source"] == "unavailable"
    assert data["cursor"]["source_error"] == "cursor upstream changed"
    assert _win(data["cursor"], "requests")["percent"] is None
    assert _win(data["cursor"], "spend")["percent"] is None
    assert data["copilot"]["source"] == "unavailable"
    assert data["copilot"]["source_error"] == "copilot upstream changed"
    assert _win(data["copilot"], "premium")["percent"] is None
    assert _win(data["copilot"], "secondary")["percent"] is None
    assert data["gemini"]["source"] == "unavailable"
    assert data["gemini"]["source_error"] == "gemini upstream changed"
    assert _win(data["gemini"], "pro")["percent"] is None
    assert _win(data["gemini"], "flash")["percent"] is None


def test_provider_defaults_do_not_suppress_live_clients(monkeypatch) -> None:
    monkeypatch.setattr(main, "CLAUDE_ENABLED", False)
    monkeypatch.setattr(main, "CODEX_ENABLED", False)
    monkeypatch.setattr(main, "CURSOR_ENABLED", False)
    monkeypatch.setattr(main, "COPILOT_ENABLED", False)
    monkeypatch.setattr(main, "GEMINI_ENABLED", False)

    monkeypatch.setattr(
        main,
        "_claude_activity",
        ActivityStub(ClaudeActivitySnapshot(last_activity=None, data_root_exists=True)),
    )
    monkeypatch.setattr(
        main,
        "_codex_activity",
        ActivityStub(CodexActivitySnapshot(last_activity=None, data_root_exists=True)),
    )

    claude = QuotaClientStub(
        SimpleNamespace(
            five_hour=_window(10),
            seven_day=_window(20),
            subscription_type="max",
        )
    )
    codex = QuotaClientStub(
        SimpleNamespace(
            five_hour=_window(30),
            seven_day=_window(40),
            plan_type="pro",
        )
    )
    monkeypatch.setattr(main, "_live", claude)
    monkeypatch.setattr(main, "_codex", codex)

    cursor = QuotaClientStub(
        SimpleNamespace(
            requests=_named_window("requests", "Premium Requests (month)", 50),
            spend=_named_window("spend", "Usage-Based Spend (month)", 60),
            plan_type="pro",
        )
    )
    copilot = QuotaClientStub(
        SimpleNamespace(
            premium=_named_window("premium", "Premium Requests (month)", 70),
            secondary=_named_window("secondary", "Chat (month)", None),
            plan_type="individual",
        )
    )
    gemini = QuotaClientStub(
        SimpleNamespace(
            pro=_named_window("pro", "Pro Requests (day)", 80),
            flash=_named_window("flash", "Flash Requests (day)", 90),
            plan_type="pro",
        )
    )
    _stub_cursor(monkeypatch, snapshot=cursor._snapshot)
    _stub_copilot(monkeypatch, snapshot=copilot._snapshot)
    _stub_gemini(monkeypatch, snapshot=gemini._snapshot)
    monkeypatch.setattr(main, "_cursor", cursor)
    monkeypatch.setattr(main, "_copilot", copilot)
    monkeypatch.setattr(main, "_gemini", gemini)

    data = main._build_payload()

    for key in ("claude", "codex", "cursor", "copilot", "gemini"):
        assert data[key]["enabled"] is False
        assert data[key]["source"] == "live"
    assert [client.calls for client in (claude, codex, cursor, copilot, gemini)] == [
        1,
        1,
        1,
        1,
        1,
    ]


def test_health_reports_configured_widget_defaults(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(main, "CLAUDE_ENABLED", False)
    monkeypatch.setattr(main, "CODEX_ENABLED", True)
    monkeypatch.setattr(main, "CURSOR_ENABLED", False)
    monkeypatch.setattr(main, "COPILOT_ENABLED", True)
    monkeypatch.setattr(main, "GEMINI_ENABLED", False)

    activity = SimpleNamespace(data_dir=tmp_path)
    monkeypatch.setattr(main, "_claude_activity", activity)
    monkeypatch.setattr(main, "_codex_activity", activity)
    monkeypatch.setattr(main, "_cursor_activity", activity)
    monkeypatch.setattr(main, "_copilot_activity", activity)
    monkeypatch.setattr(main, "_gemini_activity", activity)

    credential = tmp_path / "credential"
    credential.touch()
    clients = [QuotaClientStub() for _ in range(5)]
    for client in clients:
        client.credentials_path = credential
    monkeypatch.setattr(main, "_live", clients[0])
    monkeypatch.setattr(main, "_codex", clients[1])
    monkeypatch.setattr(main, "_cursor", clients[2])
    monkeypatch.setattr(main, "_copilot", clients[3])
    monkeypatch.setattr(main, "_gemini", clients[4])

    data = TestClient(main.app).get("/healthz").json()

    assert data["claude_enabled"] is False
    assert data["codex_enabled"] is True
    assert data["cursor_enabled"] is False
    assert data["copilot_enabled"] is True
    assert data["gemini_enabled"] is False
