"""The session harness, and one sealed run that proves it bites.

`tests/conftest.py` makes two claims for the whole suite: an unstubbed source
finds scratch data and a dead upstream rather than the operator's own, and a
test that reaches a host agent data root or dials off-box fails. A harness that
silently stopped doing either would leave every other test green, which is the
shape of the defect it was written for (#38), so both are checked here.

The proof run is sealed. It never names a real credential file: it runs a
second pytest, in a directory of its own, with `CLAUDE_DATA_DIR` pointed at a
*synthetic* tree, and it is that pointing which puts the tree in the harness's
denied set -- "wherever the data-dir variables named before the redirect" is
one of the roots `tests/conftest.py` forbids, precisely because someone running
pytest in the shell they run the dashboard from has them pointed at real agent
data. So the probes reach for a file this test wrote, and the rule they trip is
the one that protects the real one.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE_TIMEOUT_S = 120.0

#: Synthetic, and never anyone's. Distinctive enough that finding one in a
#: request header is unambiguous.
CLAUDE_BEARER = "synthetic-claude-bearer-not-a-real-token"
CODEX_BEARER = "synthetic-codex-bearer-not-a-real-token"


# ─── What the harness did before collection ──────────────────────────────────


@pytest.mark.parametrize("variable", ("CLAUDE_DATA_DIR", "CODEX_DATA_DIR"))
def test_the_data_directories_point_at_empty_scratch(
    variable: str, session_scratch: Path
) -> None:
    data_dir = Path(os.environ[variable])
    assert data_dir.is_dir(), f"{variable} names no directory"
    assert session_scratch in data_dir.parents, (
        f"{variable} is not this session's scratch"
    )
    # Not "nothing has ever been written here", which would make this test
    # depend on what ran before it. The property is that a source refreshed
    # with no stub finds no credential to send.
    credentials = sorted(
        path.name
        for path in data_dir.iterdir()
        if path.name in (".credentials.json", "auth.json")
    )
    assert credentials == [], f"{variable} holds {credentials}"


@pytest.mark.parametrize("variable", ("CLAUDE_AI_HOST", "CHATGPT_HOST"))
def test_the_upstream_hosts_point_at_a_port_nothing_answers(variable: str) -> None:
    host = os.environ[variable]
    assert host.startswith("http://127.0.0.1:"), host
    port = int(host.rsplit(":", 1)[1])
    with socket.socket() as probe:
        probe.settimeout(5.0)
        with pytest.raises(ConnectionRefusedError):
            probe.connect(("127.0.0.1", port))


def test_app_main_was_built_from_that_environment() -> None:
    """The redirect is worth nothing if `app.main` read the environment first."""
    from app import main

    assert Path(main._live.data_dir) == Path(os.environ["CLAUDE_DATA_DIR"])
    assert Path(main._codex.data_dir) == Path(os.environ["CODEX_DATA_DIR"])
    assert Path(main._claude_activity.data_dir) == Path(os.environ["CLAUDE_DATA_DIR"])
    assert Path(main._codex_activity.data_dir) == Path(os.environ["CODEX_DATA_DIR"])
    assert main._live.host == os.environ["CLAUDE_AI_HOST"]
    assert main._codex.host == os.environ["CHATGPT_HOST"]


def test_publishing_every_unstubbed_source_sends_nothing_anywhere() -> None:
    """`_publish()` refreshes all four sources; none of them may reach anyone.

    This is ambient-2 as a standing test. With no stub at all, each source runs
    for real against scratch data: the quota clients find no credential file
    and fail before any request is built, and the activity readers scan an
    empty root. What says no token left is not these assertions but the
    harness's own guard, which fails this test if anything reached a host agent
    data root or dialled off-box -- `refresh_once()` catches `Exception` whole,
    so a raise on its own would be swallowed here exactly as it was in #38.
    """
    from app import main

    for source in main._SOURCES:
        source.refresh_once()
        record = source.snapshot()
        assert record.at is not None, f"{source.name} published nothing"

    quota = (main._claude_quota_source, main._codex_quota_source)
    for source in quota:
        assert source.snapshot().error is not None, (
            f"{source.name} succeeded against an empty scratch directory, which "
            "means it read a credential file this test did not put there"
        )

    activity = (main._claude_activity_source, main._codex_activity_source)
    for source in activity:
        record = source.snapshot()
        assert record.ok, f"{source.name}: {record.error!r}"
        assert record.value.last_activity is None, (
            f"{source.name} found activity under an empty scratch root"
        )


# ─── The guard, proved by a sealed run ───────────────────────────────────────

PROBES = '''
import io
import os
import posix
import socket
from pathlib import Path

HOST_TREE = Path(os.environ["PRETEND_HOST_TREE"])
CREDENTIALS = HOST_TREE / ".credentials.json"
SCRATCH = Path(os.environ["CLAUDE_DATA_DIR"])


def _swallow(call):
    """As `SourceRefresher.refresh_once()` does: catch Exception whole."""
    try:
        call()
    except Exception:
        pass


def test_posix_listdir_of_a_host_tree():
    _swallow(lambda: posix.listdir(str(HOST_TREE)))


def test_io_fileio_of_a_host_credential_file():
    _swallow(lambda: io.FileIO(str(CREDENTIALS)).read())


def test_builtin_open_of_a_host_credential_file():
    _swallow(lambda: open(CREDENTIALS, "rb").read())


def test_os_open_of_a_host_credential_file():
    _swallow(lambda: os.open(str(CREDENTIALS), os.O_RDONLY))


def test_os_scandir_of_a_host_tree():
    _swallow(lambda: list(os.scandir(str(HOST_TREE))))


def test_dialling_a_non_loopback_address():
    sock = socket.socket()
    sock.settimeout(1.0)
    _swallow(lambda: sock.connect(("192.0.2.1", 80)))
    sock.close()


def test_udp_sendto_a_non_loopback_address():
    """`socket.connect` is not the only way an address is dialled."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _swallow(lambda: sock.sendto(b"x", ("192.0.2.1", 9)))
    sock.close()


def test_udp_sendmsg_a_non_loopback_address():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _swallow(lambda: sock.sendmsg([b"x"], [], 0, ("192.0.2.1", 9)))
    sock.close()


def test_clearing_the_record_does_not_clear_the_finding(filesystem_audit):
    """The record is a test's to read, never a test's to empty."""
    _swallow(lambda: io.FileIO(str(CREDENTIALS)).read())
    for attempt in (
        lambda: filesystem_audit.violations.clear(),
        lambda: filesystem_audit._SessionAudit__messages.clear(),
        lambda: setattr(filesystem_audit, "_SessionAudit__messages", []),
    ):
        try:
            attempt()
        except AttributeError:
            pass


def test_a_failure_of_its_own_keeps_its_own_reason():
    """Violating and failing: the harness must add a reason, not replace one."""
    _swallow(lambda: io.FileIO(str(CREDENTIALS)).read())
    assert 1 == 2, "this test's own reason"


def test_control_reading_scratch_and_dialling_loopback():
    """The control: the same shapes, at resources the suite is allowed."""
    (SCRATCH / "written-here.json").write_text("{}")
    assert list(os.scandir(str(SCRATCH)))
    assert io.FileIO(str(SCRATCH / "written-here.json")).read() == b"{}"
    (SCRATCH / "written-here.json").unlink()
    sock = socket.socket()
    sock.settimeout(5.0)
    try:
        sock.connect(("127.0.0.1", int(os.environ["CLAUDE_AI_HOST"].rsplit(":", 1)[1])))
    except ConnectionRefusedError:
        pass
    finally:
        sock.close()
'''

MUST_FAIL = (
    "test_posix_listdir_of_a_host_tree",
    "test_io_fileio_of_a_host_credential_file",
    "test_builtin_open_of_a_host_credential_file",
    "test_os_open_of_a_host_credential_file",
    "test_os_scandir_of_a_host_tree",
    "test_dialling_a_non_loopback_address",
    "test_udp_sendto_a_non_loopback_address",
    "test_udp_sendmsg_a_non_loopback_address",
    "test_clearing_the_record_does_not_clear_the_finding",
    "test_a_failure_of_its_own_keeps_its_own_reason",
)
MUST_PASS = "test_control_reading_scratch_and_dialling_loopback"


@pytest.fixture(scope="module")
def probe_run(tmp_path_factory) -> subprocess.CompletedProcess:
    """One pytest of its own, under a copy of this suite's conftest."""
    rig = tmp_path_factory.mktemp("session-audit-probe")
    shutil.copy(ROOT / "tests" / "conftest.py", rig / "conftest.py")
    (rig / "test_probes.py").write_text(PROBES, encoding="utf-8")

    # Synthetic, and never anyone's: what makes it a denied root is that
    # CLAUDE_DATA_DIR named it before the harness redirected the variable.
    # It sits *outside* the rig, because the probe run collects the rig and a
    # collection that scandirs a denied root fails the run before a test runs.
    host_tree = tmp_path_factory.mktemp("pretend-host-claude")
    (host_tree / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "synthetic-not-a-real-token"}}),
        encoding="utf-8",
    )

    environ = {
        **os.environ,
        "CLAUDE_DATA_DIR": str(host_tree),
        "PRETEND_HOST_TREE": str(host_tree),
    }
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rf"],
        cwd=rig,
        env=environ,
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_S,
    )


@pytest.mark.parametrize("probe", MUST_FAIL)
def test_reaching_a_denied_resource_fails_the_test_that_did_it(
    probe, probe_run
) -> None:
    """Six names for two resources; the record keys on the resource.

    Each probe swallows the exception the audit hook raises, exactly as a
    refresher does, so the only thing that can turn it red is the harness
    accounting for what the test touched.
    """
    assert probe_run.returncode != 0, probe_run.stdout
    assert f"test_probes.py::{probe}" in probe_run.stdout, (
        f"{probe} did not fail; the harness let it through\n{probe_run.stdout}"
    )


def test_the_probe_run_says_why(probe_run) -> None:
    assert "host agent data" in probe_run.stdout
    assert "not loopback" in probe_run.stdout


def test_a_test_that_also_failed_on_its_own_keeps_both_reasons(probe_run) -> None:
    """The harness reports what was touched; it does not overwrite a traceback.

    A test that broke for a reason of its own and reached something too must
    still say why it broke, or the harness hides the bug behind the symptom.
    """
    assert "this test's own reason" in probe_run.stdout, probe_run.stdout


def test_the_same_shapes_at_allowed_resources_pass(probe_run) -> None:
    """Without this, "everything fails" would satisfy the tests above."""
    assert f"test_probes.py::{MUST_PASS}" not in probe_run.stdout, (
        f"the control failed; the harness refuses resources it should allow\n"
        f"{probe_run.stdout}"
    )
    assert f"{len(MUST_FAIL)} failed" in probe_run.stdout, probe_run.stdout
    assert "1 passed" in probe_run.stdout, probe_run.stdout


# ─── ambient-2, made permanent ───────────────────────────────────────────────

AMBIENT_PROBE = '''
import os

import pytest

from app import main
from app.refresh import SourceRefresher


class _StubClaudeQuota:
    """One source stubbed, as tests/test_main_payload.py stubs the ones it uses."""

    def get(self):
        return {"five_hour": {"utilization": 1.0}, "seven_day": {"utilization": 2.0}}


@pytest.fixture(autouse=True)
def fresh_sources(monkeypatch):
    sources = {
        "_claude_quota_source": SourceRefresher(
            "claude-quota", lambda: main._live.get(), 30.0
        ),
        "_codex_quota_source": SourceRefresher(
            "codex-quota", lambda: main._codex.get(), 30.0
        ),
    }
    for name, source in sources.items():
        monkeypatch.setattr(main, name, source)
    monkeypatch.setattr(main, "_SOURCES", tuple(sources.values()))


def test_publishing_with_one_source_stubbed(monkeypatch):
    """#38 exactly: the Codex client nobody stubbed still runs, for real."""
    monkeypatch.setattr(main, "_live", _StubClaudeQuota())
    for source in main._SOURCES:
        source.refresh_once()
'''


@pytest.fixture(scope="module")
def ambient_rig(tmp_path_factory) -> dict:
    """Two runs of the same probe: one under the harness, one without it.

    The probe is `tests/test_main_payload.py` in miniature -- a test that stubs
    one source and calls `_publish()`, which refreshes all four. Both runs
    start from an environment that has `CLAUDE_DATA_DIR` and `CODEX_DATA_DIR`
    holding synthetic credential files and both upstream hosts pointed at a
    recording loopback stub, which is what an operator's own shell looks like.

    Running it *without* `tests/conftest.py` is what stops this being a test
    that proves nothing: if a bearer cannot leave under either arrangement, the
    arrangement is not what is stopping it.
    """
    rig = tmp_path_factory.mktemp("ambient")
    claude = rig / "claude-data"
    codex = rig / "codex-data"
    claude.mkdir()
    codex.mkdir()
    (claude / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": CLAUDE_BEARER}}), encoding="utf-8"
    )
    (codex / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": CODEX_BEARER, "account_id": "a"}}),
        encoding="utf-8",
    )

    received: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
            received.append(self.headers.get("Authorization") or "")
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    stub = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    try:
        upstream = f"http://127.0.0.1:{stub.server_address[1]}"
        environ = {
            **os.environ,
            "CLAUDE_DATA_DIR": str(claude),
            "CODEX_DATA_DIR": str(codex),
            "CLAUDE_AI_HOST": upstream,
            "CHATGPT_HOST": upstream,
            "PYTHONPATH": str(ROOT),
        }

        runs = {}
        for name, with_harness in (("unguarded", False), ("guarded", True)):
            case = rig / name
            case.mkdir()
            (case / "test_ambient.py").write_text(AMBIENT_PROBE, encoding="utf-8")
            if with_harness:
                shutil.copy(ROOT / "tests" / "conftest.py", case / "conftest.py")
            received.clear()
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                cwd=case,
                env=environ,
                capture_output=True,
                text=True,
                timeout=PROBE_TIMEOUT_S,
            )
            runs[name] = (completed, list(received))
    finally:
        stub.shutdown()
        stub.server_close()
    return runs


def test_without_the_harness_an_unstubbed_source_sends_its_bearer(ambient_rig) -> None:
    """The control. #38 as it was, so that the test below is about the fix."""
    completed, received = ambient_rig["unguarded"]
    assert completed.returncode == 0, completed.stdout
    sent = [header for header in received if CODEX_BEARER in header]
    assert sent, (
        "the unguarded probe sent no bearer, so the guarded one below proves "
        f"nothing about the harness\n{completed.stdout}"
    )


def test_under_the_harness_it_sends_nothing_and_still_passes(ambient_rig) -> None:
    """ambient-2 closed: the environment no longer decides what a test reaches."""
    completed, received = ambient_rig["guarded"]
    assert completed.returncode == 0, completed.stdout
    leaked = [
        header
        for header in received
        if CLAUDE_BEARER in header or CODEX_BEARER in header
    ]
    assert leaked == [], f"a synthetic bearer reached the stub: {leaked}"
    assert received == [], f"the stub was dialled at all: {received}"


# ─── The denied set itself ───────────────────────────────────────────────────
#
# The sealed run above proves the harness bites, and it proves it on *one* root: the tree
# `CLAUDE_DATA_DIR` named before the redirect. So every fixed root beside it -- `~/.claude`,
# `~/.codex`, the two per-user state files that sit next to them, and the container's
# `/data/claude` and `/data/codex` -- could be deleted from `tests/conftest.py` with the whole
# suite green (#78), and from then on a test reading an operator's real `~/.codex` would pass.
#
# What follows states the set here, in the test, rather than reading it back from the module
# that builds it. A pin written as `assert DENIED_ROOTS == conftest._denied_roots()` is the
# shape #78 is about: it moves with whatever it is checking.

#: Under the home directory of whoever runs pytest. The two `.json` files are siblings of the
#: roots rather than children, and prefix matching on a root never reaches a sibling, so each
#: has to be denied in its own right.
FIXED_DENIED_UNDER_HOME = (".claude", ".codex", ".claude.json", ".codex.json")

#: Where docker-compose.yml mounts the two trees, which is what the app reads in the container
#: and what a test running there would otherwise be able to open.
FIXED_DENIED_ABSOLUTE = ("/data/claude", "/data/codex")


def _conftest():
    """The harness module itself. Imported by name: pytest puts `tests/` on `sys.path`."""
    import conftest

    return conftest


@pytest.mark.parametrize("name", FIXED_DENIED_UNDER_HOME)
def test_each_agent_root_under_home_is_denied(name: str) -> None:
    root = os.path.abspath(Path.home() / name)
    denied = _conftest().DENIED_ROOTS
    assert root in denied, (
        f"{root} is no longer in the harness's denied set, so a test in this suite may read "
        f"it. Every root is named in this file; if one was dropped on purpose, drop it here "
        f"too and say why.\nDenied: {sorted(denied)}"
    )


@pytest.mark.parametrize("root", FIXED_DENIED_ABSOLUTE)
def test_each_container_data_root_is_denied(root: str) -> None:
    denied = _conftest().DENIED_ROOTS
    assert os.path.abspath(root) in denied, (
        f"{root} is no longer in the harness's denied set.\nDenied: {sorted(denied)}"
    )


def test_the_harness_refuses_a_read_under_every_one_of_those_roots() -> None:
    """The set above, driven through the observer that acts on it.

    Membership alone would stay true if the matching stopped working -- an `==` where a prefix
    test belongs, say -- so each root is also put to the object, by name and with a child path
    under it. Nothing is opened: `saw_path` is the decision the audit hook makes, called
    directly, so this reaches no real file of anyone's.
    """
    audit = _conftest().SessionAudit(_conftest().DENIED_ROOTS)
    roots = [os.path.abspath(Path.home() / name) for name in FIXED_DENIED_UNDER_HOME]
    roots += [os.path.abspath(root) for root in FIXED_DENIED_ABSOLUTE]
    for root in roots:
        assert audit.saw_path("open", root), root
        assert audit.saw_path("open", os.path.join(root, "anything")), root
    # And a sibling that merely starts with the same characters is not under it: a prefix test
    # that forgot the separator would deny half the home directory and read as a stronger bound.
    assert audit.saw_path("open", os.path.abspath(Path.home() / ".claude-notes")) is None
