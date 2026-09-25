"""The rules `ActivityGate` enforces, checked one at a time.

`tests/test_activity_readers.py` checks what the readers touch through it;
this file checks the gate's own refusals, including the ones no reader can
reach today but which the next reader would.
"""

from __future__ import annotations

import os
import threading
import time
import traceback

import pytest

from app.activity_gate import (
    OUTSIDE_THE_ROOT,
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

    # Not even in the record: it holds names from inside the data root, which
    # is the operator's own configuration, and nothing else.
    assert gate.refused.entries == (("outside the allow-list", OUTSIDE_THE_ROOT),)


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


def _open_off_the_main_thread(
    gate: ActivityGate, path, timeout: float = 5.0
) -> BaseException | None:
    """`open_bytes(path)` on a thread this test stops waiting for.

    An `open` that blocks is the failure one of these rules prevents, and a
    blocking call made from the test body would hang the suite rather than fail
    it -- which is the same thing as not testing the rule at all. So the call
    runs on a daemon thread, and a thread still alive when the wait is over is
    the refusal not having been prompt.

    Returns what the call raised, or ``None`` if it opened the file.
    """
    raised: list[BaseException] = []
    opened: list[bool] = []

    def run() -> None:
        try:
            with gate.open_bytes(path):
                opened.append(True)
        except BaseException as exc:  # noqa: BLE001 - reported, not handled
            raised.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), (
        f"the open did not return within {timeout}s: a refresher's thread would "
        "be stopped here for good, and the source served stale until a restart"
    )
    assert not opened, "the open succeeded on a path no reader may read"
    return raised[0] if raised else None


@pytest.mark.parametrize(
    ("plant", "reason"),
    [
        ("fifo", "was not a regular file when opened"),
        ("symlink", "could not be opened without following a link"),
        ("missing", "could not be opened without following a link"),
    ],
)
def test_the_open_refuses_promptly_what_admission_would_never_have_reached(
    gate, tmp_path, monkeypatch, plant: str, reason: str
) -> None:
    """The three defences `open_bytes` holds *after* the lstat that admitted the path.

    A file swapped after admission is the race the lstat alone cannot see, so
    admission is stubbed out to leave the `open` and nothing else -- the only
    way these defences are reached at all, since a fifo, a link and a missing
    file are each refused at admission. Three rules answer here, and each is
    the subject of a named mutation in `tests/test_negative_controls.py`:
    `O_NOFOLLOW`, so a link is not read through; `O_NONBLOCK` with the
    post-open `S_ISREG`, so a fifo's `open` returns instead of parking this
    thread until somebody writes; and `from None`, so the `OSError` that
    prompted the refusal -- whose text and `.filename` are the path -- is not
    rendered with it.

    The reason a missing file is refused with reads as link-flavoured, and is
    meant to: the gate answers `ENOENT` and `ELOOP` alike, because a reason that
    told them apart would say whether the path exists, which is the oracle this
    module was written to close.

    Three, not every flag in that loop: `O_CLOEXEC` is the fourth, and nothing
    here or in the mutation list pins it, because there is nothing to pin.
    Python has made every descriptor `os.open` returns non-inheritable since
    PEP 446, so removing the flag changes no behaviour a test could see. Saying
    so is the point -- a docstring claiming the loop whole would be the defect
    this test was written for.
    """
    path = tmp_path / "sessions" / "an-operators-project-name.jsonl"
    if plant == "fifo":
        os.mkfifo(path)
    elif plant == "symlink":
        path.symlink_to(tmp_path / "auth.json")
    monkeypatch.setattr(ActivityGate, "_admit", lambda self, path, op: None)

    raised = _open_off_the_main_thread(gate, path)

    assert isinstance(raised, PathRefused), f"refused with {raised!r}"
    assert (reason, "sessions/an-operators-project-name.jsonl") in gate.refused.entries
    # The same discipline as the refusals above: the reason is all a caller carries.
    rendered = "".join(
        traceback.format_exception(type(raised), raised, raised.__traceback__)
    )
    assert "an-operators-project-name" not in rendered
    assert str(tmp_path) not in rendered
    assert raised.__cause__ is None
    assert (
        raised.__context__ is None or raised.__suppress_context__
    ), "a chained OSError renders as the path it failed on"


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
