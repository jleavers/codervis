# codervis

A local web dashboard that shows your Claude Code quota usage live — the same
5-hour and weekly utilization figures you see in `/usage`, displayed as
geiger-style meters that shift from **lime → amber → coral** as you approach
the limit.

## How it works

Claude Code stores its OAuth credentials at `~/.claude/.credentials.json`.
That same token is accepted by the (undocumented) endpoint

```
GET https://claude.ai/api/oauth/usage
Authorization: Bearer <accessToken>
```

which returns:

```json
{
  "five_hour": { "utilization": 22.0, "resets_at": "2026-05-19T15:20:00Z" },
  "seven_day": { "utilization":  9.0, "resets_at": "2026-05-22T05:00:00Z" },
  ...
}
```

codervis runs as a small FastAPI container, **bind-mounts `~/.claude`
read-only**, reads the access token, and polls that endpoint. The browser
gets live updates via Server-Sent Events; CSS animates the meter fill and
the colour shifts as the percentage rises.

If the endpoint is unreachable for any reason (expired token, network down,
Anthropic changes the API), the dashboard falls back to an approximation
computed by parsing your local transcripts in `~/.claude/projects/`. The
footer shows which source is currently in use.

### Caveats

- `/api/oauth/usage` is an **internal endpoint**, not part of Anthropic's
  public API. It can change or disappear at any time. The fallback path is
  there for exactly that reason.
- The dashboard reads the credentials file Claude Code maintains. It does
  **not** implement its own OAuth flow — you need Claude Code installed and
  signed in on the host machine.
- Read-only bind mount: codervis never writes to `~/.claude`.

## Prerequisites

- Docker Desktop (or any recent Docker + Compose v2)
- Claude Code installed and signed in on the host (so
  `~/.claude/.credentials.json` exists)

## Setup

```bash
cp .env.example .env
```

Edit `.env`:

| Variable | What it does | Default |
| --- | --- | --- |
| `CLAUDE_HOME` | Host path to your Claude Code data dir. **On Windows set this explicitly** — e.g. `C:/Users/you/.claude`. | `~/.claude` |
| `DASHBOARD_PORT` | Host port the dashboard listens on. | `8765` |
| `REFRESH_INTERVAL_SECONDS` | How often the browser is pushed a fresh snapshot. | `5` |
| `QUOTA_CACHE_TTL_SECONDS` | Server-side cache for the upstream call. Keep ≥ refresh interval. | `30` |
| `CLAUDE_AI_HOST` | Override the host (rarely needed). | `https://claude.ai` |
| `FIVE_HOUR_TOKEN_LIMIT` | Only used in fallback mode — token threshold for the 5-hour gauge. | `500000` |
| `WEEKLY_TOKEN_LIMIT` | Only used in fallback mode — token threshold for the weekly gauge. | `3000000` |

### Windows note

Docker Compose on Windows does **not** expand `~` in bind-mount paths, so
the default `${CLAUDE_HOME:-~/.claude}` only works if you set `CLAUDE_HOME`
explicitly. The simplest thing is:

```dotenv
CLAUDE_HOME=C:/Users/yourname/.claude
```

(Forward slashes work fine inside `.env`.)

## Run

```bash
docker compose up --build -d
```

Open <http://localhost:8765> (or whichever port you set).

To stop:

```bash
docker compose down
```

## What you see

- **5-Hour Window** gauge — percentage of your 5-hour rolling quota used,
  with a countdown to when it resets.
- **Weekly Window** gauge — same, on a 7-day window.
- Footer shows the data source (`live` or `fallback`), your plan
  (`pro` / `max` / …), and the timestamp of your most recent Claude activity
  on this machine.

The fill colour is computed from the percentage: lime under 50%, sliding
through amber, to coral as you approach 100%.

## File layout

```
.
├── app/
│   ├── main.py          # FastAPI app + SSE stream
│   ├── quota.py         # live client → claude.ai/api/oauth/usage
│   ├── usage.py         # transcript-based fallback estimator
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
| Footer shows `source: fallback` | Credentials file missing inside the container, token expired, or the endpoint returned non-200. Check `docker compose logs codervis`. |
| `credentials_present: false` from `/healthz` | Bind mount didn't pick up the credentials file. Verify `CLAUDE_HOME` points at your real `.claude` directory. |
| Both gauges always 100% in fallback | Your transcripts exceed the configured `FIVE_HOUR_TOKEN_LIMIT` / `WEEKLY_TOKEN_LIMIT`. Tune those, or fix whatever is breaking the live call so you don't need them. |
| Browser shows `reconnecting…` | The container restarted; SSE will reconnect on its own. |

`/healthz` returns JSON with `data_root_exists` and `credentials_present`
flags that are useful for quick diagnosis.

## Security notes

- `~/.claude/.credentials.json` contains a long-lived OAuth bearer token. The
  bind mount is read-only and the dashboard binds to `127.0.0.1` by default
  (via the `DASHBOARD_PORT` mapping). **Don't expose this port to the public
  internet** — anyone who can reach it can read your usage. If you need
  remote access, put it behind a reverse proxy with auth.
- The dashboard never logs the token. If you regenerated `usage-debug.log`
  during setup, delete it.
