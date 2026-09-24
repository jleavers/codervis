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
python -m py_compile app/main.py app/quota.py app/claude_activity.py app/codex_quota.py app/codex_activity.py app/refresh.py app/budget.py app/egress.py app/ingress.py
docker compose exec codervis python -m app.egress check
```

Install test dependencies with `python -m pip install -r requirements-dev.txt`.
The pytest suite stubs live quota clients and uses temporary credential/activity
directories; it must not call upstream quota endpoints or read host tokens.

## Implementation Notes

- `app/main.py` owns the FastAPI routes, SSE stream, payload assembly, the
  lifespan that starts one refresher per source, and the `Host` allow-list
  (`DASHBOARD_ALLOWED_HOSTS`) that decides which clients the dashboard answers
  at all. Keep that check wrapped around the whole app, and pure ASGI: per-route
  checks miss `/static`, and `BaseHTTPMiddleware` would buffer the SSE stream.
  `_build_payload()` does no I/O: it reads the last published snapshot. Never
  call a quota client or an activity reader from a request handler — the
  refresher's cadence is the only thing that bounds how often a credential is
  read or a token is sent upstream.
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
  someone else writes gets the same treatment, and its knob goes in the "Read
  budgets" table in `README.md`, plus `.env.example` and `docker-compose.yml`.
- `app/quota.py` owns the Claude live client and must convert any upstream,
  auth, parse, or file-read failure into `LiveQuotaError`, including a
  `BudgetExceeded` from a body that is too large or too slow.
- `app/claude_activity.py` owns Claude last-activity reporting. It should read
  only project transcript timestamps, must not read `.credentials.json`, and
  must not compute quota or fallback usage statistics.
- `app/codex_quota.py` owns the Codex live client and must convert any failure
  into `CodexLiveQuotaError` so the UI can show `source: "unavailable"`. One
  deadline spans both candidate paths; do not give the second attempt a fresh
  timeout.
- `app/codex_activity.py` owns Codex last-activity reporting. It should derive
  timestamps from safe file metadata only and must not read `auth.json` or
  session contents.
- `app/egress.py` is the allow-listing `CONNECT` proxy that is the dashboard
  container's only route out; `app/ingress.py` publishes the dashboard's port,
  because the dashboard sits on an internal-only network. Keep that container
  off every non-internal network and free of `ports:`, and keep the published
  port on `DASHBOARD_BIND`, which defaults to loopback.
- `app/static/app.js` is the single source of truth for gauge color calculation
  on both initial paint and SSE updates.
- `tests/` contains automated coverage for parser tolerance, unavailable
  states, disabled Codex state, safe activity-reader boundaries, and — in
  `tests/test_payload_budget.py` — the read budgets, the refresher, and the
  property that SSE frames cause no upstream calls. Add cases there when you
  change what a read is allowed to cost.

## Safety Rules

- Never log or print OAuth tokens from `.credentials.json`, `auth.json`, `.env`,
  debug captures, or Docker output. `source_error` is served unauthenticated:
  surface a client's own error type, but name any other exception by type
  only, because one raised while a request is built can quote the header.
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
