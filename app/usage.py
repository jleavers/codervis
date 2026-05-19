from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock


@dataclass(frozen=True)
class Record:
    ts: datetime
    tokens: int
    model: str


@dataclass
class WindowStat:
    name: str
    label: str
    used: int
    limit: int
    window_seconds: int
    resets_at: datetime | None

    @property
    def percent(self) -> float:
        if self.limit <= 0:
            return 0.0
        return min(100.0, (self.used / self.limit) * 100.0)


@dataclass
class Snapshot:
    five_hour: WindowStat
    weekly: WindowStat
    total_records: int
    last_activity: datetime | None
    data_root_exists: bool


class UsageReader:
    """Reads Claude Code transcripts and aggregates token usage in rolling windows.

    Caches per-file parse results keyed by (path, mtime, size) to avoid reparsing
    quiescent files on every refresh.
    """

    def __init__(
        self,
        data_dir: str | Path,
        five_hour_limit: int,
        weekly_limit: int,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.five_hour_limit = five_hour_limit
        self.weekly_limit = weekly_limit
        self._cache: dict[Path, tuple[float, int, list[Record]]] = {}
        self._lock = Lock()

    def _iter_transcripts(self) -> list[Path]:
        projects = self.data_dir / "projects"
        if not projects.exists():
            return []
        return list(projects.glob("*/*.jsonl"))

    def _parse_file(self, path: Path) -> list[Record]:
        records: list[Record] = []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rec = _extract_record(obj)
                    if rec is not None:
                        records.append(rec)
        except OSError:
            return []
        return records

    def _records_for(self, path: Path) -> list[Record]:
        try:
            st = path.stat()
        except OSError:
            return []
        key = (st.st_mtime, st.st_size)
        cached = self._cache.get(path)
        if cached is not None and cached[0] == key[0] and cached[1] == key[1]:
            return cached[2]
        records = self._parse_file(path)
        self._cache[path] = (key[0], key[1], records)
        return records

    def collect(self) -> list[Record]:
        with self._lock:
            paths = self._iter_transcripts()
            # evict cache entries for files that no longer exist
            for stale in set(self._cache.keys()) - set(paths):
                self._cache.pop(stale, None)
            all_records: list[Record] = []
            for p in paths:
                all_records.extend(self._records_for(p))
        return all_records

    def snapshot(self, now: datetime | None = None) -> Snapshot:
        now = now or datetime.now(timezone.utc)
        records = self.collect()

        five_hour_cutoff = now - timedelta(hours=5)
        weekly_cutoff = now - timedelta(days=7)

        five_hour_tokens = sum(r.tokens for r in records if r.ts >= five_hour_cutoff)
        weekly_tokens = sum(r.tokens for r in records if r.ts >= weekly_cutoff)
        last_activity = max((r.ts for r in records), default=None)

        # Reset time = oldest record in the window + window length.
        # Falls back to "now + window" when no records exist.
        oldest_in_5h = min(
            (r.ts for r in records if r.ts >= five_hour_cutoff),
            default=None,
        )
        oldest_in_week = min(
            (r.ts for r in records if r.ts >= weekly_cutoff),
            default=None,
        )
        resets_5h = (oldest_in_5h + timedelta(hours=5)) if oldest_in_5h else None
        resets_week = (oldest_in_week + timedelta(days=7)) if oldest_in_week else None

        return Snapshot(
            five_hour=WindowStat(
                name="five_hour",
                label="5-Hour Window",
                used=five_hour_tokens,
                limit=self.five_hour_limit,
                window_seconds=5 * 3600,
                resets_at=resets_5h,
            ),
            weekly=WindowStat(
                name="weekly",
                label="Weekly Window",
                used=weekly_tokens,
                limit=self.weekly_limit,
                window_seconds=7 * 24 * 3600,
                resets_at=resets_week,
            ),
            total_records=len(records),
            last_activity=last_activity,
            data_root_exists=self.data_dir.exists(),
        )


def _extract_record(obj: dict) -> Record | None:
    """Pull (timestamp, tokens, model) from a transcript line if it carries usage."""
    if not isinstance(obj, dict):
        return None
    if obj.get("type") != "assistant":
        return None
    message = obj.get("message") or {}
    if not isinstance(message, dict):
        return None
    usage = message.get("usage") or {}
    if not isinstance(usage, dict):
        return None
    if not usage:
        return None
    parts = [
        _usage_int(usage, "input_tokens"),
        _usage_int(usage, "output_tokens"),
        _usage_int(usage, "cache_creation_input_tokens"),
        _usage_int(usage, "cache_read_input_tokens"),
    ]
    if any(v is None for v in parts):
        return None
    tokens = sum(v for v in parts if v is not None)
    if tokens <= 0:
        return None
    ts_raw = obj.get("timestamp")
    if not isinstance(ts_raw, str) or not ts_raw:
        return None
    ts = _parse_ts(ts_raw)
    if ts is None:
        return None
    model = str(message.get("model") or "unknown")
    return Record(ts=ts, tokens=tokens, model=model)


def _usage_int(usage: dict, key: str) -> int | None:
    try:
        return int(usage.get(key) or 0)
    except (TypeError, ValueError):
        return None


def _parse_ts(value: str) -> datetime | None:
    try:
        # transcripts store ISO8601 with trailing Z
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def reader_from_env() -> UsageReader:
    data_dir = os.environ.get("CLAUDE_DATA_DIR", "/data/claude")
    five_hour_limit = int(os.environ.get("FIVE_HOUR_TOKEN_LIMIT", "500000"))
    weekly_limit = int(os.environ.get("WEEKLY_TOKEN_LIMIT", "3000000"))
    return UsageReader(data_dir, five_hour_limit, weekly_limit)
