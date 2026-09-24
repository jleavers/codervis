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
python -m py_compile app/main.py app/quota.py app/claude_activity.py app/codex_quota.py app/codex_activity.py app/egress.py app/ingress.py
docker compose exec codervis python -m app.egress check
```

Install test dependencies with `python -m pip install -r requirements-dev.txt`.
The pytest suite stubs live quota clients and uses temporary credential/activity
directories; it must not call upstream quota endpoints or read host tokens.

## Implementation Notes

- `app/main.py` owns the FastAPI routes, SSE stream, and payload assembly. It
  also owns the one boundary every independently sourced part of the payload
  passes through (`_provider_section()`), the payload schema that boundary
  enforces (`_percent`, `_iso`, `_text`), and the single strict serialization
  (`_payload_json()`) that `/api/usage`, the SSE frames and the template's
  initial payload all share. It owns the `Host` allow-list
  (`DASHBOARD_ALLOWED_HOSTS`) that decides which clients the dashboard answers
  at all, too. Keep that check wrapped around the whole app, and pure ASGI:
  per-route checks miss `/static`, and `BaseHTTPMiddleware` would buffer the SSE
  stream.
- `app/degrade.py` owns the fixed vocabulary the boundary reports failures with.
  `source_error` must always be one of those strings: never `str(exc)`, never a
  repr. It is served unauthenticated, and an exception raised while an upstream
  request is being built carries the bearer token.
- `app/quota.py` owns the Claude live client and must convert any upstream,
  auth, parse, or file-read failure into `LiveQuotaError`.
- `app/claude_activity.py` owns Claude last-activity reporting. It should read
  only project transcript timestamps, must not read `.credentials.json`, and
  must not compute quota or fallback usage statistics.
- `app/codex_quota.py` owns the Codex live client and must convert any failure
  into `CodexLiveQuotaError` so the UI can show `source: "unavailable"`.
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
  states, disabled Codex state, and safe activity-reader boundaries.
  `tests/test_payload_contract.py` is the payload contract: it runs both live
  clients through one matrix of transport faults, hostile response bodies and
  hostile credential files, and asserts the payload always matches the schema
  and always serializes. Add cases there for both providers, not one; a case
  naming a shape one provider cannot have must skip explicitly for the other,
  so the gap shows up in the test report.

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
