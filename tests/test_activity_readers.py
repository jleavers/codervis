from __future__ import annotations

import os
from datetime import datetime, timezone

from app.claude_activity import ClaudeActivityReader
from app.codex_activity import CodexActivityReader


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


def test_activity_readers_report_missing_roots(tmp_path) -> None:
    claude_snapshot = ClaudeActivityReader(tmp_path / "missing-claude").snapshot()
    codex_snapshot = CodexActivityReader(tmp_path / "missing-codex").snapshot()

    assert claude_snapshot.data_root_exists is False
    assert claude_snapshot.last_activity is None
    assert codex_snapshot.data_root_exists is False
    assert codex_snapshot.last_activity is None
