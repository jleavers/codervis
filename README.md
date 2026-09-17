# codervis

A local web dashboard that shows your **Claude Code**, **Codex CLI**,
**Cursor**, **GitHub Copilot**, and **Gemini Code Assist / Antigravity** quota
usage live, displayed as geiger-style meters that shift from **lime → amber →
coral** as you approach the limit.

## How it works

Each coding agent stores a credential locally that its own usage endpoint
accepts. codervis reads each one and polls the matching undocumented endpoint:

| Agent | Credential (read-only) | Endpoint(s) | Windows |
| --- | --- | --- | --- |
| Claude Code | `~/.claude/.credentials.json` → `claudeAiOauth.accessToken` (bearer) | `GET claude.ai/api/oauth/usage` | 5-hour + all-model weekly + Fable weekly utilization |
| Codex CLI | `~/.codex/auth.json` → `tokens.access_token` + `account_id` (bearer) | `GET chatgpt.com/backend-api/wham/usage` | 5-hour + weekly utilization |
| Cursor | `…/Cursor/User/globalStorage/state.vscdb` → SQLite key `cursorAuth/accessToken` (cookie) | `GET cursor.com/api/usage` + `/api/dashboard/*` | monthly premium-requests + usage-based spend |
| GitHub Copilot (file mode) | `…/github-copilot/apps.json` → `oauth_token` (`token` header) | `GET api.github.com/copilot_internal/user` | monthly premium-requests + chat |
| GitHub Copilot (PAT mode) | fine-grained PAT (`Plan` read) you create, via `COPILOT_GITHUB_TOKEN` | `GET api.github.com/users/{user}/settings/billing/premium_request/usage` | monthly premium-requests + usage-based spend |
| Gemini Code Assist / Antigravity | `~/.gemini/antigravity-cli/antigravity-oauth-token` → `token.access_token` (bearer) | `POST …/v1internal:retrieveUserQuotaSummary` (legacy fallback) | daily request quota by model family |

Claude and Codex return utilization as a percentage plus a reset timestamp.
**Cursor** is different: it meters on a monthly billing cycle, its
token lives in a SQLite database rather than a JSON dotfile, and it
authenticates with a `Cookie: WorkosCursorSessionToken=<userId>::<jwt>` header
instead of a bearer token. codervis derives two monthly gauges for it — premium
request count vs. cap, and usage-based dollar spend vs. hard limit.
**Copilot** also meters monthly, with two modes. In **file mode** codervis reads
the GitHub OAuth token the Copilot editor plugin/CLI stores in `apps.json`
(Neovim/JetBrains/Eclipse/language-server) and calls the same internal endpoint
VS Code's status-bar usage indicator uses, showing premium-request count vs.
allowance plus a chat gauge (which reads `unlimited` on paid plans). **VS Code
keeps its token in the OS keychain, not a file** — so VS-Code-only users set a
fine-grained Personal Access Token (`COPILOT_GITHUB_TOKEN`, `Plan` read) and
codervis uses GitHub's *documented* billing REST API instead, showing
premium-request count vs. allowance plus usage-based dollar spend. See
[Copilot setup](#github-copilot-setup).
**Gemini** supports Antigravity CLI (`agy`) 1.0.6's file-backed OAuth token and
calls the internal Cloud Code Assist quota endpoints. It prefers
`retrieveUserQuotaSummary`, matching `agy` 1.0.6, with a narrow fallback to the
legacy endpoint when the summary method is unavailable. These endpoints expose
daily request buckets by model, so codervis displays the most constrained Pro
and Flash request buckets rather than the Gemini web app's 5-hour / weekly
limits.

codervis runs as a small FastAPI container, **bind-mounts all five data
directories read-only**, reads the tokens, and polls the endpoints. The browser
gets live updates via Server-Sent Events; CSS animates the meter fill and the
colour shifts as the percentage rises.

Each provider header has a browser-local toggle. Switching a widget off keeps
its card visible but dimmed, labels it `disabled`, and removes it from the
overall status summary. Choices are stored in browser `localStorage`, survive
container restarts, and do not affect other browsers.

If a live endpoint is unreachable for any reason (expired token, network
down, or the vendor changes the API), that panel renders an "unavailable"
state. codervis does not estimate quota usage from local transcripts; it only
reads timestamp/metadata to show each agent's local last activity.

### Caveats

- Most live endpoints are **internal**, not part of the vendors' public APIs.
  They can change or disappear at any time. Copilot PAT mode is the exception:
  it uses GitHub's documented billing API.
- The dashboard reads the credential each tool maintains. It does
  **not** implement OAuth flows of its own — you need Claude Code, Codex
  CLI, Cursor, the GitHub Copilot plugin, and/or Antigravity CLI installed and
  signed in on the host machine.
- **Gemini does not report web-app 5-hour or weekly limits.** The Antigravity
  path currently exposes daily Gemini Code Assist request buckets by model
  family. codervis shows the most constrained Pro and Flash buckets.
- **Gemini requires `agy`'s file-backed credential.** Linux keyring-only
  sessions are intentionally unsupported: codervis does not expose the host
  D-Bus Secret Service to Docker.
- **Codex quota windows can move between primary and secondary slots.** Codervis
  classifies 5-hour and weekly windows from their reported duration, then falls
  back to the legacy primary/secondary ordering when duration metadata is
  absent. A lone durationless `primary_window` remains weekly for compatibility
  with the earlier weekly-only response. If either window is omitted, its gauge
  reads “—” while the other remains live.
- **Claude's Fable limit is plan-dependent.** Codervis reads the
  `weekly_scoped` Fable entry from the endpoint's `limits` list. If Anthropic
  omits that optional entry or returns it malformed, the Fable gauge shows “—”
  while the 5-hour and all-model weekly gauges remain live.
- **Cursor on the free plan** has no fixed premium-request cap and no
  usage-based billing, so both Cursor gauges honestly show "—" (the raw
  request count still appears under the meter). The gauges populate on
  Pro/Business plans. Cursor's Premium Requests gauge tracks the legacy
  premium/fast-request model; on accounts fully migrated off it,
  `maxRequestUsage` is `null` and that gauge shows "—" too.
- **Copilot's second gauge ("Chat") reads "—" on paid plans.** Pro/Pro+
  include unlimited chat and completions, so only the premium-requests gauge
  fills; the chat gauge honestly shows "unlimited". On the free plan (capped
  chat/completions) it shows a real percentage.
- **Copilot for org/enterprise-managed seats may show `unavailable`.** The
  internal usage endpoint is geared to individually-billed Copilot
  (Free/Pro/Pro+). If your licence is administered by an org or enterprise,
  the call can return no quota; the panel then degrades visibly.
- **Copilot token location varies by client.** File mode reads
  `apps.json`/`hosts.json` under the Copilot config dir. **VS Code keeps the
  token in the OS keychain, not a file**, so VS-Code-only users see
  `unavailable` in file mode — use [PAT mode](#github-copilot-setup) instead.
- **PAT mode shows usage-based $ spend as its second gauge** (not chat), and
  needs the plan cap to draw the premium-request percentage — set
  `COPILOT_PLAN` (or `COPILOT_PREMIUM_ALLOWANCE`). The billing report has no
  allowance field of its own.
- Claude Code refreshes its own access token. If you have not opened Claude
  Code for a while, Anthropic may return `HTTP 401` to codervis until the host
  CLI runs and refreshes `~/.claude/.credentials.json`. Open Claude Code from a
  terminal on the host, then wait for the next dashboard refresh.
- Read-only bind mounts: codervis never writes to `~/.claude`, `~/.codex`,
  your Cursor directory, Copilot config, or `~/.gemini`. The Cursor
  `state.vscdb` is opened read-only without `immutable` so SQLite sees live
  WAL-mode writes. If Docker's read-only mount prevents SQLite from opening
  the WAL sidecars directly, codervis reads from a short-lived temp snapshot
  inside the container.
- The `*_ENABLED` variables only choose the initial toggle state for a browser
  with no saved preference. All provider clients are still constructed and
  polled, so these variables do not suppress credential reads or upstream
  calls.

## Prerequisites

- Docker Desktop (or any recent Docker + Compose v2)
- Any combination of, installed and signed in on the host:
  - Claude Code (so `~/.claude/.credentials.json` exists)
  - Codex CLI (so `~/.codex/auth.json` exists)
  - Cursor (so `…/Cursor/User/globalStorage/state.vscdb` exists)
  - GitHub Copilot — either a client that writes `…/github-copilot/apps.json`
    (Neovim/JetBrains/Eclipse/language-server), or a fine-grained PAT for VS
    Code users (see [Copilot setup](#github-copilot-setup))
  - Antigravity CLI (`agy`) 1.0.6 / Gemini Code Assist (so
    `~/.gemini/antigravity-cli/antigravity-oauth-token` exists)

## Setup

```bash
cp .env.example .env
```

Edit `.env`:

| Variable | What it does | Default |
| --- | --- | --- |
| `CLAUDE_HOME` | Host path to your Claude Code data dir. **On Windows set this explicitly** — e.g. `C:/Users/you/.claude`. | `~/.claude` |
| `CODEX_HOME` | Host path to your Codex CLI data dir. Same Windows caveat. | `~/.codex` |
| `CURSOR_HOME` | Host path to your Cursor data dir (the one containing `User/globalStorage/state.vscdb`). OS-specific — see below. | `~/.config/Cursor` |
| `COPILOT_HOME` | Host path to your GitHub Copilot config dir (the one containing `apps.json`/`hosts.json`). OS-specific — see below. | `~/.config/github-copilot` |
| `GEMINI_HOME` | Host path to your Gemini / Antigravity config dir (the one containing `antigravity-cli/antigravity-oauth-token`). | `~/.gemini` |
| `CLAUDE_ENABLED` | First-visit browser widget default. | `true` |
| `CODEX_ENABLED` | First-visit browser widget default. | `true` |
| `CURSOR_ENABLED` | First-visit browser widget default. | `true` |
| `COPILOT_ENABLED` | First-visit browser widget default. | `true` |
| `GEMINI_ENABLED` | First-visit browser widget default. | `true` |
| `COPILOT_GITHUB_TOKEN` | Fine-grained PAT (`Plan` read) → switches Copilot to PAT mode (billing REST API). Needed for VS-Code-only setups. See [Copilot setup](#github-copilot-setup). | _(unset → file mode)_ |
| `COPILOT_GITHUB_USER` | GitHub login for PAT mode. Auto-detected from the token if unset. | _(auto)_ |
| `COPILOT_PLAN` | Plan whose monthly premium-request cap is the gauge denominator in PAT mode: `free`/`pro`/`pro+`/`business`/`enterprise`. | `pro` |
| `COPILOT_PREMIUM_ALLOWANCE` | Explicit premium-request cap (overrides `COPILOT_PLAN`). | _(from plan)_ |
| `COPILOT_SPEND_BUDGET` | Monthly $ budget; makes PAT mode's spend gauge a percentage instead of a raw figure. | _(unset → $ only)_ |
| `DASHBOARD_PORT` | Host port the dashboard listens on. | `8765` |
| `REFRESH_INTERVAL_SECONDS` | How often the browser is pushed a fresh snapshot. | `5` |
| `QUOTA_CACHE_TTL_SECONDS` | Server-side cache for the upstream calls. Keep ≥ refresh interval. | `30` |
| `CLAUDE_ACTIVITY_CACHE_TTL_SECONDS` | Server-side cache for Claude local transcript timestamp scans. | `5` |
| `CODEX_ACTIVITY_CACHE_TTL_SECONDS` | Server-side cache for Codex local activity metadata scans. | `5` |
| `CURSOR_ACTIVITY_CACHE_TTL_SECONDS` | Server-side cache for Cursor local activity metadata scans. | `5` |
| `COPILOT_ACTIVITY_CACHE_TTL_SECONDS` | Server-side cache for Copilot local activity metadata scans. | `5` |
| `GEMINI_ACTIVITY_CACHE_TTL_SECONDS` | Server-side cache for Gemini / Antigravity local activity metadata scans. | `5` |
| `CLAUDE_AI_HOST` | Override the Claude host (rarely needed). | `https://claude.ai` |
| `CHATGPT_HOST` | Override the Codex host (rarely needed). | `https://chatgpt.com` |
| `CURSOR_HOST` | Override the Cursor host (rarely needed). | `https://cursor.com` |
| `GITHUB_API_HOST` | Override the Copilot/GitHub API host (rarely needed). | `https://api.github.com` |
| `GEMINI_CODE_ASSIST_HOST` | Override the Gemini Code Assist host (rarely needed). | `https://daily-cloudcode-pa.googleapis.com` |

Cursor's data directory is **not** a dotfile in your home directory — it's
the editor's application-data folder:

| OS | `CURSOR_HOME` |
| --- | --- |
| Windows | `${APPDATA}/Cursor` (e.g. `C:/Users/you/AppData/Roaming/Cursor`) |
| macOS | `~/Library/Application Support/Cursor` |
| Linux | `~/.config/Cursor` |

GitHub Copilot keeps its OAuth token in a config dir (which must contain
`apps.json` or `hosts.json`):

| OS | `COPILOT_HOME` |
| --- | --- |
| Windows | `${USERPROFILE}/.config/github-copilot` (some clients use `${LOCALAPPDATA}/github-copilot`) |
| macOS | `~/.config/github-copilot` |
| Linux | `~/.config/github-copilot` |

### GitHub Copilot setup

Copilot has two modes; pick the one matching how you signed in:

**File mode (default).** If you use a client that stores the OAuth token in a
file — Neovim `copilot.vim`/`copilot.lua`, JetBrains, Eclipse, or the Copilot
language server — point `COPILOT_HOME` at the dir holding `apps.json` /
`hosts.json` and you're done. (Run e.g. Neovim's `:Copilot setup` once to
create it.)

**PAT mode (VS Code and other keychain-only setups).** VS Code keeps its
Copilot token in the OS keychain, so there's no file to read. Instead:

1. Create a **fine-grained** PAT at **github.com → Settings → Developer
   settings → Personal access tokens → Fine-grained tokens**.
2. Under **Account permissions**, grant **"Plan" → Read-only**. (No repo
   access needed.)
3. Put it in `.env`:
   ```dotenv
   COPILOT_GITHUB_TOKEN=github_pat_xxxxxxxx
   COPILOT_PLAN=pro          # free=50 / pro=300 / pro+=1500 / business=300 / enterprise=1000
   # COPILOT_SPEND_BUDGET=10 # optional: turns the spend gauge into a percentage
   ```

When `COPILOT_GITHUB_TOKEN` is set, codervis uses GitHub's documented billing
API and ignores `COPILOT_HOME`. The premium-request gauge fills against the
plan cap; the second gauge shows usage-based dollar spend. **PAT mode reports
nothing for Copilot licences billed through an organization or enterprise** —
that endpoint only covers individually-billed plans, so the panel will read
`unavailable` for managed seats.

### Gemini / Antigravity setup

Gemini support is compatible with Antigravity CLI (`agy`) 1.0.6 when `agy`
uses its file-backed credential fallback:

```text
~/.gemini/antigravity-cli/antigravity-oauth-token
```

Run `agy` to authenticate, then verify the file exists before starting
codervis:

```bash
test -f ~/.gemini/antigravity-cli/antigravity-oauth-token
```

On Linux, `agy` may store credentials only in the OS keyring. codervis
intentionally does not expose the host D-Bus Secret Service to Docker, so a
keyring-only session renders Gemini unavailable.

Point `GEMINI_HOME` at the directory containing that `antigravity-cli`
subdirectory. The live client first calls `v1internal:loadCodeAssist` in
health-check mode to discover the companion project, then calls
`v1internal:retrieveUserQuotaSummary` for that project. It falls back to
`v1internal:retrieveUserQuota` only if the summary method returns HTTP 404 or
405. The response contains daily request buckets by model; codervis groups
those into Pro and Flash gauges and uses the most constrained bucket in each
group.

### Windows note

Docker Compose on Windows does **not** expand `~` in bind-mount paths, so
the home-directory defaults only work if you set the paths explicitly. The
simplest thing is:

```dotenv
CLAUDE_HOME=C:/Users/yourname/.claude
CODEX_HOME=C:/Users/yourname/.codex
CURSOR_HOME=C:/Users/yourname/AppData/Roaming/Cursor
COPILOT_HOME=C:/Users/yourname/.config/github-copilot
GEMINI_HOME=C:/Users/yourname/.gemini
```

(Forward slashes work fine inside `.env`.)

If you don't use an agent, point its `*_HOME` at an existing directory and set
its `*_ENABLED=false` to make the widget initially dimmed. This is only a
browser presentation default; the app still checks the provider credential
path and polls any configured live client.

## Run

```bash
docker compose up --build -d
```

Open <http://localhost:8765> (or whichever port you set). The default Compose
port mapping also exposes the dashboard on your LAN at
`http://<your-host-ip>:8765`.

To stop:

```bash
docker compose down
```

## Test

Install development dependencies, then run the suite:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
python -m py_compile app/main.py app/quota.py app/claude_activity.py app/codex_quota.py app/codex_activity.py app/cursor_quota.py app/cursor_activity.py app/copilot_quota.py app/copilot_activity.py app/gemini_quota.py app/gemini_activity.py
```

The tests use temporary directories, an in-memory SQLite DB for the Cursor
path, and stubbed upstream clients. They do not read your real credential
files and do not call the live quota endpoints.

## What you see

One panel per agent (Claude Code, Codex, Cursor, GitHub Copilot, Gemini Code
Assist):

- **Claude** shows **5-Hour Window**, **Weekly Window**, and **Weekly Window
  (Fable)** gauges, each with a countdown when its reset time is available.
- **Codex** shows **5-Hour Window** and **Weekly Window** gauges with their reset
  countdowns. If Codex omits either window, that gauge reads “—”.
- **Cursor** shows a **Premium Requests (month)** gauge and a **Usage-Based
  Spend (month)** gauge, both on your monthly billing cycle. The line under
  each meter shows the raw figures (e.g. `42 / 500 reqs`, `$3.40 / $20.00`).
  On the free plan these have no cap, so the percentage reads "—".
- **Copilot** shows a **Premium Requests (month)** gauge (e.g. `34 / 300 reqs`)
  and a **Chat (month)** gauge, both on your monthly cycle. On Pro/Pro+ the
  chat gauge reads "unlimited", so its percentage shows "—".
- **Gemini** shows **Pro Requests (day)** and **Flash Requests (day)** gauges
  from Code Assist request buckets, each with a daily reset timestamp when the
  upstream response includes one.
- Per-panel header shows the server source state (`live` / `unavailable`) or
  the browser-local presentation state (`disabled`); footer shows the plan
  and most recent local activity. Claude
  activity comes from project transcript timestamps; Codex from local
  history/session file metadata; Cursor from `state.vscdb` and
  History/workspace directory metadata; Copilot from `apps.json`/`hosts.json`
  config metadata (a coarser signal — Copilot keeps no local transcript);
  Gemini from safe Antigravity/Gemini file metadata.
  The footer shows an error string when a live quota call fails.

The fill colour is computed from the percentage: lime under 50%, sliding
through amber, to coral as you approach 100%. Unavailable gauges and
browser-disabled cards are dimmed.

## File layout

```
.
├── app/
│   ├── main.py          # FastAPI app + SSE stream
│   ├── quota.py         # Claude live client → claude.ai/api/oauth/usage
│   ├── claude_activity.py # Claude local activity timestamp reader
│   ├── codex_quota.py   # Codex live client → chatgpt.com/backend-api/wham/usage
│   ├── codex_activity.py # Codex local activity metadata reader
│   ├── cursor_quota.py  # Cursor live client → cursor.com (reads state.vscdb)
│   ├── cursor_activity.py # Cursor local activity metadata reader
│   ├── copilot_quota.py # Copilot live client → api.github.com/copilot_internal/user
│   ├── copilot_activity.py # Copilot local activity metadata reader
│   ├── gemini_quota.py # Gemini live client → daily-cloudcode-pa.googleapis.com
│   ├── gemini_activity.py # Gemini / Antigravity local activity metadata reader
│   ├── templates/
│   │   └── index.html
│   └── static/
│       ├── style.css
│       ├── widget-state.js # Browser-local persistence and presentation state
│       └── app.js          # Gauge rendering, DOM updates, and SSE handling
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── requirements-dev.txt
├── pytest.ini
├── tests/
├── .env.example
└── .gitignore
```

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Claude chip shows `unavailable` with `HTTP 401` | Claude Code's access token has likely expired and the host CLI has not refreshed it yet. Open Claude Code from a terminal on the host, then wait for the next dashboard refresh. |
| Claude chip shows `unavailable` | `~/.claude/.credentials.json` missing or unreadable inside the container, token expired/refresh hasn't run, or Anthropic changed the endpoint. Hover the chip for the error. |
| Codex chip shows `unavailable` | `~/.codex/auth.json` missing or unreadable inside the container, token expired/refresh hasn't run, or OpenAI changed the endpoint. Hover the chip for the error. |
| Cursor chip shows `unavailable` | `state.vscdb` missing/unreadable inside the container, you're signed out of Cursor (no `cursorAuth/accessToken`), token expired, or Cursor changed the endpoint/schema. Hover the chip for the error. |
| Cursor gauges show `—` but chip is `live` | Expected on the free plan (no request cap, usage-based billing off). The raw request count still shows under the meter. |
| Copilot chip shows `unavailable` | `apps.json`/`hosts.json` missing or unreadable inside the container (token may be in the OS keychain instead of a file), you're signed out of Copilot, the licence is org/enterprise-managed, the token expired, or GitHub changed the endpoint. Hover the chip for the error. |
| Copilot Chat gauge shows `—` but chip is `live` | Expected on Pro/Pro+ — chat is unlimited. Only the premium-requests gauge fills. |
| Gemini chip shows `unavailable` | `~/.gemini/antigravity-cli/antigravity-oauth-token` is missing or unreadable inside the container (including an `agy` keyring-only session), Antigravity is signed out, the token expired, or Google changed the Cloud Code Assist endpoint. Hover the chip for the error. |
| `claude_credentials_present: false` from `/healthz` | Bind mount didn't pick up the credentials file. Verify `CLAUDE_HOME` points at your real `.claude` directory. |
| `codex_credentials_present: false` from `/healthz` | Same, for `CODEX_HOME` / `~/.codex/auth.json`. |
| `cursor_credentials_present: false` from `/healthz` | Bind mount missed `state.vscdb`. Verify `CURSOR_HOME` points at your Cursor data dir (it must contain `User/globalStorage/state.vscdb`). |
| `copilot_credentials_present: false` from `/healthz` | Bind mount missed `apps.json`/`hosts.json`. Verify `COPILOT_HOME` points at your Copilot config dir. |
| `gemini_credentials_present: false` from `/healthz` | The token file is absent. Verify `GEMINI_HOME` points at your `.gemini` directory and that `agy` created `antigravity-cli/antigravity-oauth-token`; keyring-only credentials are not visible to codervis. |
| Browser shows `reconnecting…` | The container restarted; SSE will reconnect on its own. |

`/healthz` returns JSON with `data_root_exists` and `credentials_present`
flags that are useful for quick diagnosis.

## Security notes

- `~/.claude/.credentials.json`, `~/.codex/auth.json`, Cursor's
  `state.vscdb`, Copilot's `apps.json`/`hosts.json`, and Gemini's
  `antigravity-oauth-token` all contain long-lived session tokens. All five
  bind mounts are read-only, and
  `state.vscdb` is opened in SQLite read-only mode. codervis reads only the
  `oauth_token` from the Copilot config and only `token.access_token` from the
  Gemini token JSON, and never logs either. The container does not receive the
  host D-Bus socket or Linux keyring access. The default Compose port mapping
  publishes the dashboard on all host interfaces,
  so machines on your LAN can reach it at `http://<your-host-ip>:8765`.
  **Don't expose this port to the public internet** — anyone who can
  reach it can read your usage. If you need remote access, put it behind
  a reverse proxy with auth.
- Cursor's `state.vscdb` also holds your chat/composer history. codervis
  queries **only** the `cursorAuth/accessToken` and membership ItemTable keys.
  When direct WAL reads fail on a read-only mount, it temporarily copies
  `state.vscdb` and `state.vscdb-wal` inside the container, queries those keys,
  and deletes the snapshot.
- The dashboard never logs the tokens. If you regenerated `usage-debug.log`
  during setup, delete it.
