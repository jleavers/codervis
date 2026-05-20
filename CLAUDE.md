# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local FastAPI dashboard that displays **Claude Code** and **Codex CLI**
5-hour and weekly quota utilization as colour-shifting meters, side-by-side.
Runs as a single Docker container, bind-mounts the host's `~/.claude` and
`~/.codex` directories read-only, and authenticates upstream using each
agent's own stored OAuth token.

User-facing setup, env vars, and troubleshooting live in `README.md`.

## Architecture

The JSON payload is keyed by provider — `claude` and `codex` — assembled
in `app/main.py:_build_payload()` from two independent sections.

### Claude (`app/main.py:_claude_section()`)

Live-only by design:

- **`app/quota.py`** — `LiveQuotaClient` reads
  `$CLAUDE_DATA_DIR/.credentials.json` on every call, extracts
  `claudeAiOauth.accessToken`, and hits
  `GET https://claude.ai/api/oauth/usage` with `Authorization: Bearer …`.
  Response has `five_hour.utilization` and `seven_day.utilization` as
  percentages already — no token-count maths needed. Results are cached
  in-memory for `QUOTA_CACHE_TTL_SECONDS` so all SSE clients share one
  upstream fetch.
- On any failure (`LiveQuotaError`) the section returns
  `source: "unavailable"` with `percent: null` for both gauges and the
  error string surfaced to the UI. The app does not estimate quota usage
  locally.
- **`app/claude_activity.py`** — `ClaudeActivityReader` reports Claude
  `last_activity` from timestamp fields in local project transcript files.
  It does not read `.credentials.json`, inspect usage fields, or influence
  quota.

### Codex (`app/main.py:_codex_section()`)

Live-only by design.

- **`app/codex_quota.py`** — `CodexLiveQuotaClient` reads
  `$CODEX_DATA_DIR/auth.json` on every call, extracts
  `tokens.access_token` and `tokens.account_id`, and hits
  `GET https://chatgpt.com/backend-api/wham/usage` with
  `Authorization: Bearer …` and `ChatGPT-Account-Id: …`. The response
  shape is parsed defensively — primary/secondary windows may appear either
  at the top level or under `rate_limit` (`primary_window` / `five_hour`,
  `secondary_window` / `weekly` / `seven_day`) and percentages come from
  whichever of `utilization` / `percent_used` / `used_percent` /
  `percent_left` / `remaining_percent` is present.
- On any failure (`CodexLiveQuotaError`) the section returns
  `source: "unavailable"` with `percent: null` for both gauges and the
  error string surfaced to the UI. The frontend dims the panel and
  shows the error rather than synthesizing fake numbers.
- **`app/codex_activity.py`** — `CodexActivityReader` reports Codex
  `last_activity` from safe local file metadata only: `history.jsonl`,
  `session_index.jsonl`, and files under `sessions/` and
  `archived_sessions/`. It does not read `auth.json` or session contents,
  and it does not influence quota.
- If `CODEX_ENABLED` is falsy, `_codex` is `None` and the section
  returns `source: "disabled"` (panel still rendered but dimmed).

The frontend (`app/static/app.js`) shows each provider's source state
as a chip in its column header. The SSE loop is in `main.py:stream()`.
The frontend keeps relative-time labels alive between server pushes
via a 1-second `setInterval`.

## Load-bearing assumption: both live endpoints are undocumented

Neither endpoint is part of its vendor's public API.
- `/api/oauth/usage` was discovered by running
  `claude --debug-file path -d api` and grepping for `fetchUtilization`.
- `/backend-api/wham/usage` was reverse-engineered from the `codex-rs`
  backend client (also referenced as `/backend-api/codex/usage` in
  some builds).

Either can change or disappear at any time. Both panels are allowed to
degrade visibly.
**Do not assume the endpoint shapes are stable**:
- in `quota.py`, preserve the "any failure → `LiveQuotaError` →
  `unavailable` state" contract;
- in `codex_quota.py`, preserve the "any failure →
  `CodexLiveQuotaError` → `unavailable` state" contract — and keep
  field-name parsing tolerant (`_pick()` / the cascading checks in
  `_window()`).

## Commands

```bash
# Build & run
docker compose up --build -d
docker compose logs -f codervis
docker compose down

# Health check (no auth needed)
curl http://localhost:8765/healthz

# One-shot JSON snapshot (same payload SSE pushes)
curl http://localhost:8765/api/usage
```

No automated test suite exists. Smoke-tests during development are
done in-process via FastAPI's `TestClient` and direct module imports —
see the patterns in the conversation history if you need to re-run them.

## Local dev gotchas

- **Windows + Docker Compose**: `~` does not expand in bind-mount paths.
  The compose file's `${CLAUDE_HOME:-~/.claude}` default only works on
  Linux/macOS; Windows users must set `CLAUDE_HOME` explicitly in `.env`
  (forward slashes are fine: `C:/Users/name/.claude`).
- The credentials files are bind-mounted read-only at
  `/data/claude/.credentials.json` and `/data/codex/auth.json` inside
  the container. Both CLIs refresh their access tokens on the host;
  the dashboard re-reads the files on each upstream call so it rides
  along on those refresh cadences. There is no token-refresh logic in
  this repo.

## When changing the dashboard

- The colour ramp (lime → amber → coral) is computed in
  `app/static/app.js:colorFor()` as HSL — hue glides 90° → 45° at 60%,
  then 45° → 5° to 100%. The server-rendered initial paint and the
  SSE-driven updates both go through this function, so keep them in
  sync if you change the curve.
- Percentage values are floats 0–100 from the upstream API. Never
  multiply by 100 in either live path.
