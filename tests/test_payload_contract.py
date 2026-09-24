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
import math
import socket
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Request

from app import degrade, main
from app.claude_activity import ClaudeActivityReader
from app.codex_activity import CodexActivityReader
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

    def read(self) -> bytes:
        if self._read_fault is not None:
            raise self._read_fault
        return self._body

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

        for provider, path in self.dirs.items():
            path.mkdir(parents=True, exist_ok=True)
            self.write_credentials(provider, healthy_credentials(provider))

        monkeypatch.setattr(
            main,
            "_live",
            LiveQuotaClient(self.dirs["claude"], host=CLAUDE_HOST, cache_ttl_seconds=0),
        )
        monkeypatch.setattr(
            main,
            "_codex",
            CodexLiveQuotaClient(
                self.dirs["codex"], host=CODEX_HOST, cache_ttl_seconds=0
            ),
        )
        monkeypatch.setattr(
            main,
            "_claude_activity",
            ClaudeActivityReader(self.dirs["claude"], cache_ttl_seconds=0),
        )
        monkeypatch.setattr(
            main,
            "_codex_activity",
            CodexActivityReader(self.dirs["codex"], cache_ttl_seconds=0),
        )
        monkeypatch.setattr(urllib.request, "urlopen", self._urlopen)

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
        provider = "claude" if CLAUDE_HOST in request.full_url else "codex"
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


def _assert_window_in_schema(window: dict) -> None:
    assert set(window) == {"name", "label", "percent", "resets_at", "detail"}
    assert isinstance(window["name"], str)
    assert isinstance(window["label"], str)
    percent = window["percent"]
    if percent is not None:
        assert isinstance(percent, float) and not isinstance(percent, bool)
        assert math.isfinite(percent)
        assert main.MIN_PERCENT <= percent <= main.MAX_PERCENT
    for field in ("resets_at", "detail"):
        value = window[field]
        assert value is None or isinstance(value, str)
    if window["resets_at"] is not None:
        moment = datetime.fromisoformat(window["resets_at"])
        assert main.MIN_DATE <= moment <= main.MAX_DATE


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
        plan = section["subscription_type"]
        assert plan is None or (
            isinstance(plan, str) and 0 < len(plan) <= main.MAX_TEXT_CHARS
        )
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

    serialized = assert_payload_in_schema(main._build_payload())

    # The other provider is a separate source and stays live.
    other = "codex" if provider == "claude" else "claude"
    assert main._build_payload()[other]["source"] == "live", serialized


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("case", sorted(transport_faults()))
def test_transport_fault_stays_in_schema(deployment, provider, case) -> None:
    deployment.faults[provider] = transport_faults()[case]

    payload = main._build_payload()

    assert_payload_in_schema(payload)
    assert payload[provider]["source"] == "unavailable"
    other = "codex" if provider == "claude" else "claude"
    assert payload[other]["source"] == "live"


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("case", sorted(transport_faults()))
def test_fault_raised_from_read_stays_in_schema(deployment, provider, case) -> None:
    deployment.read_faults[provider] = transport_faults()[case]

    payload = main._build_payload()

    assert_payload_in_schema(payload)
    assert payload[provider]["source"] == "unavailable"


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
        assert recovered["claude"]["source"] == "unavailable"
        assert recovered["claude"]["source_error"] == degrade.MESSAGES[degrade.INTERNAL]
        assert recovered["codex"]["source"] == "unavailable"


def test_the_script_form_cannot_break_out_of_the_script_block() -> None:
    serialized = main._payload_json({"plan": "</script><script>alert(1)</script>"})
    script_form = main._payload_script_json(serialized)

    assert "<" not in script_form and ">" not in script_form and "&" not in script_form
    assert json.loads(script_form) == {"plan": "</script><script>alert(1)</script>"}


# ─── One serialization, three consumers ──────────────────────────────────────
#
# The endpoints are awaited directly rather than driven through a test client so
# the SSE frame can be read without waiting out a refresh interval.


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
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
            "app": main.app,
        }
    )


def _index_html() -> str:
    async def render() -> str:
        response = await main.index(_request("/"))
        return response.body.decode("utf-8")

    return asyncio.run(render())


def _usage_body() -> str:
    async def call() -> str:
        response = await main.api_usage()
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


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("case", sorted(transport_faults()))
def test_the_routes_answer_through_every_transport_fault(
    deployment, provider, case
) -> None:
    deployment.faults[provider] = transport_faults()[case]

    assert_payload_in_schema(json.loads(_usage_body()))
    assert "__INITIAL_PAYLOAD__" in _index_html()
