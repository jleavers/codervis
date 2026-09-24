from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .activity_gate import STAT, ActivityGate, PathRefused, WalkBudget
from .budget import env_float, env_int


ACTIVITY_FILES = ("history.jsonl", "session_index.jsonl")
ACTIVITY_DIRS = ("sessions", "archived_sessions")


@dataclass
class CodexActivitySnapshot:
    last_activity: datetime | None
    data_root_exists: bool


class CodexActivityReader:
    """Derives Codex's local last-activity timestamp from safe file metadata.

    What "safe" means is not this docstring: it is ``self.gate``, an
    ``ActivityGate`` constructed with ``ACTIVITY_FILES``, ``ACTIVITY_DIRS`` and
    the ``STAT`` operation alone, and this reader reaches the filesystem
    through nothing else. So auth.json is not stat-able here because it is not
    on the allow-list, session *contents* are not readable here because this
    reader was never granted ``READ``, and a link planted under the data root
    -- ``sessions/x.jsonl`` pointing at the Claude credential file, or an
    ``archived_sessions`` that is itself a link -- is refused rather than
    followed. ``tests/test_activity_readers.py`` asserts that on the gate's own
    record of what it admitted, which is what makes a regression visible.

    No content is read, so there is no byte budget to set here; what is
    unbounded is the *walk*, over a tree someone else fills. ``max_files`` and
    ``scan_deadline_seconds`` bound it through one ``WalkBudget`` shared by
    both subtrees, and a truncated walk reports the timestamps already found.

    There is no TTL here any more: the refresher in ``app.main`` owns how often
    this runs, and caches a failure as well as a success.
    """

    def __init__(
        self,
        data_dir: str | Path,
        scan_deadline_seconds: float = 5.0,
        max_files: int = 20000,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.scan_deadline_seconds = scan_deadline_seconds
        self.max_files = max_files
        self.gate = ActivityGate(
            self.data_dir,
            files=ACTIVITY_FILES,
            trees=ACTIVITY_DIRS,
            operations={STAT},
        )

    def snapshot(self) -> CodexActivitySnapshot:
        """One bounded scan. Called only from this source's refresher."""
        return self._scan()

    def _scan(self) -> CodexActivitySnapshot:
        self.gate.start_scan()
        budget = WalkBudget(
            max_entries=self.max_files,
            deadline=time.monotonic() + self.scan_deadline_seconds,
        )
        data_root_exists = self.gate.root_exists()
        last_activity: datetime | None = None
        if data_root_exists:
            for st in self._activity_stats(budget):
                dt = _mtime(st)
                if dt is not None and (last_activity is None or dt > last_activity):
                    last_activity = dt
        return CodexActivitySnapshot(
            last_activity=last_activity,
            data_root_exists=data_root_exists,
        )

    def _activity_stats(self, budget: WalkBudget) -> Iterator[os.stat_result]:
        """Metadata for every path the gate admits, and for nothing else.

        The allow-list is read off the gate rather than restated here: there is
        one place that decides what this reader may reach.
        """
        for name in self.gate.files:
            try:
                yield self.gate.stat(self.gate.root / name)
            except PathRefused:
                continue
        for tree in self.gate.trees:
            for path in self.gate.walk(tree, budget):
                try:
                    yield self.gate.stat(path)
                except PathRefused:
                    continue


def _mtime(st: os.stat_result) -> datetime | None:
    try:
        return datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def reader_from_env() -> CodexActivityReader:
    data_dir = os.environ.get("CODEX_DATA_DIR", "/data/codex")
    return CodexActivityReader(
        data_dir,
        scan_deadline_seconds=env_float("ACTIVITY_SCAN_DEADLINE_SECONDS", 5.0),
        max_files=env_int("ACTIVITY_MAX_FILES", 20000),
    )
