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

from app import codex_quota, degrade, main, quota
from app.budget import BudgetExceeded, bounded_lines, env_float, env_int, read_capped
from app.claude_activity import ClaudeActivityReader
from app.codex_activity import CodexActivityReader
from app.refresh import (
    SourceRefresher,
    SourceSnapshot,
    SourceStale,
    stale_after,
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

    started = time.monotonic()
    # The cap is well above what 0.2 s of this sender can deliver, so the
    # deadline is what must end it -- but it is a cap a lost deadline can still
    # reach in seconds. A 1 GiB cap here would take ~44 minutes instead, so a
    # deadline regression would wedge the suite rather than fail it.
    with pytest.raises(BudgetExceeded):
        read_capped(endless, max_bytes=1 << 20, deadline=time.monotonic() + 0.2)
    held = time.monotonic() - started

    assert held < 5.0, "the deadline, not the byte cap, is what ended this"
    assert endless.served < (1 << 20)


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

    with pytest.raises(quota.LiveQuotaError, match="upstream read budget") as raised:
        client.get()

    # The transfer never completed, so it is a transport failure. Without this
    # the payload would call an oversized or trickling body an unreadable
    # *shape*, which points a reader at the parser instead of the sender.
    assert raised.value.code == degrade.TRANSPORT


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
    with pytest.raises(quota.LiveQuotaError, match="upstream read budget") as raised:
        client.get()

    # The transfer never completed, so it is a transport failure. Without this
    # the payload would call an oversized or trickling body an unreadable
    # *shape*, which points a reader at the parser instead of the sender.
    assert raised.value.code == degrade.TRANSPORT
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

    with pytest.raises(codex_quota.CodexLiveQuotaError, match="upstream read budget") as raised:
        client.get()

    # The transfer never completed, so it is a transport failure. Without this
    # the payload would call an oversized or trickling body an unreadable
    # *shape*, which points a reader at the parser instead of the sender.
    assert raised.value.code == degrade.TRANSPORT


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
    # "no data yet", not a fault in what upstream sent.
    assert payload["claude"]["source_error"] == degrade.MESSAGES[degrade.UNCLASSIFIED]
    assert payload["claude"]["data_root_exists"] is False
    for window in payload["claude"]["windows"]:
        assert window["percent"] is None


def test_an_unexpected_exception_is_never_quoted(stub_sources) -> None:
    """The refresher has to catch everything; nothing it catches may be quoted.

    An exception raised while an upstream request is being built can carry the
    bearer token in its message, and `source_error` is served unauthenticated.
    Since #14 not even the exception's type name reaches the payload: the
    boundary reports one of `app/degrade.py`'s fixed strings and nothing else.
    """
    stub_sources["claude_quota"].error = RuntimeError("Bearer sk-ant-secret-value")
    main._claude_quota_source.refresh_once()

    payload = main._build_payload()

    assert payload["claude"]["source"] == "unavailable"
    assert payload["claude"]["source_error"] == degrade.MESSAGES[degrade.INTERNAL]
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
    # The raw record is unchanged: staleness is applied on read, not published.
    assert source.snapshot().ok and source.snapshot().value == "old"


@pytest.mark.parametrize(
    "name, provider, other",
    [
        ("_claude_quota_source", "claude", "codex"),
        ("_codex_quota_source", "codex", "claude"),
    ],
)
def test_a_stale_quota_source_degrades_that_provider_only(
    stub_sources, monkeypatch, name, provider, other
) -> None:
    """AC5 for the wedge-after-success case, end to end, for each provider.

    Staling only one of the two would leave the other's handler free to go back
    to `snapshot()` unnoticed, which is how this test read before.
    """
    live = SimpleNamespace(
        five_hour=SimpleNamespace(percent=41.0, resets_at=None),
        seven_day=SimpleNamespace(percent=12.0, resets_at=None),
        subscription_type="pro",
        plan_type="pro",
    )
    source = SourceRefresher(name, lambda: live, 30.0, stale_after_seconds=90.0)
    source.refresh_once()
    monkeypatch.setattr(main, name, source)

    # The other provider publishes its own failure, so the assertion below is
    # about it keeping that failure rather than about it never having run.
    other_source = getattr(main, f"_{other}_quota_source")
    other_source.refresh_once()

    assert main._build_payload()[provider]["source"] == "live"

    source._snapshot = SourceSnapshot(
        ok=True,
        value=live,
        error=None,
        at=datetime.now(timezone.utc) - timedelta(seconds=600),
        monotonic_at=time.monotonic() - 600,
    )
    payload = main._build_payload()

    assert payload[provider]["source"] == "unavailable", (
        f"{provider} served numbers from a source that stopped being refreshed"
    )
    assert all(w["percent"] is None for w in payload[provider]["windows"])
    assert payload[provider]["source_error"] == degrade.MESSAGES[degrade.STALE]
    # The neighbour is untouched by this one going stale, and reports its own
    # kind of failure rather than staleness.
    assert payload[other]["source"] == "unavailable"
    assert payload[other]["source_error"] == degrade.MESSAGES[degrade.UNCLASSIFIED]


def test_a_stale_activity_scan_reports_no_activity_rather_than_an_old_time(
    stub_sources, monkeypatch
) -> None:
    """Through `_build_payload()`, so main's own wiring is what is pinned.

    Asserting on `_activity_fields(source.current())` alone would pass even if
    the handler still called `snapshot()`.
    """
    from app.claude_activity import ClaudeActivitySnapshot

    scan = ClaudeActivitySnapshot(
        last_activity=datetime(2020, 1, 1, tzinfo=timezone.utc), data_root_exists=True
    )
    for name, provider in (("_claude_activity_source", "claude"), ("_codex_activity_source", "codex")):
        source = SourceRefresher("activity", lambda: scan, 5.0, stale_after_seconds=35.0)
        source.refresh_once()
        monkeypatch.setattr(main, name, source)

        assert main._build_payload()[provider]["last_activity"] == "2020-01-01T00:00:00+00:00"

        source._snapshot = SourceSnapshot(
            ok=True,
            value=scan,
            error=None,
            at=datetime.now(timezone.utc) - timedelta(seconds=90),
            monotonic_at=time.monotonic() - 90,
        )
        assert main._build_payload()[provider]["last_activity"] is None, (
            f"{provider} served an activity time from a scan that stopped being refreshed"
        )


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
    assert main._degrade_code(current.error, quota.LiveQuotaError) != degrade.STALE


def test_every_sources_staleness_limit_clears_its_own_worst_case() -> None:
    """No source may be called stale while it is still within its read budgets.

    The budgets are read off the live objects rather than written out here, so
    that changing a default -- or an operator setting one -- cannot leave this
    passing while a working source flaps to `unavailable`.
    """
    worst = {
        "claude-quota": main.QUOTA_REFRESH_SECONDS
        + main._live.total_deadline_seconds
        + main._live.timeout_seconds,
        "codex-quota": main.QUOTA_REFRESH_SECONDS
        + main._codex.total_deadline_seconds
        + main._codex.timeout_seconds,
        "claude-activity": main.CLAUDE_ACTIVITY_REFRESH_SECONDS
        + main._claude_activity.scan_deadline_seconds,
        "codex-activity": main.CODEX_ACTIVITY_REFRESH_SECONDS
        + main._codex_activity.scan_deadline_seconds,
    }
    assert {s.name for s in main._SOURCES} == set(worst), "a source lost its worst case"
    for source in main._SOURCES:
        assert source.stale_after_seconds > worst[source.name], (
            f"{source.name} is called stale inside its own read budget"
        )


def test_each_source_takes_its_limit_from_its_own_budgets(monkeypatch) -> None:
    """The wiring, not the margin the shipped defaults happen to leave.

    At shipped values a quota source's limit is the interval-multiple floor
    either way, so an assertion at defaults cannot see whether `app/main.py`
    passes the budget at all. Raising the two deadlines the cadence-only form
    used to flap on is what makes the wiring observable.
    """
    import importlib

    monkeypatch.setenv("QUOTA_TOTAL_DEADLINE_SECONDS", "90")
    monkeypatch.setenv("ACTIVITY_SCAN_DEADLINE_SECONDS", "40")
    reloaded = importlib.reload(main)
    try:
        limits = {s.name: s.stale_after_seconds for s in reloaded._SOURCES}
        # Cadence-only would give 90 s and 35 s; both sit inside the raised budgets.
        assert limits["claude-quota"] == stale_after(30.0, 90.0 + 8.0)
        assert limits["codex-quota"] == stale_after(30.0, 90.0 + 8.0)
        assert limits["claude-activity"] == stale_after(5.0, 40.0)
        assert limits["codex-activity"] == stale_after(5.0, 40.0)
        for source in reloaded._SOURCES:
            budget = 98.0 if "quota" in source.name else 40.0
            assert source.stale_after_seconds > source.interval_seconds + budget, (
                f"{source.name} would be called stale inside its own read budget"
            )
    finally:
        # Put the env back *before* the reload that restores shipped values,
        # so the module other tests import is the one they expect.
        monkeypatch.undo()
        importlib.reload(main)

    assert main._claude_quota_source.stale_after_seconds == stale_after(30.0, 18.0)


def test_a_raised_read_budget_raises_the_staleness_limit_with_it() -> None:
    """The flap the cadence-only form allowed: a deadline past two intervals."""
    # Cadence alone would give max(3 x 5, 5 + 30) = 35 s for a scan allowed 40 s.
    assert stale_after(5.0, 40.0) > 5.0 + 40.0
    assert stale_after(30.0, 90.0 + 8.0) > 30.0 + 98.0
    # A cheap read still gets the interval-multiple floor.
    assert stale_after(30.0, 0.0) == 90.0
    assert stale_after(5.0, 0.0) == 35.0
    # A negative budget cannot pull the limit down at all. A large one is not
    # the test: the interval-multiple floor swallows it either way. A small one
    # is only caught by the clamp.
    assert stale_after(5.0, -1.0) == 35.0
    assert stale_after(30.0, -1000.0) == 90.0


def test_the_staleness_limit_has_a_floor_like_the_interval() -> None:
    """Zero would mark every snapshot stale the instant it was published."""
    source = SourceRefresher("no-limit", lambda: "x", 30.0, stale_after_seconds=0.0)
    source.refresh_once()

    assert source.stale_after_seconds > 0.0
    assert source.current().ok, "a snapshot published this instant is not stale"


def test_a_backward_wall_clock_step_does_not_switch_the_staleness_check_off(
    monkeypatch,
) -> None:
    """NTP or a restored VM snapshot must not make an old snapshot look fresh."""
    source = SourceRefresher("clock", lambda: "x", 30.0, stale_after_seconds=90.0)
    source._snapshot = SourceSnapshot(
        ok=True,
        value="x",
        error=None,
        # The wall clock has been stepped back an hour, so by it this snapshot
        # was published in the future.
        at=datetime.now(timezone.utc) + timedelta(seconds=3600),
        monotonic_at=time.monotonic() - 600,
    )

    current = source.current()
    assert not current.ok, "the monotonic clock still knows this is 10 minutes old"
    assert isinstance(current.error, SourceStale)


def test_a_host_resume_does_not_make_an_old_snapshot_look_young() -> None:
    """The other half of using both clocks.

    `time.monotonic()` excludes time a Linux host spends suspended, so a
    snapshot taken before a suspend looks recent by it. The wall clock is what
    still knows how long it has really been.
    """
    source = SourceRefresher("resumed", lambda: "x", 30.0, stale_after_seconds=90.0)
    source._snapshot = SourceSnapshot(
        ok=True,
        value="x",
        error=None,
        at=datetime.now(timezone.utc) - timedelta(seconds=3600),
        monotonic_at=time.monotonic() - 1,  # the monotonic clock was asleep
    )

    current = source.current()
    assert not current.ok, "the wall clock still knows this is an hour old"
    assert isinstance(current.error, SourceStale)


def test_every_published_snapshot_carries_both_stamps() -> None:
    """Either stamp going missing would silently halve the clock check."""
    ok_source = SourceRefresher("ok", lambda: "x", 30.0)
    ok_source.refresh_once()
    failed_source = SourceRefresher("failed", lambda: 1 / 0, 30.0)
    failed_source.refresh_once()

    for source in (ok_source, failed_source):
        snapshot = source.snapshot()
        assert snapshot.at is not None, source.name
        assert snapshot.monotonic_at is not None, source.name

    # And a stale snapshot derived on read keeps the original's stamps.
    published = ok_source.snapshot()
    ok_source._snapshot = SourceSnapshot(
        ok=True,
        value="x",
        error=None,
        at=datetime.now(timezone.utc) - timedelta(seconds=600),
        monotonic_at=time.monotonic() - 600,
    )
    derived = ok_source.current()
    assert not derived.ok
    assert derived.at == ok_source.snapshot().at
    assert derived.monotonic_at == ok_source.snapshot().monotonic_at
    assert published.monotonic_at is not None


def test_a_snapshot_with_no_monotonic_stamp_still_ages_on_the_wall_clock() -> None:
    source = SourceRefresher("legacy", lambda: "x", 30.0, stale_after_seconds=90.0)
    source._snapshot = SourceSnapshot(
        ok=True, value="x", error=None, at=datetime.now(timezone.utc) - timedelta(seconds=600)
    )

    assert not source.current().ok


def test_a_recorded_failure_does_not_grow_a_traceback_on_every_build(
    stub_sources, monkeypatch
) -> None:
    """The same exception object is re-raised on every payload build.

    Each raise appends a frame to that *same* object, so the chain would grow
    without bound exactly where this design is supposed to hold: a source that
    published a failure and then wedged is never republished, so nothing ever
    replaces the snapshot holding it.
    """
    def failing():
        raise quota.LiveQuotaError("upstream down")

    source = SourceRefresher("claude-quota", failing, 30.0)
    source.refresh_once()
    monkeypatch.setattr(main, "_claude_quota_source", source)

    def traceback_entries() -> int:
        tb, n = source.snapshot().error.__traceback__, 0
        while tb is not None:
            n += 1
            tb = tb.tb_next
        return n

    for _ in range(200):
        payload = main._build_payload()

    assert payload["claude"]["source"] == "unavailable"
    assert traceback_entries() <= 2, (
        "the stored exception grew a frame per payload build"
    )


def test_a_pending_source_is_still_reported_when_its_error_is_falsy() -> None:
    """The guard is the snapshot's `ok`, not whether an exception is truthy."""

    class Falsy(Exception):
        def __bool__(self) -> bool:
            return False

    assert isinstance(main._recorded(Falsy("x"), "pending"), Falsy)
    assert isinstance(main._recorded(None, "pending"), main._NotPublished)


def test_healthz_does_not_stall_other_routes(monkeypatch) -> None:
    """`/healthz` stats the same bind mounts every other read moved off the loop.

    A hung `~/.claude` would otherwise block the event loop here and stall
    every route, including `/api/usage`, which does no I/O of its own.

    Driven through the app, so the route's own wiring is what is measured --
    asserting on `main.healthz()` directly would pass even with the decorator
    on some other function. The clock starts *before* control reaches the
    loop: a blocking endpoint runs to completion before the first `await`
    resumes, so a timer started after that yield measures nothing.
    """
    import types
    from pathlib import Path

    from fastapi.testclient import TestClient

    class SlowPath(type(Path("/"))):
        def exists(self, *args, **kwargs):
            time.sleep(1.5)
            return True

    slow = SlowPath("/tmp")
    for name, attr in (
        ("_claude_activity", "data_dir"),
        ("_codex_activity", "data_dir"),
        ("_live", "credentials_path"),
        ("_codex", "credentials_path"),
    ):
        monkeypatch.setattr(main, name, types.SimpleNamespace(**{attr: slow}))

    # 1. The route's own wiring: `/healthz` must reach `healthz()`, which is
    #    what adds `ok` and the thread hop. A `TestClient` runs its own loop,
    #    so this half proves the wiring, not the concurrency.
    client = TestClient(main.app, base_url="http://127.0.0.1:8765")
    response = client.get("/healthz")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True, "/healthz lost its `ok` flag"
    assert "claude_credentials_present" in data

    # 2. The concurrency, on one loop: how long before an unrelated coroutine
    #    gets to run at all. An endpoint that stats inline never yields, so
    #    everything else waits for the whole stall.
    reached_at: list[float] = []

    async def drive() -> None:
        started = time.monotonic()

        async def other_work() -> None:
            reached_at.append(time.monotonic() - started)
            main._build_payload()

        await asyncio.gather(main.healthz(), other_work())

    asyncio.run(drive())

    assert reached_at and reached_at[0] < 0.5, (
        f"an unrelated coroutine waited {reached_at[0]:.2f}s behind /healthz's stats"
    )
