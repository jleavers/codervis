# codervis

[![CI](https://github.com/jleavers/codervis/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/jleavers/codervis/actions/workflows/ci.yml?query=branch%3Amain)
[![Licence: Apache 2.0](https://img.shields.io/badge/licence-Apache%202.0-blue.svg)](LICENSE)

A local web dashboard that shows your **Claude Code** and **Codex CLI** quota
usage live, displayed as geiger-style meters that shift from **lime → amber →
coral** as you approach the limit.

![The codervis dashboard: a Claude Code card with 5-hour, weekly and Fable weekly meters, and a Codex card with 5-hour and weekly meters. As the percentages climb from single digits to the high nineties, the meters and their readouts shift from lime through amber to coral. The data shown is fabricated.](docs/images/usage-ramp.gif)

## How it works

Each coding agent stores a credential locally that its own usage endpoint
accepts. codervis reads each one and polls the matching undocumented endpoint:

| Agent | Credential (read-only) | Endpoint(s) | Windows |
| --- | --- | --- | --- |
| Claude Code | `~/.claude/.credentials.json` → `claudeAiOauth.accessToken` (bearer) | `GET claude.ai/api/oauth/usage` | 5-hour + all-model weekly + Fable weekly utilization |
| Codex CLI | `~/.codex/auth.json` → `tokens.access_token` + `account_id` (bearer) | `GET chatgpt.com/backend-api/wham/usage` | 5-hour + weekly utilization |

codervis runs as a small FastAPI container that **bind-mounts both data
directories read-only**, polls those endpoints on a fixed cadence, and pushes
each reading to the browser over Server-Sent Events. Neither endpoint is a
public API, so either can change without notice; a panel that loses its
endpoint reads `unavailable` rather than guessing.

Two small gateway services bound it. The dashboard's container has no route off
the host except `egress`, a proxy that admits `claude.ai` and `chatgpt.com` and
nothing else, and its port is published by `ingress` on `127.0.0.1` only, to an
app that answers only the host names you list. On Docker Engine 28.3.3 or newer
that default is this machine and nothing else; an older engine leaves part of
it open, and the [security model](docs/security-model.md) says what, and how
to close it.

## Prerequisites

- Docker Engine 28.3.3+ with Compose v2 — a Docker Desktop new enough to
  bundle it counts. Two of this stack's bounds are the engine's to keep: the
  egress bound needs 28.0+, and the loopback publish needs 28.0+, and 28.3.3+
  wherever firewalld runs. [The engine and your front
  door](docs/security-model.md#the-engine-and-your-front-door) and [Check the
  egress bound](docs/operations.md#check-the-egress-bound) say what an older
  engine leaves open, and what to do if you cannot upgrade.
- Either or both of, installed and signed in on the host:
  - Claude Code (so `~/.claude/.credentials.json` exists)
  - Codex CLI (so `~/.codex/auth.json` exists)

  **On macOS, Claude Code's file is usually not there.** Claude Code keeps its
  login in the macOS Keychain, and writes `~/.claude/.credentials.json` only
  when the Keychain refuses the write ([Claude Code's authentication
  docs](https://code.claude.com/docs/en/authentication#credential-management)).
  The container reads that file and cannot reach the Keychain, so on a Mac the
  Claude card normally reads `unavailable` with `stored credential unavailable
  or unusable`, and `/healthz` reports `claude_credentials_present: false`. The
  Claude activity footer still works, because it reads `~/.claude/projects/`.

## Quick start

```bash
cp .env.example .env
docker compose up --build -d
```

Open <http://localhost:8765> on the machine running it. By default that is the
only machine it answers: the port is published on `127.0.0.1` and the app
serves only `localhost`, `127.0.0.1` and `::1`. On an engine older than 28.3.3,
read [The engine and your front
door](docs/security-model.md#the-engine-and-your-front-door) first — part of
that default is the engine's to keep, and below that release it does not.

The defaults suit most hosts. **On Windows, set `CLAUDE_HOME` and `CODEX_HOME`
in `.env` explicitly**, because Compose does not expand `~` there ([Windows
note](docs/operations.md#windows-note)). Every other setting, and how to serve
machines other than this one, is in [operations.md](docs/operations.md).

To stop it:

```bash
docker compose down
```

## What you see

One panel per agent (Claude Code, Codex):

- **Claude** shows **5-Hour Window**, **Weekly Window**, and **Weekly Window
  (Fable)** gauges, each with a countdown when its reset time is available.
- **Codex** shows **5-Hour Window** and **Weekly Window** gauges with their reset
  countdowns. If Codex omits either window, that gauge reads “—”.
- Per-panel header shows the server source state (`live` / `unavailable`) or
  the browser-local presentation state (`disabled`); footer shows the plan
  and most recent local activity. Claude activity comes from project
  transcript timestamps; Codex from local history/session file metadata. When a
  live quota call fails the footer shows one of a fixed set of messages
  (`app/degrade.py`): the dashboard never echoes an exception's own text,
  because that text can carry the stored token.

The fill colour is computed from the percentage: lime under 50%, sliding
through amber, to coral as you approach 100%. Unavailable gauges and
browser-disabled cards are dimmed.

Each provider header has a browser-local toggle. Switching a widget off keeps
its card visible but dimmed, labels it `disabled`, and removes it from the
overall status summary. Choices are stored in browser `localStorage`, survive
container restarts, and do not affect other browsers. codervis does not
estimate quota usage from local transcripts; it reads only their timestamps
and metadata, to show each agent's last local activity.

## Security in brief

- **There is no login, so who can reach the dashboard is its access control.**
  The default is this machine alone: `DASHBOARD_BIND` publishes the port on
  `127.0.0.1`, and `DASHBOARD_ALLOWED_HOSTS` names the hosts the app answers.
  That holds on its own from Docker Engine 28.3.3. Below it, an older engine
  lets machines on your own network segment in, and a rule in Docker's
  `DOCKER-USER` chain is what closes that ([The engine and your front
  door](docs/security-model.md#the-engine-and-your-front-door)). **Do not
  publish the port to the public internet**; to reach the dashboard from
  elsewhere, put a reverse proxy with authentication in front of it ([Security
  notes](docs/security-model.md#security-notes)).
- **The tokens stay in the container.** Both home trees are mounted read-only,
  and a token never reaches the page or the log. Code in that container can
  read the whole of both trees, though, and
  [Caveats](docs/security-model.md#caveats) says what that leaves readable.
- **Outbound traffic is allow-listed.** The container's only way out is the
  `egress` proxy, to `claude.ai` and `chatgpt.com` over HTTPS.
  `docker compose exec codervis python -m app.egress check` establishes that on
  your own host by dialling what the container can reach ([Check the egress
  bound](docs/operations.md#check-the-egress-bound)).
- **The dashboard's third-party code is fixed by content.** Every package it
  installs is pinned by hash and its base image by digest, and the page loads
  nothing from a CDN, under a Content-Security-Policy that names only its own
  origin.

[`docs/security-model.md`](docs/security-model.md) is the whole of it, and
[`SECURITY.md`](SECURITY.md) is how to report a way round it.

## Troubleshooting

The four most common; [operations.md](docs/operations.md#troubleshooting) has
every message the dashboard can show.

| Symptom | Likely cause |
| --- | --- |
| Chip shows `unavailable` with `upstream rejected the stored credential` | The agent's access token has expired and the host CLI has not refreshed it yet. Open Claude Code (or Codex) from a terminal on the host, then wait for the next dashboard refresh. |
| Chip shows `unavailable` with `stored credential unavailable or unusable` | `~/.claude/.credentials.json` or `~/.codex/auth.json` is missing, unreadable inside the container, not JSON, or has no access token. On macOS, see [Prerequisites](#prerequisites). |
| Chip shows `unavailable` with `upstream unreachable` | The upstream host could not be reached. `docker compose logs egress` names a host it refused. |
| Browser shows `Host not served by this dashboard` (`403`) | The name in the address bar is not in `DASHBOARD_ALLOWED_HOSTS`. Add it (and widen `DASHBOARD_BIND` if the request comes from another machine), then `docker compose up -d`. |

## Documentation

- [`docs/operations.md`](docs/operations.md): every setting, serving other
  machines, checking the egress bound, and the full troubleshooting table.
- [`docs/security-model.md`](docs/security-model.md): what the container may
  read and reach, who can reach the dashboard, and what an older Docker engine
  leaves open.
- [`docs/package-layout.md`](docs/package-layout.md): what each file in the
  repository is for.
- [`CONTRIBUTING.md`](CONTRIBUTING.md): setting up, running the tests, and the
  conventions.
- [`SECURITY.md`](SECURITY.md): reporting a vulnerability, and what is in scope.

## Contributing

Issues and small pull requests are welcome; a large change is worth an issue
first. [`CONTRIBUTING.md`](CONTRIBUTING.md) has the setup, the conventions and
the one rule above the others: never read a credential file to check
something. Security reports go through
[private vulnerability reporting](https://github.com/jleavers/codervis/security/advisories/new),
not the issue tracker — [`SECURITY.md`](SECURITY.md) says what is in scope.

## Licence

[Apache License 2.0](LICENSE).
