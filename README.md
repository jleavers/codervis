# codervis

A local web dashboard that shows your **Claude Code** and **Codex CLI** quota
usage live, side-by-side — the 5-hour and weekly utilization figures each
agent's own `/status` exposes, displayed as geiger-style meters that shift
from **lime → amber → coral** as you approach the limit.

## How it works

Each coding agent stores OAuth credentials in a dotfile under your home
directory. The same tokens are accepted by undocumented usage endpoints:

| Agent | Credentials file | Endpoint |
| --- | --- | --- |
| Claude Code | `~/.claude/.credentials.json` (field `claudeAiOauth.accessToken`) | `GET https://claude.ai/api/oauth/usage` |
| Codex CLI | `~/.codex/auth.json` (fields `tokens.access_token`, `tokens.account_id`) | `GET https://chatgpt.com/backend-api/wham/usage` |

Both return per-window utilization as a percentage plus a reset timestamp.

codervis runs as a small FastAPI container, **bind-mounts both data
directories read-only**, reads the access tokens, and polls the endpoints.
The browser gets live updates via Server-Sent Events; CSS animates the
meter fill and the colour shifts as the percentage rises.

If either live endpoint is unreachable for any reason (expired token, network
down, or the vendor changes the API), that panel renders an "unavailable"
state. codervis does not estimate quota usage from local transcripts.

### Caveats

- Both endpoints are **internal**, not part of Anthropic's or OpenAI's
  public API. They can change or disappear at any time.
- The dashboard reads the credentials files each CLI maintains. It does
  **not** implement OAuth flows of its own — you need Claude Code and/or
  Codex CLI installed and signed in on the host machine.
- Claude Code refreshes its own access token. If you have not opened Claude
  Code for a while, Anthropic may return `HTTP 401` to codervis until the host
  CLI runs and refreshes `~/.claude/.credentials.json`. Open Claude Code from a
  terminal on the host, then wait for the next dashboard refresh.
- Read-only bind mounts: codervis never writes to `~/.claude` or `~/.codex`.
- If you only use one of the two agents, set `CODEX_ENABLED=false`. The Codex
  panel remains visible but dimmed with a `disabled` source state.

## Prerequisites

- Docker Desktop (or any recent Docker + Compose v2)
- Claude Code installed and signed in on the host (so
  `~/.claude/.credentials.json` exists), and/or
- Codex CLI installed and signed in on the host (so `~/.codex/auth.json`
  exists)

## Setup

```bash
cp .env.example .env
```

Edit `.env`:

| Variable | What it does | Default |
| --- | --- | --- |
| `CLAUDE_HOME` | Host path to your Claude Code data dir. **On Windows set this explicitly** — e.g. `C:/Users/you/.claude`. | `~/.claude` |
| `CODEX_HOME` | Host path to your Codex CLI data dir. Same Windows caveat. | `~/.codex` |
| `CODEX_ENABLED` | Set to `false` to render the Codex panel dimmed with a `disabled` state. | `true` |
| `DASHBOARD_PORT` | Host port the dashboard listens on. | `8765` |
| `REFRESH_INTERVAL_SECONDS` | How often the browser is pushed a fresh snapshot. | `5` |
| `QUOTA_CACHE_TTL_SECONDS` | Server-side cache for the upstream calls. Keep ≥ refresh interval. | `30` |
| `CODEX_ACTIVITY_CACHE_TTL_SECONDS` | Server-side cache for Codex local activity metadata scans. | `5` |
| `CLAUDE_AI_HOST` | Override the Claude host (rarely needed). | `https://claude.ai` |
| `CHATGPT_HOST` | Override the Codex host (rarely needed). | `https://chatgpt.com` |

### Windows note

Docker Compose on Windows does **not** expand `~` in bind-mount paths, so
the defaults `${CLAUDE_HOME:-~/.claude}` and `${CODEX_HOME:-~/.codex}` only
work if you set both explicitly. The simplest thing is:

```dotenv
CLAUDE_HOME=C:/Users/yourname/.claude
CODEX_HOME=C:/Users/yourname/.codex
```

(Forward slashes work fine inside `.env`.)

If you don't use Codex, point `CODEX_HOME` at any existing directory and
set `CODEX_ENABLED=false`. The panel will be dimmed with a `disabled` state
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

## What you see

Two columns, one per agent (Claude Code on the left, Codex on the right):

- **5-Hour Window** gauge — percentage of your 5-hour rolling quota used,
  with a countdown to when it resets.
- **Weekly Window** gauge — same, on a 7-day window.
- Per-column header shows the source state (`live` / `unavailable` /
  `disabled`); footer shows the plan. Codex also shows the timestamp of your
  most recent local activity from local history/session file metadata. The
  footer shows an error string when a live quota call fails.

The fill colour is computed from the percentage: lime under 50%, sliding
through amber, to coral as you approach 100%. Panels in `unavailable` or
`disabled` state are dimmed.

## File layout

```
.
├── app/
│   ├── main.py          # FastAPI app + SSE stream
│   ├── quota.py         # Claude live client → claude.ai/api/oauth/usage
│   ├── codex_quota.py   # Codex live client → chatgpt.com/backend-api/wham/usage
│   ├── codex_activity.py # Codex local activity metadata reader
│   ├── templates/
│   │   └── index.html
│   └── static/
│       ├── style.css
│       └── app.js
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
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

- `~/.claude/.credentials.json` and `~/.codex/auth.json` contain long-lived
  OAuth bearer tokens. Both bind mounts are read-only. The default Compose
  port mapping publishes the dashboard on all host interfaces, so machines on
  your LAN can reach it at `http://<your-host-ip>:8765`.
  **Don't expose this port to the public internet** — anyone who can
  reach it can read your usage. If you need remote access, put it behind
  a reverse proxy with auth.
- The dashboard never logs the tokens. If you regenerated `usage-debug.log`
  during setup, delete it.
