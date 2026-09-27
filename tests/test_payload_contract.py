"""The payload contract: one boundary, one schema, one fixed error vocabulary.

Both providers run through the same matrix of transport faults, hostile response
bodies and hostile credential files, and the payload is asserted to stay inside
the declared schema every time. The per-parser tests next door check tolerance
for one provider at a time; this file is what stops the two providers drifting
apart again, so a case added here must run for both.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import math
import os
import re
import socket
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app import degrade, main
from app.claude_activity import ClaudeActivityReader
from app.codex_activity import CodexActivityReader
from app.budget import BudgetExceeded
from app.refresh import SourceRefresher
from app.codex_quota import CodexLiveQuotaClient, CodexLiveQuotaError
from app.quota import LiveQuotaClient, LiveQuotaError


# Stands in for a bearer token. No payload, and no exception the boundary
# reports, may contain it.
SECRET = "sk-ant-oat01-CONTRACT-TEST-SECRET"

CLAUDE_HOST = "http://claude.test"
CODEX_HOST = "http://codex.test"

PROVIDERS = ("claude", "codex")


def healthy_credentials(provider: str) -> str:
    if provider == "claude":
        return json.dumps(
            {"claudeAiOauth": {"accessToken": SECRET, "subscriptionType": "max"}}
        )
    return json.dumps(
        {"tokens": {"access_token": SECRET, "account_id": "acct-1"}, "plan_type": "pro"}
    )


def healthy_body(provider: str) -> bytes:
    if provider == "claude":
        return json.dumps(
            {
                "five_hour": {"utilization": 12.5, "resets_at": "2026-05-20T12:00:00Z"},
                "seven_day": {"utilization": 40.0},
                "limits": [
                    {
                        "kind": "weekly_scoped",
                        "percent": 7.5,
                        "scope": {"model": {"display_name": "Fable 5.1"}},
                    }
                ],
            }
        ).encode()
    return json.dumps(
        {
            "primary_window": {
                "utilization": 12.5,
                "limit_window_seconds": 18_000,
                "resets_at": "2026-05-20T12:00:00Z",
            },
            "secondary_window": {"utilization": 40.0, "limit_window_seconds": 604_800},
        }
    ).encode()


def _body_with_percent(provider: str, raw_percent: str) -> bytes:
    """A healthy body whose 5-hour utilization is the given raw JSON text."""
    if provider == "claude":
        return (
            b'{"five_hour": {"utilization": '
            + raw_percent.encode()
            + b'}, "seven_day": {"utilization": 40.0}}'
        )
    return (
        b'{"primary_window": {"utilization": '
        + raw_percent.encode()
        + b', "limit_window_seconds": 18000}}'
    )


def _body_with_resets(provider: str, raw_resets: str) -> bytes:
    if provider == "claude":
        return (
            b'{"five_hour": {"utilization": 1, "resets_at": '
            + raw_resets.encode()
            + b'}, "seven_day": {"utilization": 2}}'
        )
    return (
        b'{"primary_window": {"utilization": 1, "limit_window_seconds": 18000,'
        b' "resets_at": ' + raw_resets.encode() + b"}}"
    )


def hostile_bodies(provider: str) -> dict[str, bytes]:
    """Bodies an undocumented endpoint could plausibly start returning."""
    huge_int = "9" * 400
    deep = b"[" * 20_000 + b"]" * 20_000
    bodies = {
        "not-json": b"<html>502</html>",
        "json-null": b"null",
        "json-array": b"[1, 2, 3]",
        "json-number": b"42",
        "json-string": b'"nope"',
        "json-deeply-nested": deep,
        "empty-object": b"{}",
        "percent-nan": _body_with_percent(provider, "NaN"),
        "percent-infinity": _body_with_percent(provider, "Infinity"),
        "percent-negative-infinity": _body_with_percent(provider, "-Infinity"),
        "percent-true": _body_with_percent(provider, "true"),
        "percent-null": _body_with_percent(provider, "null"),
        "percent-string": _body_with_percent(provider, '"12.5"'),
        "percent-object": _body_with_percent(provider, "{}"),
        "percent-array": _body_with_percent(provider, "[]"),
        "percent-huge-int": _body_with_percent(provider, huge_int),
        "percent-negative": _body_with_percent(provider, "-500"),
        "percent-above-range": _body_with_percent(provider, "1e6"),
        "resets-huge-year": _body_with_resets(provider, '"+099999-01-01T00:00:00Z"'),
        "resets-huge-number": _body_with_resets(provider, "1e18"),
        "resets-negative-number": _body_with_resets(provider, "-1e18"),
        "resets-not-a-date": _body_with_resets(provider, '"whenever"'),
        "resets-object": _body_with_resets(provider, "{}"),
        "window-not-object": (
            b'{"five_hour": "nope", "seven_day": "nope"}'
            if provider == "claude"
            else b'{"primary_window": "nope", "secondary_window": []}'
        ),
    }
    if provider == "claude":
        bodies["limits-not-a-list"] = (
            b'{"five_hour": {"utilization": 1}, "seven_day": {"utilization": 2},'
            b' "limits": "nope"}'
        )
        bodies["fable-entry-hostile"] = json.dumps(
            {
                "five_hour": {"utilization": 1},
                "seven_day": {"utilization": 2},
                "limits": [
                    {
                        "kind": "weekly_scoped",
                        "percent": "not a number",
                        "scope": {"model": {"display_name": "Fable 5.1"}},
                    }
                ],
            }
        ).encode()
        bodies["fable-entry-out-of-range"] = json.dumps(
            {
                "five_hour": {"utilization": 1},
                "seven_day": {"utilization": 2},
                "limits": [
                    {
                        "kind": "weekly_scoped",
                        "percent": 4000,
                        "scope": {"model": {"display_name": "Fable 5.1"}},
                    }
                ],
            }
        ).encode()
    else:
        bodies["plan-lone-surrogate"] = (
            b'{"primary_window": {"utilization": 1, "limit_window_seconds": 18000},'
            b' "plan_type": "pro\\ud800"}'
        )
        bodies["plan-control-characters"] = (
            b'{"primary_window": {"utilization": 1, "limit_window_seconds": 18000},'
            b' "plan_type": "pro\\u0000\\r\\nSet-Cookie: x"}'
        )
        bodies["plan-very-long"] = json.dumps(
            {
                "primary_window": {"utilization": 1, "limit_window_seconds": 18_000},
                "plan_type": "p" * 5_000,
            }
        ).encode()
        bodies["percent-left-out-of-range"] = (
            b'{"primary_window": {"percent_left": -500, "limit_window_seconds": 18000}}'
        )
    return bodies


def transport_faults() -> dict[str, BaseException]:
    """Faults urlopen(), getresponse() and read() raise in the wild.

    Only two of these are URLError, which is the whole point: the rest used to
    travel straight past each client's `except` list.
    """
    return {
        "remote-disconnected": http.client.RemoteDisconnected("closed early"),
        "incomplete-read": http.client.IncompleteRead(b"partial"),
        "bad-status-line": http.client.BadStatusLine("garbage"),
        "line-too-long": http.client.LineTooLong("header line"),
        "connection-reset": ConnectionResetError(104, "Connection reset by peer"),
        "socket-timeout": socket.timeout("timed out"),
        "timeout-error": TimeoutError("timed out"),
        "ssl-error": ssl.SSLError("record layer failure"),
        "url-error": urllib.error.URLError("name resolution failed"),
        "http-error-401": urllib.error.HTTPError(
            CLAUDE_HOST, 401, "Unauthorized", {}, None
        ),
        "http-error-500": urllib.error.HTTPError(
            CLAUDE_HOST, 500, "Server Error", {}, None
        ),
        # http.client quotes the whole header value when it rejects one, so this
        # is the shape of exception that carries the bearer token.
        "value-error-with-bearer": ValueError(
            f"Invalid header value b'Bearer {SECRET}'"
        ),
        "recursion-error": RecursionError("maximum recursion depth exceeded"),
        "overflow-error": OverflowError("int too large to convert to float"),
        # An undeclared programming error inside a client.
        "key-error": KeyError("five_hour"),
        "attribute-error": AttributeError("'NoneType' object has no attribute 'get'"),
        "unicode-error": UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogate"),
        # The read budget cutting off a body that is too large or still
        # trickling at the deadline. The transfer never completed, so it
        # belongs with the transport faults rather than the shape ones.
        "budget-exceeded": BudgetExceeded("upstream body exceeded 1048576 bytes"),
    }


def transport_fault_codes() -> dict[str, str]:
    """The classification each fault above must be reported as.

    Asserting only "unavailable and in schema" would pass even if both clients
    dropped their `except (OSError, http.client.HTTPException)` clause or stopped
    telling 401 from 500 — `main`'s boundary contains anything either way. But
    the message an operator reads is keyed off this, and README sends them
    somewhere different for each one: "upstream unreachable" is the network,
    "upstream rejected the stored credential" is a re-login, and "internal
    error" is a bug to report. A fault classified as the wrong one of those
    sends them to the wrong place, so the mapping is the contract, not an
    implementation detail.
    """
    transport = (
        "remote-disconnected",
        "incomplete-read",
        "bad-status-line",
        "line-too-long",
        "connection-reset",
        "socket-timeout",
        "timeout-error",
        "ssl-error",
        "url-error",
        "budget-exceeded",
    )
    # A ValueError out of the send path is how http.client rejects a header it
    # cannot put on the wire, which is a credential problem — and the exception
    # that carries the bearer token.
    credentials = ("value-error-with-bearer", "unicode-error")
    # Nothing a client declares: these escape it undeclared and are a bug here.
    internal = ("recursion-error", "overflow-error", "key-error", "attribute-error")
    codes = {case: degrade.TRANSPORT for case in transport}
    codes.update({case: degrade.CREDENTIALS for case in credentials})
    codes.update({case: degrade.INTERNAL for case in internal})
    codes["http-error-401"] = degrade.AUTH
    codes["http-error-500"] = degrade.HTTP
    assert set(codes) == set(transport_faults()), "every fault needs a classification"
    return codes


def hostile_hosts() -> dict[str, str]:
    """Hosts an operator can put in CLAUDE_AI_HOST / CHATGPT_HOST.

    The host the clients build their URL from is another operator-controlled
    input. A typo there must not be reported as a dashboard bug.
    """
    return {
        "empty": "",
        "no-scheme": "claude.ai",
        "path-only": "/backend-api/usage",
        "unsupported-scheme": "gopher://example.invalid",
        "malformed-ipv6": "http://[::1",
        "with-a-space": "http://exa mple.invalid",
        "with-a-newline": "http://example.invalid\n",
        "very-long": "http://" + "h" * 10_000,
    }


def hostile_credentials(provider: str) -> dict[str, str | None]:
    """Credential files a headless or hand-assembled deployment can produce.

    None means "no file at all"; the boundary must contain that too.
    """
    token_key = "accessToken" if provider == "claude" else "access_token"
    wrapper = "claudeAiOauth" if provider == "claude" else "tokens"
    cases: dict[str, str | None] = {
        "absent": None,
        "empty": "",
        "whitespace": "   \n",
        "truncated-json": '{"' + wrapper + '": {',
        "json-null": "null",
        "json-array": "[]",
        "json-number": "7",
        "json-string": '"nope"',
        "no-wrapper": "{}",
        "wrapper-is-a-string": json.dumps({wrapper: "nope"}),
        "wrapper-is-a-list": json.dumps({wrapper: []}),
        "no-token": json.dumps({wrapper: {}}),
        "token-is-a-number": json.dumps({wrapper: {token_key: 7}}),
        "token-is-an-object": json.dumps({wrapper: {token_key: {"a": 1}}}),
        "token-is-empty": json.dumps({wrapper: {token_key: ""}}),
        "token-with-crlf": json.dumps({wrapper: {token_key: f"{SECRET}\r\nX-Evil: 1"}}),
        "token-with-lf": json.dumps({wrapper: {token_key: f"{SECRET}\nX-Evil: 1"}}),
        "token-with-nul": json.dumps({wrapper: {token_key: f"{SECRET}\x00"}}),
        "deeply-nested": "[" * 20_000 + "]" * 20_000,
    }
    if provider == "claude":
        cases["plan-is-a-number"] = json.dumps(
            {wrapper: {token_key: SECRET, "subscriptionType": 7}}
        )
        cases["plan-lone-surrogate"] = (
            '{"claudeAiOauth": {"accessToken": "'
            + SECRET
            + '", "subscriptionType": "max\\ud800"}}'
        )
    else:
        cases["account-id-with-lf"] = json.dumps(
            {"tokens": {token_key: SECRET, "account_id": "acct\nX-Evil: 1"}}
        )
        cases["account-id-is-a-number"] = json.dumps(
            {"tokens": {token_key: SECRET, "account_id": 7}}
        )
        cases["plan-is-a-list"] = json.dumps(
            {"tokens": {token_key: SECRET}, "plan_type": ["pro"]}
        )
    return cases


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200, read_fault: BaseException | None = None):
        self.status = status
        self._body = body
        self._read_fault = read_fault

    # `read_capped()` asks for a bounded amount, and prefers `read1` so that a
    # sender trickling bytes is noticed against the deadline rather than after
    # the whole body. `http.client.HTTPResponse` has both; a stub with a
    # no-argument `read()` models a response that does not exist.
    def read1(self, amount: int = -1) -> bytes:
        if self._read_fault is not None:
            raise self._read_fault
        if amount is None or amount < 0:
            chunk, self._body = self._body, b""
            return chunk
        chunk, self._body = self._body[:amount], self._body[amount:]
        return chunk

    def read(self, amount: int = -1) -> bytes:
        return self.read1(amount)

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _Deployment:
    """Both providers wired up with real clients and real readers over tmp files."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._monkeypatch = monkeypatch
        self.dirs = {
            "claude": tmp_path / "claude",
            "codex": tmp_path / "codex",
        }
        self.bodies = {provider: healthy_body(provider) for provider in PROVIDERS}
        self.faults: dict[str, BaseException | None] = {p: None for p in PROVIDERS}
        self.read_faults: dict[str, BaseException | None] = {p: None for p in PROVIDERS}
        self.statuses = {provider: 200 for provider in PROVIDERS}
        # Every Request the clients hand to urlopen, so a test can assert on
        # what would have gone on the wire and not only on what came back.
        self.requests: list[urllib.request.Request] = []

        for provider, path in self.dirs.items():
            path.mkdir(parents=True, exist_ok=True)
            self.write_credentials(provider, healthy_credentials(provider))

        monkeypatch.setattr(
            main,
            "_live",
            LiveQuotaClient(self.dirs["claude"], host=CLAUDE_HOST),
        )
        monkeypatch.setattr(
            main,
            "_codex",
            CodexLiveQuotaClient(self.dirs["codex"], host=CODEX_HOST),
        )
        monkeypatch.setattr(
            main,
            "_claude_activity",
            ClaudeActivityReader(self.dirs["claude"]),
        )
        monkeypatch.setattr(
            main,
            "_codex_activity",
            CodexActivityReader(self.dirs["codex"]),
        )
        monkeypatch.setattr(urllib.request, "urlopen", self._urlopen)
        # A degraded source is logged once until its classification changes, so
        # each test starts from "nothing logged yet".
        monkeypatch.setattr(main, "_logged_degrade", {})

        # Payload-feeding I/O now happens in a refresher, not in a handler, so
        # swapping a client in no longer changes what the next payload says --
        # the source has to be read again first. These tests mean "build a
        # payload from these clients, now", so each build refreshes all four
        # sources synchronously: the same work the background threads do on
        # their cadence, on this thread, with nothing cached in between.
        #
        # The refreshers are fresh per test and resolve main's module globals
        # at call time, so `set_host()` and the fault switches keep working.
        sources = (
            SourceRefresher("claude-quota", lambda: main._live.get(), 30.0),
            SourceRefresher("claude-activity", lambda: main._claude_activity.snapshot(), 5.0),
            SourceRefresher("codex-quota", lambda: main._codex.get(), 30.0),
            SourceRefresher("codex-activity", lambda: main._codex_activity.snapshot(), 5.0),
        )
        for name, source in zip(
            (
                "_claude_quota_source",
                "_claude_activity_source",
                "_codex_quota_source",
                "_codex_activity_source",
            ),
            sources,
        ):
            monkeypatch.setattr(main, name, source)
        monkeypatch.setattr(main, "_SOURCES", sources)

        build_from_snapshots = main._build_payload

        def build_payload() -> dict:
            for source in sources:
                source.refresh_once()
            return build_from_snapshots()

        monkeypatch.setattr(main, "_build_payload", build_payload)
        self.sources = sources

    def set_host(self, provider: str, host: str) -> None:
        if provider == "claude":
            client = LiveQuotaClient(
                self.dirs["claude"], host=host
            )
        else:
            client = CodexLiveQuotaClient(
                self.dirs["codex"], host=host
            )
        self._monkeypatch.setattr(
            main, "_live" if provider == "claude" else "_codex", client
        )

    def credentials_path(self, provider: str) -> Path:
        name = ".credentials.json" if provider == "claude" else "auth.json"
        return self.dirs[provider] / name

    def write_credentials(self, provider: str, content: str | None) -> None:
        path = self.credentials_path(provider)
        if content is None:
            path.unlink(missing_ok=True)
            return
        path.write_text(content, encoding="utf-8")

    def _urlopen(self, request: urllib.request.Request, timeout: float | None = None):
        self.requests.append(request)
        url = request.full_url
        if url.startswith(CLAUDE_HOST):
            provider = "claude"
        elif url.startswith(CODEX_HOST):
            provider = "codex"
        else:
            # Any other host is as unreachable here as it would be in the
            # container, whose only route out is a proxy that allow-lists two
            # names. Without this the stub would answer for a host override the
            # client should never have been able to dial.
            raise urllib.error.URLError(f"unknown host for {request.host}")
        fault = self.faults[provider]
        if fault is not None:
            raise fault
        return _FakeResponse(
            self.bodies[provider],
            status=self.statuses[provider],
            read_fault=self.read_faults[provider],
        )


@pytest.fixture
def deployment(monkeypatch, tmp_path):
    return _Deployment(monkeypatch, tmp_path)


# ─── The schema, restated here rather than read from the module it bounds ────
#
# These are the numbers and the character class `app/main.py` enforces, written out again in
# this file. The assertions below used `main.MAX_TEXT_CHARS` and `main._UNPRINTABLE` directly,
# which made the schema check move with whatever it was checking: raising `MAX_TEXT_CHARS` from
# 120 to 100,000, or taking `\u2028` out of the unprintable class, widened the module and the
# assertion about the module in one edit, with the suite green (#78).
#
# `test_the_schema_the_boundary_enforces_is_the_one_stated_here` below is the other half: it
# requires the module to still agree with these, so a deliberate change is one line there and
# a reviewer reading the number move -- rather than nothing at all.
SCHEMA_MAX_TEXT_CHARS = 120
#: C0, DEL and C1, plus the two Unicode line terminators. The last two are the ones that matter
#: most here and the easiest to drop by accident: JavaScript ends a line on U+2028 and U+2029,
#: so a payload embedded in a <script> block carries them out of the string it was put in.
SCHEMA_UNPRINTABLE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")
SCHEMA_MIN_PERCENT = 0.0
SCHEMA_MAX_PERCENT = 100.0
SCHEMA_MIN_DATE = datetime(1970, 1, 1, tzinfo=timezone.utc)
SCHEMA_MAX_DATE = datetime(2100, 1, 1, tzinfo=timezone.utc)


def test_the_schema_the_boundary_enforces_is_the_one_stated_here() -> None:
    """What `app/main.py` holds must be what this file asserts against.

    Without this, restating the schema above would merely make the two drift apart quietly
    instead of widening together; with it, they are one statement checked in two places.
    """
    assert main.MAX_TEXT_CHARS == SCHEMA_MAX_TEXT_CHARS
    assert main._UNPRINTABLE.pattern == SCHEMA_UNPRINTABLE.pattern
    assert (main.MIN_PERCENT, main.MAX_PERCENT) == (SCHEMA_MIN_PERCENT, SCHEMA_MAX_PERCENT)
    assert (main.MIN_DATE, main.MAX_DATE) == (SCHEMA_MIN_DATE, SCHEMA_MAX_DATE)


def test_the_unprintable_class_catches_each_kind_of_character_it_names() -> None:
    """A class is only what it matches, so each region of it is exercised by a character.

    `\u2028` was in the pattern and in nothing else: deleting it left every other test green
    while a payload gained a way to end a JavaScript line.
    """
    for char in ("\x00", "\x1f", "\x7f", "\x9f", "\u2028", "\u2029"):
        assert SCHEMA_UNPRINTABLE.search(char), repr(char)
        assert main._text(f"plan{char}name") == "plan name"
    for char in ("a", " ", "é", "☃"):
        assert not SCHEMA_UNPRINTABLE.search(char), repr(char)


def _assert_text_in_schema(value: object) -> None:
    """A free-form string is bounded and printable, or null.

    Printability is not cosmetic. The payload is embedded in a <script> block,
    so a line terminator would end a JavaScript line, and a control character
    can forge a line in whatever else reads the payload. Length alone would let
    a raw "pro\x00\r\nX-Evil: 1" through.
    """
    if value is None:
        return
    assert isinstance(value, str)
    assert 0 < len(value) <= SCHEMA_MAX_TEXT_CHARS
    assert SCHEMA_UNPRINTABLE.search(value) is None
    value.encode("utf-8")


def _assert_window_in_schema(window: dict) -> None:
    assert set(window) == {"name", "label", "percent", "resets_at", "detail"}
    assert isinstance(window["name"], str)
    assert isinstance(window["label"], str)
    percent = window["percent"]
    if percent is not None:
        assert isinstance(percent, float) and not isinstance(percent, bool)
        assert math.isfinite(percent)
        assert SCHEMA_MIN_PERCENT <= percent <= SCHEMA_MAX_PERCENT
    for field in ("resets_at", "detail"):
        value = window[field]
        assert value is None or isinstance(value, str)
    _assert_text_in_schema(window["detail"])
    if window["resets_at"] is not None:
        moment = datetime.fromisoformat(window["resets_at"])
        assert SCHEMA_MIN_DATE <= moment <= SCHEMA_MAX_DATE


def assert_payload_in_schema(payload: dict) -> str:
    """Assert the payload matches the declared schema, and return its one form."""
    assert set(payload) == {"claude", "codex", "server_time"}
    datetime.fromisoformat(payload["server_time"])

    for provider in PROVIDERS:
        section = payload[provider]
        assert set(section) == {
            "enabled",
            "windows",
            "source",
            "source_error",
            "subscription_type",
            "last_activity",
            "data_root_exists",
        }
        assert isinstance(section["enabled"], bool)
        assert isinstance(section["data_root_exists"], bool)
        assert section["source"] in ("live", "unavailable")
        error = section["source_error"]
        if section["source"] == "live":
            assert error is None
        else:
            # The fixed vocabulary, and nothing else: no str(exc), no repr.
            assert error in degrade.VOCABULARY
        _assert_text_in_schema(section["subscription_type"])
        if section["last_activity"] is not None:
            datetime.fromisoformat(section["last_activity"])
        for window in section["windows"]:
            _assert_window_in_schema(window)

    expected_names = {
        "claude": ["five_hour", "seven_day", "seven_day_fable"],
        "codex": ["five_hour", "seven_day"],
    }
    for provider, names in expected_names.items():
        assert [w["name"] for w in payload[provider]["windows"]] == names

    # Strict, encodable, round-tripping, and free of the credential.
    serialized = main._payload_json(payload)
    assert json.loads(serialized) == payload
    assert serialized == json.dumps(
        payload, allow_nan=False, ensure_ascii=True, separators=(",", ":")
    )
    serialized.encode("utf-8")
    assert SECRET not in serialized
    assert main._payload_script_json(serialized).encode("utf-8")
    for fragment in ("</script", "NaN", "Infinity"):
        assert fragment not in main._payload_script_json(serialized)
    return serialized


def test_healthy_payload_is_in_schema(deployment) -> None:
    payload = main._build_payload()

    assert_payload_in_schema(payload)
    for provider in PROVIDERS:
        assert payload[provider]["source"] == "live"
    assert [w["percent"] for w in payload["claude"]["windows"]] == [12.5, 40.0, 7.5]
    assert [w["percent"] for w in payload["codex"]["windows"]] == [12.5, 40.0]


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("case", sorted(hostile_bodies("codex") | hostile_bodies("claude")))
def test_hostile_response_body_stays_in_schema(deployment, provider, case) -> None:
    bodies = hostile_bodies(provider)
    if case not in bodies:
        pytest.skip(f"{case} is not a {provider} body shape")
    deployment.bodies[provider] = bodies[case]
    if provider == "codex" and case.startswith("plan-"):
        # auth.json's plan_type wins over the body's, so clear it to let the
        # body's hostile plan string through to the boundary.
        deployment.write_credentials(
            "codex", json.dumps({"tokens": {"access_token": SECRET}})
        )

    serialized = assert_payload_in_schema(main._build_payload())

    # The other provider is a separate source and stays live.
    other = "codex" if provider == "claude" else "claude"
    assert main._build_payload()[other]["source"] == "live", serialized
    # And through the routes, which is where the acceptance criterion is
    # written: GET / and /api/usage answer 200 for every case in the matrix.
    assert_payload_in_schema(json.loads(_usage_body()))
    assert SECRET not in _index_html()


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("case", sorted(transport_faults()))
def test_transport_fault_stays_in_schema(deployment, provider, case) -> None:
    deployment.faults[provider] = transport_faults()[case]

    payload = main._build_payload()

    assert_payload_in_schema(payload)
    assert payload[provider]["source"] == "unavailable"
    expected = degrade.MESSAGES[transport_fault_codes()[case]]
    assert payload[provider]["source_error"] == expected
    other = "codex" if provider == "claude" else "claude"
    assert payload[other]["source"] == "live"


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("case", sorted(transport_faults()))
def test_fault_raised_from_read_stays_in_schema(deployment, provider, case) -> None:
    deployment.read_faults[provider] = transport_faults()[case]

    payload = main._build_payload()

    assert_payload_in_schema(payload)
    assert payload[provider]["source"] == "unavailable"
    # A fault raised from read() is classified the same as one raised from
    # urlopen(): it is the same `except` clause that has to contain it.
    expected = degrade.MESSAGES[transport_fault_codes()[case]]
    assert payload[provider]["source_error"] == expected


@pytest.mark.parametrize(
    "account_id",
    (
        "acct\r\nX-Evil: 1",
        # http.client's own check is `\n(?![ \t])`, so an LF followed by a space
        # passes it and is sent as an obs-fold continuation line. Only the
        # client's own guard refuses this one.
        "acct\n X-Evil: 1",
        "acct\n\tX-Evil: 1",
        "acct\x00evil",
    ),
)
def test_a_hostile_account_id_never_reaches_a_header(deployment, account_id) -> None:
    """The account id is a credential-file string that goes out as a header.

    Asserting only that the payload stays in schema would pass with the guard
    deleted, because the request still succeeds — it would just carry the
    forged header upstream. So assert on what went on the wire.
    """
    deployment.write_credentials(
        "codex",
        json.dumps({"tokens": {"access_token": SECRET, "account_id": account_id}}),
    )

    payload = main._build_payload()

    assert_payload_in_schema(payload)
    sent = [r for r in deployment.requests if r.full_url.startswith(CODEX_HOST)]
    assert sent, "the codex client never built a request"
    for request in sent:
        for name, value in request.header_items():
            assert "\r" not in value and "\n" not in value and "\x00" not in value
            assert "X-Evil" not in value
            assert "X-Evil" not in name
        # Unusable means omitted, not sent in some other form.
        assert request.get_header("Chatgpt-account-id") is None


def test_a_usable_account_id_is_still_sent(deployment) -> None:
    # The guard above must refuse hostile values, not every value.
    deployment.write_credentials(
        "codex",
        json.dumps({"tokens": {"access_token": SECRET, "account_id": "acct-1"}}),
    )

    main._build_payload()

    sent = [r for r in deployment.requests if r.full_url.startswith(CODEX_HOST)]
    assert sent and sent[0].get_header("Chatgpt-account-id") == "acct-1"


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("status", (201, 204, 301, 418, 500, 503))
def test_unexpected_status_stays_in_schema(deployment, provider, status) -> None:
    deployment.statuses[provider] = status

    payload = main._build_payload()

    assert_payload_in_schema(payload)
    assert payload[provider]["source"] == "unavailable"


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "case", sorted(hostile_credentials("codex") | hostile_credentials("claude"))
)
def test_hostile_credentials_stay_in_schema(deployment, provider, case) -> None:
    cases = hostile_credentials(provider)
    if case not in cases:
        pytest.skip(f"{case} is not a {provider} credentials shape")
    deployment.write_credentials(provider, cases[case])

    payload = main._build_payload()

    assert_payload_in_schema(payload)
    other = "codex" if provider == "claude" else "claude"
    assert payload[other]["source"] == "live"
    # Through the routes as well, not only through _build_payload: the
    # acceptance criterion is that GET / and /api/usage answer 200 for every
    # case in the matrix, and it is the routes that a browser meets.
    served = json.loads(_usage_body())
    assert_payload_in_schema(served)
    assert SECRET not in json.dumps(served)
    assert SECRET not in _index_html()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_credentials_path_is_a_directory(deployment, provider) -> None:
    path = deployment.credentials_path(provider)
    path.unlink(missing_ok=True)
    path.mkdir()

    payload = main._build_payload()

    assert_payload_in_schema(payload)
    assert payload[provider]["source"] == "unavailable"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_credential_in_a_header_never_reaches_the_payload(deployment, provider) -> None:
    """The credential tail of #14: an exception that carries the bearer token."""
    deployment.write_credentials(
        provider,
        hostile_credentials(provider)["token-with-crlf"],
    )
    payload = main._build_payload()
    serialized = assert_payload_in_schema(payload)

    assert payload[provider]["source"] == "unavailable"
    assert payload[provider]["source_error"] == degrade.MESSAGES[degrade.CREDENTIALS]
    assert SECRET not in serialized
    assert "X-Evil" not in serialized

    # Also when the fault is raised at send time rather than caught by the
    # client's own credential check.
    deployment.write_credentials(provider, healthy_credentials(provider))
    deployment.faults[provider] = transport_faults()["value-error-with-bearer"]
    serialized = assert_payload_in_schema(main._build_payload())
    assert SECRET not in serialized


@pytest.mark.parametrize(
    "code,expected",
    (
        (degrade.CREDENTIALS, "stored credential unavailable or unusable"),
        (degrade.AUTH, "upstream rejected the stored credential"),
        (degrade.HTTP, "upstream returned an error response"),
        (degrade.TRANSPORT, "upstream unreachable"),
        (degrade.SHAPE, "upstream response not understood"),
        (degrade.STALE, "provider data is no longer being refreshed"),
        (degrade.ACTIVITY, "local activity reading unavailable"),
        (degrade.UNCLASSIFIED, "provider data unavailable"),
        (degrade.INTERNAL, "internal error"),
    ),
)
def test_error_vocabulary_is_fixed(code, expected) -> None:
    assert degrade.MESSAGES[code] == expected
    assert degrade.message(code) == expected
    assert set(degrade.MESSAGES) == {
        degrade.CREDENTIALS,
        degrade.AUTH,
        degrade.HTTP,
        degrade.TRANSPORT,
        degrade.SHAPE,
        degrade.STALE,
        degrade.ACTIVITY,
        degrade.UNCLASSIFIED,
        degrade.INTERNAL,
    }


@pytest.mark.parametrize("code", ("", "nonsense", None, 7, object()))
def test_an_unknown_classification_falls_back_to_the_vocabulary(code) -> None:
    assert degrade.message(code) in degrade.VOCABULARY


@pytest.mark.parametrize(
    "provider,error",
    (
        ("claude", LiveQuotaError("boom", code=degrade.AUTH)),
        ("codex", CodexLiveQuotaError("boom", code=degrade.AUTH)),
    ),
)
def test_a_classified_client_error_reports_its_classification(
    deployment, provider, error
) -> None:
    deployment.faults[provider] = error

    section = main._build_payload()[provider]

    assert section["source_error"] == degrade.MESSAGES[degrade.AUTH]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_an_unclassified_client_error_reports_the_generic_message(
    deployment, provider
) -> None:
    error = (
        LiveQuotaError("upstream changed")
        if provider == "claude"
        else CodexLiveQuotaError("upstream changed")
    )
    error.code = "not-a-classification"
    deployment.faults[provider] = error

    section = main._build_payload()[provider]

    assert section["source_error"] == degrade.MESSAGES[degrade.UNCLASSIFIED]
    assert "upstream changed" not in section["source_error"]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_an_undeclared_error_reports_an_internal_error(deployment, provider) -> None:
    deployment.faults[provider] = ZeroDivisionError("division by zero")

    section = main._build_payload()[provider]

    assert section["source_error"] == degrade.MESSAGES[degrade.INTERNAL]


# ─── The boundary does not trust the clients' return values either ───────────


class _ClientStub:
    credentials_path = Path("unused")

    def __init__(self, snapshot: object) -> None:
        self._snapshot = snapshot

    def get(self) -> object:
        return self._snapshot


def _window(percent: object, resets_at: object = None, detail: object = None):
    return SimpleNamespace(percent=percent, resets_at=resets_at, detail=detail)


class _FailingClientStub:
    credentials_path = Path("unused")

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def get(self) -> object:
        raise self._error


@pytest.mark.parametrize(
    ("provider", "error"),
    (
        ("claude", LiveQuotaError("x", code=degrade.STALE)),
        ("codex", CodexLiveQuotaError("x", code=degrade.STALE)),
    ),
)
def test_the_stale_classification_is_never_claimed_by_a_client(
    deployment, monkeypatch, provider, error
) -> None:
    """Only the refresher knows whether a source stopped being refreshed.

    A client tagging itself `stale` — by a copied constant or a future edit —
    would report "provider data is no longer being refreshed" about a source
    that is being refreshed on its cadence, and send an operator looking for a
    wedged thread that does not exist.
    """
    attribute = "_live" if provider == "claude" else "_codex"
    monkeypatch.setattr(main, attribute, _FailingClientStub(error))

    payload = main._build_payload()

    assert_payload_in_schema(payload)
    assert payload[provider]["source"] == "unavailable"
    assert payload[provider]["source_error"] != degrade.MESSAGES[degrade.STALE]
    assert payload[provider]["source_error"] == degrade.MESSAGES[degrade.UNCLASSIFIED]


@pytest.mark.parametrize(
    ("provider", "error"),
    (
        ("claude", LiveQuotaError("x", code=degrade.ACTIVITY)),
        ("codex", CodexLiveQuotaError("x", code=degrade.ACTIVITY)),
    ),
)
def test_the_activity_classification_is_never_served_as_a_quota_error(
    deployment, monkeypatch, provider, error
) -> None:
    """`activity` is diagnostic only, so a client cannot borrow its message.

    A failed activity read degrades to `last_activity: null` and says nothing in
    the payload. If a quota client tagged itself `activity` — by a copied
    constant or a future edit — "local activity reading unavailable" would
    appear as a quota `source_error` and describe the wrong subsystem.
    """
    attribute = "_live" if provider == "claude" else "_codex"
    monkeypatch.setattr(main, attribute, _FailingClientStub(error))

    payload = main._build_payload()

    assert_payload_in_schema(payload)
    assert payload[provider]["source"] == "unavailable"
    assert payload[provider]["source_error"] != degrade.MESSAGES[degrade.ACTIVITY]
    assert payload[provider]["source_error"] == degrade.MESSAGES[degrade.UNCLASSIFIED]


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        # A float artefact a hair outside the range is served as the legal value
        # it rounds to, rather than blacking the whole card out. Codex computes
        # `100.0 - percent_left`, so a percent_left of -1e-9 lands here.
        (100.0 - -1e-9, 100.0),
        (100.004, 100.0),
        (-0.0001, 0.0),
        (0.0, 0.0),
        (100.0, 100.0),
        (12.345, 12.35),
    ),
)
def test_a_percent_is_range_checked_on_the_value_it_serves(raw, expected) -> None:
    assert main._percent(raw) == expected


def test_a_percent_that_rounds_to_zero_is_not_negative_zero() -> None:
    """-0.0 passes the range check but renders differently on each side.

    The template's "%.1f" gives "-0.0%" and JavaScript's toFixed(1) gives
    "0.0", so serving it would make the first paint and the first SSE update
    disagree — the one thing the single serialization is there to prevent.
    `-0.0 == 0.0` in Python, so asserting equality cannot see this.
    """
    for raw in (-0.0001, -0.0, -0.004):
        served = main._percent(raw)
        assert served == 0.0
        assert math.copysign(1.0, served) == 1.0, f"{raw!r} served as negative zero"
        assert repr(served) == "0.0"
        assert f"{served:.1f}" == "0.0"


@pytest.mark.parametrize("raw", (100.01, -0.01, 600.0, -500.0, 1e9))
def test_a_percent_that_does_not_round_into_range_is_refused(raw) -> None:
    # Rounding must not become clamping: an upstream value that is genuinely
    # out of range is still a shape error.
    with pytest.raises(main._SchemaError):
        main._percent(raw)


OUT_OF_SCHEMA_PERCENTS = (
    float("nan"),
    float("inf"),
    float("-inf"),
    True,
    False,
    "12.5",
    -0.1,
    100.1,
    1e309,
    10**400,
    {},
    [],
    object(),
)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("percent", OUT_OF_SCHEMA_PERCENTS, ids=repr)
def test_an_out_of_schema_percent_degrades_the_section(
    deployment, monkeypatch, provider, percent
) -> None:
    attribute = "_live" if provider == "claude" else "_codex"
    monkeypatch.setattr(
        main,
        attribute,
        _ClientStub(
            SimpleNamespace(
                five_hour=_window(percent),
                seven_day=_window(10.0),
                seven_day_fable=_window(10.0),
                subscription_type="max",
                plan_type="pro",
            )
        ),
    )

    payload = main._build_payload()

    assert_payload_in_schema(payload)
    assert payload[provider]["source"] == "unavailable"
    assert payload[provider]["source_error"] == degrade.MESSAGES[degrade.SHAPE]


@pytest.mark.parametrize("percent", OUT_OF_SCHEMA_PERCENTS, ids=repr)
def test_an_out_of_schema_fable_percent_isolates_to_its_own_gauge(
    deployment, monkeypatch, percent
) -> None:
    monkeypatch.setattr(
        main,
        "_live",
        _ClientStub(
            SimpleNamespace(
                five_hour=_window(11.0),
                seven_day=_window(22.0),
                seven_day_fable=_window(percent),
                subscription_type="max",
            )
        ),
    )

    section = main._build_payload()["claude"]

    assert section["source"] == "live"
    assert [w["percent"] for w in section["windows"]] == [11.0, 22.0, None]


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "resets_at",
    (
        "2026-05-20T12:00:00Z",
        datetime(1, 1, 1),
        datetime(9999, 12, 31, tzinfo=timezone.utc),
        datetime(1969, 1, 1, tzinfo=timezone.utc),
        0,
        object(),
        float("nan"),
    ),
    ids=repr,
)
def test_an_out_of_schema_reset_date_becomes_null(
    deployment, monkeypatch, provider, resets_at
) -> None:
    attribute = "_live" if provider == "claude" else "_codex"
    monkeypatch.setattr(
        main,
        attribute,
        _ClientStub(
            SimpleNamespace(
                five_hour=_window(11.0, resets_at),
                seven_day=_window(22.0),
                seven_day_fable=_window(33.0),
                subscription_type="max",
                plan_type="pro",
            )
        ),
    )

    payload = main._build_payload()

    assert_payload_in_schema(payload)
    section = payload[provider]
    assert section["source"] == "live"
    assert section["windows"][0]["resets_at"] is None


@pytest.mark.parametrize(
    "plan",
    ("pro\ud800", "pro\x00\r\nX-Evil: 1", "p" * 5_000, 7, [], None, object(), ""),
    ids=repr,
)
def test_an_out_of_schema_plan_string_is_cleaned_or_dropped(
    deployment, monkeypatch, plan
) -> None:
    monkeypatch.setattr(
        main,
        "_codex",
        _ClientStub(
            SimpleNamespace(
                five_hour=_window(11.0), seven_day=_window(22.0), plan_type=plan
            )
        ),
    )

    payload = main._build_payload()

    assert_payload_in_schema(payload)
    assert payload["codex"]["source"] == "live"


def test_a_control_character_in_a_plan_string_is_scrubbed_not_just_bounded() -> None:
    """The scrubbing, not the length bound, is what makes the string safe.

    `_UNPRINTABLE` is the defence that stops a line terminator ending a
    JavaScript line inside the <script> block. A short hostile string satisfies
    every other part of the text rule, so only this asserts the substitution.
    """
    hostile = "pro\x00\r\nX-Evil: 1\u2028alert(1)"

    cleaned = main._text(hostile)

    assert cleaned == "pro   X-Evil: 1 alert(1)"
    assert SCHEMA_UNPRINTABLE.search(cleaned) is None
    # A string that is nothing but control characters scrubs to whitespace,
    # which is still truthy. Without the strip it would be served as a plan
    # name of three spaces instead of dropped.
    assert main._text("\x00\r\n") is None
    assert main._text("   ") is None


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_client_returning_no_snapshot_degrades_the_section(
    deployment, monkeypatch, provider
) -> None:
    attribute = "_live" if provider == "claude" else "_codex"
    monkeypatch.setattr(main, attribute, _ClientStub(None))

    payload = main._build_payload()

    assert_payload_in_schema(payload)
    assert payload[provider]["source"] == "unavailable"
    assert payload[provider]["source_error"] == degrade.MESSAGES[degrade.SHAPE]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_snapshot_missing_its_windows_degrades_to_no_values(
    deployment, monkeypatch, provider
) -> None:
    attribute = "_live" if provider == "claude" else "_codex"
    monkeypatch.setattr(main, attribute, _ClientStub(SimpleNamespace()))

    payload = main._build_payload()

    assert_payload_in_schema(payload)
    assert all(w["percent"] is None for w in payload[provider]["windows"])


# ─── An activity read degrades on its own ────────────────────────────────────


class _RaisingReader:
    data_dir = Path("unused")

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def snapshot(self):
        raise self._error


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "error",
    (
        RecursionError("maximum recursion depth exceeded"),
        OSError(5, "Input/output error"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        ValueError("year 99999 is out of range"),
        ZeroDivisionError("division by zero"),
    ),
    ids=repr,
)
def test_a_failing_activity_read_leaves_the_quota_section_live(
    deployment, monkeypatch, provider, error
) -> None:
    attribute = "_claude_activity" if provider == "claude" else "_codex_activity"
    monkeypatch.setattr(main, attribute, _RaisingReader(error))

    payload = main._build_payload()

    assert_payload_in_schema(payload)
    section = payload[provider]
    assert section["source"] == "live"
    assert section["last_activity"] is None
    assert section["data_root_exists"] is False


@pytest.mark.parametrize(
    "line",
    (
        "[" * 20_000 + "]" * 20_000,
        '{"timestamp": "+099999-01-01T00:00:00Z"}',
        '{"timestamp": ' + "9" * 400 + "}",
        '{"timestamp": {"nested": "value"}}',
        "\x00\xff not json at all",
    ),
    ids=repr,
)
def test_a_hostile_transcript_line_leaves_the_claude_section_live(
    deployment, line
) -> None:
    transcript = deployment.dirs["claude"] / "projects" / "p" / "session.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(line + "\n", encoding="utf-8")

    payload = main._build_payload()

    assert_payload_in_schema(payload)
    assert payload["claude"]["source"] == "live"
    assert payload["claude"]["last_activity"] is None


@pytest.mark.parametrize(
    "out_of_schema_snapshot",
    (
        SimpleNamespace(last_activity="2026-05-20T09:30:00Z", data_root_exists=True),
        SimpleNamespace(last_activity=datetime(9999, 1, 1), data_root_exists=True),
        SimpleNamespace(last_activity=0, data_root_exists="yes"),
        SimpleNamespace(),
        None,
    ),
    ids=repr,
)
def test_an_out_of_schema_activity_snapshot_becomes_null(
    deployment, monkeypatch, out_of_schema_snapshot
) -> None:
    monkeypatch.setattr(
        main,
        "_claude_activity",
        SimpleNamespace(
            data_dir=Path("unused"), snapshot=lambda: out_of_schema_snapshot
        ),
    )

    payload = main._build_payload()

    assert_payload_in_schema(payload)
    assert payload["claude"]["last_activity"] is None


# ─── The serializer is the last line ─────────────────────────────────────────


def test_the_serializer_refuses_a_payload_it_cannot_serialize_strictly() -> None:
    # Unreachable through the boundary; this asserts the last-ditch behaviour.
    for unserializable in (
        {"claude": float("nan")},
        {"claude": object()},
        {"claude": {float("inf")}},
    ):
        serialized = main._payload_json(unserializable)
        recovered = json.loads(serialized)
        # It is built from literals rather than by the boundary, so it is the
        # one section shape nothing else pins. A key added to _provider_section
        # and forgotten here would reach app.js as `undefined`.
        assert_payload_in_schema(recovered)
        assert recovered["claude"]["source"] == "unavailable"
        assert recovered["claude"]["source_error"] == degrade.MESSAGES[degrade.INTERNAL]
        assert recovered["codex"]["source"] == "unavailable"


def test_a_serializer_failure_is_logged_and_carries_no_exception_text(
    monkeypatch,
) -> None:
    # Both cards read "internal error" here. Without a log line there is no way
    # to tell a serializer fault from a genuine dual-provider outage.
    # Start from "nothing logged yet" rather than inheriting whatever the
    # previous test left in the once-per-state map.
    monkeypatch.setattr(main, "_logged_degrade", {})
    records = _captured_records(
        lambda: main._payload_json({"claude": f"unserializable {SECRET}", "x": object()})
    )

    assert any(
        "source=payload" in r.getMessage()
        and f"classification={degrade.INTERNAL}" in r.getMessage()
        for r in records
    )
    for record in records:
        assert SECRET not in record.getMessage()
        assert record.exc_info is None
        assert record.stack_info is None


def test_the_script_form_cannot_break_out_of_the_script_block() -> None:
    serialized = main._payload_json({"plan": "</script><script>alert(1)</script>"})
    script_form = main._payload_script_json(serialized)

    assert "<" not in script_form and ">" not in script_form and "&" not in script_form
    assert json.loads(script_form) == {"plan": "</script><script>alert(1)</script>"}


# ─── One serialization, three consumers ──────────────────────────────────────
#
# The endpoints are awaited directly rather than driven through a test client so
# the SSE frame can be read without waiting out a refresh interval.


# A host the app actually serves. `main.app` refuses every name outside
# `DASHBOARD_ALLOWED_HOSTS` with 403, so a scope built with `TestClient`'s
# `testserver` default would be a request the app would never answer — the
# helpers below would still return 200, because they await the handlers, but the
# fixture would be lying about what it stands for.
SERVED_HOST = "127.0.0.1:8765"

requires_default_hosts = pytest.mark.skipif(
    bool(os.environ.get(main.ALLOWED_HOSTS_ENV)),
    reason=f"{main.ALLOWED_HOSTS_ENV} is set in this shell, and the app read it at import",
)


def _request(path: str = "/") -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", SERVED_HOST.encode())],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8765),
            "app": main.app,
        }
    )


def _index_html() -> str:
    async def render() -> str:
        response = await main.index(_request("/"))
        assert response.status_code == 200
        return response.body.decode("utf-8")

    return asyncio.run(render())


def _usage_body() -> str:
    async def call() -> str:
        response = await main.api_usage()
        assert response.status_code == 200
        assert response.media_type == "application/json"
        return response.body.decode("utf-8")

    return asyncio.run(call())


def _first_sse_frame() -> str:
    class _ConnectedRequest:
        async def is_disconnected(self) -> bool:
            return False

    async def first_frame() -> str:
        response = await main.stream(_ConnectedRequest())
        frame = await anext(aiter(response.body_iterator))
        await response.body_iterator.aclose()
        return frame if isinstance(frame, str) else frame.decode("utf-8")

    return asyncio.run(first_frame())


def test_all_three_consumers_serve_the_one_serialization(deployment, monkeypatch) -> None:
    payload = main._build_payload()
    monkeypatch.setattr(main, "_build_payload", lambda: payload)
    serialized = assert_payload_in_schema(payload)

    assert _usage_body() == serialized
    assert _first_sse_frame() == f"data: {serialized}\n\n"
    expected = f"window.__INITIAL_PAYLOAD__ = {main._payload_script_json(serialized)};"
    assert expected in _index_html()


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "case", ("json-null", "percent-nan", "percent-true", "percent-huge-int")
)
def test_the_routes_answer_with_a_hostile_provider(deployment, provider, case) -> None:
    deployment.bodies[provider] = hostile_bodies(provider)[case]

    html = _index_html()
    body = _usage_body()

    assert "connecting…" in html
    assert "unavailable" in html
    served = json.loads(body)
    assert_payload_in_schema(served)
    assert served[provider]["source"] == "unavailable"
    # Every consumer reports the same thing; only the clock moves between calls.
    framed = json.loads(_first_sse_frame().removeprefix("data: ").strip())
    assert {k: v for k, v in framed.items() if k != "server_time"} == {
        k: v for k, v in served.items() if k != "server_time"
    }


@requires_default_hosts
@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("case", ("json-null", "percent-nan"))
def test_the_routes_answer_through_the_whole_stack(deployment, provider, case) -> None:
    """The acceptance criterion is about `GET /` and `/api/usage`, not about handlers.

    The helpers above await the route functions, which is what lets an SSE frame be
    read without waiting out a refresh interval — but it steps over the `Host`
    allow-list wrapped round the app, so on its own it would no longer prove a
    status code. This drives the served app end to end for the same hostile
    bodies.
    """
    deployment.bodies[provider] = hostile_bodies(provider)[case]
    # `raise_server_exceptions=False` so that a regression is reported as the 500 it
    # would be in the container, rather than re-raised into pytest's traceback: the
    # exception the boundary exists to contain is the one that quotes the bearer
    # token, and it must not reach a test's output either.
    client = TestClient(
        main.app, base_url=f"http://{SERVED_HOST}", raise_server_exceptions=False
    )

    page = client.get("/")
    usage = client.get("/api/usage")

    assert page.status_code == 200, page.text[:200]
    assert usage.status_code == 200, usage.text[:200]
    assert usage.headers["content-type"].startswith("application/json")
    payload = usage.json()
    assert_payload_in_schema(payload)
    assert payload[provider]["source"] == "unavailable"
    assert payload[provider]["source_error"] in {
        degrade.MESSAGES[code] for code in degrade.SERVABLE
    }
    assert SECRET not in page.text
    assert SECRET not in usage.text


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("case", sorted(transport_faults()))
def test_the_routes_answer_through_every_transport_fault(
    deployment, provider, case
) -> None:
    deployment.faults[provider] = transport_faults()[case]

    served = json.loads(_usage_body())
    assert_payload_in_schema(served)
    assert served[provider]["source"] == "unavailable"
    assert served[provider]["source_error"] == degrade.MESSAGES[
        transport_fault_codes()[case]
    ]
    assert "__INITIAL_PAYLOAD__" in _index_html()


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("case", sorted(hostile_hosts()))
def test_a_hostile_host_override_stays_in_schema(deployment, provider, case) -> None:
    deployment.set_host(provider, hostile_hosts()[case])

    payload = main._build_payload()

    assert_payload_in_schema(payload)
    assert payload[provider]["source"] == "unavailable"
    # A host the dashboard cannot build a request for, or cannot reach, is a
    # configuration problem, not a dashboard bug: "internal error" would send
    # the operator to the issue tracker instead of to their own .env.
    assert payload[provider]["source_error"] == degrade.MESSAGES[degrade.TRANSPORT]
    other = "codex" if provider == "claude" else "claude"
    assert payload[other]["source"] == "live"
    assert_payload_in_schema(json.loads(_usage_body()))
    assert SECRET not in _index_html()


# ─── What the boundary is allowed to log ─────────────────────────────────────


class _CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _captured_records(build) -> list[logging.LogRecord]:
    handler = _CapturingHandler()
    logger = logging.getLogger(main.__name__)
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        build()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    return handler.records


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("case", sorted(transport_faults()))
def test_the_log_carries_the_classification_and_nothing_the_exception_said(
    deployment, provider, case
) -> None:
    fault = transport_faults()[case]
    deployment.faults[provider] = fault

    records = _captured_records(main._build_payload)

    assert any(f"source={provider}.quota" in r.getMessage() for r in records)
    for record in records:
        message = record.getMessage()
        assert SECRET not in message
        assert str(fault) not in message
        # No traceback either: a rendered traceback would include the chained
        # exception, and that is where the bearer token is.
        assert record.exc_info is None
        assert record.stack_info is None


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_hostile_credential_is_never_logged(deployment, provider) -> None:
    deployment.write_credentials(
        provider, hostile_credentials(provider)["token-with-crlf"]
    )

    records = _captured_records(main._build_payload)

    assert records
    for record in records:
        assert SECRET not in record.getMessage()
        assert "X-Evil" not in record.getMessage()


def test_a_failing_activity_read_is_logged_as_its_own_source(
    deployment, monkeypatch
) -> None:
    monkeypatch.setattr(
        main, "_claude_activity", _RaisingReader(RecursionError("too deep"))
    )

    records = _captured_records(main._build_payload)
    messages = [r.getMessage() for r in records]

    assert any(
        f"source=claude.last_activity classification={degrade.ACTIVITY}" in m
        for m in messages
    )
    assert all("too deep" not in m for m in messages)
    assert main._build_payload()["claude"]["source"] == "live"


def test_a_healthy_payload_logs_nothing(deployment) -> None:
    assert _captured_records(main._build_payload) == []


def test_a_source_that_stays_broken_is_logged_once(deployment) -> None:
    deployment.faults["claude"] = transport_faults()["remote-disconnected"]

    first = _captured_records(main._build_payload)
    repeats = _captured_records(lambda: [main._build_payload() for _ in range(5)])

    assert len(first) == 1
    assert repeats == []


def test_a_changed_classification_is_logged_again(deployment) -> None:
    deployment.faults["claude"] = transport_faults()["remote-disconnected"]
    assert len(_captured_records(main._build_payload)) == 1

    deployment.faults["claude"] = None
    deployment.write_credentials("claude", hostile_credentials("claude")["no-token"])
    records = _captured_records(main._build_payload)

    assert len(records) == 1
    assert "classification=credentials" in records[0].getMessage()


def test_a_recovered_source_is_logged_once(deployment) -> None:
    deployment.faults["claude"] = transport_faults()["remote-disconnected"]
    assert len(_captured_records(main._build_payload)) == 1

    deployment.faults["claude"] = None
    records = _captured_records(main._build_payload)

    assert [r.getMessage() for r in records] == ["recovered source=claude.quota"]
    assert _captured_records(main._build_payload) == []


@pytest.mark.parametrize(
    "hostile_value",
    ("max\ud800", "max\udfff", "\ud800\ud800"),
    ids=repr,
)
def test_an_unencodable_string_that_got_past_the_boundary_still_answers(
    deployment, monkeypatch, hostile_value
) -> None:
    """The last line: `ensure_ascii` hides a lone surrogate, so prove encodability.

    `_text()` replaces surrogates, so this payload is unreachable through the
    boundary. It is what `GET /` would have to render if it ever were reachable:
    the template's context is parsed back out of the serialized form, and a lone
    surrogate cannot be encoded as UTF-8.
    """
    payload = main._build_payload()
    payload["claude"]["subscription_type"] = hostile_value
    monkeypatch.setattr(main, "_build_payload", lambda: payload)

    serialized = main._payload_json(payload)
    served = json.loads(serialized)

    serialized.encode("utf-8")
    assert served["claude"]["subscription_type"] is None
    assert served["claude"]["source_error"] == degrade.MESSAGES[degrade.INTERNAL]
    # The degraded payload stamps its own clock, so compare everything else.
    from_route = json.loads(_usage_body())
    assert {k: v for k, v in from_route.items() if k != "server_time"} == {
        k: v for k, v in served.items() if k != "server_time"
    }
    assert "__INITIAL_PAYLOAD__" in _index_html()
