"""TB-ACTIVITY, checked rather than described.

These tests do not assert the timestamp a reader returned. A reader that
opened `.credentials.json`, `auth.json` and every session file on its way to
the right timestamp returns exactly the same one, and that is how the boundary
went unenforced: the fixtures were "not json" placeholders and the assertions
were on the answer. Here the fixtures are parseable -- a credential file whose
contents would move the answer if it were read -- and the assertions are on
`ActivityGate`'s record of what each reader touched and what it did there,
plus, in `test_the_gate_is_the_only_way_...`, on every filesystem call the
process actually made during the scan.
"""

from __future__ import annotations

import builtins
import io
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePath

import pytest

from app.activity_gate import PathRefused
from app.claude_activity import ClaudeActivityReader
from app.codex_activity import ACTIVITY_DIRS, ACTIVITY_FILES, CodexActivityReader

TRANSCRIPT_TIME = datetime(2026, 5, 20, 11, 30, tzinfo=timezone.utc)
HISTORY_TIME = datetime(2026, 5, 20, 8, 0, tzinfo=timezone.utc)
SESSION_TIME = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
AUTH_TIME = datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc)
# Later than anything legitimate, in the file's mtime *and* in a timestamp
# field inside it: whichever way a reader reached a credential file, the
# timestamp it reported would move.
CREDENTIAL_TIME = datetime(2030, 1, 1, tzinfo=timezone.utc)

CLAUDE_FILES: tuple[str, ...] = ()
CLAUDE_TREES = ("projects",)


def _set_mtime(path: Path, dt: datetime) -> None:
    ts = dt.timestamp()
    os.utime(path, (ts, ts))


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    """The container's layout: two sibling data roots, one process, root.

    A link under either root resolves in that namespace whether or not it
    dangles on the host, which is what made the trees each other's reach.
    """
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    (claude / "projects" / "demo").mkdir(parents=True)
    (codex / "sessions").mkdir(parents=True)

    credentials = claude / ".credentials.json"
    credentials.write_text(
        json.dumps(
            {
                "claudeAiOauth": {"accessToken": "not-a-real-token"},
                "timestamp": CREDENTIAL_TIME.isoformat().replace("+00:00", "Z"),
            }
        ),
        encoding="utf-8",
    )
    _set_mtime(credentials, CREDENTIAL_TIME)

    transcript = claude / "projects" / "demo" / "session.jsonl"
    transcript.write_text(
        "\n".join(
            [
                '{"timestamp":"2026-05-20T08:00:00Z","usage":{"total":999}}',
                "not json",
                f'{{"timestamp":"{TRANSCRIPT_TIME.isoformat()}"}}',
                '{"timestamp":null}',
            ]
        ),
        encoding="utf-8",
    )

    auth = codex / "auth.json"
    auth.write_text(
        json.dumps({"tokens": {"access_token": "not-a-real-token", "account_id": "x"}}),
        encoding="utf-8",
    )
    history = codex / "history.jsonl"
    history.write_text(f'{{"timestamp":"{CREDENTIAL_TIME.isoformat()}"}}\n', encoding="utf-8")
    session = codex / "sessions" / "session.jsonl"
    session.write_text(f'{{"timestamp":"{CREDENTIAL_TIME.isoformat()}"}}\n', encoding="utf-8")

    _set_mtime(history, HISTORY_TIME)
    _set_mtime(session, SESSION_TIME)
    _set_mtime(auth, AUTH_TIME)
    return claude, codex


# --------------------------------------------------------- what was touched


def test_claude_reader_touches_transcripts_and_nothing_else(roots) -> None:
    claude, _ = roots

    reader = ClaudeActivityReader(claude)
    snapshot = reader.snapshot()

    assert snapshot.data_root_exists is True
    assert snapshot.last_activity == TRANSCRIPT_TIME
    assert set(reader.gate.admitted.entries) == {
        ("walk", "projects"),
        ("walk", "projects/demo"),
        ("stat", "projects/demo/session.jsonl"),
        ("read", "projects/demo/session.jsonl"),
    }
    assert reader.gate.admitted.truncated is False
    assert ".credentials.json" not in reader.gate.admitted.paths


def test_codex_reader_stats_known_files_and_opens_nothing(roots) -> None:
    _, codex = roots

    reader = CodexActivityReader(codex)
    snapshot = reader.snapshot()

    assert snapshot.data_root_exists is True
    assert set(reader.gate.admitted.entries) == {
        ("stat", "history.jsonl"),
        ("walk", "sessions"),
        ("stat", "sessions/session.jsonl"),
    }
    # The operation, not just the path: session *contents* are off limits even
    # though the session file itself is on the allow-list.
    assert all(op == "stat" for op, path in reader.gate.admitted.entries if "." in path)
    assert "auth.json" not in reader.gate.admitted.paths
    assert snapshot.last_activity == SESSION_TIME


def test_codex_reader_was_never_granted_a_read(roots) -> None:
    _, codex = roots
    reader = CodexActivityReader(codex)

    with pytest.raises(PathRefused):
        with reader.gate.open_bytes(codex / "sessions" / "session.jsonl"):
            pass  # pragma: no cover - the gate refuses before the body runs

    assert ("operation not granted to this reader", "sessions/session.jsonl") in (
        reader.gate.refused.entries
    )


def test_readers_refuse_a_path_the_allow_list_does_not_cover(roots) -> None:
    claude, codex = roots

    with pytest.raises(PathRefused):
        CodexActivityReader(codex).gate.stat(codex / "auth.json")
    with pytest.raises(PathRefused):
        ClaudeActivityReader(claude).gate.stat(claude / ".credentials.json")


# ------------------------------------------------------- links out of a tree


def test_a_link_planted_under_codex_does_not_reach_the_claude_credentials(
    roots,
) -> None:
    """ambient-inputs-1: this published the token-refresh time as activity."""
    claude, codex = roots
    (codex / "sessions" / "x.jsonl").symlink_to("../../claude/.credentials.json")

    reader = CodexActivityReader(codex)
    snapshot = reader.snapshot()

    assert snapshot.last_activity == SESSION_TIME
    assert snapshot.last_activity != CREDENTIAL_TIME
    assert "sessions/x.jsonl" not in reader.gate.admitted.paths
    assert ("not a regular file reached without a link", "sessions/x.jsonl") in (
        reader.gate.refused.entries
    )
    # And nothing the gate admitted names a path outside the Codex root.
    assert all(not p.startswith("/") for p in reader.gate.admitted.paths)


def test_a_symlinked_subtree_root_is_not_an_existence_oracle(roots) -> None:
    """The answer must not differ by whether the guessed target exists."""
    claude, codex = roots
    _set_mtime(claude / "projects" / "demo" / "session.jsonl", CREDENTIAL_TIME)

    def answer(target: Path) -> tuple[datetime | None, frozenset[str]]:
        link = codex / "archived_sessions"
        if link.is_symlink():
            link.unlink()
        link.symlink_to(target)
        reader = CodexActivityReader(codex)
        return reader.snapshot().last_activity, reader.gate.admitted.paths

    hit = answer(claude / "projects")
    miss = answer(claude / "projects" / "no-such-project-name")

    assert hit == miss
    assert hit[0] == SESSION_TIME
    assert "archived_sessions" not in hit[1]


def test_a_linked_transcript_does_not_reach_the_credential_file(roots) -> None:
    claude, _ = roots
    (claude / "projects" / "demo" / "leak.jsonl").symlink_to(
        claude / ".credentials.json"
    )

    reader = ClaudeActivityReader(claude)
    snapshot = reader.snapshot()

    # The credential file parses, and carries a later timestamp than the real
    # transcript: following the link would show in the answer.
    assert snapshot.last_activity == TRANSCRIPT_TIME
    assert "projects/demo/leak.jsonl" not in reader.gate.admitted.paths


def test_a_symlinked_project_directory_is_not_descended(roots, tmp_path) -> None:
    claude, _ = roots
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "elsewhere.jsonl").write_text(
        f'{{"timestamp":"{CREDENTIAL_TIME.isoformat()}"}}\n', encoding="utf-8"
    )
    (claude / "projects" / "linked").symlink_to(outside)

    reader = ClaudeActivityReader(claude)
    snapshot = reader.snapshot()

    assert snapshot.last_activity == TRANSCRIPT_TIME
    assert ("not a regular file reached without a link", "projects/linked") in (
        reader.gate.refused.entries
    )


# ---------------------------------------------- the gate is the only way out


@contextmanager
def _watch_filesystem(monkeypatch, scope: Path):
    """Record every filesystem call the process makes under ``scope``.

    The gate is only the enforcement point if a reader has no other way to the
    filesystem. Nothing in Python stops one adding `open(...)`, so this watches
    the calls themselves rather than trusting the gate's own record.
    """
    seen: list[tuple[str, Path]] = []

    def record(kind: str, target: object) -> None:
        try:
            path = Path(os.fsdecode(target))
        except TypeError:
            return  # a file descriptor, not a path
        if path == scope or scope in path.parents:
            seen.append((kind, path))

    def wrap(module: object, name: str, kind: str) -> None:
        original = getattr(module, name)

        def wrapper(*args, **kwargs):
            if args:
                record(kind, args[0])
            return original(*args, **kwargs)

        monkeypatch.setattr(module, name, wrapper)

    wrap(os, "stat", "stat")
    wrap(os, "lstat", "stat")
    wrap(os, "scandir", "scandir")
    wrap(os, "open", "open")
    wrap(os, "listdir", "scandir")
    wrap(io, "open", "open")
    wrap(builtins, "open", "open")
    try:
        yield seen
    finally:
        monkeypatch.undo()


def _assert_within(seen, root: Path, *, files=(), trees=()) -> None:
    for kind, path in seen:
        try:
            rel = PurePath(path).relative_to(root)
        except ValueError:
            pytest.fail(f"{kind} outside this reader's data root: {path}")
        parts = rel.parts
        if not parts:
            continue  # the data root itself
        if len(parts) == 1 and parts[0] in files:
            continue
        assert parts[0] in trees, f"{kind} of {rel}, which TB-ACTIVITY does not admit"


def test_the_gate_is_the_only_way_either_reader_reaches_the_filesystem(
    roots, tmp_path, monkeypatch
) -> None:
    """operator-tooling-1: the suite could not see a reader reading tokens.

    Reproduced before the gate existed by giving each reader an `open()` of
    `.credentials.json`, `auth.json`, `history.jsonl` and a session file: all
    641 tests still passed. This is the test that does not.
    """
    claude, codex = roots
    (codex / "sessions" / "x.jsonl").symlink_to("../../claude/.credentials.json")

    claude_reader = ClaudeActivityReader(claude)
    codex_reader = CodexActivityReader(codex)

    with _watch_filesystem(monkeypatch, tmp_path) as seen:
        claude_reader.snapshot()
        claude_seen = list(seen)
        seen.clear()
        codex_reader.snapshot()
        codex_seen = list(seen)

    assert claude_seen and codex_seen, "the watcher saw nothing; it is not wired up"
    _assert_within(claude_seen, claude, files=CLAUDE_FILES, trees=CLAUDE_TREES)
    _assert_within(codex_seen, codex, files=ACTIVITY_FILES, trees=ACTIVITY_DIRS)
    # Codex holds STAT alone: not one file was opened, by the gate or past it.
    assert [kind for kind, _ in codex_seen if kind == "open"] == []


def test_activity_readers_report_missing_roots(tmp_path) -> None:
    claude_snapshot = ClaudeActivityReader(tmp_path / "missing-claude").snapshot()
    codex_snapshot = CodexActivityReader(tmp_path / "missing-codex").snapshot()

    assert claude_snapshot.data_root_exists is False
    assert claude_snapshot.last_activity is None
    assert codex_snapshot.data_root_exists is False
    assert codex_snapshot.last_activity is None
