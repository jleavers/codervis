"""The rules `ActivityGate` enforces, checked one at a time.

`tests/test_activity_readers.py` checks what the readers touch through it;
this file checks the gate's own refusals, including the ones no reader can
reach today but which the next reader would.
"""

from __future__ import annotations

import os
import time
import traceback

import pytest

from app.activity_gate import (
    READ,
    STAT,
    AccessRecord,
    ActivityGate,
    PathRefused,
    WalkBudget,
)


@pytest.fixture
def gate(tmp_path):
    (tmp_path / "sessions" / "deep").mkdir(parents=True)
    (tmp_path / "sessions" / "deep" / "a.jsonl").write_text("a", encoding="utf-8")
    (tmp_path / "history.jsonl").write_text("h", encoding="utf-8")
    (tmp_path / "auth.json").write_text("secret", encoding="utf-8")
    return ActivityGate(
        tmp_path,
        files=("history.jsonl",),
        trees=("sessions",),
        operations={STAT, READ},
    )


def test_an_allow_listed_file_is_admitted(gate, tmp_path) -> None:
    st = gate.stat(tmp_path / "history.jsonl")

    assert st.st_size == 1
    assert ("stat", "history.jsonl") in gate.admitted.entries


def test_a_file_the_allow_list_does_not_name_is_refused(gate, tmp_path) -> None:
    with pytest.raises(PathRefused):
        gate.stat(tmp_path / "auth.json")

    assert "auth.json" not in gate.admitted.paths
    assert ("outside the allow-list", "auth.json") in gate.refused.entries


def test_a_path_outside_the_root_is_refused(gate, tmp_path) -> None:
    with pytest.raises(PathRefused):
        gate.stat(tmp_path.parent / "history.jsonl")


def test_a_dot_dot_is_not_walked_back_out_of_the_root(gate, tmp_path) -> None:
    """Lexical, so it never becomes a resolved path inside the allow-list."""
    outside = tmp_path.parent / "elsewhere.jsonl"
    outside.write_text("x", encoding="utf-8")

    with pytest.raises(PathRefused):
        gate.stat(tmp_path / "sessions" / ".." / ".." / "elsewhere.jsonl")


def test_a_link_is_refused_even_where_a_real_file_would_be_admitted(
    gate, tmp_path
) -> None:
    (tmp_path / "sessions" / "link.jsonl").symlink_to(tmp_path / "auth.json")

    with pytest.raises(PathRefused):
        gate.stat(tmp_path / "sessions" / "link.jsonl")


def test_a_linked_directory_between_the_root_and_the_file_is_refused(
    gate, tmp_path
) -> None:
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "a.jsonl").write_text("x", encoding="utf-8")
    (tmp_path / "sessions" / "linked").symlink_to(outside)

    with pytest.raises(PathRefused):
        gate.stat(tmp_path / "sessions" / "linked" / "a.jsonl")


def test_the_open_refuses_a_link_even_if_admission_were_passed(
    gate, tmp_path, monkeypatch
) -> None:
    """O_NOFOLLOW is the check that survives the gap after the lstat.

    A file swapped for a link between the two is the race the lstat alone
    cannot see, so admission is stubbed out here to leave only the `open`.
    """
    target = tmp_path / "auth.json"
    link = tmp_path / "sessions" / "swapped.jsonl"
    link.symlink_to(target)
    monkeypatch.setattr(ActivityGate, "_admit", lambda self, path, op: None)

    with pytest.raises(PathRefused):
        with gate.open_bytes(link):
            pass  # pragma: no cover - the open never succeeds


def test_a_fifo_is_not_a_regular_file(gate, tmp_path) -> None:
    """A blocking `open` of a fifo would stop this source's thread for good."""
    fifo = tmp_path / "sessions" / "pipe.jsonl"
    os.mkfifo(fifo)

    with pytest.raises(PathRefused):
        gate.stat(fifo)
    with pytest.raises(PathRefused):
        with gate.open_bytes(fifo):
            pass  # pragma: no cover - the gate refuses first


def test_an_operation_the_reader_was_not_granted_is_refused(tmp_path) -> None:
    (tmp_path / "history.jsonl").write_text("h", encoding="utf-8")
    stat_only = ActivityGate(tmp_path, files=("history.jsonl",), operations={STAT})

    stat_only.stat(tmp_path / "history.jsonl")
    with pytest.raises(PathRefused):
        with stat_only.open_bytes(tmp_path / "history.jsonl"):
            pass  # pragma: no cover - the gate refuses first


def test_an_unknown_operation_is_a_construction_error(tmp_path) -> None:
    with pytest.raises(ValueError):
        ActivityGate(tmp_path, operations={"delete"})


@pytest.mark.parametrize("plant", ["symlink", "missing", "unreadable-parent"])
def test_a_refusal_never_carries_the_path_it_refused(gate, tmp_path, plant) -> None:
    """`AGENTS.md`: an exception's own text must not reach a log or the payload.

    The operator's project directory names are exactly what the oracle leaked.
    A chained `OSError` is the path too -- its text and its `.filename` -- so
    the whole rendered traceback is what this checks, not just the message.
    """
    secret = tmp_path / "sessions" / "an-operators-project-name.jsonl"
    if plant == "symlink":
        secret.symlink_to(tmp_path / "auth.json")
    elif plant == "unreadable-parent":
        secret = secret.parent / "deep" / "gone" / secret.name

    with pytest.raises(PathRefused) as caught:
        gate.stat(secret)

    rendered = "".join(
        traceback.format_exception(
            type(caught.value), caught.value, caught.value.__traceback__
        )
    )
    assert "an-operators-project-name" not in rendered
    assert str(tmp_path) not in rendered
    # `from None`, so an OSError that prompted this is never rendered with it.
    assert caught.value.__cause__ is None
    assert (
        caught.value.__context__ is None or caught.value.__suppress_context__
    ), "a chained OSError renders as the path it failed on"


def test_a_file_with_a_second_name_is_refused(gate, tmp_path) -> None:
    """A hard link is the walk out of the tree with no symlink on the path."""
    outside = tmp_path / "auth.json"
    try:
        os.link(outside, tmp_path / "sessions" / "hard.jsonl")
    except OSError as exc:  # pragma: no cover - same filesystem in CI and dev
        pytest.skip(f"the fixture filesystem refused a hard link: {exc.errno}")

    with pytest.raises(PathRefused):
        gate.stat(tmp_path / "sessions" / "hard.jsonl")
    with pytest.raises(PathRefused):
        with gate.open_bytes(tmp_path / "sessions" / "hard.jsonl"):
            pass  # pragma: no cover - the gate refuses first
    assert ("has more than one name", "sessions/hard.jsonl") in gate.refused.entries


def test_a_directory_is_examined_once_a_scan(gate, tmp_path, monkeypatch) -> None:
    """Re-walking the chain above every file is what this used to cost."""
    (tmp_path / "sessions" / "deep" / "b.jsonl").write_text("b", encoding="utf-8")
    seen: list[str] = []
    real_lstat = os.lstat
    monkeypatch.setattr(
        os, "lstat", lambda p, *a, **k: (seen.append(str(p)), real_lstat(p, *a, **k))[1]
    )

    gate.start_scan()
    gate.stat(tmp_path / "sessions" / "deep" / "a.jsonl")
    gate.stat(tmp_path / "sessions" / "deep" / "b.jsonl")
    monkeypatch.undo()

    assert seen.count(str(tmp_path / "sessions")) == 1
    assert seen.count(str(tmp_path / "sessions" / "deep")) == 1
    # ... and the file itself is still examined every time it is admitted.
    assert seen.count(str(tmp_path / "sessions" / "deep" / "a.jsonl")) == 1


def test_a_walk_yields_regular_files_and_skips_links(gate, tmp_path) -> None:
    (tmp_path / "sessions" / "link.jsonl").symlink_to(tmp_path / "auth.json")
    budget = WalkBudget(max_entries=100, deadline=time.monotonic() + 30)

    found = {p.name for p in gate.walk("sessions", budget)}

    assert found == {"a.jsonl"}
    assert budget.exhausted is False


def test_a_walk_of_an_unlisted_subtree_is_refused(gate, tmp_path) -> None:
    (tmp_path / "elsewhere").mkdir()
    budget = WalkBudget(max_entries=100, deadline=time.monotonic() + 30)

    with pytest.raises(PathRefused):
        list(gate.walk("elsewhere", budget))


def test_a_walk_of_a_linked_subtree_root_stops_at_the_link(gate, tmp_path) -> None:
    elsewhere = tmp_path.parent / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    (elsewhere / "b.jsonl").write_text("b", encoding="utf-8")
    linked = ActivityGate(tmp_path, trees=("archive",), operations={STAT})
    (tmp_path / "archive").symlink_to(elsewhere)
    budget = WalkBudget(max_entries=100, deadline=time.monotonic() + 30)

    assert list(linked.walk("archive", budget)) == []
    assert "archive" not in linked.admitted.paths


def test_a_walk_budget_is_shared_across_subtrees(tmp_path) -> None:
    for tree in ("sessions", "archived"):
        (tmp_path / tree).mkdir()
        for i in range(10):
            (tmp_path / tree / f"{i}.jsonl").write_text("x", encoding="utf-8")
    gate = ActivityGate(
        tmp_path, trees=("sessions", "archived"), operations={STAT}
    )
    budget = WalkBudget(max_entries=12, deadline=time.monotonic() + 30)

    found = sum(
        1 for tree in gate.trees for _ in gate.walk(tree, budget)
    )

    assert found <= 12
    assert budget.exhausted is True


def test_a_spent_deadline_stops_a_walk_without_raising(gate, tmp_path) -> None:
    budget = WalkBudget(max_entries=100, deadline=time.monotonic())

    assert list(gate.walk("sessions", budget)) == []


def test_the_record_is_bounded_and_says_when_it_truncated() -> None:
    record = AccessRecord(limit=2)
    record.add("stat", "a")
    record.add("stat", "a")
    record.add("stat", "b")

    assert record.truncated is False
    record.add("stat", "c")
    assert record.truncated is True
    assert len(record) == 2

    record.clear()
    assert record.truncated is False
    assert record.entries == ()


def test_a_scan_records_what_that_scan_touched(gate, tmp_path) -> None:
    gate.stat(tmp_path / "history.jsonl")
    gate.start_scan()

    assert gate.admitted.entries == ()
    assert gate.refused.entries == ()


def test_the_root_may_itself_be_a_link(tmp_path) -> None:
    """It is the operator's own configuration. Nothing below it is."""
    real = tmp_path / "real"
    (real / "sessions").mkdir(parents=True)
    (real / "sessions" / "a.jsonl").write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(real)
    gate = ActivityGate(link, trees=("sessions",), operations={STAT})
    budget = WalkBudget(max_entries=100, deadline=time.monotonic() + 30)

    assert gate.root_exists() is True
    assert [p.name for p in gate.walk("sessions", budget)] == ["a.jsonl"]
    assert gate.stat(link / "sessions" / "a.jsonl").st_size == 1
