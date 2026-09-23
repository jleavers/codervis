# codervis

A local web dashboard that shows your **Claude Code** and **Codex CLI** quota
usage live, displayed as geiger-style meters that shift from **lime → amber →
coral** as you approach the limit.

## How it works

Each coding agent stores a credential locally that its own usage endpoint
accepts. codervis reads each one and polls the matching undocumented endpoint:

| Agent | Credential (read-only) | Endpoint(s) | Windows |
| --- | --- | --- | --- |
| Claude Code | `~/.claude/.credentials.json` → `claudeAiOauth.accessToken` (bearer) | `GET claude.ai/api/oauth/usage` | 5-hour + all-model weekly + Fable weekly utilization |
| Codex CLI | `~/.codex/auth.json` → `tokens.access_token` + `account_id` (bearer) | `GET chatgpt.com/backend-api/wham/usage` | 5-hour + weekly utilization |

Claude and Codex return utilization as a percentage plus a reset timestamp.

codervis runs as a small FastAPI container, **bind-mounts both data
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

- Both live endpoints are **internal**, not part of the vendors' public APIs.
  They can change or disappear at any time.
- The dashboard reads the credential each tool maintains. It does
  **not** implement OAuth flows of its own — you need Claude Code and/or Codex
  CLI installed and signed in on the host machine.
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
- Claude Code refreshes its own access token. If you have not opened Claude
  Code for a while, Anthropic may return `HTTP 401` to codervis until the host
  CLI runs and refreshes `~/.claude/.credentials.json`. Open Claude Code from a
  terminal on the host, then wait for the next dashboard refresh.
- Read-only bind mounts: codervis never writes to `~/.claude` or `~/.codex`.
- The `*_ENABLED` variables only choose the initial toggle state for a browser
  with no saved preference. All provider clients are still constructed and
  polled, so these variables do not suppress credential reads or upstream
  calls.

## Prerequisites

- Docker Desktop (or any recent Docker + Compose v2)
- Either or both of, installed and signed in on the host:
  - Claude Code (so `~/.claude/.credentials.json` exists)
  - Codex CLI (so `~/.codex/auth.json` exists)

## Setup

```bash
cp .env.example .env
```

Edit `.env`:

| Variable | What it does | Default |
| --- | --- | --- |
| `CLAUDE_HOME` | Host path to your Claude Code data dir. **On Windows set this explicitly** — e.g. `C:/Users/you/.claude`. | `~/.claude` |
| `CODEX_HOME` | Host path to your Codex CLI data dir. Same Windows caveat. | `~/.codex` |
| `CLAUDE_ENABLED` | First-visit browser widget default. | `true` |
| `CODEX_ENABLED` | First-visit browser widget default. | `true` |
| `DASHBOARD_PORT` | Host port the dashboard listens on. | `8765` |
| `REFRESH_INTERVAL_SECONDS` | How often the browser is pushed a fresh snapshot. | `5` |
| `QUOTA_CACHE_TTL_SECONDS` | Server-side cache for the upstream calls. Keep ≥ refresh interval. | `30` |
| `CLAUDE_ACTIVITY_CACHE_TTL_SECONDS` | Server-side cache for Claude local transcript timestamp scans. | `5` |
| `CODEX_ACTIVITY_CACHE_TTL_SECONDS` | Server-side cache for Codex local activity metadata scans. | `5` |
| `CLAUDE_AI_HOST` | Override the Claude host (rarely needed). | `https://claude.ai` |
| `CHATGPT_HOST` | Override the Codex host (rarely needed). | `https://chatgpt.com` |

### Windows note

Docker Compose on Windows does **not** expand `~` in bind-mount paths, so
the home-directory defaults only work if you set the paths explicitly. The
simplest thing is:

```dotenv
CLAUDE_HOME=C:/Users/yourname/.claude
CODEX_HOME=C:/Users/yourname/.codex
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
python -m py_compile app/main.py app/quota.py app/claude_activity.py app/codex_quota.py app/codex_activity.py
```

The tests use temporary directories and stubbed upstream clients. They do not
read your real credential files and do not call the live quota endpoints.

## What you see

One panel per agent (Claude Code, Codex):

- **Claude** shows **5-Hour Window**, **Weekly Window**, and **Weekly Window
  (Fable)** gauges, each with a countdown when its reset time is available.
- **Codex** shows **5-Hour Window** and **Weekly Window** gauges with their reset
  countdowns. If Codex omits either window, that gauge reads “—”.
- Per-panel header shows the server source state (`live` / `unavailable`) or
  the browser-local presentation state (`disabled`); footer shows the plan
  and most recent local activity. Claude activity comes from project
  transcript timestamps; Codex from local history/session file metadata. The
  footer shows an error string when a live quota call fails.

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
| `claude_credentials_present: false` from `/healthz` | Bind mount didn't pick up the credentials file. Verify `CLAUDE_HOME` points at your real `.claude` directory. |
| `codex_credentials_present: false` from `/healthz` | Same, for `CODEX_HOME` / `~/.codex/auth.json`. |
| Browser shows `reconnecting…` | The container restarted; SSE will reconnect on its own. |

`/healthz` returns JSON with `data_root_exists` and `credentials_present`
flags that are useful for quick diagnosis.

## Security notes

- `~/.claude/.credentials.json` and `~/.codex/auth.json` both contain
  long-lived session tokens. Both bind mounts are read-only. The default
  Compose port mapping publishes the dashboard on all host interfaces,
  so machines on your LAN can reach it at `http://<your-host-ip>:8765`.
  **Don't expose this port to the public internet** — anyone who can
  reach it can read your usage. If you need remote access, put it behind
  a reverse proxy with auth.
- The dashboard never logs the tokens. If you regenerated `usage-debug.log`
  during setup, delete it.
