# codervis

[![CI](https://github.com/jleavers/codervis/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/jleavers/codervis/actions/workflows/ci.yml?query=branch%3Amain)
[![Licence: Apache 2.0](https://img.shields.io/badge/licence-Apache%202.0-blue.svg)](LICENSE)

A local web dashboard that shows your **Claude Code** and **Codex CLI** quota
usage live, displayed as geiger-style meters that shift from **lime → amber →
coral** as you approach the limit.

![The codervis dashboard: a Claude Code card with 5-hour, weekly and Fable weekly meters, and a Codex card with 5-hour and weekly meters. As the percentages climb from single digits to the high nineties, the meters and their readouts shift from lime through amber to coral. The data shown is fabricated.](docs/images/usage-ramp.gif)

## Contents

- [How it works](#how-it-works) — what is read, what is called, and the two bounds around it
- [Prerequisites](#prerequisites) · [Setup](#setup) · [Run](#run) · [Test](#test)
- [What you see](#what-you-see) · [Troubleshooting](#troubleshooting)
- [Security notes](#security-notes) — who can reach it, and where a token can go
- [Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md) · [Licence](#licence)

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

**What code in this container is allowed is two things, and this is both of
them.** The principal to read that against is a dependency of the app that has
been compromised: it runs with everything the app has, and both tokens are in
the container by design. So what it is allowed is written down here, once, with
each half no wider than the truth:

- **It can read both agent home trees, read-only, and no other file of yours.**
  The app itself reads seven paths inside them — two under `~/.claude`
  (`.credentials.json`, `projects/`) and five under `~/.codex` (`auth.json`,
  `history.jsonl`, `session_index.jsonl`, `sessions/`, `archived_sessions/`),
  and it stats each root besides, which is what `/healthz` reports. The mounts
  are the whole trees rather than those paths, because each credential file
  sits at its tree's root and a mount of a file follows the inode it was made
  from: a CLI that refreshes a token by writing a new file and renaming it over
  the old one — and a logout and login, which replaces the file for certain —
  would leave this container reading what was replaced. Everything else in both
  trees is therefore readable by code in that container too, and
  [Caveats](#caveats) names what that is.
- **It can reach the hosts on the egress allow-list, and no host off this
  project's own network.** That bounds the destination host and nothing inside
  the connection: the proxy relays the TLS session without opening it, so which
  account or tenant a token is used against at an allowed host — and anything
  else inside the tunnel — is not bounded by anything here.

Neither half contains a compromise of the image; what they do is keep
everything outside those two budgets out of a compromised dependency's reach.

**Outbound traffic is allow-listed.** The dashboard's container sits on an
internal Docker network that gives it no default route and leaves the host no
address on the network's bridge, so every peer it can dial is another
container in this project. Its only way out is the `egress` service, a
CONNECT-only proxy that admits `claude.ai` and `chatgpt.com` and nothing else.
A redirect, a host override or a compromised dependency therefore cannot send
a token to a host that is not on that list, and plain `http://` is refused
outright. Both halves of the network bound are asserted by probing, not by
assumption: `python -m app.egress check` dials what the container can actually
reach ([below](#check-the-egress-bound)), and it needs Docker Engine 28.0+ to
be true — an engine from before then either refuses the option that keeps the
host off the bridge or ignores it, and the check is what says which. Docker
cannot publish a port from an internal-only container, so the port you open in
the browser belongs to `ingress`, a relay that forwards to the dashboard.
Neither gateway service holds a credential.

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
reads timestamp/metadata to show each agent's local last activity. Each
reader reaches the filesystem only through `app/activity_gate.py`, which admits
regular files inside that reader's own allow-listed subtrees and follows no
link out of them.

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
  Code for a while, Anthropic rejects codervis's call until the host CLI runs
  and refreshes `~/.claude/.credentials.json`, and the card reads `unavailable`
  with `upstream rejected the stored credential`. Open Claude Code from a
  terminal on the host, then wait for the next dashboard refresh.
- **A hard-linked data directory reads as no activity.** The activity readers
  refuse any file with more than one name, because a second name inside the
  allow-list can be a file outside it and nothing on the path shows which.
  If a snapshot or deduplication tool (`rsnapshot`, `rsync --link-dest`,
  `cp -al`, `jdupes -L`, `rdfind`) has hard-linked the files under `~/.claude`
  or `~/.codex`, that provider's footer reads "no recent activity" however
  recently you used it. Quota gauges are unaffected. `find ~/.claude/projects
  -type f -links +1` lists what is being skipped.
- **Both bind mounts are whole home trees, and read-only.** codervis never
  writes to `~/.claude` or `~/.codex`, but any code in its container can read
  all of both — settings and config files, and any third-party secret an env
  block in one of them holds, alongside the transcripts, session files and
  history the activity readers use. They are not narrowed to the seven paths it
  reads inside them ([How it works](#how-it-works)) because each credential
  file sits at the root of its tree, and a bind mount of a single file follows
  the inode it was made from rather than the name: if a CLI refreshes its token
  by writing a new file and renaming it over the old one, or when you log out
  and back in, the container would go on reading the file that was replaced and
  that card would read `unavailable` until the next `docker compose up`.
  (Compose's short volume syntax also *creates* a source path that is missing,
  as a directory.) Narrowing the mounts yourself means taking that failure
  instead, which is why this repository does not.
- The `*_ENABLED` variables only choose the initial toggle state for a browser
  with no saved preference. All provider clients are still constructed and
  polled, so these variables do not suppress credential reads or upstream
  calls.

## Prerequisites

- Docker Engine 28.0+ with Compose v2 — a Docker Desktop new enough to bundle
  it counts. The `inside` network asks the bridge driver for
  `gateway_mode_ipv4: isolated`, so that the host holds no address on it. Older
  engines leave that undone in two different ways: 27.x refuses to create the
  network at all, and 26.x and older start the stack with the host still on that
  bridge, saying nothing. [Check the egress bound](#check-the-egress-bound) has
  both, and what to do if you cannot upgrade.
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
| `ACTIVITY_MAX_FILES` | Most directory entries one activity scan walks. Charged per entry looked at, not per file used. | `20000` |

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

### Check the egress bound

To confirm it from inside the dashboard's container:

```bash
docker compose exec codervis python -m app.egress check
```

```text
[ OK ] http://egress:3128 refused egress-probe.invalid (403)
[ OK ] CLAUDE_AI_HOST: claude.ai:443 admitted
[ OK ] CHATGPT_HOST: chatgpt.com:443 admitted
[ OK ] 172.30.0.1 is on-link by design: the proxy http://egress:3128, which is the allow-listed way off this project rather than a way round it
[ OK ] example.com:443 unreachable directly: no route to a public address round the proxy
```

The last line has six more forms, and the difference between the first four
is what a failed name lookup is allowed to prove. A container whose
resolver declines public names — which is what an internal network's usually
does — cannot look `example.com` up at all, and a lookup that failed says
nothing on its own about whether packets can leave. So the check reads the
routing table, which is where `internal: true` shows up as the absence of a
default route:

```text
[ OK ] example.com does not resolve here, and the routing table names no default route: there is no route round the proxy to take
[ OK ] example.com could not be looked up, because the resolver did not answer, and the routing table names no default route: there is no route round the proxy to take
[FAIL] example.com could not be looked up, and this container has a default route: it has a way off its own subnets, and whether that reaches round the proxy is unverified
[FAIL] example.com could not be looked up and /proc/net/route could not be read, so neither way of telling whether this container has a route off it was available and the bound is unverified
```

Only the first two are passes, and the routing table is what makes them so.
What separates those two is the resolver, not the routing: the first is a
resolver that answered and declined the name, the second one that did not
answer at all — a container that cannot reach its own resolver. The table
settles both, because it says what it says either way, and the other two
lines are `FAIL`s because of what it said: it named a default route, or it
could not be read. The two forms not shown here are the other half of the
line: the name resolved and something answered it, which is a route round
the proxy; or the probe could not be made
(`example.com:443 was not settled`), which is `unverified` like the two
`FAIL`s above — nothing was established either way.

The address on the on-link line is whatever the container's own routing tables
yield — the first address of each on-link subnet, plus any gateway a route
names — so it differs between deployments,
and a container on two networks gets one line per network. Both families are
read (`/proc/net/route` and `/proc/net/ipv6_route`), so a network with
`enable_ipv6` has its second gateway address dialled as well: that address is
on-link in the container's own prefix and needs no route either. A container on
a kernel with no IPv6 has no second table, and the line says nothing about a
family that is not there. The admission probes
open a TCP connection to each host through the proxy and send nothing; the
on-link and direct probes open one directly and send nothing either. An address
that answers at all answers at once; it is the `OK` that costs one timeout per
port, so that is the line that can take a few seconds to print. The
public-name line has a bound of its own, and that bound covers the name
lookup: at most ten seconds to resolve `example.com` and dial the addresses
it resolves to, together. The lookup is guaranteed half of it and cut off
there; the dials get the rest, which after a quick lookup is nearly all of
it. Not ten seconds an address, and not a resolver's own budget first —
`/etc/resolv.conf` gives that `timeout:` seconds, 5 by default, once per
`attempts:` per nameserver, which is what a container that cannot reach its
resolver would otherwise wait out before any of this began. A resolver slower
than the five seconds the lookup gets makes the line the routing-table
assertion below rather than a dial, and says so. To change the allow-list,
edit `EGRESS_ALLOW` in `.env` and run `docker compose up -d egress`.

**What a resolver that does not answer costs** is worth stating separately,
because a name lookup is not covered by the timeout of the connection that
follows it: `getaddrinfo` takes none of its own and spends the resolver's budget
instead — `/etc/resolv.conf`'s `timeout:`, 5 seconds by default, once per
`attempts:` per nameserver listed — and a connection's timeout does not start
until the lookup has returned. So the names this command resolves carry
deadlines of their own:

- **Each proxy probe is bounded whole, at the 10 seconds it is given**, the
  lookup of the proxy's own name included — `egress`, which Docker's embedded
  DNS normally answers in under a millisecond. Half of the 10 seconds is the
  lookup's share and the connection takes what is left, shared in turn between
  the addresses the name resolved to: a proxy with an address per family whose
  first one is silent is still dialled on the second, inside the same 10
  seconds. There is one probe for the reserved name and one per configured
  upstream, so three on the default configuration. That is a ceiling of 30
  seconds across them, and a container that cannot reach its resolver at all
  spends about 5 of each probe's 10 — the lookup's share, after which the probe
  gives up with nothing to dial — so about 15 seconds in total rather than the
  resolver's own budget three times over. The full 30 is what a resolver that
  answers just in time followed by a proxy that never answers would cost.
- **The on-link line's peer labelling is bounded at 5 seconds a name**, which is
  one full resolver attempt, and it looks up two: this container's own name and
  the proxy's. That labelling is what decides whether an on-link address is
  accounted for rather than dialled, so a name that does not resolve in time
  loses its label, the address is dialled, and the line fails rather than
  passing — the same cost a name that does not resolve at all already had. The
  budget is per name rather than shared between them, so a slow lookup of this
  container's own name cannot spend the proxy's.

The 5 seconds is deliberately the generous end of the range. Too long and the
command is slow, which is what the bound is for; too short and the *proxy's*
label is lost on a resolver that was merely slow, which fails a deployment that
is whole. The public-name probe on the last line resolves a name too, and that
one lookup is still the resolver's own budget rather than the check's.

The two directions are separate bounds, and the on-link line is the one an
internal network does not settle on its own. `internal: true` withholds the
default route, which is what the last line asks about. It does **not** withhold
the host's own address on the network's bridge: that address is on-link in the
container's subnet and needs no route, so whatever the host listens on is a
second way off the dashboard. The compose file closes it with
`com.docker.network.bridge.gateway_mode_ipv4: isolated`.

**What the on-link line is really telling you** is who holds the first address
of the container's subnet, which is the address a bridge's gateway takes:

- **The proxy holds it** — the line above. An engine honouring `isolated`
  allocates no gateway address at all, so that address is free and the first
  container attached takes it, which the compose file's start order makes
  `egress`. The host is not on the bridge, and the proxy being there is the
  evidence of it.
- **Nobody answers on it** — also `OK`. Nothing holds the address, or nothing on
  it answers ports 443, 80 and 22.
- **It is this container's own address** — `OK`, and nothing was dialled:
  reaching yourself establishes nothing either way, so the line says so rather
  than passing over it.
- **Something that is neither answers** — `FAIL`, and on an engine that ignored
  the option that something is the host.

Which engine you have decides which of those you see:

| Docker Engine | What it does with `gateway_mode_ipv4: isolated` | What you see |
|---|---|---|
| 28.0 and newer | Honours it. No gateway address is allocated, and the bridge gets none. | The stack starts; the on-link line reads `OK`. |
| 27.x | Knows the option, not that value. Network creation fails with `unknown gateway mode isolated`. | `docker compose up` fails on the `inside` network. |
| 26.x and older | Does not know the option, and ignores it without a word. | The stack starts, the host keeps its address on the bridge, and the on-link line reads `FAIL`. |

How to read the on-link line, in the order the cases are worth knowing:

- A `FAIL` on it means the host is reachable from the dashboard's container,
  whether it *accepted* the connection or *refused* it — a refusal comes from
  a live host, so only what it happens to be listening on stands between a
  compromised dependency and the host. The firewall rule below closes it, and
  the line reads `OK` once it is in place — which is the one thing to read
  carefully: silence bounds the probe, not the network. An `OK` there says
  nothing answered the three ports asked, and a host that drops packets from
  that bridge looks exactly the same as a host that is not on it. That is why
  the bound is three assertions and not this one.
- A `FAIL` naming an address that turns out to be **another container in this
  project** is the start order, not the host: the check accounts for this
  container and the proxy, and on an engine honouring `isolated` the subnet's
  first address belongs to whichever container attached first. If `docker
  network inspect` (on the host) shows the address belongs to `ingress` rather
  than to `egress`, the bound is intact; `docker compose up -d --force-recreate`
  puts the start order back.
- A `FAIL` that says **unverified** — on this line or on the public-name one —
  is not a reachable host: it means the check could not ask. The causes, all of
  the ones a run of this command can print: the container's routing table was
  unreadable; its IPv6 routing table was there and unreadable, which leaves
  that one family unknown while the addresses of the other are still dialled;
  it yielded no address to dial; it yielded more than the check will dial, and
  the rest are named on that line; a connection never left the container (a
  local reject rule, a descriptor limit); the public name's lookup could not be
  made at all, which is not the same as a resolver declining it; the
  public-name probe ran out of budget with addresses of the name still
  undialled, so the name was only partly asked; or the public name could not be
  looked up, and the container either has a default route in either family or
  has a routing table that could not be read, so neither way of telling whether
  it can reach off its own subnets was available. An unasked question is
  reported as a failure rather than passed over, because that is the defect
  these lines exist to prevent.
- The budget cause above is the one an egress **firewall** can produce where
  `internal: true` cannot, and only where the name has more than one address.
  With no default route the kernel rejects each connect immediately — no
  route — so every address of `example.com` is dialled for nothing and the
  line passes. Where egress is bounded by *dropping* packets instead, the
  first address is silent for the whole budget and the rest go unasked, which
  is a `FAIL` saying so rather than a pass. Read it as "several addresses were
  silent and I ran out of time"; the check errs towards saying it does not
  know.

**If you cannot upgrade to 28.0+**, add a host firewall rule that drops new
inbound connections arriving on that bridge's interface; nothing in the stack
ever connects to the host over it. On 27.x you must also delete the
`driver_opts` block from the `inside` network, or the network is not created
at all — which then fails `tests/test_compose_topology.py`, since that block is
what the test pins; on 26.x and older the block is ignored and can stay.

### Stop

```bash
docker compose down
```

## Test

Install development dependencies, then run the suite:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
python -m py_compile app/main.py app/quota.py app/activity_gate.py app/claude_activity.py app/codex_quota.py app/codex_activity.py app/refresh.py app/budget.py app/server.py app/egress.py app/ingress.py
```

The tests use temporary directories and stubbed upstream clients. They do not
read your real credential files and do not call the live quota endpoints. The
proxy and relay tests use loopback sockets only.

That is enforced for the whole session by `tests/conftest.py`, not left to each
test remembering to stub what it uses. Before anything is collected it points
`CLAUDE_DATA_DIR` and `CODEX_DATA_DIR` at empty scratch directories and
`CLAUDE_AI_HOST` and `CHATGPT_HOST` at a loopback port nothing listens on, and
for the rest of the run an audit hook fails any test that opens a path under a
host agent data directory or dials a non-loopback address. So running
`python -m pytest` in a shell where those variables point at your real `~/.claude`
and `~/.codex` reads neither. **It affects `pytest` and nothing else** — no
agent, editor or shell configuration is installed or changed.
`tests/test_compose_topology.py` renders `docker-compose.yml` with
`docker compose config`, which needs the Docker CLI but no daemon. It is skipped
where Docker is not installed, unless `REQUIRE_DOCKER=1` says it must not be —
CI sets that, so the compose pins fail rather than vanish into a skip.

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

## File layout

```
.
├── app/
│   ├── main.py          # FastAPI app + SSE stream + the payload boundary
│   ├── degrade.py       # The fixed vocabulary the boundary reports failures with
│   ├── quota.py         # Claude live client → claude.ai/api/oauth/usage
│   ├── activity_gate.py # The one gate both activity readers reach the filesystem through
│   ├── claude_activity.py # Claude local activity timestamp reader
│   ├── codex_quota.py   # Codex live client → chatgpt.com/backend-api/wham/usage
│   ├── codex_activity.py # Codex local activity metadata reader
│   ├── refresh.py       # One background refresher per source: when a source is read
│   ├── budget.py        # What a single payload-feeding read may cost
│   ├── server.py        # The server the image launches, and its request/connection bound
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
├── tools/screenshots/   # Regenerates the README's image from fabricated data
├── docs/images/         # That image
├── .env.example
├── .gitignore
├── CONTRIBUTING.md
├── SECURITY.md
└── LICENSE
```

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Chip shows `unavailable` with `upstream rejected the stored credential` | The agent's access token has expired and the host CLI has not refreshed it yet. Open Claude Code (or Codex) from a terminal on the host, then wait for the next dashboard refresh. |
| Chip shows `unavailable` with `stored credential unavailable or unusable` | `~/.claude/.credentials.json` or `~/.codex/auth.json` is missing, unreadable inside the container, not JSON, or has no access token. A token containing a newline is also refused, because it cannot be sent as an HTTP header. |
| Chip shows `unavailable` with `upstream unreachable` | The upstream host could not be reached: no route out, the `egress` proxy refused the host name, a DNS failure, a dropped connection, or a `CLAUDE_AI_HOST`/`CHATGPT_HOST` override that is not a usable `https://…` URL. `docker compose logs egress` names a host it refused. |
| Chip shows `unavailable` with `upstream returned an error response` | The endpoint answered with a status other than 200 — including `405` when a `CLAUDE_AI_HOST`/`CHATGPT_HOST` override uses `http://`, since egress is HTTPS only. |
| Chip shows `unavailable` with `upstream response not understood` | The undocumented endpoint changed shape, or returned a value the payload schema refuses (a percentage that is not a finite number in 0–100, for instance). |
| Chip shows `unavailable` with `provider data is no longer being refreshed` | That source's background refresher has stopped advancing — almost always a read that cannot be interrupted, on a bind mount that has hung (an unreachable network mount, or a disk that is not answering). The last data it fetched is deliberately *not* shown, because it is no longer current. Check that `~/.claude` and `~/.codex` still answer (`ls` them on the host), then restart with `docker compose restart codervis`. `docker compose logs codervis` names the source. |
| Chip shows `unavailable` with `provider data unavailable` | Either that source has not finished its first refresh yet — expected for the first second or two after a start, and for longer if a source is slower than `STARTUP_REFRESH_WAIT_SECONDS` — or the provider's client failed in a way it declared but did not classify. If it persists past one refresh interval, treat it as the generic form of the rows above: check the credential file and the egress log first, and report it if neither explains it. |
| Chip shows `unavailable` with `internal error` | A bug in the dashboard rather than in the credential or the endpoint. Please report it. |
| `claude_credentials_present: false` from `/healthz` | Bind mount didn't pick up the credentials file. Verify `CLAUDE_HOME` points at your real `.claude` directory. |
| `codex_credentials_present: false` from `/healthz` | Same, for `CODEX_HOME` / `~/.codex/auth.json`. |
| Browser shows `reconnecting…` | The container restarted; SSE will reconnect on its own. |
| Every chip reads `unavailable` and `docker compose logs egress` shows a refused host | The host is not on the egress allow-list: a `CLAUDE_AI_HOST`/`CHATGPT_HOST` override without a matching `EGRESS_ALLOW` entry, or the vendor redirected to another host. |
| Browser shows `Host not served by this dashboard` (`403`) | The name in the address bar is not in `DASHBOARD_ALLOWED_HOSTS`. Add it (and widen `DASHBOARD_BIND` if the request comes from another machine), then `docker compose up -d`. |
| `python -m app.egress check` reports a `FAIL` on the on-link line, naming an address that accepted or refused | Something that is neither this container nor the proxy is on-link. On an engine older than 28.0 that is the host: a 26.x engine ignores `gateway_mode_ipv4` without a word and keeps its address on the bridge. Upgrade, or see [Check the egress bound](#check-the-egress-bound) for the firewall rule that replaces it — and for the case where the address is another container in this project. |
| `docker compose up` fails creating the `inside` network with `unknown gateway mode isolated` | A 27.x engine: it knows the option but not that value. Upgrade to 28.0+, or delete the `driver_opts` block from the `inside` network and use the firewall rule instead. |
| `docker compose up` reports `dependency failed to start` | The `egress` proxy is unhealthy, and the dashboard waits for it. Check `docker compose logs egress`. |

`/healthz` returns JSON with `data_root_exists` and `credentials_present`
flags that are useful for quick diagnosis.

## Security notes

- `~/.claude/.credentials.json` and `~/.codex/auth.json` both contain
  long-lived session tokens. Both bind mounts are read-only, and each is a
  whole home tree rather than the files the dashboard reads: [How it
  works](#how-it-works) states what code in that container is allowed, on both
  axes, and [Caveats](#caveats) says what the whole-tree mounts leave readable
  and why they stay whole.
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
- That bound is two things, and the second is easy to miss: the container has no
  default route, *and* the host holds no address on its network's bridge. Without
  the second, the bridge's gateway is on-link in the container's subnet and
  reachable with no route at all, so whatever the host listens on is a way off
  the dashboard for a compromised dependency holding both tokens.
  `docker compose exec codervis python -m app.egress check` is what tells you
  which you have, by dialling the addresses the container can reach rather than
  by trusting the compose file
  ([Check the egress bound](#check-the-egress-bound)). An engine older than 28.0
  either refuses the option (27.x) or ignores it without saying so (26.x and
  older), and there a host firewall rule that drops new inbound connections
  arriving on that bridge's interface is what closes it.
- Whoever reaches the dashboard is bounded in what they can cost, by the server
  that bears the cost and not only by the relay in front of it. The dashboard's
  own process (`app/server.py`, which is what the image launches) refuses a
  request head over 16 KiB with `431`, and a connection that has not completed
  a head within 10 seconds with `408` — on **every** request of a connection,
  not just the first, and whichever route the connection arrived by. A request
  **body** that has not arrived in full within 10 seconds of its head is
  refused too — with `408` where the dashboard has not answered yet, and by
  simply dropping the connection where it has, which is the case for every route
  it actually has, since none of them reads a body. Neither deadline is renewed
  by an arriving byte, so a peer cannot hold a connection by dribbling either
  half of a request. It holds at
  most 320 connections, refusing a further one with `503` as it is accepted,
  and answers `503` to a request that arrives once 320 connections or running
  requests are held, so at most 319 are served at a time. `ingress` is the
  outer layer on the published port, with the same head budget and deadline and
  a budget of 256 connections, applied to the first head of each connection
  before the dashboard is dialled at all; it bounds no body, because it relays
  bytes blind once it has read that first head. What is deliberately **not**
  bounded is a **response**: an SSE response lasts as long as the browser tab,
  and it costs one of the 320 connections and no more. Every service's log is
  capped at 3 × 10 MB.
- The dashboard never logs the tokens, and never serves an exception's own
  text: a failure is reported with one of the fixed messages in
  `app/degrade.py`. A debug capture of an upstream call made with the CLI's own
  `--debug` flags *does* carry the token, so treat any such log file as one.

## Contributing

Issues and small pull requests are welcome; a large change is worth an issue
first. [`CONTRIBUTING.md`](CONTRIBUTING.md) has the setup, the conventions and
the one rule above the others: never read a credential file to check
something. Security reports go through
[private vulnerability reporting](https://github.com/jleavers/codervis/security/advisories/new),
not the issue tracker — [`SECURITY.md`](SECURITY.md) says what is in scope.

## Licence

[Apache License 2.0](LICENSE).
