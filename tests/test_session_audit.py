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
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE_TIMEOUT_S = 120.0


# ─── What the harness did before collection ──────────────────────────────────


@pytest.mark.parametrize("variable", ("CLAUDE_DATA_DIR", "CODEX_DATA_DIR"))
def test_the_data_directories_point_at_empty_scratch(
    variable: str, session_scratch: Path
) -> None:
    data_dir = Path(os.environ[variable])
    assert data_dir.is_dir(), f"{variable} names no directory"
    assert list(data_dir.iterdir()) == [], f"{variable} is not empty"
    assert session_scratch in data_dir.parents, (
        f"{variable} is not this session's scratch"
    )


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


def test_the_same_shapes_at_allowed_resources_pass(probe_run) -> None:
    """Without this, "everything fails" would satisfy the tests above."""
    assert f"test_probes.py::{MUST_PASS}" not in probe_run.stdout, (
        f"the control failed; the harness refuses resources it should allow\n"
        f"{probe_run.stdout}"
    )
    assert f"{len(MUST_FAIL)} failed" in probe_run.stdout, probe_run.stdout
    assert "1 passed" in probe_run.stdout, probe_run.stdout
