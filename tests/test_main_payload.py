from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import main
from app.claude_activity import ClaudeActivitySnapshot
from app.codex_activity import CodexActivitySnapshot

LOOPBACK = "http://127.0.0.1:8765"


def loopback_client() -> TestClient:
    """A client that speaks as a browser on this machine does.

    The app serves only the hosts the operator named, and `TestClient`'s own default
    (`testserver`) is not one of them; `tests/test_host_allowlist.py` covers the refusal.
    """
    return TestClient(main.app, base_url=LOOPBACK)


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


def _window(percent: float | None, resets_at: datetime | None = None):
    return SimpleNamespace(percent=percent, resets_at=resets_at)


def _win(section: dict, name: str) -> dict:
    return next(w for w in section["windows"] if w["name"] == name)


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
                seven_day_fable=_window(45.678, reset),
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
    response = loopback_client().get("/api/usage")

    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"claude", "codex", "server_time"}
    assert data["claude"]["source"] == "live"
    assert data["claude"]["enabled"] is True
    assert [window["name"] for window in data["claude"]["windows"]] == [
        "five_hour",
        "seven_day",
        "seven_day_fable",
    ]
    assert _win(data["claude"], "five_hour")["percent"] == 12.35
    assert _win(data["claude"], "seven_day")["percent"] == 67.89
    assert _win(data["claude"], "seven_day_fable")["percent"] == 45.68
    assert _win(data["claude"], "seven_day_fable")["label"] == "Weekly Window (Fable)"
    assert _win(data["claude"], "five_hour")["resets_at"] == "2026-05-20T12:00:00+00:00"
    assert (
        _win(data["claude"], "seven_day_fable")["resets_at"]
        == "2026-05-20T12:00:00+00:00"
    )
    assert data["claude"]["subscription_type"] == "max"
    assert data["claude"]["last_activity"] == "2026-05-20T09:30:00+00:00"
    assert data["codex"]["source"] == "live"
    assert data["codex"]["enabled"] is True
    assert [window["name"] for window in data["codex"]["windows"]] == [
        "five_hour",
        "seven_day",
    ]
    assert _win(data["codex"], "five_hour")["percent"] == 33.33
    assert _win(data["codex"], "seven_day")["percent"] == 88.89
    assert data["codex"]["subscription_type"] == "pro"


def test_claude_section_renders_stable_fable_window(monkeypatch) -> None:
    reset = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        main,
        "_claude_activity",
        ActivityStub(ClaudeActivitySnapshot(last_activity=None, data_root_exists=True)),
    )
    monkeypatch.setattr(
        main,
        "_live",
        QuotaClientStub(
            SimpleNamespace(
                five_hour=_window(10),
                seven_day=_window(20),
                seven_day_fable=_window(30.125, reset),
                subscription_type="max",
            )
        ),
    )

    data = main._claude_section()

    assert data["windows"] == [
        {
            "name": "five_hour",
            "label": "5-Hour Window",
            "percent": 10.0,
            "resets_at": None,
            "detail": None,
        },
        {
            "name": "seven_day",
            "label": "Weekly Window",
            "percent": 20.0,
            "resets_at": None,
            "detail": None,
        },
        {
            "name": "seven_day_fable",
            "label": "Weekly Window (Fable)",
            "percent": 30.12,
            "resets_at": "2026-09-07T08:00:00+00:00",
            "detail": None,
        },
    ]


def test_codex_section_renders_five_hour_and_weekly_windows(monkeypatch) -> None:
    activity = datetime(2026, 5, 20, 9, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(
        main,
        "_codex_activity",
        ActivityStub(CodexActivitySnapshot(last_activity=activity, data_root_exists=True)),
    )
    monkeypatch.setattr(
        main,
        "_codex",
        QuotaClientStub(
            SimpleNamespace(
                five_hour=_window(12.345),
                seven_day=_window(33.333),
                plan_type="pro",
            )
        ),
    )

    data = main._codex_section()

    assert data["source"] == "live"
    assert data["source_error"] is None
    assert data["windows"] == [
        {
            "name": "five_hour",
            "label": "5-Hour Window",
            "percent": 12.35,
            "resets_at": None,
            "detail": None,
        },
        {
            "name": "seven_day",
            "label": "Weekly Window",
            "percent": 33.33,
            "resets_at": None,
            "detail": None,
        }
    ]
    assert data["subscription_type"] == "pro"
    assert data["last_activity"] == "2026-05-20T09:30:00+00:00"


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
    assert _win(data["claude"], "five_hour")["percent"] is None
    assert _win(data["claude"], "seven_day")["percent"] is None
    assert _win(data["claude"], "seven_day_fable")["percent"] is None
    assert data["codex"]["source"] == "unavailable"
    assert data["codex"]["source_error"] == "codex upstream changed"
    assert [window["name"] for window in data["codex"]["windows"]] == [
        "five_hour",
        "seven_day",
    ]
    assert _win(data["codex"], "five_hour")["percent"] is None
    assert _win(data["codex"], "seven_day")["percent"] is None


def test_provider_defaults_do_not_suppress_live_clients(monkeypatch) -> None:
    monkeypatch.setattr(main, "CLAUDE_ENABLED", False)
    monkeypatch.setattr(main, "CODEX_ENABLED", False)

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

    data = main._build_payload()

    for key in ("claude", "codex"):
        assert data[key]["enabled"] is False
        assert data[key]["source"] == "live"
    assert [client.calls for client in (claude, codex)] == [1, 1]


def test_health_reports_configured_widget_defaults(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(main, "CLAUDE_ENABLED", False)
    monkeypatch.setattr(main, "CODEX_ENABLED", True)

    activity = SimpleNamespace(data_dir=tmp_path)
    monkeypatch.setattr(main, "_claude_activity", activity)
    monkeypatch.setattr(main, "_codex_activity", activity)

    credential = tmp_path / "credential"
    credential.touch()
    clients = [QuotaClientStub() for _ in range(2)]
    for client in clients:
        client.credentials_path = credential
    monkeypatch.setattr(main, "_live", clients[0])
    monkeypatch.setattr(main, "_codex", clients[1])

    data = loopback_client().get("/healthz").json()

    assert data["claude_enabled"] is False
    assert data["codex_enabled"] is True


def test_index_renders_accessible_widget_toggles(monkeypatch) -> None:
    def section(enabled: bool) -> dict:
        return {
            "enabled": enabled,
            "windows": [],
            "source": "live",
            "source_error": None,
            "subscription_type": None,
            "last_activity": None,
            "data_root_exists": True,
        }

    payload = {
        "claude": section(True),
        "codex": section(False),
        "server_time": "2026-06-08T00:00:00+00:00",
    }
    monkeypatch.setattr(main, "_build_payload", lambda: payload)

    response = loopback_client().get("/")

    assert response.status_code == 200
    html = response.text
    assert html.count('class="widget-toggle-input"') == 2
    for key, title in (
        ("claude", "Claude Code"),
        ("codex", "Codex"),
    ):
        assert f'id="toggle-{key}"' in html
        assert f'data-provider="{key}"' in html
        assert f'aria-label="Enable {title} widget"' in html
    codex_start = html.index('id="provider-codex"')
    codex_card = html[codex_start : html.index(">", codex_start)]
    assert 'data-source="disabled"' in codex_card
    assert 'data-widget-enabled="false"' in codex_card
    assert html.index("/static/widget-state.js") < html.index("/static/app.js")
    assert "window.__INITIAL_PAYLOAD__" in html
