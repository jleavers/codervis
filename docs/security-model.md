# Security model

codervis runs beside two long-lived bearer tokens, so most of what is worth knowing about it
is what bounds it. This is the whole of that: what code in the container may read and reach,
what the whole-tree mounts leave readable, who can reach the dashboard and what an older
Docker engine leaves open there, and what each bound costs a peer. The
[README](../README.md) is the short version, [operations.md](operations.md) is how to check
the bounds on your own host, and [`SECURITY.md`](../SECURITY.md) is how to report a way round
one.

## How it works

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
  trees is therefore readable by code in that container too — including what a
  tree's own name does not suggest, such as the copies of `~/.claude.json` that
  Claude Code keeps under `~/.claude/backups/` although that file itself sits
  outside the mount. [Caveats](#caveats) names what each CLI writes there.
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
reach ([operations.md](operations.md#check-the-egress-bound)), and it needs Docker Engine 28.0+ to
be true — an engine from before then either refuses the option that keeps the
host off the bridge or ignores it, and the check is what says which. Docker
cannot publish a port from an internal-only container, so the port you open in
the browser belongs to `ingress`, a relay that forwards to the dashboard.
Neither gateway service holds a credential.

**Inbound traffic is yours to name, and from Docker Engine 28.3.3 that is the
whole of it.** There is no login, so who can reach the dashboard *is* its
access control, and the default is this machine and nothing else: the port is
published on `127.0.0.1` (`DASHBOARD_BIND`) and the app answers only the host
names you listed (`DASHBOARD_ALLOWED_HOSTS`). Serving anyone else is a change
you make on purpose — see [Serving other machines](operations.md#serving-other-machines).
On an older engine the publish address is not the whole of it: before 28.0 a
machine on the same network segment reaches the container whatever address the
port was published on, and 28.2.0 through 28.3.2 reopen that on every firewalld
reload. [The engine and your front door](#the-engine-and-your-front-door) says
what is exposed there, what it is worth to whoever takes it, and the one
firewall rule that closes it.

If a live endpoint is unreachable for any reason (expired token, network
down, or the vendor changes the API), that panel renders an "unavailable"
state. codervis does not estimate quota usage from local transcripts; it only
reads timestamp/metadata to show each agent's local last activity. Each
reader reaches the filesystem only through `app/activity_gate.py`, which admits
regular files inside that reader's own allow-listed subtrees and follows no
link out of them.

## Caveats

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
  all of both. What that is worth is decided by what each CLI writes there and
  not by what codervis reads, so this is the vendors' side of it — current at
  the time of writing, and `ls -a ~/.claude ~/.codex` is the version that
  counts on your machine:
  - `~/.claude/backups/` — rolling copies of `~/.claude.json`, five of them.
    That file holds your Claude Code sign-in session and your MCP server
    configuration, so the static headers and env values you gave a personal MCP
    server — third-party API tokens, most of the time — are inside the mount
    even though `~/.claude.json` itself sits outside it.
  - `~/.claude/debug/` — the CLI's own debug output. Treat it as
    token-bearing: a debug capture of an upstream call carries the bearer that
    was used for it.
  - `~/.claude/file-history/` and `~/.claude/paste-cache/` — what the CLI kept
    of files it changed and of text you pasted into it, from every project you
    have used it in, whatever those files held.
  - `~/.codex/config.toml` — Codex's own configuration, which can carry bearer
    values in the entries you added to it.
  - the transcripts, session files and session history the activity readers
    use (`~/.claude/projects/`, `~/.codex/sessions/`, `archived_sessions/`,
    `history.jsonl`), and anything else either CLI has written there since.

  They are not narrowed to the seven paths it
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

## The engine and your front door

Publishing on `127.0.0.1` is what keeps the dashboard to this machine, and
part of that is the engine's doing rather than the compose file's. Ask yours
which it is:

```bash
docker version --format '{{.Server.Version}}'
```

| Docker Engine | What a machine on your own network segment can reach |
|---|---|
| 28.3.3 and newer | Nothing. This is the release to be on. |
| 28.2.0 – 28.3.2 | Nothing, until firewalld is reloaded: that takes Docker's own rules with it and the daemon does not put them back, so both paths below are open until it restarts. |
| 28.0 – 28.1.x | Nothing. 28.0 is where both paths below were closed. |
| Older than 28.0 | Both paths below. Debian 13's packaged `docker.io` (26.1.5) is such an engine, and so is a 27.x on which you deleted the `driver_opts` block to make the stack start at all. |

Two paths, and on an engine that old the publish address closes neither:

- **The container's own address.** `ingress` listens on port 8000 on this
  project's `outside` bridge — the one network here the host holds an address
  on, and so the one it has a route to — and reaching that address involves no
  published mapping at all: a peer that can route to the subnet, on a segment
  where nothing stops it adding the route, connects to the port directly. 28.0
  is where the engine began dropping traffic routed to a container from off the
  host.
- **The published mapping.** A published port is a DNAT rule matching packets
  addressed to `127.0.0.1:8765`, and a peer on the same segment can put that
  address in a packet and your host's MAC on the frame. What decides whether
  your kernel entertains one is `net.ipv4.conf.<interface>.route_localnet`,
  which is off by default and which some Kubernetes, VPN and load-balancer
  setups turn on. Below 28.0 nothing in Docker stands behind that setting.

Whoever takes either path reads your usage, your plan tier, your reset times
and — through `last_activity`, at roughly ten-second resolution — whether you
are at the keyboard, and can hold `ingress`'s connection slots against you. No
credential goes that way: the tokens stay in the container and never reach the
payload. `DASHBOARD_ALLOWED_HOSTS` is no second lock either, because whoever
reaches the port writes the `Host` header and `localhost` is on the list; that
setting answers a page in *your own* browser pointing a name of its own at
`127.0.0.1`, which is a different attack.

**If you cannot run 28.3.3 or newer**, close both paths with one rule in
Docker's own `DOCKER-USER` chain, naming the interface your network is on:

```bash
sudo iptables  -I DOCKER-USER -i eth0 -m conntrack --ctstate NEW,INVALID -j DROP
sudo ip6tables -I DOCKER-USER -i eth0 -m conntrack --ctstate NEW,INVALID -j DROP
```

**Match the state, not just the interface.** `DOCKER-USER` is consulted before
Docker's own rules for *forwarded* traffic — which is why it works at all here,
and also why a bare `-i eth0 -j DROP` is the wrong rule: the replies to this
stack's own outbound TLS arrive on that same interface and are forwarded to a
container exactly as an inbound connection would be, so a blanket drop takes
`egress` down with the front door and puts every quota panel into
`unavailable`. The first packet of an inbound connection — to the published
mapping or straight to the container — is `NEW`, and that is what these refuse.

Being in the forward path is also the whole of why an ordinary `ufw` or
`firewalld` rule does not stop this: the packet is on its way to a container,
so it is never delivered to the host and the `INPUT` chain those rules are in
never sees it. That, and not "a firewall cannot help you", is what that failure
amounts to. The rule above covers every container on this host, so where you
publish something else here deliberately, aim it at this project's `outside`
bridge instead — `-o br-<id>`, with `<id>` the first 12 characters of that
network's ID from `docker network ls`.

On a firewalld host, add it permanently rather than with `iptables`, or the
next reload drops it, and check afterwards that both the chain and the rule are
there:

```bash
sudo firewall-cmd --permanent --direct --add-rule ipv4 filter DOCKER-USER 0 \
  -i eth0 -m conntrack --ctstate NEW,INVALID -j DROP
sudo firewall-cmd --permanent --direct --add-rule ipv6 filter DOCKER-USER 0 \
  -i eth0 -m conntrack --ctstate NEW,INVALID -j DROP
sudo firewall-cmd --reload
sudo iptables -S DOCKER-USER
```

**On 28.2.0 – 28.3.2 that last check is the point of the exercise**, because
the reload those versions mishandle takes Docker's own rules with it — the
`FORWARD` jump into `DOCKER-USER`, and the chain itself. A rule inside a chain
nothing jumps to is not consulted, and a `--direct` rule naming a chain that is
not there does not apply, so on that range this remedy needs the daemon put
back behind it: `sudo systemctl restart docker` after every reload, and the
`iptables -S` above to confirm. Upgrading to 28.3.3 is the answer that does not
need remembering.

This is not the same rule as the one in [Check the egress
bound](operations.md#check-the-egress-bound) in operations.md, and neither stands in for the other:
that one is about the dashboard's container reaching the **host** over the
bridge, and this one about your network reaching the **container**.

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

  How much of the first one the publish address really decides is the engine's
  to say: below 28.3.3 it either never held (before 28.0) or lapses on every
  firewalld reload (28.2.0 – 28.3.2), and there you need a rule in Docker's own
  `DOCKER-USER` chain — an ordinary `ufw` or `firewalld` rule does not stop it,
  because Docker forwards such a packet to a container rather than delivering
  it here, so the `INPUT` chain those rules are in never sees it. [The engine
  and your front door](#the-engine-and-your-front-door) has the exposure and
  the rule. The second one is the publish address's own doing on any engine: a
  co-resident container's packet is addressed to the bridge gateway, and the
  mapping's rule matches `127.0.0.1`, so it never matches that packet at all.
  `DASHBOARD_ALLOWED_HOSTS` is a second lock on none of them: whoever reaches
  the port writes the `Host` header, and `localhost` is on the list. It answers
  the third, and only the third.
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
  ([Check the egress bound](operations.md#check-the-egress-bound)). An engine older than 28.0
  either refuses the option (27.x) or ignores it without saying so (26.x and
  older), and there a host firewall rule that drops new inbound connections
  arriving on that bridge's interface is what closes it.
- **Every dependency the process installs, and every one a contributor's venv
  installs, is fixed by content rather than by name.** `requirements.txt`,
  `requirements-dev.txt` and `requirements-screenshots.txt` are the runtime,
  development and screenshot-tool sets resolved in full
  — every package, direct or transitive, pinned to one version and to a
  `sha256` of the artefact — and every install that puts one of them into an
  environment, the image's, CI's, a contributor's venv and the screenshot tool's,
  passes `pip install --require-hashes`, which refuses a file that has lost a hash
  and refuses a package the file does not name. The dev and screenshot locks each
  agree with the runtime lock package for package, so those environments are one
  set of artefacts rather than three resolutions of it. The base image is pinned
  by digest
  rather than by the `python:3.14-slim` tag, so the `pip` and the CA bundle the
  build uses are fixed too. Nothing is fetched from a CDN at page load: the
  dashboard serves its own three static files and no others, and there is no
  Swagger or ReDoc page here to load one. Dependabot moves the locks and the
  digest on; regenerate them by hand with the command in each lock's header.
  The optional screenshot tool in `tools/screenshots/` has a lock of its own,
  `requirements-screenshots.txt`, and installs it the same way — which it did not
  until [#108](https://github.com/jleavers/codervis/issues/108), when it still
  fetched `playwright` and `pillow` by bare name. It is no part of the image or of
  the test set either way. **What is not fixed by content is the browser**:
  `playwright install chromium` fetches three archives — Chromium, FFmpeg and the
  Chrome Headless Shell, which is the one the screenshot tools actually launch — and
  upstream publishes a digest for none of them, so there is no hash to require.
  Which revisions are asked for *is* fixed, by the pinned `playwright` wheel;
  what arrives is backed by TLS to Playwright's download hosts and nothing else.
  [`tools/screenshots/README.md`](../tools/screenshots/README.md) sets that out, and
  it is the one step in this repository that no part of the dashboard needs.
- **The dashboard's origin carries a Content-Security-Policy**, set on every
  response the app makes — the `403` for a `Host` it does not serve and the
  last-resort `500` included, because the layer sits outside every other one.
  (What carries no policy is the handful of refusals the *server* writes before
  a request ever reaches the app: the `431`, `408` and `503` of the next bullet,
  and uvicorn's own `400` for a head it cannot parse. None is a gap — each is a
  `text/plain` response that closes the connection.) It names this origin and
  nothing else: `default-src 'none'`, scripts and styles from
  `'self'`, `connect-src 'self'` so the payload cannot be sent anywhere, and
  `frame-ancestors 'none'` so the page cannot be framed.
  The one inline script — the initial payload — runs under a per-response
  nonce rather than `'unsafe-inline'`, so an event-handler attribute injected
  into the page would not run.
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
