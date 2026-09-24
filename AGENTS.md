# AGENTS.md

Guidance for Codex and other agentic coding tools working in this repo.

## Project Summary

codervis is a local FastAPI dashboard for Claude Code and Codex CLI quota
usage. It runs in Docker, bind-mounts the host `~/.claude` and `~/.codex`
directories read-only, reads each tool's existing OAuth token, and pushes live
usage snapshots to the browser via Server-Sent Events.

The upstream quota endpoints are undocumented and can change without warning.
Keep failures contained: Claude and Codex should render unavailable states
instead of synthesizing quota usage.

## Commands

```bash
docker compose up --build -d
docker compose logs -f codervis
docker compose down
curl http://localhost:8765/healthz
curl http://localhost:8765/api/usage
python -m pytest
python -m py_compile app/main.py app/quota.py app/activity_gate.py app/claude_activity.py app/codex_quota.py app/codex_activity.py app/refresh.py app/budget.py app/egress.py app/ingress.py
docker compose exec codervis python -m app.egress check
```

Install test dependencies with `python -m pip install -r requirements-dev.txt`.
The pytest suite stubs live quota clients and uses temporary credential/activity
directories; it must not call upstream quota endpoints or read host tokens.

## Implementation Notes

- `app/main.py` owns the FastAPI routes, SSE stream, payload assembly, and the
  lifespan that starts one refresher per source. It also owns the one boundary
  every independently sourced part of the payload passes through
  (`_provider_section()`), the payload schema that boundary enforces
  (`_percent`, `_iso`, `_text`), and the single strict serialization
  (`_payload_json()`) that `/api/usage`, the SSE frames and the template's
  initial payload all share. It owns the `Host` allow-list
  (`DASHBOARD_ALLOWED_HOSTS`) that decides which clients the dashboard answers
  at all, too. Keep that check wrapped around the whole app, and pure ASGI:
  per-route checks miss `/static`, and `BaseHTTPMiddleware` would buffer the SSE
  stream. The boundary does no I/O: it reads the last snapshot each refresher
  published. Never call a quota client or an activity reader from a request
  handler — the refresher's cadence is the only thing that bounds how often a
  credential is read or a token is sent upstream.
- `app/degrade.py` owns the fixed vocabulary the boundary reports failures with.
  `source_error` must always be one of those strings: never `str(exc)`, never a
  repr. It is served unauthenticated, and an exception raised while an upstream
  request is being built carries the bearer token. A source that has gone stale
  is reported through it like any other failure, so how old a snapshot is never
  reaches the payload.
- `app/refresh.py` owns `SourceRefresher`: one daemon thread per source, a
  fixed cadence, and a published snapshot that records failure as well as
  success. `refresh_once()` must keep catching `Exception` whole; an escape
  would kill the worker and freeze that source. Handlers read `current()`, not
  `snapshot()`: a read with no deadline can stop a thread advancing without
  ever failing it, and `current()` is what turns a success that has gone stale
  into `unavailable` instead of old numbers labelled `live`.
- `app/budget.py` owns what a single payload-feeding read may cost —
  `read_capped()` for upstream bodies (deadline and byte cap),
  `bounded_lines()` for transcript records (per-record and per-file caps, under
  the scan's own deadline), `read_text_capped()` for the credential files (byte
  cap only; it says there why it has no deadline). Any new read of something
  someone else writes gets a byte cap always, and a deadline unless it
  provably cannot take one — the credential read's exemption is not a
  precedent, and a read that genuinely cannot be timed must sit in a refresher
  whose `stale_after_seconds` covers it, so that a hang shows as `unavailable`
  rather than as old numbers. Each new knob goes in the "Read budgets" table
  in `README.md`, plus `.env.example` and `docker-compose.yml`.
- `app/quota.py` owns the Claude live client and must convert any upstream,
  auth, parse, or file-read failure into `LiveQuotaError`, including a
  `BudgetExceeded` from a body that is too large or too slow.
- `app/activity_gate.py` owns what an activity reader may reach: the
  allow-listed subtrees of that reader's own data root, the operation it was
  granted (`STAT` for Codex, `STAT | READ` for Claude), and the rule that no
  link is ever followed — `lstat` on every component below the root,
  `O_NOFOLLOW` on every read. It is the only way either reader reaches the
  filesystem; keep it that way, and put a new reader's paths on its allow-list
  rather than opening them directly. It records what it admitted and refused
  per scan, which is what the tests assert on. A refusal is not a failure: the
  path contributes no timestamp and the scan carries on. `PathRefused` carries
  a reason and never a path, because an operator's project directory names are
  what the old oracle leaked.
- `app/claude_activity.py` owns Claude last-activity reporting. It reads only
  project transcript timestamps and must not compute quota or fallback usage
  statistics. That it cannot read `.credentials.json` is the gate's doing, not
  the reader's: its `ActivityGate` admits the `projects` subtree only.
- `app/codex_quota.py` owns the Codex live client and must convert any failure
  into `CodexLiveQuotaError` so the UI can show `source: "unavailable"`. One
  deadline spans both candidate paths; do not give the second attempt a fresh
  timeout.
- `app/codex_activity.py` owns Codex last-activity reporting. It derives
  timestamps from safe file metadata only; `auth.json` is off its gate's
  allow-list and session *contents* are off its granted operations, so neither
  is reachable from here rather than merely avoided here.
- `app/egress.py` is the allow-listing `CONNECT` proxy that is the dashboard
  container's only route out; `app/ingress.py` publishes the dashboard's port,
  because the dashboard sits on an internal-only network. Keep that container
  off every non-internal network and free of `ports:`, and keep the published
  port on `DASHBOARD_BIND`, which defaults to loopback.
- `app/static/app.js` is the single source of truth for gauge color calculation
  on both initial paint and SSE updates.
- `tests/` contains automated coverage for parser tolerance, unavailable
  states, disabled Codex state, and safe activity-reader boundaries.
  `tests/test_activity_readers.py` is the activity boundary: it asserts the set
  of paths each reader touched and the operations it performed, on the gate's
  own record, plus the process's real filesystem calls during a scan. It does
  not assert the timestamp a reader returned, because that is what a reader
  reading credential files returns too — which is how a reader that opened
  `.credentials.json`, `auth.json`, `history.jsonl` and a session file once
  passed the whole suite. Its fixtures are parseable credential files and
  planted links out of the tree, so a regression changes behaviour.
  `tests/test_activity_gate.py` pins the gate's own refusals.
  `tests/test_payload_contract.py` is the payload contract: it runs both live
  clients through one matrix of transport faults, hostile response bodies and
  hostile credential files, and asserts the payload always matches the schema
  and always serializes. Add cases there for both providers, not one; a case
  naming a shape one provider cannot have must skip explicitly for the other,
  so the gap shows up in the test report. `tests/test_payload_budget.py` pins
  the read budgets, the refresher and its staleness bound, and the property
  that SSE frames cause no upstream calls. Add cases there when you change what
  a read is allowed to cost.

## Safety Rules

- Never log or print OAuth tokens from `.credentials.json`, `auth.json`, `.env`,
  debug captures, or Docker output. That includes indirectly: do not let an
  exception's text reach the payload, a log line, or a test's output, because
  the exception raised for a malformed header quotes the whole header value.
- Preserve read-only bind mounts for `/data/claude` and `/data/codex`.
- Preserve the egress bound: the `codervis` service joins internal networks
  only, and `DEFAULT_ALLOW` in `app/egress.py` names only hosts the live
  clients call.
- Do not add token refresh or OAuth flow logic here; the host CLIs own that.
- Do not multiply live utilization values by 100. The live APIs are expected
  to already be on a 0-100 scale.
- Keep parser changes tolerant of alternate field names and missing data.
- When changing Docker or environment behavior, keep `README.md`,
  `.env.example`, `docker-compose.yml`, and `CLAUDE.md` in sync.
