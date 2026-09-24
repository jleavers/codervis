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

**Outbound traffic is allow-listed.** The dashboard's container sits on an
internal Docker network with no route off the host. Its only way out is the
`egress` service, a CONNECT-only proxy that admits `claude.ai` and
`chatgpt.com` and nothing else. A redirect, a host override or a compromised
dependency therefore cannot carry a token anywhere else, and plain `http://` is
refused outright. Docker cannot publish a port from an internal-only container,
so the port you open in the browser belongs to `ingress`, a relay that forwards
to the dashboard. Neither gateway service holds a credential.

**Inbound traffic is yours to name.** There is no login, so who can reach the
dashboard *is* its access control, and the default is this machine and nothing
else: the port is published on `127.0.0.1` (`DASHBOARD_BIND`) and the app
answers only the host names you listed (`DASHBOARD_ALLOWED_HOSTS`). Serving
anyone else is a change you make on purpose — see
[Serving other machines](#serving-other-machines).

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
| `DASHBOARD_BIND` | Host address the dashboard's port is published on. The default serves this machine alone; `0.0.0.0` serves every interface. Widen it together with `DASHBOARD_ALLOWED_HOSTS`. | `127.0.0.1` |
| `DASHBOARD_ALLOWED_HOSTS` | Host names a browser may use to reach the dashboard, comma- or space-separated. Checked on every route, `/static` and `/healthz` included; anything else gets `403`. Names match exactly and a port is ignored, so `dash.example` covers `dash.example:8765` but not `sub.dash.example`, and `*.dash.example` is not a pattern — it is dropped with a warning. `*` on its own accepts any name. | `localhost,127.0.0.1,::1` |
| `DASHBOARD_PORT` | Host port the dashboard listens on. A port alone: an `address:port` value used to work here and no longer does — the address is `DASHBOARD_BIND`. | `8765` |
| `REFRESH_INTERVAL_SECONDS` | How often the browser is pushed a fresh snapshot. | `5` |
| `QUOTA_REFRESH_INTERVAL_SECONDS` | How often each provider's quota is fetched in the background. This alone decides how often your token is sent upstream — browsers and tabs do not add fetches. Keep ≥ refresh interval. Old name `QUOTA_CACHE_TTL_SECONDS` still works. | `30` |
| `CLAUDE_ACTIVITY_REFRESH_INTERVAL_SECONDS` | How often Claude transcript timestamps are scanned. Old name `CLAUDE_ACTIVITY_CACHE_TTL_SECONDS` still works. | `5` |
| `CODEX_ACTIVITY_REFRESH_INTERVAL_SECONDS` | How often Codex activity metadata is scanned. Old name `CODEX_ACTIVITY_CACHE_TTL_SECONDS` still works. | `5` |
| `STARTUP_REFRESH_WAIT_SECONDS` | How long startup waits for the first refresh of every source, so the first page load shows real data. The app starts either way. | `2` |
| `CLAUDE_AI_HOST` | Override the Claude host (rarely needed). Must be `https://`; add the host to `EGRESS_ALLOW`. | `https://claude.ai` |
| `CHATGPT_HOST` | Override the Codex host (rarely needed). Must be `https://`; add the host to `EGRESS_ALLOW`. | `https://chatgpt.com` |
| `EGRESS_ALLOW` | Extra hosts the egress proxy admits, comma- or space-separated. `host` means port 443, `host:port` names another, and `.example.com` admits the domain and everything under it. It extends the built-in `claude.ai` and `chatgpt.com`; it never replaces them. | empty |

### Read budgets

Everything that feeds the payload is written by someone else: the vendor's
response body, whatever an operator's `CLAUDE_AI_HOST`/`CHATGPT_HOST` points
at, and whatever writes under `~/.claude`. Each of those reads is therefore
bounded. A byte cap applies to all of them, so nothing grows without bound; a
deadline applies to the ones that can be given one — the upstream body, and a
local activity scan as a whole. The credential files get a byte cap only, since
a read of a hung mount cannot portably be interrupted from here. Exceeding a
budget degrades that source to `unavailable` in the UI until its next refresh,
and no other route is affected. As a backstop for the reads that have no
deadline, a source whose last successful refresh has gone stale is also
reported `unavailable` rather than serving numbers that have stopped being
updated. "Stale" is three of its own refresh intervals, or its interval plus
its read budget plus 30 s, whichever is larger — so raising a deadline below
raises that limit with it, so a source is not reported unavailable for
spending its whole budget. The
shipped values suit the real endpoints, and a value that cannot be parsed is
ignored in favour of the default.

| Variable | What it does | Default |
| --- | --- | --- |
| `QUOTA_TIMEOUT_SECONDS` | urllib's timeout, per socket operation. | `8` |
| `QUOTA_TOTAL_DEADLINE_SECONDS` | Deadline across a whole quota fetch, including both of Codex's candidate paths. This is what bounds a sender that trickles bytes forever, which the per-operation timeout cannot. | `10` |
| `QUOTA_MAX_RESPONSE_BYTES` | Most an upstream usage response may be. | `1048576` |
| `CREDENTIALS_MAX_BYTES` | Most `.credentials.json` / `auth.json` may be. They are read on every refresh and sit in the same writable tree as the transcripts. | `1048576` |
| `ACTIVITY_SCAN_DEADLINE_SECONDS` | Deadline across a whole local activity scan. A scan that runs out reports what it found and catches up next time. | `5` |
| `ACTIVITY_MAX_LINE_BYTES` | Most one transcript record may be. A longer one is skipped; the rest of the file is still read. | `1048576` |
| `ACTIVITY_MAX_FILE_BYTES` | Most that is read from one transcript file. | `16777216` |
| `ACTIVITY_MAX_FILES` | Most files one activity scan walks. | `20000` |

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

Open <http://localhost:8765> (or whichever port you set) on the machine running
it. By default that is the only machine it answers: the port is published on
`127.0.0.1` and the app serves only `localhost`, `127.0.0.1` and `::1`.

### Serving other machines

A browser on another machine needs both halves widened, in `.env`:

```bash
# The interface they reach you on (or 0.0.0.0 for all of them).
DASHBOARD_BIND=192.168.1.10
# Every name or address they type, alongside the local ones.
DASHBOARD_ALLOWED_HOSTS=localhost,127.0.0.1,::1,192.168.1.10
```

Then `docker compose up -d`. A request whose `Host` is not on the list gets
`403` from every route, which is also what a page doing DNS rebinding gets.

Anyone who can reach the port can read your dashboard: there is no login, and
the list of names is not one. Widen it on a network you trust, and see
[Security notes](#security-notes) before you reach for a reverse proxy.

To confirm the egress bound from inside the dashboard's container:

```bash
docker compose exec codervis python -m app.egress check
```

```text
[ OK ] http://egress:3128 refused egress-probe.invalid (403)
[ OK ] CLAUDE_AI_HOST: claude.ai:443 admitted
[ OK ] CHATGPT_HOST: chatgpt.com:443 admitted
[ OK ] example.com:443 unreachable directly: no route round the proxy
```

The admission probes open a TCP connection to each host through the proxy and
send nothing. To change the allow-list, edit `EGRESS_ALLOW` in `.env` and run
`docker compose up -d egress`.

To stop:

```bash
docker compose down
```

## Test

Install development dependencies, then run the suite:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
python -m py_compile app/main.py app/quota.py app/claude_activity.py app/codex_quota.py app/codex_activity.py app/refresh.py app/budget.py app/egress.py app/ingress.py
```

The tests use temporary directories and stubbed upstream clients. They do not
read your real credential files and do not call the live quota endpoints. The
proxy and relay tests use loopback sockets only.
`tests/test_compose_topology.py` renders `docker-compose.yml` with
`docker compose config`, which needs the Docker CLI but no daemon. It is skipped
where Docker is not installed.

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
│   ├── refresh.py       # One background refresher per source: when a source is read
│   ├── budget.py        # What a single payload-feeding read may cost
│   ├── egress.py        # Allow-listing CONNECT proxy: the dashboard's only route out
│   ├── ingress.py       # Relay that publishes the dashboard's port
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
| Chip shows `unavailable` with `Tunnel connection failed: 403 Forbidden` | The host is not on the egress allow-list: a `CLAUDE_AI_HOST`/`CHATGPT_HOST` override without a matching `EGRESS_ALLOW` entry, or the vendor redirected to another host. `docker compose logs egress` names the host it refused. |
| Chip shows `unavailable` with `HTTP Error 405: Method Not Allowed` | A host override uses `http://`. Egress is HTTPS only. |
| Browser shows `Host not served by this dashboard` (`403`) | The name in the address bar is not in `DASHBOARD_ALLOWED_HOSTS`. Add it (and widen `DASHBOARD_BIND` if the request comes from another machine), then `docker compose up -d`. |
| `docker compose up` reports `dependency failed to start` | The `egress` proxy is unhealthy, and the dashboard waits for it. Check `docker compose logs egress`. |

`/healthz` returns JSON with `data_root_exists` and `credentials_present`
flags that are useful for quick diagnosis.

## Security notes

- `~/.claude/.credentials.json` and `~/.codex/auth.json` both contain
  long-lived session tokens. Both bind mounts are read-only.
- **The dashboard has no login, so reachability is the whole of its access
  control, and you set it.** `DASHBOARD_BIND` publishes the port on
  `127.0.0.1` by default, and `DASHBOARD_ALLOWED_HOSTS` names the hosts the
  app answers; anything else gets `403` on every route. Together they keep out
  three kinds of client that would otherwise read your usage, your plan tier
  and — at roughly ten-second resolution, through `last_activity` — whether
  you are at the keyboard:
  - machines on any network this host joins;
  - other containers on this Docker host, which reach a published port
    through the bridge gateway;
  - any web page you visit, by pointing a name of its own at `127.0.0.1`
    (DNS rebinding) — which the host list refuses even on a loopback-only
    instance.

  A host firewall does not stop the first two: Docker's forwarding runs ahead
  of ufw's and firewalld's rules, so these settings are what decide it.
- **To reach it from outside this machine, put a reverse proxy with auth in
  front of it — and let the proxy be the only way in.** Keep
  `DASHBOARD_BIND=127.0.0.1` so the dashboard's own port stays off the
  network, point the proxy at `127.0.0.1:8765`, and put the name browsers type
  at the proxy in `DASHBOARD_ALLOWED_HOSTS`, alongside the local names —
  that name is what arrives as `Host` from Caddy or Traefik, while nginx sends
  the upstream's name unless you set `proxy_set_header Host $host`. A proxy that
  authenticates callers while port 8765 is published beside it on the same
  network authenticates nobody. **Do not publish this port to the public
  internet**, proxy or no proxy.
- Outbound traffic from the dashboard's container can only reach the hosts on
  the egress allow-list, and only over HTTPS. The proxy sees host names, never
  the TLS session or the tokens inside it. This bounds where a token can be
  sent; it does not change who can reach the published port.
- Whoever can reach the port is bounded in what they can cost: `ingress` holds
  at most 256 connections, and it drops a client that has not sent a complete
  request within 10 seconds. Every service's log is capped at 3 × 10 MB.
- The dashboard never logs the tokens. If you regenerated `usage-debug.log`
  during setup, delete it.
