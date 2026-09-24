from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .activity_gate import READ, STAT, ActivityGate, PathRefused, WalkBudget
from .budget import bounded_lines, env_float, env_int


TRANSCRIPT_DIR = "projects"
TRANSCRIPT_SUFFIX = ".jsonl"


@dataclass
class ClaudeActivitySnapshot:
    last_activity: datetime | None
    data_root_exists: bool


class ClaudeActivityReader:
    """Reports Claude Code's local last activity from transcript timestamps.

    What it is allowed to reach is not this docstring: it is ``self.gate``, an
    ``ActivityGate`` over the ``projects`` subtree of this reader's own data
    root, granted ``STAT`` (for the per-file cache) and ``READ`` (for the
    transcript records), and this reader reaches the filesystem through nothing
    else. So .credentials.json is not readable here because it is not on the
    allow-list, and a ``projects/x.jsonl`` that is a link -- to that credential
    file, to a path outside the root, to an endless device -- is refused rather
    than followed. ``tests/test_activity_readers.py`` asserts that on the
    gate's own record of what it admitted, which is what makes a regression
    visible; the timestamp alone never was.

    Everything it reads is written by someone else -- anything that can write
    under ``~/.claude/projects`` -- so the scan is budgeted three ways:
    ``max_line_bytes`` per record, ``max_file_bytes`` per file, and
    ``scan_deadline_seconds`` (with ``max_files``) across the whole walk.
    Exceeding a budget truncates the scan and reports the timestamps already
    found; it never fails the section and never grows without bound.

    There is no TTL here any more. The refresher in ``app.main`` owns how often
    this runs; the per-file cache below is a different thing and stays, because
    it keys on (mtime, size) and is what keeps a steady-state scan cheap.
    """

    def __init__(
        self,
        data_dir: str | Path,
        max_line_bytes: int = 1024 * 1024,
        max_file_bytes: int = 16 * 1024 * 1024,
        scan_deadline_seconds: float = 5.0,
        max_files: int = 20000,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.max_line_bytes = max_line_bytes
        self.max_file_bytes = max_file_bytes
        self.scan_deadline_seconds = scan_deadline_seconds
        self.max_files = max_files
        self.gate = ActivityGate(
            self.data_dir,
            trees=(TRANSCRIPT_DIR,),
            operations={STAT, READ},
        )
        self._file_cache: dict[Path, tuple[float, int, datetime | None]] = {}

    def snapshot(self) -> ClaudeActivitySnapshot:
        """One bounded scan. Called only from this source's refresher."""
        return self._scan()

    def _scan(self) -> ClaudeActivitySnapshot:
        self.gate.start_scan()
        deadline = time.monotonic() + self.scan_deadline_seconds
        data_root_exists = self.gate.root_exists()
        paths = self._iter_transcripts(deadline) if data_root_exists else []
        for stale in set(self._file_cache.keys()) - set(paths):
            self._file_cache.pop(stale, None)

        last_activity: datetime | None = None
        for path in paths:
            dt = self._last_activity_for(path)
            if dt is not None and (last_activity is None or dt > last_activity):
                last_activity = dt
            if time.monotonic() >= deadline:
                # Out of time. The per-file (mtime, size) cache makes the files
                # already done nearly free next time, so a scan cut short here
                # gets further on the next pass rather than losing the tail for
                # good. The max_files cut below is not like that -- it stops at
                # the same prefix every time -- which is why its default is set
                # far above any real tree.
                break
        return ClaudeActivitySnapshot(
            last_activity=last_activity,
            data_root_exists=data_root_exists,
        )

    def _iter_transcripts(self, deadline: float) -> list[Path]:
        # Charged per entry touched, like Codex's walk: the gate uses scandir
        # rather than rglob, because rglob follows a symlinked directory out of
        # the tree, and a walk that skips links has to look at each entry to
        # know it is skipping one.
        budget = WalkBudget(max_entries=self.max_files, deadline=deadline)
        return list(
            self.gate.walk(TRANSCRIPT_DIR, budget, suffix=TRANSCRIPT_SUFFIX)
        )

    def _last_activity_for(self, path: Path) -> datetime | None:
        try:
            st = self.gate.stat(path)
        except PathRefused:
            return None

        cached = self._file_cache.get(path)
        if cached is not None and cached[0] == st.st_mtime and cached[1] == st.st_size:
            return cached[2]

        last_activity = self._parse_last_activity(path)
        self._file_cache[path] = (st.st_mtime, st.st_size, last_activity)
        return last_activity

    def _parse_last_activity(self, path: Path) -> datetime | None:
        last_activity: datetime | None = None
        try:
            # Binary, so that the record boundary is found before anything is
            # decoded: a text-mode iterator would build the whole unterminated
            # record first, which is the case this is here to stop.
            with self.gate.open_bytes(path) as f:
                for record in bounded_lines(
                    f,
                    max_line_bytes=self.max_line_bytes,
                    max_file_bytes=self.max_file_bytes,
                ):
                    line = record.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    dt = _timestamp_from_obj(obj)
                    if dt is not None and (last_activity is None or dt > last_activity):
                        last_activity = dt
        except (OSError, PathRefused):
            return None
        return last_activity


def _timestamp_from_obj(obj: object) -> datetime | None:
    if not isinstance(obj, dict):
        return None
    value = obj.get("timestamp")
    if not isinstance(value, str) or not value:
        return None
    return _parse_ts(value)


def _parse_ts(value: str) -> datetime | None:
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def reader_from_env() -> ClaudeActivityReader:
    data_dir = os.environ.get("CLAUDE_DATA_DIR", "/data/claude")
    return ClaudeActivityReader(
        data_dir,
        max_line_bytes=env_int("ACTIVITY_MAX_LINE_BYTES", 1024 * 1024),
        max_file_bytes=env_int("ACTIVITY_MAX_FILE_BYTES", 16 * 1024 * 1024),
        scan_deadline_seconds=env_float("ACTIVITY_SCAN_DEADLINE_SECONDS", 5.0),
        max_files=env_int("ACTIVITY_MAX_FILES", 20000),
    )
