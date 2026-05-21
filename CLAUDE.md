# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local FastAPI dashboard that displays **Claude Code**, **Codex CLI**, and
**Cursor** quota utilization as colour-shifting meters, side-by-side. Runs as
a single Docker container, bind-mounts the host's `~/.claude`, `~/.codex`, and
Cursor data directories read-only, and authenticates upstream using each
agent's own stored credential.

User-facing setup, env vars, and troubleshooting live in `README.md`.

## Architecture

The JSON payload is keyed by provider — `claude`, `codex`, and `cursor` —
assembled in `app/main.py:_build_payload()` from three independent sections.

Each section exposes a `windows` list (rather than fixed `five_hour` /
`seven_day` keys) so providers can report differently-shaped quota windows.
Each window dict is `{name, label, percent, resets_at, detail}` — `name` is
the gauge's DOM-id slot, `label` is the display heading, and `detail` is an
optional short string shown under the meter (e.g. Cursor's `12 / 500 reqs`).
The template (`index.html`) and `app.js` iterate `windows` generically.

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

### Cursor (`app/main.py:_cursor_section()`)

Live-only by design, but Cursor differs from the other two in three ways:
it meters on a **monthly** billing cycle (not 5-hour/weekly), keeps its
credential in a **SQLite DB** (not a JSON dotfile), and authenticates with
a **cookie** (not a bearer token).

- **`app/cursor_quota.py`** — `CursorLiveQuotaClient` reads the session
  token from `$CURSOR_DATA_DIR/User/globalStorage/state.vscdb` (SQLite
  table `ItemTable`, key `cursorAuth/accessToken`) on every call. The DB
  is opened **read-only without `immutable=1`** so SQLite sees live
  WAL-mode writes while avoiding writes to the host Cursor directory. The
  client falls back to a short-lived temp snapshot of `state.vscdb` plus
  `state.vscdb-wal` if Docker's read-only bind mount prevents SQLite from
  opening WAL sidecars in place. The token is a JWT; the user id is its `sub`
  claim up to the first `|`. Auth is
  `Cookie: WorkosCursorSessionToken=<userId>::<jwt>`.
  - Window `requests` ("Premium Requests (month)") comes from
    `GET https://cursor.com/api/usage?user=<id>` — `gpt-4.numRequests` /
    `gpt-4.maxRequestUsage`. On plans with no fixed cap (`maxRequestUsage`
    is `null`, e.g. free) `percent` is `null` and only the raw count is
    shown in `detail`.
  - Window `spend` ("Usage-Based Spend (month)") is **best-effort**: it
    posts to `/api/dashboard/get-hard-limit` and
    `/api/dashboard/get-monthly-invoice`. If usage-based billing is off
    (`noUsageBasedAllowed`) or these calls fail, the window degrades to a
    `null` percent **while the section stays `live`** — only a failure of
    the core `/api/usage` call raises `CursorLiveQuotaError` and takes the
    whole section to `unavailable`.
- **`app/cursor_activity.py`** — `CursorActivityReader` reports Cursor
  `last_activity` from safe file metadata only: the mtime of
  `state.vscdb` and the `User/History` and `User/workspaceStorage`
  directories. It never reads `state.vscdb` contents or the stored token.
- If `CURSOR_ENABLED` is falsy, `_cursor` is `None` and the section
  returns `source: "disabled"`.

The frontend (`app/static/app.js`) shows each provider's source state
as a chip in its column header. The SSE loop is in `main.py:stream()`.
The frontend keeps relative-time labels alive between server pushes
via a 1-second `setInterval`.

## Load-bearing assumption: every live endpoint is undocumented

None of the endpoints are part of their vendor's public API.
- `/api/oauth/usage` was discovered by running
  `claude --debug-file path -d api` and grepping for `fetchUtilization`.
- `/backend-api/wham/usage` was reverse-engineered from the `codex-rs`
  backend client (also referenced as `/backend-api/codex/usage` in
  some builds).
- `cursor.com/api/usage` and the `/api/dashboard/*` endpoints are the
  same calls Cursor's web dashboard makes, plus the `state.vscdb`
  ItemTable key layout, all reverse-engineered from the client. The token
  format (`<userId>::<jwt>` cookie) and the SQLite schema can change.

Any of these can change or disappear at any time. All panels are allowed
to degrade visibly.
**Do not assume the endpoint shapes are stable**:
- in `quota.py`, preserve the "any failure → `LiveQuotaError` →
  `unavailable` state" contract;
- in `codex_quota.py`, preserve the "any failure →
  `CodexLiveQuotaError` → `unavailable` state" contract — and keep
  field-name parsing tolerant (`_pick()` / the cascading checks in
  `_window()`);
- in `cursor_quota.py`, preserve the "core `/api/usage` failure →
  `CursorLiveQuotaError` → `unavailable` state" contract, keep the spend
  window best-effort (never let it fail the section), and always open
  `state.vscdb` read-only without `immutable=1`; if in-place WAL access
  fails, use only a temporary container-local snapshot, never a writable bind
  mount.

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
python -m py_compile app/main.py app/quota.py app/claude_activity.py app/codex_quota.py app/codex_activity.py app/cursor_quota.py app/cursor_activity.py
```

The pytest suite uses FastAPI's `TestClient`, direct parser imports, stubbed
quota clients, and temporary directories. It must not read host credential
files or call the live undocumented quota endpoints.

## Local dev gotchas

- **Windows + Docker Compose**: `~` does not expand in bind-mount paths.
  The compose file's `${CLAUDE_HOME:-~/.claude}` default only works on
  Linux/macOS; Windows users must set `CLAUDE_HOME`/`CODEX_HOME` explicitly
  in `.env` (forward slashes are fine: `C:/Users/name/.claude`).
- **Cursor data dir is OS-specific** and is *not* a dotfile in `~`:
  `${APPDATA}/Cursor` (Windows), `~/Library/Application Support/Cursor`
  (macOS), `~/.config/Cursor` (Linux). The compose default is the Linux
  path; Windows/macOS users must set `CURSOR_HOME` explicitly. The whole
  Cursor dir is mounted at `/data/cursor`; the client reads only
  `/data/cursor/User/globalStorage/state.vscdb`.
- The credentials files are bind-mounted read-only at
  `/data/claude/.credentials.json`, `/data/codex/auth.json`, and (Cursor)
  `/data/cursor/User/globalStorage/state.vscdb` inside the container. Each
  CLI refreshes its access token on the host; the dashboard re-reads the
  files on each upstream call so it rides along on those refresh cadences.
  There is no token-refresh logic in this repo. `state.vscdb` can be large
  (chat/composer state); the client opens it `mode=ro` and queries only the
  auth keys, falling back to a deleted-after-read temp snapshot when SQLite
  cannot read WAL sidecars from the read-only mount.

## When changing the dashboard

- The colour ramp (lime → amber → coral) is computed in
  `app/static/app.js:colorFor()` as HSL — hue glides 90° → 45° at 60%,
  then 45° → 5° to 100%. The server-rendered initial paint and the
  SSE-driven updates both go through this function, so keep them in
  sync if you change the curve.
- Percentage values are floats 0–100 from the upstream API. Never
  multiply by 100 in either live path.
