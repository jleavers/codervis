# codervis

A local web dashboard that shows your **Claude Code**, **Codex CLI**,
**Cursor**, and **GitHub Copilot** quota usage live, in a 2×2 grid — displayed
as geiger-style meters that shift from **lime → amber → coral** as you approach
the limit.

## How it works

Each coding agent stores a credential locally that its own usage endpoint
accepts. codervis reads each one and polls the matching undocumented endpoint:

| Agent | Credential (read-only) | Endpoint(s) | Windows |
| --- | --- | --- | --- |
| Claude Code | `~/.claude/.credentials.json` → `claudeAiOauth.accessToken` (bearer) | `GET claude.ai/api/oauth/usage` | 5-hour + weekly utilization |
| Codex CLI | `~/.codex/auth.json` → `tokens.access_token` + `account_id` (bearer) | `GET chatgpt.com/backend-api/wham/usage` | 5-hour + weekly utilization |
| Cursor | `…/Cursor/User/globalStorage/state.vscdb` → SQLite key `cursorAuth/accessToken` (cookie) | `GET cursor.com/api/usage` + `/api/dashboard/*` | monthly premium-requests + usage-based spend |
| GitHub Copilot (file mode) | `…/github-copilot/apps.json` → `oauth_token` (`token` header) | `GET api.github.com/copilot_internal/user` | monthly premium-requests + chat |
| GitHub Copilot (PAT mode) | fine-grained PAT (`Plan` read) you create, via `COPILOT_GITHUB_TOKEN` | `GET api.github.com/users/{user}/settings/billing/premium_request/usage` | monthly premium-requests + usage-based spend |

Claude and Codex return per-window utilization as a percentage plus a reset
timestamp. **Cursor** is different: it meters on a monthly billing cycle, its
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

codervis runs as a small FastAPI container, **bind-mounts all four data
directories read-only**, reads the tokens, and polls the endpoints. The browser
gets live updates via Server-Sent Events; CSS animates the meter fill and the
colour shifts as the percentage rises.

If a live endpoint is unreachable for any reason (expired token, network
down, or the vendor changes the API), that panel renders an "unavailable"
state. codervis does not estimate quota usage from local transcripts; it only
reads timestamp/metadata to show each agent's local last activity.

### Caveats

- Every endpoint is **internal**, not part of Anthropic's, OpenAI's, or
  Cursor's public API. They can change or disappear at any time.
- The dashboard reads the credential each tool maintains. It does
  **not** implement OAuth flows of its own — you need Claude Code, Codex
  CLI, Cursor, and/or the GitHub Copilot plugin installed and signed in on
  the host machine.
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
  `unavailable` in file mode — use [PAT mode](#github-copilot-setup) instead
  (or set `COPILOT_ENABLED=false`).
- **PAT mode shows usage-based $ spend as its second gauge** (not chat), and
  needs the plan cap to draw the premium-request percentage — set
  `COPILOT_PLAN` (or `COPILOT_PREMIUM_ALLOWANCE`). The billing report has no
  allowance field of its own.
- Claude Code refreshes its own access token. If you have not opened Claude
  Code for a while, Anthropic may return `HTTP 401` to codervis until the host
  CLI runs and refreshes `~/.claude/.credentials.json`. Open Claude Code from a
  terminal on the host, then wait for the next dashboard refresh.
- Read-only bind mounts: codervis never writes to `~/.claude`, `~/.codex`,
  or your Cursor directory. The Cursor `state.vscdb` is opened read-only
  without `immutable` so SQLite sees live WAL-mode writes. If Docker's
  read-only mount prevents SQLite from opening the WAL sidecars directly,
  codervis reads from a short-lived temp snapshot inside the container.
- If you don't use an agent, set its `*_ENABLED=false`. That panel remains
  visible but dimmed with a `disabled` source state.

## Prerequisites

- Docker Desktop (or any recent Docker + Compose v2)
- Any combination of, installed and signed in on the host:
  - Claude Code (so `~/.claude/.credentials.json` exists)
  - Codex CLI (so `~/.codex/auth.json` exists)
  - Cursor (so `…/Cursor/User/globalStorage/state.vscdb` exists)
  - GitHub Copilot — either a client that writes `…/github-copilot/apps.json`
    (Neovim/JetBrains/Eclipse/language-server), or a fine-grained PAT for VS
    Code users (see [Copilot setup](#github-copilot-setup))

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
| `CODEX_ENABLED` | Set to `false` to render the Codex panel dimmed with a `disabled` state. | `true` |
| `CURSOR_ENABLED` | Set to `false` to render the Cursor panel dimmed with a `disabled` state. | `true` |
| `COPILOT_ENABLED` | Set to `false` to render the Copilot panel dimmed with a `disabled` state. | `true` |
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
| `CLAUDE_AI_HOST` | Override the Claude host (rarely needed). | `https://claude.ai` |
| `CHATGPT_HOST` | Override the Codex host (rarely needed). | `https://chatgpt.com` |
| `CURSOR_HOST` | Override the Cursor host (rarely needed). | `https://cursor.com` |
| `GITHUB_API_HOST` | Override the Copilot/GitHub API host (rarely needed). | `https://api.github.com` |

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

### Windows note

Docker Compose on Windows does **not** expand `~` in bind-mount paths, so
the home-directory defaults only work if you set the paths explicitly. The
simplest thing is:

```dotenv
CLAUDE_HOME=C:/Users/yourname/.claude
CODEX_HOME=C:/Users/yourname/.codex
CURSOR_HOME=C:/Users/yourname/AppData/Roaming/Cursor
COPILOT_HOME=C:/Users/yourname/.config/github-copilot
```

(Forward slashes work fine inside `.env`.)

If you don't use an agent, point its `*_HOME` at any existing directory and
set its `*_ENABLED=false`. The panel will be dimmed with a `disabled` state
and the bind mount won't be touched by the app.

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
python -m py_compile app/main.py app/quota.py app/claude_activity.py app/codex_quota.py app/codex_activity.py app/cursor_quota.py app/cursor_activity.py app/copilot_quota.py app/copilot_activity.py
```

The tests use temporary directories, an in-memory SQLite DB for the Cursor
path, and stubbed upstream clients. They do not read your real credential
files and do not call the live quota endpoints.

## What you see

A 2×2 grid, one panel per agent (Claude Code, Codex, Cursor, GitHub Copilot):

- **Claude & Codex** each show a **5-Hour Window** gauge (percentage of your
  5-hour rolling quota used) and a **Weekly Window** gauge (same, on a 7-day
  window), each with a countdown to when it resets.
- **Cursor** shows a **Premium Requests (month)** gauge and a **Usage-Based
  Spend (month)** gauge, both on your monthly billing cycle. The line under
  each meter shows the raw figures (e.g. `42 / 500 reqs`, `$3.40 / $20.00`).
  On the free plan these have no cap, so the percentage reads "—".
- **Copilot** shows a **Premium Requests (month)** gauge (e.g. `34 / 300 reqs`)
  and a **Chat (month)** gauge, both on your monthly cycle. On Pro/Pro+ the
  chat gauge reads "unlimited", so its percentage shows "—".
- Per-panel header shows the source state (`live` / `unavailable` /
  `disabled`); footer shows the plan and most recent local activity. Claude
  activity comes from project transcript timestamps; Codex from local
  history/session file metadata; Cursor from `state.vscdb` and
  History/workspace directory metadata; Copilot from `apps.json`/`hosts.json`
  config metadata (a coarser signal — Copilot keeps no local transcript).
  The footer shows an error string when a live quota call fails.

The fill colour is computed from the percentage: lime under 50%, sliding
through amber, to coral as you approach 100%. Panels in `unavailable` or
`disabled` state are dimmed.

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
│   ├── templates/
│   │   └── index.html
│   └── static/
│       ├── style.css
│       └── app.js
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
| `claude_credentials_present: false` from `/healthz` | Bind mount didn't pick up the credentials file. Verify `CLAUDE_HOME` points at your real `.claude` directory. |
| `codex_credentials_present: false` from `/healthz` | Same, for `CODEX_HOME` / `~/.codex/auth.json`. |
| `cursor_credentials_present: false` from `/healthz` | Bind mount missed `state.vscdb`. Verify `CURSOR_HOME` points at your Cursor data dir (it must contain `User/globalStorage/state.vscdb`). |
| `copilot_credentials_present: false` from `/healthz` | Bind mount missed `apps.json`/`hosts.json`. Verify `COPILOT_HOME` points at your Copilot config dir. |
| Browser shows `reconnecting…` | The container restarted; SSE will reconnect on its own. |

`/healthz` returns JSON with `data_root_exists` and `credentials_present`
flags that are useful for quick diagnosis.

## Security notes

- `~/.claude/.credentials.json`, `~/.codex/auth.json`, Cursor's
  `state.vscdb`, and Copilot's `apps.json`/`hosts.json` all contain
  long-lived session tokens. All four bind mounts are read-only, and
  `state.vscdb` is opened in SQLite read-only mode. codervis reads only the
  `oauth_token` from the Copilot config and never logs it. The
  default Compose port mapping publishes the dashboard on all host interfaces,
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
