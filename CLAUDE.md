# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local FastAPI dashboard that displays **Claude Code**, **Codex CLI**,
**Cursor**, **GitHub Copilot**, and **Gemini Code Assist / Antigravity** quota
utilization as colour-shifting meters. Runs as a single Docker container,
bind-mounts the host's `~/.claude`, `~/.codex`, Cursor, `github-copilot`, and
`.gemini` data directories read-only, and authenticates upstream using each
agent's own stored credential.

User-facing setup, env vars, and troubleshooting live in `README.md`.

## Architecture

The JSON payload is keyed by provider — `claude`, `codex`, `cursor`,
`copilot`, and `gemini` — assembled in `app/main.py:_build_payload()` from
five independent sections.

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
  percentages already. Its optional `limits` list can also contain a
  `weekly_scoped` entry whose `scope.model.display_name` identifies Fable;
  that entry's `percent` is also already on a 0–100 scale. No token-count
  maths is needed. Results are cached in-memory for
  `QUOTA_CACHE_TTL_SECONDS` so all SSE clients share one upstream fetch.
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
  quota.

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

### Copilot (`app/main.py:_copilot_section()`)

Live-only by design and metered **monthly** like Cursor, but Copilot has **two
client implementations** in `app/copilot_quota.py`, selected by
`client_from_env()`: if a PAT is configured (`COPILOT_GITHUB_TOKEN` /
`COPILOT_TOKEN_FILE`) it returns `CopilotBillingQuotaClient`, otherwise
`CopilotLiveQuotaClient`. Both return a `CopilotLiveSnapshot` with `premium`
and `secondary` windows, expose `secondary_label` and `credentials_present()`,
and raise `CopilotLiveQuotaError`, so `_copilot_section()` treats them
interchangeably. The second window's DOM slot is always named `secondary` (the
heading text differs by mode); `_copilot_section()` reads the active client's
`secondary_label` for the placeholder so the server-rendered heading matches.

**File mode** (`CopilotLiveQuotaClient`) — for clients that persist the OAuth
token to disk (Neovim/JetBrains/Eclipse/language-server). **VS Code does not**
(its token is in the OS keychain), so VS-Code-only users need PAT mode.

- **`app/copilot_quota.py`** — `CopilotLiveQuotaClient` reads the OAuth
  token from `$COPILOT_DATA_DIR/apps.json` (falling back to `hosts.json`)
  on every call — a JSON object keyed by host (`github.com` /
  `github.com:Iv1.<appid>`) whose value carries `oauth_token`. It hits
  `GET https://api.github.com/copilot_internal/user` with
  `Authorization: token …` plus `Editor-Version` headers (the same internal
  endpoint VS Code's status-bar usage indicator uses).
  - The response's `quota_snapshots` object carries one entry per quota kind
    (`premium_interactions`, `chat`, `completions`), each with
    `entitlement` / `remaining` / `percent_remaining` / `unlimited` /
    `overage_count`; the reset is the top-level `quota_reset_date`.
  - Window `premium` ("Premium Requests (month)") comes from
    `premium_interactions`. `percent` is `100 − percent_remaining` (or
    derived from `entitlement`/`remaining`); on `unlimited` snapshots
    `percent` is `null`.
  - Window `secondary` ("Chat (month)") is **best-effort** from the `chat`
    snapshot: on paid plans it is `unlimited` so `percent` is `null` and the
    detail reads `unlimited`; only a failure of the core call (or a missing
    `premium_interactions` snapshot) raises `CopilotLiveQuotaError` and takes
    the section to `unavailable`.

**PAT mode** (`CopilotBillingQuotaClient`) — uses GitHub's *documented* billing
REST API for keychain-only setups (e.g. VS Code).

- Reads a fine-grained PAT (`Plan` read) from `COPILOT_GITHUB_TOKEN` or
  `COPILOT_TOKEN_FILE`, derives the username from `GET /user` (override with
  `COPILOT_GITHUB_USER`), then calls
  `GET /users/{user}/settings/billing/premium_request/usage?year=&month=`.
- The report gives **consumption only, no allowance**: window `premium` sums
  `usageItems[].grossQuantity` and divides by the plan cap (`COPILOT_PLAN` →
  `PLAN_ALLOWANCES`, or `COPILOT_PREMIUM_ALLOWANCE`). Window `secondary`
  ("Usage-Based Spend (month)") sums `netAmount` (dollar overage); `percent`
  is `null` unless `COPILOT_SPEND_BUDGET` is set.
- This endpoint returns nothing for org/enterprise-managed licences — that
  surfaces as `CopilotLiveQuotaError` → `unavailable` like any other failure.
- **`app/copilot_activity.py`** — `CopilotActivityReader` reports Copilot
  `last_activity` from safe file metadata only: the mtimes of `apps.json`,
  `hosts.json`, `versions.json`, and any files under `logs/`. Copilot keeps
  no per-session transcript here, so this is a coarser "recently used" signal
  than the other agents. It never reads the stored token.

### Gemini (`app/main.py:_gemini_section()`)

Live-only by design, using Antigravity CLI (`agy`) 1.0.6's file-backed OAuth
token. It reports daily Code Assist request buckets by model family, **not**
the Gemini web app's 5-hour or weekly limits. Keyring-only credentials are
intentionally unsupported; do not expose host D-Bus or Secret Service sockets
to the container.

- **`app/gemini_quota.py`** — `GeminiLiveQuotaClient` reads
  `$GEMINI_DATA_DIR/antigravity-cli/antigravity-oauth-token` on every call
  (or `GEMINI_TOKEN_FILE` if set), extracts `token.access_token`, and posts to
  `https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist` with
  `mode: "HEALTH_CHECK"` to discover the companion project. It then posts to
  `v1internal:retrieveUserQuotaSummary` with that project. Only HTTP 404 or 405
  from the summary method triggers a fallback to
  `v1internal:retrieveUserQuota`; auth, network, parse, and other HTTP failures
  remain unavailable states.
  - Summary buckets may identify a quota with `displayName` / `display_name`
    and `bucketId` / `bucket_id`. Legacy buckets use `modelId` / `model_id`
    and may include `tokenType: "REQUESTS"`.
  - Both forms expose `remainingFraction` and optional `remainingAmount` and
    `resetTime`; snake_case alternates are accepted.
  - Window `pro` ("Pro Requests (day)") selects the most constrained request
    bucket whose combined model/display/bucket identity contains `pro`.
  - Window `flash` ("Flash Requests (day)") selects the most constrained
    request bucket whose combined identity contains `flash`.
  - `percent` is derived as `(1 - remainingFraction) * 100`; tolerate
    `remainingFraction` in either `0..1` or `0..100` form.
- On any failure (`GeminiLiveQuotaError`) the section returns
  `source: "unavailable"` with `percent: null` for both gauges and the error
  string surfaced to the UI. Do not infer Gemini usage from local files.
- **`app/gemini_activity.py`** — `GeminiActivityReader` reports
  `last_activity` from safe file metadata only: the token file mtime,
  `config/.migrated`, and files under `antigravity/brain`,
  `antigravity/annotations`, and `config/projects`. It never reads token
  contents or transcript/database contents.

`CLAUDE_ENABLED`, `CODEX_ENABLED`, `CURSOR_ENABLED`, `COPILOT_ENABLED`, and
`GEMINI_ENABLED` are first-visit browser defaults only. All live clients are
constructed unconditionally. Browser-local choices live in versioned
`localStorage`; disabling a card must not stop SSE updates or change provider
error handling.

`app/static/widget-state.js` is the pure state/presentation module for storage
validation, effective source state, and global status. The frontend
(`app/static/app.js`) remains the single gauge-colour and DOM-update path for
both initial payloads and SSE messages. The SSE loop is in `main.py:stream()`;
relative-time labels stay live between server pushes via a 1-second
`setInterval`.

## Load-bearing assumption: every live endpoint is undocumented

Except for Copilot PAT mode's GitHub billing call, these endpoints are not
part of their vendor's public API.
- `/api/oauth/usage` was discovered by running
  `claude --debug-file path -d api` and grepping for `fetchUtilization`.
- `/backend-api/wham/usage` was reverse-engineered from the `codex-rs`
  backend client (also referenced as `/backend-api/codex/usage` in
  some builds).
- `cursor.com/api/usage` and the `/api/dashboard/*` endpoints are the
  same calls Cursor's web dashboard makes, plus the `state.vscdb`
  ItemTable key layout, all reverse-engineered from the client. The token
  format (`<userId>::<jwt>` cookie) and the SQLite schema can change.
- `api.github.com/copilot_internal/user` (file mode) is the internal
  Microsoft↔GitHub endpoint VS Code's usage indicator calls; it is
  unversioned and not in the public REST API. The `quota_snapshots` shape and
  the `apps.json` / `hosts.json` token layout can change. The PAT-mode
  endpoint `/users/{user}/settings/billing/premium_request/usage` *is*
  documented and stable, but needs a user-supplied fine-grained PAT, returns
  consumption without an allowance (hence the hardcoded `PLAN_ALLOWANCES`),
  and is empty for org/enterprise-managed seats.
- `daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist`,
  `v1internal:retrieveUserQuotaSummary`, and the legacy
  `v1internal:retrieveUserQuota` fallback are internal Google Cloud Code
  Assist / Antigravity endpoints. They currently expose daily request quota
  buckets by model family; they do not expose Gemini web-app 5-hour or weekly
  limits.

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
  mount;
- in `copilot_quota.py`, both clients preserve the "core call failure →
  `CopilotLiveQuotaError` → `unavailable` state" contract, return a
  `premium` + `secondary` snapshot, and keep the secondary window non-fatal
  (`unlimited` → null percent in file mode; spend without a budget → null
  percent in PAT mode). File mode only ever reads the `oauth_token` from the
  credential JSON; PAT mode reads only the configured token. Keep both
  interchangeable behind `client_from_env()`.
- in `gemini_quota.py`, preserve the "any failure →
  `GeminiLiveQuotaError` → `unavailable` state" contract, keep bucket parsing
  tolerant of alternate field names, fall back from
  `retrieveUserQuotaSummary` only on HTTP 404/405, and only ever read
  `token.access_token` from the configured Antigravity token JSON. Do not add
  host keyring or D-Bus access; `gemini_credentials_present` means only that
  the configured token file exists.

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
python -m py_compile app/main.py app/quota.py app/claude_activity.py app/codex_quota.py app/codex_activity.py app/cursor_quota.py app/cursor_activity.py app/copilot_quota.py app/copilot_activity.py app/gemini_quota.py app/gemini_activity.py
```

The pytest suite uses FastAPI's `TestClient`, direct parser imports, stubbed
quota clients, and temporary directories. It must not read host credential
files or call the live undocumented quota endpoints.

## Local dev gotchas

- **Windows + Docker Compose**: `~` does not expand in bind-mount paths.
  The compose file's `${CLAUDE_HOME:-~/.claude}` default only works on
  Linux/macOS; Windows users must set the `*_HOME` paths explicitly in `.env`
  (forward slashes are fine: `C:/Users/name/.claude`).
- **Cursor data dir is OS-specific** and is *not* a dotfile in `~`:
  `${APPDATA}/Cursor` (Windows), `~/Library/Application Support/Cursor`
  (macOS), `~/.config/Cursor` (Linux). The compose default is the Linux
  path; Windows/macOS users must set `CURSOR_HOME` explicitly. The whole
  Cursor dir is mounted at `/data/cursor`; the client reads only
  `/data/cursor/User/globalStorage/state.vscdb`.
- **Copilot data dir is OS-specific** and is *not* a dotfile in `~`. The
  default is `~/.config/github-copilot` (the editor plugin / Copilot CLI
  token dir); some clients use `${LOCALAPPDATA}/github-copilot` on Windows.
  Windows users must set `COPILOT_HOME` explicitly. The whole dir is mounted
  at `/data/copilot`; the client reads only `apps.json` / `hosts.json`.
- **Gemini data dir defaults to `~/.gemini`**. `agy` 1.0.6's supported
  file-backed credential lives under
  `antigravity-cli/antigravity-oauth-token`; Linux keyring-only credentials
  are outside codervis's security boundary. Windows users should set
  `GEMINI_HOME=C:/Users/name/.gemini` explicitly. The whole dir is mounted at
  `/data/gemini`; the quota client reads only that token JSON and the activity
  reader stats known metadata paths.
- The credentials files are bind-mounted read-only at
  `/data/claude/.credentials.json`, `/data/codex/auth.json`, (Cursor)
  `/data/cursor/User/globalStorage/state.vscdb`, and (Copilot)
  `/data/copilot/apps.json`, and (Gemini)
  `/data/gemini/antigravity-cli/antigravity-oauth-token` inside the container.
  Each
  CLI refreshes its access token on the host; the dashboard re-reads the
  files on each upstream call so it rides along on those refresh cadences.
  There is no token-refresh logic in this repo. `state.vscdb` can be large
  (chat/composer state); the client opens it `mode=ro` and queries only the
  auth keys, falling back to a deleted-after-read temp snapshot when SQLite
  cannot read WAL sidecars from the read-only mount.

## When changing the dashboard

- The colour ramp (lime → amber → coral) is computed in
  `app/static/app.js:colorFor()` as HSL — hue glides 90° → 45° at 60%,
  then 45° → 5° to 100%. Initial payload and SSE-driven updates both go
  through this function, so keep them in sync if you change the curve.
- Percentage values exposed to the frontend are floats 0–100. Claude/Codex
  live APIs already report that scale; Gemini reports remaining fractions, so
  convert exactly once in `gemini_quota.py`. Never multiply already-normalized
  utilization values by 100.
