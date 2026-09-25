"""The stat half of TB-ACTIVITY, which no audit hook can witness.

`tests/conftest.py`'s session observer sees an `open`, an `os.listdir` and an
`os.scandir` whatever name reached them, and
`tests/test_activity_readers.py` asserts on that record. It cannot see a stat:
CPython raises no audit event for `os.stat` or `os.lstat`, so a reader that
read the credential file's *metadata* -- which is all either reader needs to
publish a timestamp, and exactly the TB-ACTIVITY leak -- would leave the record
looking innocent.

So the stat half is pinned structurally instead: neither reader module names a
filesystem API at all. `app/activity_gate.py` is the only module that does, and
it is the enforcement point CLAUDE.md and AGENTS.md say it is -- "it is the only
way either reader reaches the filesystem" becomes checkable here rather than
described.

This fails closed. The `os` names a reader may use are an allow-list, so a new
`os.something` fails until somebody adds it here on purpose, and pathlib's
filesystem surface is derived from `pathlib` itself (`Path` minus `PurePath`),
so a future release adding a method adds it to the deny-list too.

What it does not reach: a filesystem call made by something a reader imports
that is neither its gate nor its budget layer, or one assembled at runtime from
strings. The first is bounded by how few imports either module has, both
asserted below; the second is a deliberate act, and the gate's own record and
the session observer are what answer it.
"""

from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path, PurePath

import pytest

ROOT = Path(__file__).resolve().parents[1]
READERS = ("app/claude_activity.py", "app/codex_activity.py")

#: The only names either reader may take from `os`. Neither touches the
#: filesystem: `environ` is how the module reads its data directory out of the
#: environment, `stat_result` is a type annotation on a value the gate returned.
OS_ALLOWED = frozenset({"environ", "stat_result"})

#: Modules whose whole point is reaching a resource. A reader importing one has
#: left the gate whatever it does with it.
FORBIDDEN_MODULES = frozenset(
    {
        "builtins",
        "codecs",
        "fcntl",
        "fileinput",
        "glob",
        "io",
        "linecache",
        "mmap",
        "posix",
        "shutil",
        "socket",
        "ssl",
        "subprocess",
        "tempfile",
        "urllib",
        "urllib.request",
    }
)

#: Derived, not typed: every `Path` method that is not on `PurePath` is one
#: that touches the filesystem, and a new one in a future pathlib lands here
#: without anybody remembering to add it.
_PATH_ONLY = {
    name for name in set(dir(Path)) - set(dir(PurePath)) if not name.startswith("_")
}

#: Matching is by method name, so a name another type also carries would fire on
#: that type's method instead. Those are derived too, and today they are exactly
#: `copy` and `replace` -- `str.replace`, `datetime.replace`. Both are pathlib
#: *writes*, which no gate grants and the read-only bind mounts refuse, so
#: dropping them costs this test nothing it exists to catch: a reader leaking a
#: credential file reads it or stats it.
_SHARED_WITH_OTHER_TYPES = {
    name
    for cls in (str, bytes, bytearray, dict, list, tuple, set, int, float, datetime)
    for name in dir(cls)
    if not name.startswith("_")
}

PATH_FILESYSTEM_METHODS = frozenset(_PATH_ONLY - _SHARED_WITH_OTHER_TYPES)

#: The gate's own surface, for the positive half: what a reader is *supposed*
#: to call. Listed so that the test below reports which of them each reader
#: used, and fails if one used none -- a reader reaching the filesystem through
#: nothing at all would otherwise pass this module trivially.
GATE_OPERATIONS = frozenset({"root_exists", "stat", "open_bytes", "walk", "start_scan"})


def _tree(relative: str) -> ast.Module:
    return ast.parse((ROOT / relative).read_text(encoding="utf-8"), filename=relative)


def _imports(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def _from_os_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os" and node.level == 0:
            names.update(alias.name for alias in node.names)
    return names


def _is_gate_call(func: ast.Attribute) -> bool:
    """True for `<something>.gate.<operation>(...)` and nothing else."""
    return isinstance(func.value, ast.Attribute) and func.value.attr == "gate"


@pytest.mark.parametrize("relative", READERS)
def test_a_reader_imports_no_module_that_reaches_a_resource(relative: str) -> None:
    imported = _imports(_tree(relative))
    forbidden = sorted(
        name
        for name in imported
        if name in FORBIDDEN_MODULES or name.split(".", 1)[0] in FORBIDDEN_MODULES
    )
    assert not forbidden, (
        f"{relative} imports {forbidden}; a reader reaches the filesystem "
        "through its ActivityGate and through nothing else"
    )


@pytest.mark.parametrize("relative", READERS)
def test_a_reader_takes_only_allow_listed_names_from_os(relative: str) -> None:
    """`os.<anything else>` is a filesystem API until somebody says otherwise."""
    tree = _tree(relative)
    used = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    }
    used |= _from_os_names(tree)
    unexpected = sorted(used - OS_ALLOWED)
    assert not unexpected, (
        f"{relative} uses os.{{{', '.join(unexpected)}}}. The gate owns what a "
        f"reader may reach; only {sorted(OS_ALLOWED)} are not a way past it."
    )


@pytest.mark.parametrize("relative", READERS)
def test_a_reader_calls_no_pathlib_method_that_touches_the_filesystem(
    relative: str,
) -> None:
    """A `Path` is a name in these modules, never a handle on a file.

    Both readers hold `self.data_dir` as a `Path`, which is fine: the pure-path
    half computes names. The half that is not on `PurePath` is the half that
    goes to the disk, and the gate does that.
    """
    # `self.gate.stat(...)` and `self.gate.walk(...)` share a name with
    # `Path.stat` and `Path.walk`, so the exemption is per *call site*, not per
    # name: exempting the name would let a `(self.data_dir / x).stat()` beside
    # them through, which is the leak this module exists for.
    touching = sorted(
        {
            node.func.attr
            for node in ast.walk(_tree(relative))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in PATH_FILESYSTEM_METHODS
            and not _is_gate_call(node.func)
        }
    )
    assert not touching, (
        f"{relative} calls {touching}, which pathlib implements against the "
        "filesystem. Route it through self.gate."
    )


@pytest.mark.parametrize("relative", READERS)
def test_a_reader_never_calls_open_directly(relative: str) -> None:
    direct = [
        node
        for node in ast.walk(_tree(relative))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("open", "eval", "exec", "__import__")
    ]
    assert not direct, f"{relative} calls {direct[0].func.id}() directly"


@pytest.mark.parametrize("relative", READERS)
def test_a_reader_does_reach_the_filesystem_through_its_gate(relative: str) -> None:
    """The positive half: these tests must not pass by the reader doing nothing.

    A module that reached the filesystem by no means at all would satisfy every
    assertion above, and would also be a reader that reports no activity. This
    is what says the allow-listing above is describing a working reader.
    """
    used = {
        node.func.attr
        for node in ast.walk(_tree(relative))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "gate"
    }
    assert used & GATE_OPERATIONS, (
        f"{relative} calls no ActivityGate operation; either it reaches the "
        "filesystem some other way or it reads nothing at all"
    )
    assert not used - GATE_OPERATIONS, (
        f"{relative} calls gate operations this test does not know about: "
        f"{sorted(used - GATE_OPERATIONS)}"
    )
