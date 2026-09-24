"""The one door the activity readers reach the filesystem through.

TB-ACTIVITY -- read only these files, never a credential file, and for Codex
never open anything -- used to live in prose alone: two docstrings, `CLAUDE.md`
and `AGENTS.md`. Each reader then built its own file set ad hoc, with calls
that follow links, so a link planted under a data root walked straight out of
it: `sessions/x.jsonl -> ../../claude/.credentials.json` resolves inside the
container, where the two trees are sibling mounts read by one process, and the
Codex reader published that file's mtime -- its token-refresh time -- as
`codex.last_activity`. A symlinked subtree root was an existence oracle for any
path the container can see.

This module is that boundary in code. A reader holds one `ActivityGate` and
reaches the filesystem through nothing else. The gate owns:

- **the allow-list**: the named files directly under that reader's own data
  root, and the subtrees of it whose regular files may be reached;
- **the operations**: `STAT` for Codex, which needs metadata only, and
  `STAT | READ` for Claude, which parses transcript records. An operation the
  reader was not granted is refused even on an allow-listed path;
- **the no-link rule**: every component below the root is examined with
  `lstat`, so a link is seen as a link rather than followed to whatever it
  names, and a read `open`s with `O_NOFOLLOW` so the final component cannot be
  swapped for one after it was checked. The root itself may be a link: it is
  the operator's own configuration. Nothing below it may be.

The gate also records what it admitted and what it refused, per scan, which is
what lets `tests/test_activity_readers.py` assert the set of paths a reader
touched and the operations it performed, rather than only the timestamp it
returned. Without that record a reader that reads credential files returns the
same timestamp as one that does not, and the suite cannot tell them apart.

Refusing is not failing: a refused path contributes no timestamp and the scan
carries on, exactly as an unreadable file always did. `PathRefused` is what the
gate raises for anything it will not do, so a caller has one thing to catch.
"""

from __future__ import annotations

import os
import stat as stat_module
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator

STAT = "stat"
READ = "read"
WALK = "walk"

OPERATIONS = frozenset({STAT, READ})

#: How many distinct (operation, path) pairs one scan's record holds. The walk
#: is bounded by ``max_files``, so this is the smaller of the two bounds and is
#: what keeps the record from being the unbounded thing in a bounded scan. A
#: test that asserts a path was *not* touched must check ``truncated`` too.
MAX_RECORDED = 4096


class PathRefused(Exception):
    """The gate would not reach this path, or not this way.

    Its message names the reason and never the path: `AGENTS.md`'s safety rule
    is that an exception's own text must not reach the payload or a log line,
    and an operator's project directory names are what the oracle leaked.
    """


class AccessRecord:
    """An ordered, deduplicated, bounded record of (operation, path) pairs."""

    def __init__(self, limit: int = MAX_RECORDED) -> None:
        self._limit = limit
        self._entries: dict[tuple[str, str], None] = {}
        self.truncated = False

    def add(self, operation: str, path: str) -> None:
        key = (operation, path)
        if key in self._entries:
            return
        if len(self._entries) >= self._limit:
            self.truncated = True
            return
        self._entries[key] = None

    def clear(self) -> None:
        self._entries.clear()
        self.truncated = False

    @property
    def entries(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._entries)

    @property
    def paths(self) -> frozenset[str]:
        return frozenset(path for _, path in self._entries)

    def operations(self, path: str) -> frozenset[str]:
        return frozenset(op for op, seen in self._entries if seen == path)

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    def __len__(self) -> int:
        return len(self._entries)


@dataclass
class WalkBudget:
    """What one scan's walk may cost, shared across a reader's subtrees.

    Charged per directory *entry touched*, not per file yielded: a tree of
    empty directories would otherwise cost the whole walk for free.
    """

    max_entries: int
    deadline: float
    entries: int = 0
    exhausted: bool = False

    def spend(self) -> bool:
        """Charge one entry. False once the walk must stop."""
        self.entries += 1
        if self.entries > self.max_entries or time.monotonic() >= self.deadline:
            self.exhausted = True
        return not self.exhausted


class ActivityGate:
    """The allow-list, the operations and the no-link rule for one reader."""

    def __init__(
        self,
        root: str | Path,
        *,
        files: Iterable[str] = (),
        trees: Iterable[str] = (),
        operations: Iterable[str],
        max_recorded: int = MAX_RECORDED,
    ) -> None:
        self.root = Path(root)
        self.files = tuple(files)
        self.trees = tuple(trees)
        self.operations = frozenset(operations)
        unknown = self.operations - OPERATIONS
        if unknown:
            raise ValueError(f"unknown operations: {sorted(unknown)}")
        self.admitted = AccessRecord(max_recorded)
        self.refused = AccessRecord(max_recorded)

    # ------------------------------------------------------------- the record

    def start_scan(self) -> None:
        """Begin a scan's record. What is kept is what the last scan touched."""
        self.admitted.clear()
        self.refused.clear()

    # --------------------------------------------------------- the operations

    def root_exists(self) -> bool:
        """Whether the data root is where the operator said it would be.

        The root may itself be a link -- a bind mount, a home directory laid
        out how its owner likes. It is configuration, not input.
        """
        try:
            return self.root.exists()
        except OSError:  # pragma: no cover - Path.exists already swallows these
            return False

    def stat(self, path: str | Path) -> os.stat_result:
        """Admit ``path``, and answer with the metadata the check already read.

        `lstat`, so the answer describes the file itself. A link never gets
        this far: it is not a regular file, and the check does not follow it.
        """
        return self._admit(Path(path), STAT)

    @contextmanager
    def open_bytes(self, path: str | Path) -> Iterator[BinaryIO]:
        """Admit ``path`` and open it for reading, binary, without following.

        `O_NOFOLLOW` is the check that survives the gap between admitting the
        path and opening it: a file swapped for a link in between fails the
        `open` rather than being read through. `O_NONBLOCK` and the `fstat`
        cover the other swap -- a fifo, whose `open` would otherwise block this
        reader's thread until somebody wrote to it.
        """
        path = Path(path)
        self._admit(path, READ)
        flags = os.O_RDONLY
        for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC"):
            flags |= getattr(os, name, 0)
        try:
            fd = os.open(path, flags)
        except OSError:
            raise self._refuse(path, "could not be opened without following a link")
        try:
            if not stat_module.S_ISREG(os.fstat(fd).st_mode):
                raise self._refuse(path, "was not a regular file when opened")
            handle = os.fdopen(fd, "rb")
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        with handle:
            yield handle

    def walk(
        self,
        tree: str,
        budget: WalkBudget,
        *,
        suffix: str | None = None,
    ) -> Iterator[Path]:
        """Yield candidate regular files from one allow-listed subtree.

        Candidates: what is yielded here is still admitted again by `stat()` or
        `open_bytes()` before anything is read from it, so the no-link rule is
        checked at the moment of use and not only at the moment of discovery.

        The walk itself follows nothing. A symlinked subtree root, a symlinked
        directory inside it and a symlinked file are each skipped without being
        resolved -- which is what stops the walk stepping outside the tree, and
        stops "did this scan take longer / find a timestamp" answering whether
        a guessed path exists.
        """
        if tree not in self.trees:
            raise self._refuse(self.root / tree, "outside the allow-list")
        root = self.root / tree
        try:
            st = os.lstat(root)
        except OSError:
            return
        if not stat_module.S_ISDIR(st.st_mode):
            self.refused.add("not a directory reached without a link", tree)
            return
        self.admitted.add(WALK, tree)

        stack = [root]
        while stack:
            try:
                scan = os.scandir(stack.pop())
            except OSError:
                continue
            with scan:
                for entry in scan:
                    if not budget.spend():
                        return
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            self.admitted.add(WALK, self._relative(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            self.refused.add(
                                "not a regular file reached without a link",
                                self._relative(entry.path),
                            )
                            continue
                    except OSError:
                        continue
                    if suffix is not None and not entry.name.endswith(suffix):
                        continue
                    yield Path(entry.path)

    # -------------------------------------------------------- the admission

    def _admit(self, path: Path, operation: str) -> os.stat_result:
        """The single check. Every filesystem access a reader makes is here.

        In order: the reader was granted this operation; the path is lexically
        inside an allow-listed part of this reader's own root; every directory
        between the root and it is a directory rather than a link; and the path
        itself is a regular file rather than a link to one.
        """
        if operation not in self.operations:
            raise self._refuse(path, "operation not granted to this reader")
        parts = self._allow_listed_parts(path)
        if parts is None:
            raise self._refuse(path, "outside the allow-list")

        walked = self.root
        for name in parts[:-1]:
            walked = walked / name
            if not stat_module.S_ISDIR(self._lstat(walked).st_mode):
                raise self._refuse(walked, "not a directory reached without a link")
            self.admitted.add(WALK, self._relative(walked))

        st = self._lstat(path)
        if not stat_module.S_ISREG(st.st_mode):
            raise self._refuse(path, "not a regular file reached without a link")
        self.admitted.add(operation, self._relative(path))
        return st

    def _allow_listed_parts(self, path: Path) -> tuple[str, ...] | None:
        """The path's parts below the root, when the allow-list covers it.

        Lexical: `relative_to` resolves nothing, so a `..` cannot be walked
        back out of the root and then in again somewhere else.
        """
        try:
            parts = path.relative_to(self.root).parts
        except ValueError:
            return None
        if not parts or os.pardir in parts:
            return None
        if len(parts) == 1:
            return parts if parts[0] in self.files else None
        return parts if parts[0] in self.trees else None

    def _lstat(self, path: Path) -> os.stat_result:
        try:
            return os.lstat(path)
        except OSError:
            raise self._refuse(path, "could not be examined")

    def _refuse(self, path: str | Path, reason: str) -> PathRefused:
        self.refused.add(reason, self._relative(path))
        return PathRefused(reason)

    def _relative(self, path: str | Path) -> str:
        """The path as the record names it: relative to the root where it can be."""
        path = Path(path)
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()
