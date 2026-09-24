# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local FastAPI dashboard that displays **Claude Code** and **Codex CLI** quota
utilization as colour-shifting meters. Runs as a Docker container that
bind-mounts the host's `~/.claude` and `~/.codex` data directories read-only
and authenticates upstream using each agent's own stored credential. Two
gateway services from the same image bound its network: see "Network
boundary" below.

User-facing setup, env vars, and troubleshooting live in `README.md`.

## Architecture

The JSON payload is keyed by provider — `claude` and `codex` — assembled in
`app/main.py:_build_payload()` from two independent sections.

### Payload I/O runs in refreshers, never in a request

`_build_payload()` does **no I/O**. It reads the last snapshot published by
one refresher per source (`app/refresh.py`), and `/`, `/api/usage` and every
`/api/stream` tick read that same snapshot. Four refreshers — Claude quota,
Codex quota, and each activity reader — are started and stopped by the app's
lifespan in `app/main.py`, each on its own daemon thread on a fixed interval.

This is load-bearing, so keep it whole:

- **Never call a quota client or an activity reader from a handler.** The
  cadence in the refresher is the *only* thing that decides how often a
  credential is read or a token is sent upstream. A call from a handler puts
  that back in the hands of whoever sends requests.
- **A refresher publishes failure exactly as it publishes success**, so a
  failing provider is retried on the cadence rather than on every tick.
  `refresh_once()` catches `Exception` whole because an escape kills the
  worker thread and freezes that source at its last snapshot forever.
- **Every payload-feeding read has a deadline and a byte cap** (`app/budget.py`)
  — the upstream body, the credential file, and each transcript file. Their
  knobs are the "Read budgets" table in `README.md`, which is the one place
  they are listed; the cadence knobs are in the table above it.
  urllib's timeout is per socket operation, so a sender that keeps trickling
  renews it indefinitely; only `read_capped()`'s total deadline ends that, and
  `bounded_lines()` is what stops one unterminated transcript record growing
  until `MemoryError`. If a new read is added, budget it the same way.
- `source_error` surfaces the client's own error type as before. Anything else
  is named by *type only* — an exception raised while an upstream request is
  built can carry the bearer token in its message.

Each section exposes a `windows` list (rather than fixed `five_hour` /
`seven_day` keys) so providers can report differently-shaped quota windows.
Each window dict is `{name, label, percent, resets_at, detail}` — `name` is
the gauge's DOM-id slot, `label` is the display heading, and `detail` is an
optional short string shown under the meter (neither provider sets it today).
The template (`index.html`) and `app.js` iterate `windows` generically.

### Claude (`app/main.py:_claude_section()`)

Live-only by design:

- **`app/quota.py`** — `LiveQuotaClient` reads
  `$CLAUDE_DATA_DIR/.credentials.json` on every call, extracts
  `claudeAiOauth.accessToken`, and hits
  `GET https://claude.ai/api/oauth/usage` with `Authorization: Bearer …`.
  Response has `five_hour.utilization` and `seven_day.utilization` as
  percentages already. Its optional `limits` list can also contain a
  `weekly_scoped` entry whose `scope.model.display_name` identifies Fable;
  that entry's `percent` is also already on a 0–100 scale. No token-count
  maths is needed. `get()` always fetches and caches nothing: its refresher
  owns the cadence (`QUOTA_REFRESH_INTERVAL_SECONDS`, or the older
  `QUOTA_CACHE_TTL_SECONDS`), so every SSE client shares one upstream fetch
  and none of them can cause another.
- `_claude_section()` always emits a stable `seven_day_fable` (“Weekly Window
  (Fable)”) slot so initial unavailable/missing data can recover through SSE.
  A missing or malformed optional Fable entry produces `percent: null` only
  for that gauge and does not make the Claude section unavailable.
- On any failure (`LiveQuotaError`) the section returns
  `source: "unavailable"` with `percent: null` for all three gauges and the
  error string surfaced to the UI. The app does not estimate quota usage
  locally.
- **`app/claude_activity.py`** — `ClaudeActivityReader` reports Claude
  `last_activity` from timestamp fields in local project transcript files.
  It does not read `.credentials.json`, inspect usage fields, or influence
  quota. It reads transcripts in binary through `bounded_lines()`, under a
  per-record cap, a per-file cap and a whole-scan deadline, because anything
  that can write under `~/.claude/projects` — including through a symlink —
  chooses what it reads. Its per-file `(mtime, size)` cache is not a TTL and
  stays; it is what keeps a steady-state scan cheap.

### Codex (`app/main.py:_codex_section()`)

Live-only by design.

- **`app/codex_quota.py`** — `CodexLiveQuotaClient` reads
  `$CODEX_DATA_DIR/auth.json` on every call, extracts
  `tokens.access_token` and `tokens.account_id`, and hits
  `GET https://chatgpt.com/backend-api/wham/usage` with
  `Authorization: Bearer …` and `ChatGPT-Account-Id: …`. The response
  shape is parsed defensively. Primary and secondary windows may appear at the
  top level or under `rate_limit`, with legacy aliases for 5-hour and weekly
  windows. The parser classifies 18,000-second/300-minute windows as 5-hour and
  604,800-second/10,080-minute windows as weekly, then falls back to legacy
  primary/secondary ordering when duration metadata is absent. A lone
  durationless `primary_window` remains weekly for compatibility with the
  earlier weekly-only response. Percentages come from whichever of
  `utilization` / `percent_used` / `used_percent` / `percent_left` /
  `remaining_percent` is present.
- `_codex_section()` emits stable `five_hour` ("5-Hour Window") and `seven_day`
  ("Weekly Window") gauges. An omitted upstream window remains live with
  `percent: null`; failures
  (`CodexLiveQuotaError`) return `source: "unavailable"` with `percent: null`
  for both gauges and surface the error in the UI. The frontend never
  synthesizes fake numbers.
- **`app/codex_activity.py`** — `CodexActivityReader` reports Codex
  `last_activity` from safe local file metadata only: `history.jsonl`,
  `session_index.jsonl`, and files under `sessions/` and
  `archived_sessions/`. It does not read `auth.json` or session contents,
  and it does not influence quota.

`CLAUDE_ENABLED` and `CODEX_ENABLED` are first-visit browser defaults only.
All live clients are constructed unconditionally. Browser-local choices live
in versioned `localStorage`; disabling a card must not stop SSE updates or
change provider error handling.

`app/refresh.py` and `app/budget.py` are the lifecycle and budget layers those
sections sit on: the first owns *when* a source is read, the second owns *how
much* a single read may cost. Neither knows anything about quota shapes.

`app/static/widget-state.js` is the pure state/presentation module for storage
validation, effective source state, and global status. The frontend
(`app/static/app.js`) remains the single gauge-colour and DOM-update path for
both initial payloads and SSE messages. The SSE loop is in `main.py:stream()`;
relative-time labels stay live between server pushes via a 1-second
`setInterval`.

## Network boundary

`docker-compose.yml` runs three services from one image:

- **`codervis`** — the dashboard. It joins the `inside` network only, which is
  `internal: true` and therefore has no default route. `HTTP(S)_PROXY` (both
  cases) points at `egress`; urllib honours them, so the live clients need no
  proxy code.
- **`egress`** — `app/egress.py`, ported from issuebot's `issuebot.egress`.
  It is a `CONNECT`-only forward proxy that admits `claude.ai` and
  `chatgpt.com` plus the operator's `EGRESS_ALLOW`, and refuses plain `http://`
  with 405. It sees host names only, never the TLS session or a token. Its
  healthcheck is `python -m app.egress healthcheck`, which requires a 403 for
  the reserved `egress-probe.invalid`.
- **`ingress`** — `app/ingress.py`, a byte relay that publishes
  `DASHBOARD_PORT` and forwards to `codervis:8000`. It is needed because Docker
  ignores `ports:` on an internal-only container. It is also the front door's
  resource bound: at most 256 connections, and a client must send a complete
  first request head (at most 16 KiB) within 10 s or get 408/431 before the
  dashboard is dialled. uvicorn itself arms no timer until it has sent a
  response. After the head, nothing is timed, so SSE is unaffected.

`egress` and `ingress` join `inside` and `outside`, run as uid 65534 with a
read-only root filesystem and all capabilities dropped, and hold no credential.
All three services log to json-file capped at 3 × 10 MB (`x-logging` in the
compose file), since a peer that reaches the port can make each of them log.
`python -m app.egress check`, run in the `codervis` container, verifies both
halves of the bound: the proxy filters by name and admits the configured
upstream hosts, and there is no direct route round it.
`tests/test_compose_topology.py` pins the compose shape.

**Keep the bound whole.** Do not give `codervis` a non-internal network or
`ports:`. Do not add a host to `DEFAULT_ALLOW` that the live clients do not
call. If a client ever needs another host, add it to `DEFAULT_ALLOW` and to the
test that checks the defaults cover the clients' own hosts.

## Load-bearing assumption: every live endpoint is undocumented

Neither endpoint is part of its vendor's public API.
- `/api/oauth/usage` was discovered by running
  `claude --debug-file path -d api` and grepping for `fetchUtilization`.
- `/backend-api/wham/usage` was reverse-engineered from the `codex-rs`
  backend client (also referenced as `/backend-api/codex/usage` in
  some builds).

Either can change or disappear at any time. Both panels are allowed
to degrade visibly.
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

# Automated tests
python -m pip install -r requirements-dev.txt
python -m pytest
python -m py_compile app/main.py app/quota.py app/claude_activity.py app/codex_quota.py app/codex_activity.py app/refresh.py app/budget.py app/egress.py app/ingress.py

# Egress bound, from inside the running dashboard container
docker compose exec codervis python -m app.egress check
```

The pytest suite uses FastAPI's `TestClient`, direct parser imports, stubbed
quota clients, and temporary directories. It must not read host credential
files or call the live undocumented quota endpoints.

Handlers read published snapshots, so a test that swaps a client in must
publish before asking for a payload — `tests/test_main_payload.py` gives each
test its own refreshers and calls `_publish()`. `TestClient(app)` starts the
refresher threads only as a context manager (`with TestClient(app)`).
`tests/test_payload_budget.py` pins the budgets and the refresher.

## Local dev gotchas

- **Windows + Docker Compose**: `~` does not expand in bind-mount paths.
  The compose file's `${CLAUDE_HOME:-~/.claude}` default only works on
  Linux/macOS; Windows users must set the `*_HOME` paths explicitly in `.env`
  (forward slashes are fine: `C:/Users/name/.claude`).
- The credentials files are bind-mounted read-only at
  `/data/claude/.credentials.json` and `/data/codex/auth.json` inside the
  container. Each CLI refreshes its access token on the host; the dashboard
  re-reads the files on each upstream call so it rides along on those refresh
  cadences. There is no token-refresh logic in this repo.

## When changing the dashboard

- The colour ramp (lime → amber → coral) is computed in
  `app/static/app.js:colorFor()` as HSL — hue glides 90° → 45° at 60%,
  then 45° → 5° to 100%. Initial payload and SSE-driven updates both go
  through this function, so keep them in sync if you change the curve.
- Percentage values exposed to the frontend are floats 0–100. Claude/Codex
  live APIs already report that scale. Never multiply already-normalized
  utilization values by 100.
