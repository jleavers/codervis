"""The budget on payload-feeding I/O, and the refresher that owns it.

Issue #16's invariant: all I/O that feeds the payload runs only in a per-source
refresher, off the event loop, on a fixed cadence, with a total deadline and a
byte cap per read, caching its outcome -- failure included -- for the interval.
Request handlers read only the last published snapshot.

Each test here names the property it pins, because the point of the change is
what *cannot* happen: request volume cannot multiply upstream calls, a slow
sender cannot stall another route, and an oversized or unterminated input
cannot grow memory without bound.
"""

from __future__ import annotations

import asyncio
import io
import json
import threading
import time
import tracemalloc
import urllib.error
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import codex_quota, main, quota
from app.budget import BudgetExceeded, bounded_lines, env_float, env_int, read_capped
from app.claude_activity import ClaudeActivityReader
from app.codex_activity import CodexActivityReader
from app.refresh import (
    SourceRefresher,
    SourceSnapshot,
    SourceStale,
    wait_for_first_publish,
)


# --------------------------------------------------------------- byte cap


class _Body:
    """A response that hands out a fixed body in sized chunks, as http.client does."""

    status = 200

    def __init__(self, payload: bytes) -> None:
        self._remaining = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._remaining)
        chunk, self._remaining = self._remaining[:size], self._remaining[size:]
        return chunk


class _Endless:
    """A sender that never stops and never exceeds the per-operation timeout."""

    status = 200

    def __init__(self, seconds_per_chunk: float = 0.01, chunk: bytes = b"a" * 4096) -> None:
        self.seconds_per_chunk = seconds_per_chunk
        self.chunk = chunk
        self.served = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        time.sleep(self.seconds_per_chunk)
        self.served += len(self.chunk)
        return self.chunk


def test_read_capped_accepts_a_body_of_exactly_the_cap() -> None:
    body = _Body(b"x" * 1000)

    assert read_capped(body, max_bytes=1000, deadline=time.monotonic() + 10) == b"x" * 1000


def test_read_capped_refuses_the_first_byte_past_the_cap() -> None:
    body = _Body(b"x" * 1001)

    with pytest.raises(BudgetExceeded, match="exceeded 1000 bytes"):
        read_capped(body, max_bytes=1000, deadline=time.monotonic() + 10)


def test_read_capped_stops_a_sender_that_never_stops() -> None:
    endless = _Endless()

    with pytest.raises(BudgetExceeded):
        read_capped(endless, max_bytes=1 << 30, deadline=time.monotonic() + 0.2)

    # Bounded by the deadline, not by the cap: without one this never returns.
    assert endless.served < (1 << 30)


def test_read_capped_prefers_read1_so_the_deadline_is_checked_between_recvs() -> None:
    """`read(n)` blocks until n bytes; `read1(n)` returns what has arrived.

    Preferring read1 is what keeps the deadline from being noticed a whole
    chunk late when a sender trickles.
    """
    asked: list[str] = []

    class Both:
        status = 200

        def read(self, size=-1):
            asked.append("read")
            return b""

        def read1(self, size=-1):
            asked.append("read1")
            return b""

    read_capped(Both(), max_bytes=100, deadline=time.monotonic() + 10)

    assert asked == ["read1"]


# ------------------------------------------------------- quota client budgets


def _claude_credentials(tmp_path) -> None:
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "fake-access-token"}}),
        encoding="utf-8",
    )


def _codex_credentials(tmp_path) -> None:
    (tmp_path / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "fake-access-token"}}),
        encoding="utf-8",
    )


def test_claude_client_caps_an_oversized_body(tmp_path, monkeypatch) -> None:
    _claude_credentials(tmp_path)
    monkeypatch.setattr(
        quota.urllib.request,
        "urlopen",
        lambda req, timeout: _Body(b"{" + b" " * 5000 + b"}"),
    )
    client = quota.LiveQuotaClient(tmp_path, host="https://example.test", max_response_bytes=1024)

    with pytest.raises(quota.LiveQuotaError, match="upstream read budget"):
        client.get()


def test_claude_client_cuts_off_a_trickling_sender(tmp_path, monkeypatch) -> None:
    """The finding: every socket operation stays inside the timeout, forever."""
    _claude_credentials(tmp_path)
    monkeypatch.setattr(
        quota.urllib.request, "urlopen", lambda req, timeout: _Endless(0.01)
    )
    client = quota.LiveQuotaClient(
        tmp_path,
        host="https://example.test",
        timeout_seconds=8.0,
        total_deadline_seconds=0.2,
        max_response_bytes=1 << 30,
    )

    started = time.monotonic()
    with pytest.raises(quota.LiveQuotaError, match="upstream read budget"):
        client.get()
    held = time.monotonic() - started

    assert held < 4.0, "the total deadline, not the 8 s per-operation timeout, bounds this"


def test_codex_client_caps_an_oversized_body(tmp_path, monkeypatch) -> None:
    _codex_credentials(tmp_path)
    monkeypatch.setattr(
        codex_quota.urllib.request,
        "urlopen",
        lambda req, timeout: _Body(b"{" + b" " * 5000 + b"}"),
    )
    client = codex_quota.CodexLiveQuotaClient(
        tmp_path, host="https://example.test", max_response_bytes=1024
    )

    with pytest.raises(codex_quota.CodexLiveQuotaError, match="upstream read budget"):
        client.get()


def test_codex_deadline_spans_both_candidate_paths(tmp_path, monkeypatch) -> None:
    """Falling through to the alternate path must not add a second timeout."""
    _codex_credentials(tmp_path)
    timeouts: list[float] = []

    def fake_urlopen(req, timeout):
        timeouts.append(timeout)
        time.sleep(0.15)
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", hdrs=None, fp=None)

    monkeypatch.setattr(codex_quota.urllib.request, "urlopen", fake_urlopen)
    client = codex_quota.CodexLiveQuotaClient(
        tmp_path,
        host="https://example.test",
        timeout_seconds=8.0,
        total_deadline_seconds=0.2,
    )

    with pytest.raises(codex_quota.CodexLiveQuotaError):
        client.get()

    assert len(timeouts) >= 1
    # The second attempt gets what is left of the one deadline, not a fresh 8 s.
    assert all(t <= 0.2 for t in timeouts), timeouts


def test_quota_clients_no_longer_cache_so_the_refresher_owns_the_cadence(
    tmp_path, monkeypatch
) -> None:
    _claude_credentials(tmp_path)
    calls: list[object] = []

    def fake_urlopen(req, timeout):
        calls.append(req)
        return _Body(
            json.dumps(
                {"five_hour": {"utilization": 1}, "seven_day": {"utilization": 2}}
            ).encode("utf-8")
        )

    monkeypatch.setattr(quota.urllib.request, "urlopen", fake_urlopen)
    client = quota.LiveQuotaClient(tmp_path, host="https://example.test")

    client.get()
    client.get()

    assert len(calls) == 2


# ------------------------------------------------------------- bounded lines


def test_bounded_lines_drops_an_overlong_record_and_keeps_the_rest() -> None:
    stream = io.BytesIO(b"a\n" + b"x" * 5000 + b"\nb\n")

    records = list(bounded_lines(stream, max_line_bytes=100, max_file_bytes=1 << 20))

    assert records == [b"a", b"b"]


def test_bounded_lines_stops_at_the_file_cap_when_there_is_no_newline() -> None:
    """A file with no newline and no end is the MemoryError case."""
    class Endless(io.RawIOBase):
        def read(self, size=-1):
            return b"x" * (4096 if size is None or size < 0 else min(size, 4096))

    records = list(
        bounded_lines(Endless(), max_line_bytes=1024, max_file_bytes=64 * 1024)
    )

    assert records == []


def test_bounded_lines_yields_a_final_record_without_a_trailing_newline() -> None:
    stream = io.BytesIO(b"a\nb")

    assert list(bounded_lines(stream, max_line_bytes=100, max_file_bytes=1 << 20)) == [
        b"a",
        b"b",
    ]


# -------------------------------------------------------- activity budgets


def test_claude_reader_does_not_build_an_unbounded_line(tmp_path) -> None:
    """hostile-input-8: one endless record used to grow until MemoryError."""
    root = tmp_path / "claude"
    project = root / "projects" / "demo"
    project.mkdir(parents=True)
    hostile = project / "session.jsonl"
    with hostile.open("wb") as f:
        f.write(b"x" * (8 * 1024 * 1024))  # 8 MiB, one record, no newline

    reader = ClaudeActivityReader(
        root, max_line_bytes=64 * 1024, max_file_bytes=32 * 1024 * 1024
    )
    tracemalloc.start()
    try:
        snapshot = reader.snapshot()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert snapshot.last_activity is None
    assert peak < 4 * 1024 * 1024, f"held {peak} bytes for an 8 MiB record"


def test_claude_reader_reads_past_a_hostile_record_in_the_same_file(tmp_path) -> None:
    root = tmp_path / "claude"
    project = root / "projects" / "demo"
    project.mkdir(parents=True)
    (project / "session.jsonl").write_bytes(
        b'{"timestamp":"2026-05-20T08:00:00Z"}\n'
        + b"x" * 200_000
        + b'\n{"timestamp":"2026-05-20T11:30:00Z"}\n'
    )

    snapshot = ClaudeActivityReader(root, max_line_bytes=1024).snapshot()

    assert snapshot.last_activity == datetime(2026, 5, 20, 11, 30, tzinfo=timezone.utc)


def test_claude_reader_caps_what_it_reads_from_one_file(tmp_path) -> None:
    root = tmp_path / "claude"
    project = root / "projects" / "demo"
    project.mkdir(parents=True)
    early = b'{"timestamp":"2026-05-20T08:00:00Z"}\n'
    (project / "session.jsonl").write_bytes(
        early + b'{"timestamp":"2026-05-20T09:00:00Z"}\n' * 10_000
    )

    snapshot = ClaudeActivityReader(root, max_file_bytes=len(early)).snapshot()

    # Only the first record was in budget; the rest was never read.
    assert snapshot.last_activity == datetime(2026, 5, 20, 8, 0, tzinfo=timezone.utc)


def test_activity_readers_report_what_they_found_when_the_scan_runs_out_of_time(
    tmp_path,
) -> None:
    claude_root = tmp_path / "claude"
    (claude_root / "projects" / "demo").mkdir(parents=True)
    (claude_root / "projects" / "demo" / "a.jsonl").write_text(
        '{"timestamp":"2026-05-20T08:00:00Z"}\n', encoding="utf-8"
    )
    codex_root = tmp_path / "codex"
    (codex_root / "sessions").mkdir(parents=True)
    (codex_root / "sessions" / "a.jsonl").write_text("x", encoding="utf-8")

    # A deadline already spent: the scan must still answer, not raise or hang.
    claude = ClaudeActivityReader(claude_root, scan_deadline_seconds=0.0).snapshot()
    codex = CodexActivityReader(codex_root, scan_deadline_seconds=0.0).snapshot()

    assert claude.data_root_exists is True
    assert codex.data_root_exists is True


# ----------------------------------------------------------------- refresher


def test_refresher_publishes_a_failure_as_it_publishes_a_success() -> None:
    """exposure-3: only successes were memoized, so failures were refetched."""
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise quota.LiveQuotaError("provider is failing")

    refresher = SourceRefresher("failing", boom, 30.0)
    refresher.refresh_once()

    for _ in range(50):
        snapshot = refresher.snapshot()

    assert calls["n"] == 1, "reading the snapshot must never re-run the fetch"
    assert snapshot.ok is False
    assert snapshot.published is True
    assert isinstance(snapshot.error, quota.LiveQuotaError)


def test_refresher_survives_any_exception_including_memoryerror() -> None:
    """An escape would kill the thread and freeze the source forever."""
    raised: list[type[BaseException]] = [MemoryError, RecursionError, ValueError]

    def boom():
        raise raised.pop(0)("hostile input")

    refresher = SourceRefresher("boom", boom, 0.0)
    for expected in (MemoryError, RecursionError, ValueError):
        snapshot = refresher.refresh_once()
        assert isinstance(snapshot.error, expected)


def test_refresher_is_pending_until_it_has_published() -> None:
    refresher = SourceRefresher("idle", lambda: "value", 30.0)

    assert refresher.snapshot().published is False
    assert refresher.snapshot().ok is False

    refresher.refresh_once()

    assert refresher.snapshot().published is True
    assert refresher.snapshot().value == "value"


def test_refresher_thread_runs_on_its_own_cadence_and_stops() -> None:
    calls = {"n": 0}
    refresher = SourceRefresher("ticking", lambda: calls.__setitem__("n", calls["n"] + 1), 0.01)
    refresher.start()
    try:
        deadline = time.monotonic() + 2.0
        while calls["n"] < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        refresher.stop()
    settled = calls["n"]
    time.sleep(0.1)

    assert settled >= 3, "the refresher ran without anyone asking for a payload"
    assert calls["n"] == settled, "stop() stopped it"


def test_refresher_start_is_idempotent() -> None:
    refresher = SourceRefresher("once", lambda: None, 0.05)
    refresher.start()
    first = refresher._thread
    refresher.start()
    try:
        assert refresher._thread is first
    finally:
        refresher.stop()


def test_wait_for_first_publish_gives_up_at_its_timeout() -> None:
    never = SourceRefresher("never", lambda: None, 30.0)

    started = time.monotonic()
    published = wait_for_first_publish([never], 0.1)

    assert published is False
    assert time.monotonic() - started < 2.0


# ------------------------------------------------------- the routes' budget


class _CountingSource:
    """Stands in for a quota client, counting the calls a request could cause."""

    credentials_path = None

    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    def get(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result

    snapshot = get


@pytest.fixture
def stub_sources(monkeypatch):
    """Four fresh refreshers over counting stubs, published by the test only."""
    from app.claude_activity import ClaudeActivitySnapshot
    from app.codex_activity import CodexActivitySnapshot

    stubs = {
        "claude_quota": _CountingSource(error=quota.LiveQuotaError("upstream down")),
        "claude_activity": _CountingSource(
            ClaudeActivitySnapshot(last_activity=None, data_root_exists=True)
        ),
        "codex_quota": _CountingSource(
            error=codex_quota.CodexLiveQuotaError("upstream down")
        ),
        "codex_activity": _CountingSource(
            CodexActivitySnapshot(last_activity=None, data_root_exists=True)
        ),
    }
    sources = {
        "_claude_quota_source": SourceRefresher(
            "claude-quota", stubs["claude_quota"].get, 30.0
        ),
        "_claude_activity_source": SourceRefresher(
            "claude-activity", stubs["claude_activity"].snapshot, 5.0
        ),
        "_codex_quota_source": SourceRefresher(
            "codex-quota", stubs["codex_quota"].get, 30.0
        ),
        "_codex_activity_source": SourceRefresher(
            "codex-activity", stubs["codex_activity"].snapshot, 5.0
        ),
    }
    for name, source in sources.items():
        monkeypatch.setattr(main, name, source)
    monkeypatch.setattr(main, "_SOURCES", tuple(sources.values()))
    return stubs


def test_building_the_payload_calls_no_source(stub_sources) -> None:
    """AC1/AC2: the handlers' whole job is to read the last published snapshot."""
    for source in main._SOURCES:
        source.refresh_once()
    before = {name: stub.calls for name, stub in stub_sources.items()}

    for _ in range(25):
        main._build_payload()

    assert {name: stub.calls for name, stub in stub_sources.items()} == before


def test_sse_frames_do_not_multiply_upstream_calls(stub_sources, monkeypatch) -> None:
    """exposure-3: held connections used to re-run the fetch on every tick."""
    monkeypatch.setattr(main, "REFRESH_SECONDS", 0)
    for source in main._SOURCES:
        source.refresh_once()
    before = stub_sources["claude_quota"].calls

    class _Connected:
        async def is_disconnected(self) -> bool:
            return False

    async def drive() -> int:
        response = await main.stream(_Connected())
        frames = 0
        async for _ in response.body_iterator:
            frames += 1
            if frames >= 20:
                break
        return frames

    assert asyncio.run(drive()) == 20
    assert stub_sources["claude_quota"].calls == before


def test_a_wedged_source_does_not_stall_the_event_loop(monkeypatch) -> None:
    """The remaining half of exposure-3: the build used to block every route."""
    release = threading.Event()

    def wedged():
        release.wait(5.0)
        raise quota.LiveQuotaError("still wedged")

    wedged_source = SourceRefresher("wedged", wedged, 30.0)
    monkeypatch.setattr(main, "_claude_quota_source", wedged_source)

    async def drive() -> tuple[float, dict]:
        worker = asyncio.get_running_loop().run_in_executor(None, wedged_source.refresh_once)
        await asyncio.sleep(0.05)  # let the fetch wedge
        started = time.monotonic()
        payload = main._build_payload()
        elapsed = time.monotonic() - started
        release.set()
        await worker
        return elapsed, payload

    elapsed, payload = asyncio.run(drive())

    assert elapsed < 0.5, "a payload was produced while the source was still wedged"
    assert payload["claude"]["source"] == "unavailable"


def test_a_source_that_has_not_published_degrades_rather_than_guessing(
    stub_sources,
) -> None:
    payload = main._build_payload()

    assert payload["claude"]["source"] == "unavailable"
    assert payload["claude"]["source_error"] == "waiting for the first refresh"
    assert payload["claude"]["data_root_exists"] is False
    for window in payload["claude"]["windows"]:
        assert window["percent"] is None


def test_an_unexpected_exception_is_named_by_type_and_never_quoted(
    stub_sources,
) -> None:
    """The refresher has to catch everything; nothing it catches may be quoted.

    An exception raised while an upstream request is being built can carry the
    bearer token in its message, and `source_error` is served unauthenticated.
    """
    stub_sources["claude_quota"].error = RuntimeError("Bearer sk-ant-secret-value")
    main._claude_quota_source.refresh_once()

    payload = main._build_payload()

    assert payload["claude"]["source"] == "unavailable"
    assert payload["claude"]["source_error"] == "unexpected RuntimeError while refreshing"
    assert "sk-ant" not in json.dumps(payload)


def test_a_failed_activity_scan_does_not_take_the_quota_section_with_it(
    stub_sources,
) -> None:
    stub_sources["claude_activity"].error = OSError("transcript tree is gone")
    stub_sources["claude_quota"].error = None
    stub_sources["claude_quota"].result = _live_snapshot()
    for source in main._SOURCES:
        source.refresh_once()

    payload = main._build_payload()

    assert payload["claude"]["source"] == "live"
    assert payload["claude"]["last_activity"] is None
    assert payload["claude"]["data_root_exists"] is False


def _live_snapshot():
    from types import SimpleNamespace

    window = SimpleNamespace(percent=10.0, resets_at=None)
    return SimpleNamespace(
        five_hour=window,
        seven_day=window,
        seven_day_fable=None,
        subscription_type="max",
    )


# ------------------------------------------------------------------- env


def test_env_budgets_fall_back_to_the_shipped_value_when_unusable(monkeypatch) -> None:
    """A typo in an operator's .env must not stop the dashboard from starting."""
    for bad in ("", "   ", "not-a-number", "0", "-5"):
        monkeypatch.setenv("CODERVIS_TEST_BUDGET", bad)
        assert env_float("CODERVIS_TEST_BUDGET", 7.5) == 7.5
        assert env_int("CODERVIS_TEST_BUDGET", 11) == 11

    monkeypatch.setenv("CODERVIS_TEST_BUDGET", "3.5")
    assert env_float("CODERVIS_TEST_BUDGET", 7.5) == 3.5


def test_env_budget_honours_the_old_name_for_the_same_number(monkeypatch) -> None:
    monkeypatch.delenv("CODERVIS_TEST_NEW", raising=False)
    monkeypatch.setenv("CODERVIS_TEST_OLD", "12")

    assert env_float("CODERVIS_TEST_NEW", 30.0, fallback="CODERVIS_TEST_OLD") == 12.0

    monkeypatch.setenv("CODERVIS_TEST_NEW", "9")
    assert env_float("CODERVIS_TEST_NEW", 30.0, fallback="CODERVIS_TEST_OLD") == 9.0


# ------------------------------------------------ lifespan and thread safety


def test_lifespan_starts_and_stops_every_source(monkeypatch) -> None:
    """The refreshers are the change; nothing else runs them in production."""
    from fastapi.testclient import TestClient

    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        raise quota.LiveQuotaError("down")

    sources = tuple(SourceRefresher(f"s{i}", counted, 30.0) for i in range(4))
    monkeypatch.setattr(main, "_SOURCES", sources)
    monkeypatch.setattr(main, "_claude_quota_source", sources[0])
    monkeypatch.setattr(main, "_claude_activity_source", sources[1])
    monkeypatch.setattr(main, "_codex_quota_source", sources[2])
    monkeypatch.setattr(main, "_codex_activity_source", sources[3])
    monkeypatch.setattr(main, "STARTUP_REFRESH_WAIT_SECONDS", 2.0)

    def live_threads() -> int:
        return sum(1 for t in threading.enumerate() if t.name.startswith("refresh:s"))

    # `http://127.0.0.1:8765`, not `TestClient`'s own `testserver`: the app now serves
    # only the hosts the operator named (#15), and this test is about the lifespan.
    with TestClient(main.app, base_url="http://127.0.0.1:8765") as client:
        assert live_threads() == 4
        # The startup wait means the first request is served real outcomes.
        assert all(s.snapshot().published for s in sources)
        response = client.get("/api/usage")
        assert response.status_code == 200
        assert response.json()["claude"]["source"] == "unavailable"

    deadline = time.monotonic() + 5.0
    while live_threads() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert live_threads() == 0, "the lifespan must stop what it started"
    assert calls["n"] >= 4


def test_start_after_a_stop_that_could_not_join_does_not_double_up() -> None:
    """Two live threads would double the source's call volume.

    `stop()` cannot kill a thread wedged in a read, so it gives up joining. A
    later `start()` has to notice that thread is still there.
    """
    release = threading.Event()
    running = threading.Event()

    def wedged():
        running.set()
        release.wait(10.0)

    refresher = SourceRefresher("wedged", wedged, 0.01)
    refresher.start()
    try:
        assert running.wait(2.0)
        refresher.stop(timeout=0.05)  # cannot join: the fetch is still wedged

        refresher.start()
        live = [t for t in threading.enumerate() if t.name == "refresh:wedged"]

        assert len(live) == 1, f"{len(live)} threads are fetching this source"
    finally:
        release.set()
        refresher.stop(timeout=2.0)

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not [t for t in threading.enumerate() if t.name == "refresh:wedged"]:
            break
        time.sleep(0.01)
    assert not [t for t in threading.enumerate() if t.name == "refresh:wedged"]


def test_refresher_refuses_a_hot_loop_interval() -> None:
    assert SourceRefresher("x", lambda: None, 0.0).interval_seconds > 0
    assert SourceRefresher("x", lambda: None, -5.0).interval_seconds > 0


# ------------------------------------------------------ credential budgets


@pytest.mark.parametrize(
    ("client_class", "filename", "error", "key"),
    [
        (
            quota.LiveQuotaClient,
            ".credentials.json",
            quota.LiveQuotaError,
            "claudeAiOauth",
        ),
        (
            codex_quota.CodexLiveQuotaClient,
            "auth.json",
            codex_quota.CodexLiveQuotaError,
            "tokens",
        ),
    ],
)
def test_credentials_file_is_capped(tmp_path, client_class, filename, error, key) -> None:
    """The credential files are payload-feeding reads in the same writable tree."""
    (tmp_path / filename).write_text(
        json.dumps({key: {"accessToken": "x" * 20_000}}), encoding="utf-8"
    )
    client = client_class(
        tmp_path, host="https://example.test", max_credentials_bytes=1024
    )

    with pytest.raises(error, match="credentials read budget"):
        client.get()


def test_a_credentials_file_inside_the_cap_still_works(tmp_path, monkeypatch) -> None:
    _claude_credentials(tmp_path)
    monkeypatch.setattr(
        quota.urllib.request,
        "urlopen",
        lambda req, timeout: _Body(
            json.dumps(
                {"five_hour": {"utilization": 1}, "seven_day": {"utilization": 2}}
            ).encode("utf-8")
        ),
    )

    snapshot = quota.LiveQuotaClient(
        tmp_path, host="https://example.test", max_credentials_bytes=1024
    ).get()

    assert snapshot.five_hour.percent == 1


# ------------------------------------------------------- walk budgets


def test_codex_walk_is_bounded_by_entries_touched_not_files_yielded(tmp_path) -> None:
    """A tree of directories used to cost the whole walk for free."""
    sessions = tmp_path / "sessions"
    for i in range(200):
        (sessions / f"d{i}").mkdir(parents=True)

    reader = CodexActivityReader(tmp_path, scan_deadline_seconds=30.0, max_files=5)
    walked: list[object] = []
    real_rglob = type(sessions).rglob

    def counting_rglob(self, pattern):
        for path in real_rglob(self, pattern):
            walked.append(path)
            yield path

    original = type(sessions).rglob
    type(sessions).rglob = counting_rglob
    try:
        reader.snapshot()
    finally:
        type(sessions).rglob = original

    assert len(walked) <= 6, f"walked {len(walked)} entries with max_files=5"


def test_bounded_lines_does_not_yield_a_record_the_file_cap_cut_in_half() -> None:
    """A truncated record that happened to parse would be treated as real."""
    stream = io.BytesIO(b'{"timestamp":"2026-05-20T08:00:00Z"}\n{"timestamp":"2026')

    records = list(bounded_lines(stream, max_line_bytes=1 << 20, max_file_bytes=40))

    assert records == [b'{"timestamp":"2026-05-20T08:00:00Z"}']


# ------------------------------------------------ staleness: a wedge after a success


def test_a_successful_snapshot_stops_being_live_once_it_stops_being_refreshed() -> None:
    """A source that wedges *after* a success must not serve old numbers as `live`.

    The reads without a deadline of their own -- the credential file, a single
    transcript file -- can hang a refresher's thread without ever failing it.
    `at` then stops advancing while `ok` stays true.
    """
    source = SourceRefresher("wedges-later", lambda: "fresh", 10.0, stale_after_seconds=30.0)
    source.refresh_once()

    assert source.current().ok, "a snapshot just published is live"
    assert source.current().value == "fresh"

    # The thread has not published for well over the limit.
    stale_at = datetime.now(timezone.utc) - timedelta(seconds=120)
    source._snapshot = SourceSnapshot(ok=True, value="old", error=None, at=stale_at)

    current = source.current()
    assert not current.ok, "a snapshot two minutes past the limit is not live"
    assert isinstance(current.error, SourceStale)
    assert current.value is None, "the stale reading must not reach the payload"
    assert "120s ago" in str(current.error)
    # The raw record is unchanged: staleness is applied on read, not published.
    assert source.snapshot().ok and source.snapshot().value == "old"


def test_a_stale_source_degrades_that_provider_only(stub_sources, monkeypatch) -> None:
    """AC5 for the wedge-after-success case, end to end through the payload."""
    live = SimpleNamespace(
        five_hour=SimpleNamespace(percent=41.0, resets_at=None),
        seven_day=SimpleNamespace(percent=12.0, resets_at=None),
        subscription_type="pro",
    )
    claude = SourceRefresher("claude-quota", lambda: live, 30.0, stale_after_seconds=90.0)
    claude.refresh_once()
    monkeypatch.setattr(main, "_claude_quota_source", claude)

    # Codex publishes its own failure, so the assertion below is about it
    # keeping that failure rather than about it never having run.
    main._codex_quota_source.refresh_once()

    assert main._build_payload()["claude"]["source"] == "live"

    claude._snapshot = SourceSnapshot(
        ok=True,
        value=live,
        error=None,
        at=datetime.now(timezone.utc) - timedelta(seconds=600),
    )
    payload = main._build_payload()

    assert payload["claude"]["source"] == "unavailable"
    assert [w["percent"] for w in payload["claude"]["windows"]] == [None, None, None]
    assert "600s ago" in payload["claude"]["source_error"]
    # The other provider is untouched by its neighbour going stale.
    assert payload["codex"]["source"] == "unavailable"
    assert "upstream down" in payload["codex"]["source_error"]


def test_a_stale_activity_scan_reports_no_activity_rather_than_an_old_time() -> None:
    from app.claude_activity import ClaudeActivitySnapshot

    scan = ClaudeActivitySnapshot(
        last_activity=datetime(2020, 1, 1, tzinfo=timezone.utc), data_root_exists=True
    )
    source = SourceRefresher("activity", lambda: scan, 5.0, stale_after_seconds=35.0)
    source._snapshot = SourceSnapshot(
        ok=True, value=scan, error=None, at=datetime.now(timezone.utc) - timedelta(seconds=90)
    )

    assert main._activity_fields(source.current()) == (None, False)


def test_a_slow_but_working_source_does_not_flap_to_stale() -> None:
    """The threshold is slack on purpose: a long cycle is not a wedge.

    A 5 s cadence whose scan spends its whole 5 s deadline publishes every
    ~10 s; three intervals alone would be 15 s and would flap. The grace term
    is what stops that.
    """
    source = SourceRefresher("slow-activity", lambda: "x", 5.0)

    assert source.stale_after_seconds == 35.0
    source._snapshot = SourceSnapshot(
        ok=True, value="x", error=None, at=datetime.now(timezone.utc) - timedelta(seconds=12)
    )
    assert source.current().ok, "a cycle that ran long is still live"


def test_an_already_failed_snapshot_keeps_its_own_error_when_it_ages() -> None:
    """Age says less about a failed source than the failure does."""
    source = SourceRefresher("failing", lambda: 1 / 0, 30.0, stale_after_seconds=1.0)
    source._snapshot = SourceSnapshot(
        ok=False,
        value=None,
        error=quota.LiveQuotaError("upstream down"),
        at=datetime.now(timezone.utc) - timedelta(seconds=600),
    )

    current = source.current()
    assert not current.ok
    assert isinstance(current.error, quota.LiveQuotaError)
    assert main._source_error(current, quota.LiveQuotaError) == "upstream down"


def test_the_shipped_staleness_limits_clear_each_source_worst_case() -> None:
    """Every shipped limit must exceed what a healthy cycle of that source can cost."""
    # Quota: one cycle is at most the total deadline plus one outstanding
    # socket timeout, on top of the cadence.
    quota_source = SourceRefresher("q", lambda: None, main.QUOTA_REFRESH_SECONDS)
    worst_quota_cycle = main.QUOTA_REFRESH_SECONDS + 10.0 + 8.0
    assert quota_source.stale_after_seconds > worst_quota_cycle

    # Activity: one cycle is at most the whole-scan deadline on top of the cadence.
    for interval in (
        main.CLAUDE_ACTIVITY_REFRESH_SECONDS,
        main.CODEX_ACTIVITY_REFRESH_SECONDS,
    ):
        source = SourceRefresher("a", lambda: None, interval)
        assert source.stale_after_seconds > interval + 5.0
