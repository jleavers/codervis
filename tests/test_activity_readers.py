from __future__ import annotations

import os
from datetime import datetime, timezone

from app.claude_activity import ClaudeActivityReader
from app.codex_activity import CodexActivityReader
from app.copilot_activity import CopilotActivityReader
from app.cursor_activity import CursorActivityReader
from app.gemini_activity import GeminiActivityReader


def _set_mtime(path, dt: datetime) -> None:
    ts = dt.timestamp()
    os.utime(path, (ts, ts))


def test_claude_activity_reads_project_transcript_timestamps_only(tmp_path) -> None:
    root = tmp_path / "claude"
    project = root / "projects" / "demo"
    project.mkdir(parents=True)
    (root / ".credentials.json").write_text("not json", encoding="utf-8")
    (project / "session.jsonl").write_text(
        "\n".join(
            [
                '{"timestamp":"2026-05-20T08:00:00Z","usage":{"total":999}}',
                "not json",
                '{"timestamp":"2026-05-20T11:30:00+00:00"}',
                '{"timestamp":null}',
            ]
        ),
        encoding="utf-8",
    )

    snapshot = ClaudeActivityReader(root, cache_ttl_seconds=0).snapshot()

    assert snapshot.data_root_exists is True
    assert snapshot.last_activity == datetime(2026, 5, 20, 11, 30, tzinfo=timezone.utc)


def test_codex_activity_uses_known_metadata_and_ignores_auth_json(tmp_path) -> None:
    root = tmp_path / "codex"
    sessions = root / "sessions"
    sessions.mkdir(parents=True)
    history = root / "history.jsonl"
    session = sessions / "session.jsonl"
    auth = root / "auth.json"
    history.write_text("content is not parsed", encoding="utf-8")
    session.write_text("content is not parsed", encoding="utf-8")
    auth.write_text("newer but ignored", encoding="utf-8")

    history_time = datetime(2026, 5, 20, 8, 0, tzinfo=timezone.utc)
    session_time = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    ignored_auth_time = datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc)
    _set_mtime(history, history_time)
    _set_mtime(session, session_time)
    _set_mtime(auth, ignored_auth_time)

    snapshot = CodexActivityReader(root, cache_ttl_seconds=0).snapshot()

    assert snapshot.data_root_exists is True
    assert snapshot.last_activity == session_time
    assert snapshot.last_activity != ignored_auth_time


def test_cursor_activity_uses_state_db_metadata(tmp_path) -> None:
    root = tmp_path / "cursor"
    global_storage = root / "User" / "globalStorage"
    global_storage.mkdir(parents=True)
    state_db = global_storage / "state.vscdb"
    state_db.write_text("token bytes are not parsed", encoding="utf-8")

    state_time = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
    _set_mtime(state_db, state_time)

    snapshot = CursorActivityReader(root, cache_ttl_seconds=0).snapshot()

    assert snapshot.data_root_exists is True
    assert snapshot.last_activity == state_time


def test_copilot_activity_uses_config_metadata_and_ignores_token(tmp_path) -> None:
    root = tmp_path / "github-copilot"
    root.mkdir(parents=True)
    apps = root / "apps.json"
    versions = root / "versions.json"
    apps.write_text('{"github.com": {"oauth_token": "secret not parsed"}}', encoding="utf-8")
    versions.write_text("{}", encoding="utf-8")

    apps_time = datetime(2026, 5, 20, 8, 0, tzinfo=timezone.utc)
    versions_time = datetime(2026, 5, 20, 9, 30, tzinfo=timezone.utc)
    _set_mtime(apps, apps_time)
    _set_mtime(versions, versions_time)

    snapshot = CopilotActivityReader(root, cache_ttl_seconds=0).snapshot()

    assert snapshot.data_root_exists is True
    assert snapshot.last_activity == versions_time


def test_gemini_activity_uses_metadata_and_ignores_token_contents(tmp_path) -> None:
    root = tmp_path / "gemini"
    token_dir = root / "antigravity-cli"
    brain_dir = root / "antigravity" / "brain" / "session"
    token_dir.mkdir(parents=True)
    brain_dir.mkdir(parents=True)
    token = token_dir / "antigravity-oauth-token"
    brain = brain_dir / "task.md"
    token.write_text('{"token":{"access_token":"secret not parsed"}}', encoding="utf-8")
    brain.write_text("activity content is not parsed", encoding="utf-8")

    token_time = datetime(2026, 5, 20, 8, 0, tzinfo=timezone.utc)
    brain_time = datetime(2026, 5, 20, 11, 0, tzinfo=timezone.utc)
    _set_mtime(token, token_time)
    _set_mtime(brain, brain_time)

    snapshot = GeminiActivityReader(root, cache_ttl_seconds=0).snapshot()

    assert snapshot.data_root_exists is True
    assert snapshot.last_activity == brain_time


def test_activity_readers_report_missing_roots(tmp_path) -> None:
    claude_snapshot = ClaudeActivityReader(tmp_path / "missing-claude").snapshot()
    codex_snapshot = CodexActivityReader(tmp_path / "missing-codex").snapshot()
    cursor_snapshot = CursorActivityReader(tmp_path / "missing-cursor").snapshot()
    copilot_snapshot = CopilotActivityReader(tmp_path / "missing-copilot").snapshot()
    gemini_snapshot = GeminiActivityReader(tmp_path / "missing-gemini").snapshot()

    assert claude_snapshot.data_root_exists is False
    assert claude_snapshot.last_activity is None
    assert codex_snapshot.data_root_exists is False
    assert codex_snapshot.last_activity is None
    assert cursor_snapshot.data_root_exists is False
    assert cursor_snapshot.last_activity is None
    assert copilot_snapshot.data_root_exists is False
    assert copilot_snapshot.last_activity is None
    assert gemini_snapshot.data_root_exists is False
    assert gemini_snapshot.last_activity is None
