"""One test-harness layer for the whole pytest session.

The suite makes two negative promises — no test reads a host credential file
or calls a live endpoint, and the activity-reader watcher sees every
filesystem call the process made. Until #38 both were kept one test at a time,
by patching or stubbing the names each test author thought of, so any path
nobody named was both live and unobserved and the suite still went green:

- `tests/test_main_payload.py` stubs the `main.py` globals a test cares about,
  but `app/main.py` builds all four sources from the ambient environment at
  import and `_publish()` refreshes every one of them. An unstubbed source read
  the real credential file and sent its bearer to whatever `CLAUDE_AI_HOST` or
  `CHATGPT_HOST` named.
- the watcher rebound seven module attributes, so a call through a name bound
  at import time (`from os import lstat`), through `posix.*` or through
  `io.FileIO(path)` went unseen.

This file replaces both with one thing that keys on the resource rather than on
the Python name that reached it:

1. **Before collection** — that is, before any test module imports `app.main` —
   both data directories point at empty scratch trees and both upstream hosts
   at a loopback port nothing listens on. No test's choice of what to stub can
   widen that.
2. **For the whole session** — a `sys.addaudithook` observer records every
   `open`, `os.listdir`, `os.scandir` and `socket.connect` by the resource
   touched. Reaching a host agent data root or a non-loopback address is
   refused where it happens *and* fails the test that did it, whatever name
   reached it.

**It binds pytest runs of this suite and nothing else.** It is not agent
settings, an agent hook or a sandbox: #21's committed `.claude/settings.json`
was reverted (#34, #35) because it bound the operator's own sessions, and
`tests/test_agent_tooling_context.py` keeps it that way.

**What it does not see**, enumerated because an incomplete list read as
exhaustive is how #38 happened one level down:

- `os.stat` and `os.lstat`. CPython raises no audit event for either, and a
  credential file's mtime is enough to publish as `last_activity`. That half
  is carried by a structural test instead --
  `tests/test_reader_filesystem_surface.py`.
- a path opened relative to a directory descriptor (`os.open(name, dir_fd=fd)`).
  The event carries the relative name, which resolves against the working
  directory here and so matches no denied root.
- a link planted mid-path by a test. The deny check compares an absolute path
  against each denied root and against that root's own `realpath`, which
  catches a root reached through a symlinked `HOME`; resolving every open
  would cost a syscall per call. The gate's own no-link rule covers the case
  that matters, and `tests/test_activity_gate.py` pins it.
- anything a *child process* does. An audit hook does not cross `fork`/`exec`.
  The two modules that spawn one -- `tests/test_negative_controls.py` and
  `tests/test_session_audit.py` -- spawn pytest, which loads this file again
  and is bounded by it in turn; a test that spawned something else would not
  be.
"""

from __future__ import annotations

import atexit
import ipaddress
import os
import shutil
import socket
import sys
import tempfile
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

# pytest loads this file once, as the top-level module `conftest`. Importing it
# again under another name -- `import tests.conftest`, which `pythonpath = .`
# makes possible -- re-runs everything below: a second scratch redirect, a
# second audit hook, a second dead-upstream socket. The marker lives on `sys`
# rather than in the environment because a subprocess must not inherit it:
# `tests/test_session_audit.py` runs a second pytest that has to set all this
# up for itself.
if getattr(sys, "_codervis_test_harness", False):  # pragma: no cover - import guard
    raise RuntimeError(
        "tests/conftest.py ran a second time, so something imported it under a "
        "name of its own. Ask for the `filesystem_audit` or `session_scratch` "
        "fixture instead of importing this module."
    )
sys._codervis_test_harness = True

# `app.main` reads the environment at import and builds the live clients and
# the activity readers there. If something imported it before this file ran,
# those four sources are already pointed at the ambient environment and nothing
# below can pull them back.
assert "app.main" not in sys.modules, (
    "app.main was imported before tests/conftest.py ran; its clients and readers "
    "are already built from the ambient environment"
)


# ─── 1. Scratch data, a dead upstream ────────────────────────────────────────

_SCRATCH = Path(tempfile.mkdtemp(prefix="codervis-session-"))
atexit.register(shutil.rmtree, _SCRATCH, True)

# Empty, and deliberately so: a source nobody stubbed finds no credential file,
# fails the way any unreadable credential fails, and reads nothing of anyone's.
for _name in ("claude", "codex"):
    (_SCRATCH / _name).mkdir()

# A socket bound but never listened on. Holding it for the session reserves the
# port, so "nothing is listening here" cannot become "something else is" partway
# through a run; connecting to it is refused at once.
_DEAD = socket.socket()
_DEAD.bind(("127.0.0.1", 0))
atexit.register(_DEAD.close)
_DEAD_UPSTREAM = "http://127.0.0.1:%d" % _DEAD.getsockname()[1]

# Whatever these named before the redirect is a place this process must not
# reach: someone running pytest in the shell they run the dashboard from has
# them pointed at real agent data.
_AMBIENT_DATA_DIRS = [
    os.environ.get(name)
    for name in ("CLAUDE_DATA_DIR", "CODEX_DATA_DIR", "CLAUDE_HOME", "CODEX_HOME")
]

os.environ["CLAUDE_DATA_DIR"] = str(_SCRATCH / "claude")
os.environ["CODEX_DATA_DIR"] = str(_SCRATCH / "codex")
os.environ["CLAUDE_AI_HOST"] = _DEAD_UPSTREAM
os.environ["CHATGPT_HOST"] = _DEAD_UPSTREAM


def _denied_roots() -> frozenset[str]:
    """Every host agent data root this process must not reach.

    The whole root, not just the credential file inside it: no test has
    business reading an operator's `~/.claude` at all, and a rule about one
    file is a rule about one name again.
    """
    home = Path.home()
    candidates = [
        home / ".claude",
        home / ".codex",
        # Beside the roots, not under them: an agent's own per-user state file
        # is a sibling, and prefix matching on a root never reaches a sibling.
        home / ".claude.json",
        home / ".codex.json",
        Path("/data/claude"),
        Path("/data/codex"),
        *(Path(value) for value in _AMBIENT_DATA_DIRS if value),
    ]
    # A denied root that contains the checkout, the home directory or the
    # session scratch is not a bound, it is a suite that cannot run: pytest
    # reads its own cache, the repository and the scratch trees constantly.
    # An ambient variable *can* name one -- `CLAUDE_HOME=/home/someone` -- so
    # the skip is here rather than left to whoever meets it.
    never_denied = {
        os.path.abspath(Path(__file__).resolve().parents[1]),
        os.path.abspath(home),
        os.path.abspath(_SCRATCH),
        os.path.abspath(tempfile.gettempdir()),
        os.sep,
    }
    roots: set[str] = set()
    for candidate in candidates:
        for absolute in (
            os.path.abspath(candidate),
            # A root reached through a symlinked HOME is the same root.
            os.path.realpath(os.path.abspath(candidate)),
        ):
            contains = [
                keep
                for keep in never_denied
                if absolute == keep or keep.startswith(absolute.rstrip(os.sep) + os.sep)
            ]
            if contains:
                # Loudly: the suite carries on, and from here on it is bounded
                # by less than this file says it is.
                warnings.warn(
                    f"tests/conftest.py is not denying {absolute}: it contains "
                    f"{contains[0]}, which pytest and this suite must be able "
                    "to read. Point the variable that named it at a directory "
                    "of its own.",
                    stacklevel=1,
                )
                continue
            roots.add(absolute)
    return frozenset(roots)


DENIED_ROOTS = _denied_roots()


# ─── 2. The session observer ─────────────────────────────────────────────────

# `socket.connect` is not the only way an address is dialled: UDP leaves
# through `sendto` and `sendmsg`, which are events of their own. All three
# carry the address at `args[1]`.
_ADDRESS_EVENTS = frozenset({"socket.connect", "socket.sendto", "socket.sendmsg"})
_KINDS = {"open": "open", "os.listdir": "scandir", "os.scandir": "scandir"}
_WATCHED = frozenset(_KINDS) | _ADDRESS_EVENTS


def _resolves_only_to_loopback(name: str) -> bool:
    try:
        info = socket.getaddrinfo(name, None)
    except OSError:
        return False
    return bool(info) and all(
        ipaddress.ip_address(entry[4][0].split("%", 1)[0]).is_loopback for entry in info
    )


class SessionAudit:
    """What the process touched, keyed on the resource.

    One instance per session, fed by an audit hook. `violations` is what no
    test may do at all; `watch()` is how a test asks what it did do.

    A test holds this object -- `watch()` is a fixture -- so nothing it can
    reach may disarm the guard. The count is a separate integer rather than
    `len(violations)`, and `violations` hands back a tuple, so clearing or
    rebinding the record cannot make a phase look clean; `_watchers` is an
    immutable tuple rebound whole, so a background refresher thread reading it
    while a `watch()` enters or leaves sees one state or the other rather than
    a list mid-mutation.
    """

    KINDS = frozenset({"open", "scandir"})

    def __init__(self, denied_roots: frozenset[str]) -> None:
        self._denied = tuple(denied_roots)
        self.__messages: list[str] = []
        self.__count = 0
        self._watchers: tuple[tuple[str, list[tuple[str, Path]]], ...] = ()

    @property
    def violations(self) -> tuple[str, ...]:
        return tuple(self.__messages)

    @property
    def count(self) -> int:
        """Monotonic, and never derived from the messages.

        What fails a phase is that this number moved. The messages say what
        happened; emptying them changes the report and not the verdict.
        """
        return self.__count

    def since(self, mark: int) -> tuple[str, ...]:
        """What was recorded since `mark`, counted rather than measured."""
        new = self.__count - mark
        if new <= 0:
            return ()
        kept = tuple(self.__messages[-new:])
        if len(kept) == new:
            return kept
        return kept + (
            f"{new - len(kept)} more, whose record was emptied during this test",
        )

    # -- what the hook feeds it ------------------------------------------
    def saw_path(self, kind: str, target: object) -> str | None:
        try:
            text = os.fsdecode(target)
        except TypeError:
            return None  # a file descriptor, not a path
        absolute = os.path.abspath(text)
        for watched_scope, seen in self._watchers:
            if absolute == watched_scope or absolute.startswith(watched_scope + os.sep):
                seen.append((kind, Path(absolute)))
        for root in self._denied:
            if absolute == root or absolute.startswith(root + os.sep):
                return self._violation(
                    f"{kind} of {absolute}, under host agent data {root}"
                )
        return None

    def saw_address(self, address: object) -> str | None:
        if not isinstance(address, tuple) or len(address) < 2:
            return None  # AF_UNIX and the rest: no way off this machine
        host = address[0]
        if not isinstance(host, (str, bytes)):
            return None
        text = host.decode() if isinstance(host, bytes) else host
        try:
            if ipaddress.ip_address(text.split("%", 1)[0]).is_loopback:
                return None
        except ValueError:
            # A name, not a literal. This file's whole thesis is that keying on
            # a name is the bug, so resolve it and judge the addresses: a
            # `localhost` that an /etc/hosts entry points off-box is not
            # loopback whatever it is spelled.
            if _resolves_only_to_loopback(text):
                return None
        return self._violation(
            f"socket.connect to {text}:{address[1]}, which is not loopback"
        )

    def _violation(self, message: str) -> str:
        self.__messages.append(message)
        self.__count += 1
        return message

    # -- what a test asks it ---------------------------------------------
    @contextmanager
    def watch(self, scope: Path) -> Iterator[list[tuple[str, Path]]]:
        """Record every observed filesystem call under `scope`.

        `open`, `os.listdir` and `os.scandir` only. CPython raises no audit
        event for `os.stat` or `os.lstat`, so a stat is not in here and the
        absence of one proves nothing.
        """
        seen: list[tuple[str, Path]] = []
        entry = (os.path.abspath(scope), seen)
        self._watchers = self._watchers + (entry,)
        try:
            yield seen
        finally:
            self._watchers = tuple(w for w in self._watchers if w is not entry)


AUDIT = SessionAudit(DENIED_ROOTS)


class ForbiddenResource(Exception):
    """Raised inside the audit hook, so the read or the dial never happens.

    Containment only. `SourceRefresher.refresh_once()` catches `Exception`
    whole by design, and a test's own `pytest.raises` may swallow this too,
    so the recorded violation is what actually fails the test.
    """


def _hook(event: str, args: tuple) -> None:
    # Called for every audit event CPython raises, so the miss is first and
    # cheap. A hook that raised on an event it did not understand would break
    # whatever raised it, which is why the arity is checked rather than
    # assumed.
    if event not in _WATCHED:
        return
    if event in _ADDRESS_EVENTS:
        message = AUDIT.saw_address(args[1]) if len(args) > 1 else None
    else:
        message = AUDIT.saw_path(_KINDS[event], args[0]) if args else None
    if message is not None:
        raise ForbiddenResource(message)


sys.addaudithook(_hook)


# ─── 3. Whichever test did it, fails ─────────────────────────────────────────


def _report(violations: tuple[str, ...]) -> str:
    listed = "\n".join(f"  - {item}" for item in violations)
    return (
        "this test reached a resource the suite promises it does not touch:\n"
        f"{listed}\n"
        "The suite must not read a host credential file or call a live endpoint. "
        "Stub the source, or point it at tmp_path and a loopback address."
    )


_phase_watermark = 0


def pytest_runtest_logstart(nodeid: str, location: tuple) -> None:
    global _phase_watermark
    _phase_watermark = AUDIT.count


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """Fail whichever test touched a host agent data root or dialled off-box.

    Per phase rather than in a teardown fixture, so a violation in the test
    body is reported as that test failing rather than as an error tidying up
    after it, and one during setup still lands on the test that asked for it.

    This, not the `ForbiddenResource` the hook raises, is what makes the test
    red. `SourceRefresher.refresh_once()` catches `Exception` whole by design,
    and a test's own `pytest.raises` swallows just as well, so a raise alone
    would leave #38 exactly where it was.
    """
    global _phase_watermark
    report = yield
    new = AUDIT.since(_phase_watermark)
    _phase_watermark = AUDIT.count
    if new:
        # A test that failed on its own *and* reached something keeps its own
        # traceback: overwriting it would hide the reason it broke behind the
        # symptom, and it is already red either way.
        existing = report.longrepr
        report.outcome = "failed"
        report.longrepr = (
            _report(new) if existing is None else f"{existing}\n\n{_report(new)}"
        )
    return report


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """And anything reached outside a test phase at all.

    A session-scoped fixture's finalizer runs after the last test's teardown
    report, so a violation there belongs to no phase and would otherwise be
    recorded and never mentioned. The run fails; a green exit status is the one
    thing this file exists to stop being available cheaply.
    """
    unaccounted = AUDIT.since(_phase_watermark)
    if unaccounted:
        session.exitstatus = 1
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(_report(unaccounted), red=True)


def pytest_collection_finish(session: pytest.Session) -> None:
    """Import time is not inside any test, so account for it separately."""
    if AUDIT.violations:
        raise pytest.UsageError(_report(AUDIT.violations))


@pytest.fixture
def filesystem_audit() -> SessionAudit:
    """The session's record of what the process touched.

    `tests/test_activity_readers.py` asserts on this rather than on module
    attributes it rebound: a reader reaching the filesystem through an
    import-time binding, `posix.*` or `io.FileIO` is invisible to the latter.
    """
    return AUDIT


@pytest.fixture
def session_scratch() -> Path:
    """The empty trees both data directories were pointed at before collection."""
    return _SCRATCH
