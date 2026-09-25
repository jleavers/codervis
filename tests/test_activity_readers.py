"""TB-ACTIVITY, checked rather than described.

These tests do not assert the timestamp a reader returned. A reader that
opened `.credentials.json`, `auth.json` and every session file on its way to
the right timestamp returns exactly the same one, and that is how the boundary
went unenforced: the fixtures were "not json" placeholders and the assertions
were on the answer. Here the fixtures are parseable -- a credential file whose
contents would move the answer if it were read -- and the assertions are on
`ActivityGate`'s record of what each reader touched and what it did there,
plus, in `test_the_gate_is_the_only_way_...`, on the session audit hook's
record of what the process actually opened, listed and scanned during the scan.

Be exact about what that second record reaches, because #38 was the suite
claiming more than it saw. It is `tests/conftest.py`'s `sys.addaudithook`
observer, keyed on the resource: an `open`, an `os.listdir` and an `os.scandir`
are in it whatever Python name reached them. A *stat* is not in it at all --
CPython raises no audit event for `os.stat` or `os.lstat` -- so the half of
TB-ACTIVITY about a reader reading the credential file's metadata is pinned
structurally instead, in `tests/test_reader_filesystem_surface.py`.
"""

from __future__ import annotations

import io
import json
import os
import posix
from datetime import datetime, timezone
from os import lstat as imported_lstat
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
    assert reader.gate.admitted.operations("sessions/session.jsonl") == {"stat"}
    assert reader.gate.admitted.truncated is False
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
    assert reader.gate.admitted.truncated is False
    assert "sessions/x.jsonl" not in reader.gate.admitted.paths
    assert ("not a regular file reached without a link", "sessions/x.jsonl") in (
        reader.gate.refused.entries
    )
    # And nothing the gate admitted names a path outside the Codex root.
    assert all(not p.startswith("/") for p in reader.gate.admitted.paths)


def test_an_allow_listed_name_that_is_a_link_is_refused(roots) -> None:
    """`history.jsonl` itself replaced by a link: on the allow-list by name."""
    claude, codex = roots
    (codex / "history.jsonl").unlink()
    (codex / "history.jsonl").symlink_to(claude / ".credentials.json")

    reader = CodexActivityReader(codex)
    snapshot = reader.snapshot()

    assert snapshot.last_activity == SESSION_TIME
    assert reader.gate.admitted.truncated is False
    assert "history.jsonl" not in reader.gate.admitted.paths
    assert ("not a regular file reached without a link", "history.jsonl") in (
        reader.gate.refused.entries
    )


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
    assert reader.gate.admitted.truncated is False
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


def test_a_hard_link_does_not_reach_the_claude_credentials_either(roots) -> None:
    """The same walk out, with no symlink anywhere on the path.

    A hard link inside the allow-list is a second name for a file outside it:
    there is nothing to see on the path, and what it names really is a regular
    file. It is refused because it has more than one name.
    """
    claude, codex = roots
    try:
        os.link(claude / ".credentials.json", codex / "sessions" / "hard.jsonl")
    except OSError as exc:  # pragma: no cover - same filesystem in CI and dev
        pytest.skip(f"the fixture filesystem refused a hard link: {exc.errno}")

    reader = CodexActivityReader(codex)
    snapshot = reader.snapshot()

    assert snapshot.last_activity == SESSION_TIME
    assert snapshot.last_activity != CREDENTIAL_TIME
    assert reader.gate.admitted.truncated is False
    assert "sessions/hard.jsonl" not in reader.gate.admitted.paths
    assert ("has more than one name", "sessions/hard.jsonl") in (
        reader.gate.refused.entries
    )


def test_a_hard_linked_transcript_is_not_read(roots) -> None:
    claude, _ = roots
    try:
        os.link(
            claude / ".credentials.json",
            claude / "projects" / "demo" / "hard.jsonl",
        )
    except OSError as exc:  # pragma: no cover - same filesystem in CI and dev
        pytest.skip(f"the fixture filesystem refused a hard link: {exc.errno}")

    reader = ClaudeActivityReader(claude)
    snapshot = reader.snapshot()

    # The credential file parses as a transcript and its `timestamp` field is
    # later than the real one, so reading it would show in the answer.
    assert snapshot.last_activity == TRANSCRIPT_TIME
    assert reader.gate.admitted.truncated is False
    assert "projects/demo/hard.jsonl" not in reader.gate.admitted.paths


def test_a_symlinked_projects_root_is_not_an_existence_oracle_either(
    roots, tmp_path
) -> None:
    """The Claude half of the oracle: `projects` itself replaced by a link."""
    claude, _ = roots
    real_projects = claude / "projects"
    real_projects.rename(tmp_path / "moved-projects")

    def answer(target: Path) -> tuple[object, frozenset[str], tuple]:
        if real_projects.is_symlink():
            real_projects.unlink()
        real_projects.symlink_to(target)
        reader = ClaudeActivityReader(claude)
        snapshot = reader.snapshot()
        return (
            snapshot.last_activity,
            reader.gate.admitted.paths,
            reader.gate.refused.entries,
        )

    hit = answer(tmp_path / "moved-projects")
    miss = answer(tmp_path / "no-such-directory-name")

    assert hit == miss
    assert hit[0] is None


# ---------------------------------------------- the gate is the only way out


def _assert_within(seen, root: Path, *, files=(), trees=()) -> None:
    for kind, path in seen:
        try:
            rel = PurePath(path).relative_to(root)
        except ValueError:
            pytest.fail(f"{kind} outside this reader's data root: {path}")
        parts = rel.parts
        if not parts:
            pytest.fail(f"{kind} of the data root itself")
        if len(parts) == 1 and parts[0] in files:
            continue
        assert parts[0] in trees, f"{kind} of {rel}, which TB-ACTIVITY does not admit"


def test_the_gate_is_the_only_way_either_reader_reaches_the_filesystem(
    roots, tmp_path, filesystem_audit
) -> None:
    """operator-tooling-1: the suite could not see a reader reading tokens.

    Reproduced before the gate existed by giving each reader an `open()` of
    `.credentials.json`, `auth.json`, `history.jsonl` and a session file: all
    641 tests still passed. This is the test that does not.

    **What the record covers.** It is the session audit hook in
    `tests/conftest.py`, keyed on the resource: an `open`, an `os.listdir` or
    an `os.scandir` is in it whatever Python name reached it -- an import-time
    binding, `posix.*` or `io.FileIO(path)` included. Until #38 this watcher
    rebound seven module attributes instead and saw none of those three.

    **What it does not cover** is `os.stat` and `os.lstat`, for which CPython
    raises no audit event at all. No stat appears below, and the absence of one
    is not evidence: a reader that reached the credential file's metadata by
    any means would look exactly like this. The stat half of the claim is
    `tests/test_reader_filesystem_surface.py`, which pins that neither reader
    module names a filesystem API at all.
    """
    claude, codex = roots
    (codex / "sessions" / "x.jsonl").symlink_to("../../claude/.credentials.json")

    claude_reader = ClaudeActivityReader(claude)
    codex_reader = CodexActivityReader(codex)

    with filesystem_audit.watch(tmp_path) as seen:
        claude_reader.snapshot()
        claude_seen = list(seen)
        seen.clear()
        codex_reader.snapshot()
        codex_seen = list(seen)

    assert claude_seen and codex_seen, "the watcher saw nothing; it is not wired up"
    assert {kind for kind, _ in claude_seen} <= filesystem_audit.KINDS
    _assert_within(claude_seen, claude, files=CLAUDE_FILES, trees=CLAUDE_TREES)
    _assert_within(codex_seen, codex, files=ACTIVITY_FILES, trees=ACTIVITY_DIRS)
    # Codex holds STAT alone: not one file was opened, by the gate or past it.
    assert [kind for kind, _ in codex_seen if kind == "open"] == []


def test_the_watcher_sees_a_read_reaching_past_the_names_it_used_to_patch(
    roots, tmp_path, filesystem_audit
) -> None:
    """fix-holds-1: the record is keyed on the resource, not on a name.

    The three ways round the seven rebound attributes, made against the
    fixture credential file that a reader must never reach. Each must appear
    in the record, so that a reader adding one is caught by the test above
    rather than by nobody.
    """
    claude, _ = roots
    credentials = claude / ".credentials.json"

    with filesystem_audit.watch(tmp_path) as seen:
        imported_lstat(credentials)  # `from os import lstat`, bound at import
        posix.listdir(str(claude))  # the module `os` itself forwards to
        handle = io.FileIO(str(credentials))
        handle.read()
        handle.close()
        observed = list(seen)

    assert ("scandir", claude) in observed, "posix.listdir went unseen"
    assert ("open", credentials) in observed, "io.FileIO(path) went unseen"
    # And the one nothing here can see: CPython raises no audit event for a
    # stat, so the `lstat` above is absent by design, not by innocence. Said
    # against the record's own vocabulary, so that a kind added later has to
    # come back through this assertion rather than past it.
    assert "stat" not in filesystem_audit.KINDS, (
        "the record has grown a stat kind; this test and "
        "tests/test_reader_filesystem_surface.py divide the claim between them "
        "on the assumption that it has none"
    )


def test_activity_readers_report_missing_roots(tmp_path) -> None:
    claude_snapshot = ClaudeActivityReader(tmp_path / "missing-claude").snapshot()
    codex_snapshot = CodexActivityReader(tmp_path / "missing-codex").snapshot()

    assert claude_snapshot.data_root_exists is False
    assert claude_snapshot.last_activity is None
    assert codex_snapshot.data_root_exists is False
    assert codex_snapshot.last_activity is None
