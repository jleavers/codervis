# Operations

Everything after the [README's](../README.md) quick start: every setting, serving machines
other than this one, checking the egress bound on your own host, and what each symptom
means. Why the bounds are what they are is [security-model.md](security-model.md).

## Configuration

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

## Serving other machines

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
[Security notes](security-model.md#security-notes) before you reach for a reverse proxy.

## Check the egress bound

The dashboard's container should have no way off this host except the `egress` proxy, and that
needs Docker Engine 28.0 or newer: an older engine either refuses the option that keeps the host
off the container's bridge or ignores it, and this command is what says which you have. To
confirm it from inside the dashboard's container:

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

**The command is safe to paste.** A proxy variable may carry userinfo —
`HTTPS_PROXY=http://user:secret@egress:3128` is valid, and an authenticating
proxy is configured that way — so every line that quotes the variable back
prints it with the userinfo replaced by `<userinfo redacted>`, keeping the
scheme, host and port so you can still see which proxy was dialled. The same
goes for a `CLAUDE_AI_HOST`/`CHATGPT_HOST` that carries one. An ordinary proxy
URL with no userinfo prints exactly as you configured it, which is what the
sample above shows — the redaction errs towards taking too much, so a value with
an `@` somewhere other than in front of the host loses the part before it too.
This matters because CI's `Egress bound` job runs this command into a public
Actions log.

**Every line it prints is a line it wrote.** A control character in one of those
variables is written out visibly rather than printed as itself — a newline as
`\n`, a tab as `\t`, anything else without a name by its code point, as
`\x1b`, `\u2028` or `\U000e0041`. Nothing is dropped, so the host you
configured is still there to recognise, and no two values print alike. This
matters because the parser that reads the value removes `\t`, `\r` and `\n`
from anywhere in a URL *before* reading it: a `HTTPS_PROXY` carrying a newline
names exactly the proxy it looks like it names, and printed as it was written it
would also break its own result line in two — the second half reading like a
verdict the command reached. The count of lines you read is the count of
assertions it made.

An override the command cannot read a host and a port out of — an unclosed
bracket, a port that is there but is not a number in 1–65535, a control
character, or nothing at all — is a
`FAIL` line naming the variable rather than a crash, and the other upstream is
still probed and still reported.

The value is read the way the live client reads it, which is why the command
does not tidy it up. Leading whitespace goes, because the client's urllib
removes it too. A *trailing* space does not, because by the time the client has
appended the request path that space is in the middle of the URL: the client
ends up with `claude.ai ` as its host and is refused before it dials, so a check
that tidied the space away would report `claude.ai:443 admitted` for a client
that reaches nothing. An empty value is the same — the clients default only
when the variable is *unset*, so an empty one is a `FAIL` here rather than a
silent fallback to `claude.ai`. You will not get there through `.env`: the
compose file's `${CLAUDE_AI_HOST:-https://claude.ai}` substitutes the default
for an empty entry as well as a missing one. It is reachable by exporting the
variable empty into the container some other way.

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
lookup is bounded by the check as well, inside the probe's own ten seconds: it
gets half of them at most, as the public-name line's own paragraph above says.

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

Which engine you have decides which of those you see — this table is the
outbound axis, and [The engine and your front
door](security-model.md#the-engine-and-your-front-door) is the same question for the inbound
one:

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
ever connects to the host over it. That is this axis only — the one that keeps
your own network out of the dashboard is the `DOCKER-USER` rule in [The engine
and your front door](security-model.md#the-engine-and-your-front-door), and an engine this old
needs both. On 27.x you must also delete the
`driver_opts` block from the `inside` network, or the network is not created
at all — which then fails `tests/test_compose_topology.py`, since that block is
what the test pins; on 26.x and older the block is ignored and can stay.

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
| `claude_credentials_present: false` from `/healthz` | Bind mount didn't pick up the credentials file. Verify `CLAUDE_HOME` points at your real `.claude` directory. On macOS the file is usually absent, because Claude Code keeps its login in the Keychain; see [Prerequisites](../README.md#prerequisites). |
| `codex_credentials_present: false` from `/healthz` | Same, for `CODEX_HOME` / `~/.codex/auth.json`. |
| Browser shows `reconnecting…` | The container restarted; SSE will reconnect on its own. |
| Every chip reads `unavailable` and `docker compose logs egress` shows a refused host | The host is not on the egress allow-list: a `CLAUDE_AI_HOST`/`CHATGPT_HOST` override without a matching `EGRESS_ALLOW` entry, or the vendor redirected to another host. |
| Browser shows `Host not served by this dashboard` (`403`) | The name in the address bar is not in `DASHBOARD_ALLOWED_HOSTS`. Add it (and widen `DASHBOARD_BIND` if the request comes from another machine), then `docker compose up -d`. |
| `python -m app.egress check` reports a `FAIL` on the on-link line, naming an address that accepted or refused | Something that is neither this container nor the proxy is on-link. On an engine older than 28.0 that is the host: a 26.x engine ignores `gateway_mode_ipv4` without a word and keeps its address on the bridge. Upgrade, or see [Check the egress bound](#check-the-egress-bound) for the firewall rule that replaces it — and for the case where the address is another container in this project. |
| `python -m app.egress check` reports a `FAIL` naming `CLAUDE_AI_HOST=` or `CHATGPT_HOST=` and says the bound is `unverified for that upstream` | That override is not a URL with a host and a port the live client could dial — an unclosed `[` in an IPv6 literal, a port that is there but is not a number in 1–65535 (`:0` included — nothing dials it), a stray control character, nothing in front of the host that reads as a scheme, or an empty value exported into the container (an empty `.env` entry takes the default instead). Nothing could be asked about that upstream, so nothing was. Fix the value and run `docker compose up -d`; the live client cannot reach it either. |
| `docker compose up` fails creating the `inside` network with `unknown gateway mode isolated` | A 27.x engine: it knows the option but not that value. Upgrade to 28.0+, or delete the `driver_opts` block from the `inside` network and use the firewall rule instead. |
| `docker compose up` reports `dependency failed to start` | The `egress` proxy is unhealthy, and the dashboard waits for it. Check `docker compose logs egress`. |

`/healthz` returns JSON with `data_root_exists` and `credentials_present`
flags that are useful for quick diagnosis.
