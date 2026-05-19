# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local FastAPI dashboard that displays Claude Code's 5-hour and weekly
quota utilization as colour-shifting meters. Runs as a single Docker
container, bind-mounts the host's `~/.claude` directory read-only, and
authenticates upstream using the OAuth token Claude Code already stores
there.

User-facing setup, env vars, and troubleshooting live in `README.md`.

## Architecture

The interesting part is the **dual data source with automatic fallback**,
orchestrated in `app/main.py:_build_payload()`:

1. **Live path (`app/quota.py`)** — `LiveQuotaClient` reads
   `$CLAUDE_DATA_DIR/.credentials.json` on every call, extracts
   `claudeAiOauth.accessToken`, and hits
   `GET https://claude.ai/api/oauth/usage` with `Authorization: Bearer …`.
   Response has `five_hour.utilization` and `seven_day.utilization` as
   percentages already — no token-count maths needed. Results are cached
   in-memory for `QUOTA_CACHE_TTL_SECONDS` so all SSE clients share one
   upstream fetch.

2. **Fallback path (`app/usage.py`)** — `UsageReader` walks
   `$CLAUDE_DATA_DIR/projects/*/*.jsonl`, extracts `(timestamp, tokens)`
   tuples from any line where `type=="assistant"` and `message.usage` is
   present, sums `input + output + cache_creation + cache_read` per
   message, then computes rolling 5-hour and 7-day sums. Tokens are
   compared against the `FIVE_HOUR_TOKEN_LIMIT` / `WEEKLY_TOKEN_LIMIT`
   env vars to produce a percentage. Per-file parse results are cached
   keyed by `(mtime, size)` so quiescent transcripts aren't reparsed
   every tick.

`_build_payload()` always runs the fallback reader (it's also the source
of the "last activity" footer field), then attempts the live call. On
`LiveQuotaError`, it serves the fallback percentages and reports
`source: "fallback"` in the JSON. The frontend (`app/static/app.js`)
shows which source is active and tints the footer chip accordingly.

The SSE loop is in `main.py:stream()`. The frontend keeps relative-time
labels alive between server pushes via a 1-second `setInterval`.

## Load-bearing assumption: the live endpoint is undocumented

`/api/oauth/usage` is not part of Anthropic's public API. It was
discovered by running `claude --debug-file path -d api` and grepping for
`fetchUtilization`. If Anthropic changes or removes it, the live path
will break — the fallback is precisely there to keep the dashboard
working in that case. **Do not assume the endpoint shape is stable**:
when modifying `quota.py`, preserve the "any failure → `LiveQuotaError`
→ fallback" contract.

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

- **Python 3.14 + Jinja2** has an LRU-cache bug that crashes
  `TemplateResponse` with `TypeError: cannot use 'tuple' as a dict key`.
  The Docker image pins `python:3.12-slim` and is unaffected. If you
  must run uvicorn directly on a 3.14 host, set
  `templates.env.cache = None` to work around it.
- **Windows + Docker Compose**: `~` does not expand in bind-mount paths.
  The compose file's `${CLAUDE_HOME:-~/.claude}` default only works on
  Linux/macOS; Windows users must set `CLAUDE_HOME` explicitly in `.env`
  (forward slashes are fine: `C:/Users/name/.claude`).
- The credentials file is bind-mounted read-only at
  `/data/claude/.credentials.json` inside the container. Claude Code
  refreshes the access token on the host; the dashboard re-reads the
  file on each upstream call so it rides along on that refresh cadence.
  There is no token-refresh logic in this repo.

## When changing the dashboard

- The colour ramp (lime → amber → coral) is computed in
  `app/static/app.js:colorFor()` as HSL — hue glides 90° → 45° at 60%,
  then 45° → 5° to 100%. The server-rendered initial paint and the
  SSE-driven updates both go through this function, so keep them in
  sync if you change the curve.
- Percentage values are floats 0–100 from the upstream API. Never
  multiply by 100 in either path — the fallback also returns 0–100
  via `WindowStat.percent`.
