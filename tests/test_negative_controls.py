"""The negative controls: one named break of each security rule, required to turn tests red.

A test can claim more than its assertions reach and still pass, and nothing about a green
suite says otherwise. This repository has had three instances of it: the gate's `O_NONBLOCK`,
its post-open `S_ISREG` and the `from None` on its open refusal could each be deleted with the
whole suite green, because the one test that reached past admission planted a link, which
`O_NOFOLLOW` alone answers (#46); the front door's bounds could each be widened, because every
test that exercised one passed its own value in; and the pending-work check promised to read
every shipped document while globbing one directory (#46, #21).

So the rules that carry the security properties get a control each: a deliberate break, named
here, whose removal is required to make the tests that pin it fail. The list is the standing
place that stops the next test quietly narrowing -- a rule can still be deleted, but not
without deleting its control and saying why, and a test that is renamed or narrowed until it
no longer catches its mutation fails here rather than going quiet.

How it runs: the tracked tree is copied once into a temporary directory, and each mutation is
written into the copy, run against the tests named for it, and undone. The worktree itself is
never mutated. This is the slowest module in the suite -- one pytest subprocess per mutation --
and each selection is narrowed to the tests that must answer, to keep it that way.

A control whose named tests all skip in this environment skips too, with that reason, rather
than passing: `tests/test_compose_topology.py` needs the Docker CLI, and a control that
reported green where its tests never ran would be the exact defect this module exists to stop.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: One mutant run: a whole pytest process, so generous, but finite. A mutation that makes a
#: test hang rather than fail is itself a finding -- the suite must go red, not quiet.
MUTANT_TIMEOUT_S = 240.0

GATE = "the gate"
FRONT_DOOR = "the front door"
COMPOSE = "the compose shape"
DOCUMENTS = "the document checks"
AREAS = frozenset({GATE, FRONT_DOOR, COMPOSE, DOCUMENTS})

GATE_TESTS = "tests/test_activity_gate.py"
INGRESS_TESTS = "tests/test_ingress.py"
COMPOSE_TESTS = "tests/test_compose_topology.py"
CONTEXT_TESTS = "tests/test_agent_tooling_context.py"
EGRESS_TESTS = "tests/test_egress.py"

_OPEN_PAST_ADMISSION = (
    f"{GATE_TESTS}::test_the_open_refuses_promptly_what_admission_would_never_have_reached"
)
_PENDING_WORK = f"{CONTEXT_TESTS}::test_no_shipped_document_reads_as_work_still_to_do"
_PUBLISHED_PORT_HEAD_CAP = (
    f"{INGRESS_TESTS}::test_the_port_the_dashboard_is_published_on_carries_the_head_cap_too"
)


@dataclass(frozen=True)
class Mutation:
    """One rule-breaking edit, and the tests that must fail because of it.

    ``before`` is replaced by ``after`` in ``path``, and must appear there exactly once: a
    mutation that no longer matches the code it was written against fails loudly, because a
    control that silently stopped mutating anything would pass for the wrong reason. Leaving
    ``before`` unset writes ``path`` outright, which is how a rule about a file's *absence*
    is broken.
    """

    key: str
    area: str
    rule: str
    path: str
    caught_by: tuple[str, ...]
    before: str | None = None
    after: str = ""
    marks: tuple[pytest.MarkDecorator, ...] = field(default=(), repr=False)


MUTATIONS: tuple[Mutation, ...] = (
    # ---------------------------------------------------------------------------- the gate
    Mutation(
        key="gate-allow-list",
        area=GATE,
        rule="a path the allow-list does not name is refused",
        path="app/activity_gate.py",
        before="        return parts if parts[0] in self.files else None",
        after="        return parts",
        caught_by=(f"{GATE_TESTS}::test_a_file_the_allow_list_does_not_name_is_refused",),
    ),
    Mutation(
        key="gate-operation-not-granted",
        area=GATE,
        rule="an operation the reader was not granted is refused on an allow-listed path too",
        path="app/activity_gate.py",
        before='''        if operation not in self.operations:
            raise self._refuse(path, "operation not granted to this reader")
''',
        caught_by=(
            f"{GATE_TESTS}::test_an_operation_the_reader_was_not_granted_is_refused",
        ),
    ),
    Mutation(
        key="gate-lstat-follows-links",
        area=GATE,
        rule="every component below the root is examined with lstat, so a link is seen as one",
        path="app/activity_gate.py",
        before="            return os.lstat(path)",
        after="            return os.stat(path)",
        caught_by=(
            f"{GATE_TESTS}::test_a_link_is_refused_even_where_a_real_file_would_be_admitted",
        ),
    ),
    Mutation(
        key="gate-second-name",
        area=GATE,
        rule="a file with more than one name is refused: a hard link is the same escape",
        path="app/activity_gate.py",
        before='''        if st.st_nlink != 1:''',
        after='''        if False:''',
        caught_by=(f"{GATE_TESTS}::test_a_file_with_a_second_name_is_refused",),
    ),
    Mutation(
        key="gate-o-nofollow",
        area=GATE,
        rule="a read opens with O_NOFOLLOW, so a path swapped for a link is not read through",
        path="app/activity_gate.py",
        before='for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC"):',
        after='for name in ("O_NONBLOCK", "O_CLOEXEC"):',
        caught_by=(_OPEN_PAST_ADMISSION,),
    ),
    Mutation(
        key="gate-o-nonblock",
        area=GATE,
        rule="a read opens with O_NONBLOCK, so a fifo does not park the refresher's thread",
        path="app/activity_gate.py",
        before='for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC"):',
        after='for name in ("O_NOFOLLOW", "O_CLOEXEC"):',
        caught_by=(_OPEN_PAST_ADMISSION,),
    ),
    Mutation(
        key="gate-post-open-isreg",
        area=GATE,
        rule="what was opened is checked to be a regular file, after the open",
        path="app/activity_gate.py",
        before='''            if not stat_module.S_ISREG(os.fstat(fd).st_mode):
                raise self._refuse(path, "was not a regular file when opened")
''',
        caught_by=(_OPEN_PAST_ADMISSION,),
    ),
    Mutation(
        key="gate-open-refusal-from-none",
        area=GATE,
        rule="the OSError behind an open refusal is suppressed: its text and filename are the path",
        path="app/activity_gate.py",
        before='''                path, "could not be opened without following a link"
            ) from None''',
        after='''                path, "could not be opened without following a link"
            )''',
        caught_by=(_OPEN_PAST_ADMISSION,),
    ),
    # ----------------------------------------------------------------------- the front door
    Mutation(
        key="ingress-serve-arms-no-head-cap",
        area=FRONT_DOOR,
        rule="the published port carries the head cap, not asyncio's default",
        path="app/ingress.py",
        before=(
            "return await asyncio.start_server("
            "relay.handle, bind, port, limit=MAX_REQUEST_HEAD_BYTES)"
        ),
        after="return await asyncio.start_server(relay.handle, bind, port)",
        caught_by=(_PUBLISHED_PORT_HEAD_CAP,),
    ),
    Mutation(
        key="ingress-head-cap-widened",
        area=FRONT_DOOR,
        rule="a request head is at most 16 KiB",
        path="app/ingress.py",
        before="MAX_REQUEST_HEAD_BYTES = 16 * 1024",
        after="MAX_REQUEST_HEAD_BYTES = 16 * 1024 * 1024",
        caught_by=(f"{INGRESS_TESTS}::test_the_front_doors_bounds_are_the_ones_it_documents",),
    ),
    Mutation(
        key="ingress-head-deadline-widened",
        area=FRONT_DOOR,
        rule="a client has 10 s to send a complete first head, before the dashboard is dialled",
        path="app/ingress.py",
        before="REQUEST_TIMEOUT_S = 10.0",
        after="REQUEST_TIMEOUT_S = 600.0",
        caught_by=(f"{INGRESS_TESTS}::test_the_front_doors_bounds_are_the_ones_it_documents",),
    ),
    Mutation(
        key="ingress-connection-bound-widened",
        area=FRONT_DOOR,
        rule="at most 256 connections are accepted at once",
        path="app/ingress.py",
        before="MAX_CONNECTIONS = 256",
        after="MAX_CONNECTIONS = 1_000_000",
        caught_by=(f"{INGRESS_TESTS}::test_the_front_doors_bounds_are_the_ones_it_documents",),
    ),
    Mutation(
        key="egress-default-allow-widened",
        area=FRONT_DOOR,
        rule="DEFAULT_ALLOW is the two usage endpoints and nothing else",
        path="app/egress.py",
        before='DEFAULT_ALLOW: tuple[str, ...] = ("claude.ai", "chatgpt.com")',
        after='DEFAULT_ALLOW: tuple[str, ...] = ("claude.ai", "chatgpt.com", "example.invalid")',
        caught_by=(
            f"{EGRESS_TESTS}::test_the_default_list_is_the_two_usage_endpoints_and_nothing_else",
        ),
    ),
    # --------------------------------------------------------------------- the compose shape
    Mutation(
        key="compose-inside-not-internal",
        area=COMPOSE,
        rule="the network the dashboard joins is internal, so it has no default route",
        path="docker-compose.yml",
        before="  inside:\n    internal: true\n",
        after="  inside: {}\n",
        caught_by=(
            f"{COMPOSE_TESTS}::test_the_dashboard_container_joins_internal_networks_alone",
        ),
    ),
    Mutation(
        key="compose-dashboard-on-the-outside",
        area=COMPOSE,
        rule="the dashboard container joins internal networks alone",
        path="docker-compose.yml",
        before="    networks: [inside]\n",
        after="    networks: [inside, outside]\n",
        caught_by=(
            f"{COMPOSE_TESTS}::test_the_dashboard_container_joins_internal_networks_alone",
        ),
    ),
    Mutation(
        key="compose-dashboard-publishes-a-port",
        area=COMPOSE,
        rule="the relay is the only service that publishes a port",
        path="docker-compose.yml",
        before="    networks: [inside]\n",
        after='    networks: [inside]\n    ports: ["18765:8000"]\n',
        caught_by=(
            f"{COMPOSE_TESTS}::test_the_dashboard_container_publishes_nothing_itself",
            f"{COMPOSE_TESTS}::test_only_the_relay_publishes_a_port_and_it_targets_the_dashboard",
        ),
    ),
    # ------------------------------------------------------------------ the document checks
    Mutation(
        key="doc-project-settings-file",
        area=DOCUMENTS,
        rule="the repository ships no .claude/settings.json to bind the operator's sessions",
        path=".claude/settings.json",
        after='{"permissions": {"allow": ["Bash"]}}\n',
        caught_by=(
            f"{CONTEXT_TESTS}::"
            "test_the_repository_does_not_configure_the_operators_agent_environment",
        ),
    ),
    Mutation(
        key="doc-fixed-tmp-path",
        area=DOCUMENTS,
        rule="no shipped document names a fixed path in shared /tmp",
        path="README.md",
        before="# codervis",
        after="# codervis\n\nCache wheels under /tmp/codervis-cache first.",
        caught_by=(f"{CONTEXT_TESTS}::test_no_shipped_document_names_a_fixed_path_in_shared_tmp",),
    ),
    Mutation(
        key="doc-unticked-box",
        area=DOCUMENTS,
        rule="no shipped document reads as work still to do",
        path="docs/superpowers/plans/archive/2026-06-08-browser-widget-toggles.md",
        before="**Goal:**",
        after="- [ ] Finish the remaining toggle work.\n\n**Goal:**",
        caught_by=(_PENDING_WORK,),
    ),
    Mutation(
        key="doc-archived-header-dropped",
        area=DOCUMENTS,
        rule="every shipped document says up front that it is archived",
        path="docs/superpowers/specs/2026-06-08-agy-1.0.6-compatibility-design.md",
        before="> **Archived — this work was abandoned.**",
        after="This design is ready to implement.",
        caught_by=(_PENDING_WORK,),
    ),
)


def test_the_list_covers_every_security_rule_area() -> None:
    """The list shrinking to nothing is the failure this module would not otherwise show.

    Naming the four areas here means a control cannot be dropped along with the rule it was
    the only witness to: the area goes missing and this fails.
    """
    assert {mutation.area for mutation in MUTATIONS} == AREAS
    keys = [mutation.key for mutation in MUTATIONS]
    assert len(keys) == len(set(keys)), "two mutations share a key"


@pytest.fixture(scope="module")
def pristine(tmp_path_factory) -> Path:
    """A copy of the tracked tree, which is what a clone gets, with an index for `git ls-files`.

    Copied once and repaired after each mutation, rather than copied per mutation: the
    mutations are applied from this module, so what has to be undone is known exactly.
    `git init` because one of the document checks lists the tracked files to decide what a
    clone would read; nothing is committed, since an index answers `git ls-files`.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    names = [name for name in listed.split("\0") if name]
    assert names, "git ls-files returned nothing; is this a checkout?"

    copy = tmp_path_factory.mktemp("mutant-tree")
    for name in names:
        source = ROOT / name
        if not source.is_file():  # a submodule, or a file already deleted in the worktree
            continue
        target = copy / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for command in (["git", "init", "--quiet"], ["git", "add", "-A"]):
        subprocess.run(command, cwd=copy, capture_output=True, timeout=120, check=True)
    return copy


def _apply(mutation: Mutation, tree: Path) -> None:
    target = tree / mutation.path
    if mutation.before is None:
        assert not target.exists(), (
            f"{mutation.key}: {mutation.path} exists, so this mutation no longer breaks "
            "a rule about its absence"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(mutation.after, encoding="utf-8")
        return
    text = target.read_text(encoding="utf-8")
    found = text.count(mutation.before)
    assert found == 1, (
        f"{mutation.key}: the text it mutates appears {found} times in {mutation.path}. "
        "The rule was moved or rewritten; re-point the mutation at it, or delete the "
        "control and say why the rule no longer needs one."
    )
    target.write_text(text.replace(mutation.before, mutation.after), encoding="utf-8")


def _undo(mutation: Mutation, tree: Path) -> None:
    target = tree / mutation.path
    if mutation.before is None:
        target.unlink(missing_ok=True)
        return
    shutil.copy2(ROOT / mutation.path, target)


def _run(mutation: Mutation, tree: Path, report: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--no-header",
            "--tb=no",
            "-p",
            "no:cacheprovider",
            f"--junit-xml={report}",
            *mutation.caught_by,
        ],
        cwd=tree,
        capture_output=True,
        text=True,
        timeout=MUTANT_TIMEOUT_S,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _outcomes(report: Path) -> dict[str, int]:
    """How the mutant run's own tests ended, counted from its JUnit report.

    The exit status alone cannot tell a mutation that was caught from one whose tests never
    ran: a selection that skipped -- no Docker CLI, say -- exits 0 exactly as a passing one
    does, and reporting that as a control is the defect this module exists to catch.
    """
    if not report.exists():
        return {"total": 0}
    counts = {"total": 0, "failed": 0, "skipped": 0, "passed": 0}
    for case in ElementTree.parse(report).getroot().iter("testcase"):
        counts["total"] += 1
        if case.find("failure") is not None or case.find("error") is not None:
            counts["failed"] += 1
        elif case.find("skipped") is not None:
            counts["skipped"] += 1
        else:
            counts["passed"] += 1
    return counts


@pytest.mark.parametrize(
    "mutation", [pytest.param(m, id=m.key, marks=m.marks) for m in MUTATIONS]
)
def test_breaking_the_rule_turns_its_tests_red(mutation: Mutation, pristine, tmp_path) -> None:
    """The control itself: with the rule broken, the tests named for it must fail."""
    report = tmp_path / "mutant.xml"
    _apply(mutation, pristine)
    try:
        try:
            result = _run(mutation, pristine, report)
        except subprocess.TimeoutExpired:
            pytest.fail(
                f"{mutation.key} ({mutation.area}: {mutation.rule}): the tests named for it "
                f"did not finish within {MUTANT_TIMEOUT_S}s. A broken rule has to turn the "
                "suite red, not hang it."
            )
        counts = _outcomes(report)
    finally:
        _undo(mutation, pristine)

    context = (
        f"{mutation.key} ({mutation.area}: {mutation.rule})\n"
        f"  selection: {' '.join(mutation.caught_by)}\n"
        f"  exit: {result.returncode}\n{result.stdout[-2000:]}{result.stderr[-2000:]}"
    )
    assert counts["total"], (
        "the tests named for this mutation collected nothing. Renamed or removed -- or, if "
        "they live in a file that is new, not yet tracked: the copy is the tracked tree, "
        "which is what a clone gets.\n" + context
    )
    if counts["skipped"] == counts["total"]:
        pytest.skip(
            f"every test that pins {mutation.key} skips in this environment "
            f"(the compose checks need the Docker CLI): {mutation.caught_by}"
        )
    assert counts["failed"], "the rule was broken and its tests still passed\n" + context
