"""The compose topology that makes the egress proxy unavoidable rather than advisory.

An allow-list bounds egress only while there is no route around it, so these assert the shape
of the rendered compose file: the dashboard's container joins internal networks alone, those
networks give the host no address on their bridge, the proxy is the one service with a leg on
each kind, and only the ingress relay publishes a port. The same file grants the other axis of
that container's budget, so what it may read is pinned here too (#45): the two agent data roots,
read-only, and nothing else.
Rendering needs the Docker CLI but no daemon; the test is skipped where Docker is not installed,
unless `REQUIRE_DOCKER` says it must not be -- CI sets that, because a pin that skips silently
where the CLI has gone missing is a pin that disappears with a green build.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from app.egress import ALLOW_ENV, PROXY_ENV_NAMES
from app.main import ALLOWED_HOSTS_ENV, parse_allowed_hosts

ROOT = Path(__file__).resolve().parents[1]
PROXY_URL = "http://egress:3128"


REQUIRE_DOCKER_ENV = "REQUIRE_DOCKER"


def _no_docker(reason: str) -> None:
    """Skip where the CLI is absent, unless this is somewhere it was promised."""
    if os.environ.get(REQUIRE_DOCKER_ENV, "").strip().lower() in ("", "0", "false", "no"):
        pytest.skip(reason)
    raise AssertionError(f"{reason}, and {REQUIRE_DOCKER_ENV} says these must not be skipped")


def _render(env_file: str, **extra: str) -> dict:
    if shutil.which("docker") is None:
        _no_docker("docker CLI not installed")
    # Only what the CLI needs to find its plugins, so nothing in the developer's shell
    # (DASHBOARD_PORT, CLAUDE_HOME, ...) leaks into the interpolation under test.
    keep = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "XDG_RUNTIME_DIR")
    env = {name: os.environ[name] for name in keep if name in os.environ}
    env.update(extra)
    result = subprocess.run(
        ["docker", "compose", "--env-file", env_file, "config", "--format", "json"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0 and "is not a docker command" in result.stderr:
        _no_docker("docker compose plugin not installed")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def config(tmp_path_factory) -> dict:
    home = tmp_path_factory.mktemp("userprofile")
    return _render(".env.example", USERPROFILE=str(home))


@pytest.fixture(scope="module")
def bare_config(tmp_path_factory) -> dict:
    """What someone who never wrote a `.env` gets: the compose file's own defaults, which
    are the ones nothing else would catch if they were dropped."""
    empty = tmp_path_factory.mktemp("no-env") / "env"
    empty.write_text("")
    return _render(str(empty))


def _internal(config: dict, service: str) -> dict[str, bool]:
    networks = config["services"][service].get("networks") or {}
    return {name: bool(config["networks"][name].get("internal")) for name in networks}


def test_the_dashboard_container_joins_internal_networks_alone(config: dict) -> None:
    joined = _internal(config, "codervis")
    assert joined, "codervis must name its networks, not fall back to the default bridge"
    assert all(joined.values()), joined


GATEWAY_MODE_IPV4 = "com.docker.network.bridge.gateway_mode_ipv4"
GATEWAY_MODE_IPV6 = "com.docker.network.bridge.gateway_mode_ipv6"


@pytest.mark.parametrize("rendered", ["config", "bare_config"])
def test_the_dashboards_networks_give_the_host_no_address_on_their_bridge(
    request, rendered: str
) -> None:
    """`internal: true` withholds the default route, not the host's own address on the bridge:
    that address is on-link in the container's subnet, so a compromised dependency reaches it
    with no route at all (#37). `isolated` is the gateway mode that leaves the bridge with no
    address to dial, and it belongs next to `internal: true` on every network the dashboard
    joins, or the bound is back to being one assumption."""
    config = request.getfixturevalue(rendered)
    joined = _internal(config, "codervis")
    assert joined
    for name in joined:
        network = config["networks"][name]
        assert network.get("driver") == "bridge", name
        opts = network.get("driver_opts") or {}
        assert opts.get(GATEWAY_MODE_IPV4) == "isolated", name
        # IPv6 is a second family with a second gateway address, so it may only be turned on
        # together with its own isolation: otherwise the host is back on the bridge over IPv6.
        if network.get("enable_ipv6"):
            assert opts.get(GATEWAY_MODE_IPV6) == "isolated", name


def test_the_dashboard_container_publishes_nothing_itself(config: dict) -> None:
    """Docker ignores a published port on an internal-only container, so the port lives on
    the relay, and a `ports:` line here would be a silent no-op."""
    assert not config["services"]["codervis"].get("ports")


DATA_MOUNTS = {
    "/data/claude": ".claude",
    "/data/codex": ".codex",
}


def _mounts(config: dict, service: str) -> dict[str, dict]:
    return {
        entry["target"]: entry for entry in config["services"][service].get("volumes") or []
    }


@pytest.mark.parametrize("rendered", ["config", "bare_config"])
def test_the_dashboard_mounts_the_two_agent_trees_read_only_and_nothing_more(
    request, rendered: str
) -> None:
    """This list is the read half of the container's budget, and the only place it is set (#45).

    Each mount is a whole home tree rather than the seven paths the app reads, because a
    credential file sits at each tree's root and a bind mount of a file follows the inode it was
    made from -- the container would keep reading the file its CLI replaced on the next token
    refresh. So the budget is "these two trees, read-only", which makes a third entry, a source
    that is not one of those trees, or a mount that drops `:ro` a widening someone has to make
    on purpose. Checked with no `.env` too, because the compose file's own defaults are the ones
    nothing else would catch: a default widened from `~/.claude` to `~` would hand this
    container the whole home directory.
    """
    config = request.getfixturevalue(rendered)
    entries = config["services"]["codervis"].get("volumes") or []
    mounts = _mounts(config, "codervis")
    # The list's length as well as the set of targets: two entries sharing a target would
    # otherwise collapse into one key and read as the budget.
    assert len(entries) == len(DATA_MOUNTS), entries
    assert set(mounts) == set(DATA_MOUNTS), sorted(mounts)
    for target, entry in mounts.items():
        # Compose renders the short `source:target:ro` syntax as a long-form bind, which is
        # where each of these three lives.
        assert entry.get("type") == "bind", (target, entry)
        assert entry.get("read_only") is True, (target, entry)
        assert Path(str(entry.get("source"))).name == DATA_MOUNTS[target], (target, entry)


def test_the_dashboard_runs_the_image_s_own_bounded_server(config: dict) -> None:
    """The head cap and the connection ceiling are armed by the command the image starts (#43),
    so a `command:` here that replaced it would disarm both while every other pin stayed green.
    An override is allowed only where it still launches that module."""
    service = config["services"]["codervis"]
    for key in ("entrypoint", "command"):
        override = service.get(key)
        if not override:
            continue
        # Compose accepts both forms, and a list is joined rather than searched element by element,
        # because a command may name the module inside a shell invocation of its own.
        text = override if isinstance(override, str) else " ".join(override)
        assert "app.server" in text, (override, key)


def test_the_dashboard_reaches_the_proxy_through_both_spellings(config: dict) -> None:
    environment = config["services"]["codervis"]["environment"]
    for name in PROXY_ENV_NAMES:
        assert name in environment, name
        if name.lower() != "no_proxy":
            assert environment[name] == PROXY_URL, name


def test_the_dashboard_waits_for_a_healthy_proxy(config: dict) -> None:
    depends = config["services"]["codervis"]["depends_on"]
    assert depends["egress"]["condition"] == "service_healthy"


def test_the_proxy_bridges_an_internal_network_and_an_outside_one(config: dict) -> None:
    joined = _internal(config, "egress")
    assert set(joined.values()) == {True, False}, joined
    shared = set(_internal(config, "codervis")) & set(joined)
    assert shared, "the proxy must share an internal network with the dashboard"
    assert config["services"]["egress"]["environment"][ALLOW_ENV] == ""
    assert config["services"]["egress"].get("healthcheck", {}).get("test")


def test_only_the_relay_publishes_a_port_and_it_targets_the_dashboard(config: dict) -> None:
    publishing = [name for name, svc in config["services"].items() if svc.get("ports")]
    assert publishing == ["ingress"]
    (port,) = config["services"]["ingress"]["ports"]
    assert port["target"] == 8000
    assert port["published"] == "8765"
    command = config["services"]["ingress"]["command"]
    assert command[command.index("--target") + 1] == "codervis:8000"


@pytest.mark.parametrize("rendered", ["config", "bare_config"])
def test_the_published_port_reaches_this_machine_alone_by_default(request, rendered: str) -> None:
    """The dashboard has no login, so the default publish is the one the operator can widen
    on purpose: without a host address here it would answer every LAN peer and every
    co-resident container, neither of which the host's own `INPUT` chain sees, since Docker
    forwards those packets to the container rather than delivering them (#15). What this
    setting is worth also depends on the engine keeping its side (#79), which is the pin
    below's half. Checked with no
    `.env` as well, because that is the reading `.env.example` would otherwise cover up."""
    (port,) = request.getfixturevalue(rendered)["services"]["ingress"]["ports"]
    assert port.get("host_ip") == "127.0.0.1"


@pytest.mark.parametrize("rendered", ["config", "bare_config"])
def test_the_dashboard_answers_only_loopback_names_by_default(request, rendered: str) -> None:
    """The other half: a loopback publish alone still answers a rebound page as same-origin."""
    setting = request.getfixturevalue(rendered)["services"]["codervis"]["environment"]
    assert parse_allowed_hosts(setting[ALLOWED_HOSTS_ENV]) == {"localhost", "127.0.0.1", "::1"}


# ------------------------------------------------------------- what the documents promise
# The pins above are the shape the deployment has; these are what the documents tell a stranger
# that shape gives them. They read the files directly, so unlike the rest of this module they
# need no Docker CLI and run everywhere.
#
# Neither promise holds unconditionally (#79). What the loopback publish keeps out is the
# engine's to keep: before 28.0 nothing drops traffic routed from off the host to a container's
# own address, and 28.2.0 through 28.3.2 lose Docker's own rules on every firewalld reload. And
# the whole-tree mounts leave readable whatever each CLI writes into its tree, which is a good
# deal more than the transcripts the app reads. A document that states either promise without
# the condition it rests on is the defect, because it is what a stranger decides on.

# The release where both loopback exposures are closed, and the chain that closes them for an
# operator who cannot reach it. A host INPUT rule does not: Docker's DNAT and its forward rules
# run ahead of that chain, which is what the old text generalised into "a host firewall cannot".
ENGINE_FLOOR = "28.3.3"
WORKING_CHAIN = "DOCKER-USER"
FRONT_DOOR_DOCS = ("README.md", ".env.example", "docker-compose.yml")
FRONT_DOOR_SECTION = "### The engine and your front door"


def _document(name: str) -> str:
    return (ROOT / name).read_text()


def _readme_section(heading: str) -> str:
    """The text under one README heading, so a pin cannot be satisfied from somewhere else."""
    readme = _document("README.md")
    assert heading in readme, f"README has no {heading!r} section"
    depth = len(heading) - len(heading.lstrip("#"))
    rest = readme[readme.index(heading) + len(heading) :]
    following = re.search(r"(?m)^#{1,%d} " % depth, rest)
    return " ".join((rest[: following.start()] if following else rest).split())


@pytest.mark.parametrize("name", FRONT_DOOR_DOCS)
def test_every_document_that_promises_the_loopback_publish_names_its_condition(name: str) -> None:
    """All three say what publishing on `DASHBOARD_BIND` keeps out, so all three carry what it
    rests on. A reader who only ever opens `.env.example` is the one this is for."""
    text = _document(name)
    assert "DASHBOARD_BIND" in text, f"{name} no longer makes the claim; move this pin"
    assert ENGINE_FLOOR in text, (
        f"{name} says what the loopback publish keeps out without naming the engine release "
        f"that makes it so ({ENGINE_FLOOR})"
    )
    assert WORKING_CHAIN in text, (
        f"{name} leaves an operator below {ENGINE_FLOOR} with nothing that closes it: a host "
        f"INPUT rule does not, and a {WORKING_CHAIN} rule does"
    )


def test_the_readme_has_one_place_that_says_what_an_older_engine_leaves_open() -> None:
    """Two different lapses are fixed by one release, and an operator's own version decides
    which they have. Naming the floor alone would leave 28.2.0-28.3.2 reading as closed."""
    section = _readme_section(FRONT_DOOR_SECTION)
    for phrase in ("28.0", "28.2.0", "28.3.2", ENGINE_FLOOR, "firewalld", WORKING_CHAIN):
        assert phrase in section, f"{FRONT_DOOR_SECTION} does not name {phrase}"


# What the mount exposes is what each CLI writes into its own tree, so the Caveats' list is
# derived from that rather than from the paths codervis reads (#79). `~/.claude.json` is the
# one to keep: the file itself is outside both mounts, and its copies under `backups/` are not.
VENDOR_WRITTEN = (
    "~/.claude.json",
    "backups/",
    "debug/",
    "file-history/",
    "paste-cache/",
    "config.toml",
)


@pytest.mark.parametrize("written", VENDOR_WRITTEN)
def test_the_caveat_on_the_whole_tree_mounts_names_what_each_cli_writes_there(written: str) -> None:
    """The mounts fall under the compromised-dependency principal #45 accepts. What a list of
    examples decides is whether a stranger's acceptance is an informed one, so the list names
    the contents worth most rather than the ones easiest to describe."""
    caveats = _readme_section("### Caveats")
    assert written in caveats, (
        f"README's Caveats do not name {written}, which the whole-tree mount leaves readable"
    )


@pytest.mark.parametrize("service", ["codervis", "egress", "ingress"])
def test_every_service_rotates_its_log(config: dict, service: str) -> None:
    """A peer that can reach the port can make every service log on its behalf, so no
    service's log may grow without bound (#20)."""
    logging = config["services"][service].get("logging") or {}
    assert logging.get("driver") == "json-file"
    options = logging.get("options") or {}
    assert options.get("max-size")
    assert options.get("max-file")


@pytest.mark.parametrize("service", ["egress", "ingress"])
def test_the_gateway_services_run_with_nothing_to_spare(config: dict, service: str) -> None:
    """They hold no credential and are the two processes with a leg on the outside."""
    svc = config["services"][service]
    assert svc.get("user") not in (None, "", "root", "0", "0:0")
    assert svc.get("read_only") is True
    assert svc.get("cap_drop") == ["ALL"]
    assert "no-new-privileges:true" in (svc.get("security_opt") or [])
    assert "volumes" not in svc

# Every pin above that renders the compose file needs the Docker CLI, so those skip where it
# is absent unless `REQUIRE_DOCKER` says they must not. That makes CI's own configuration part of
# the pin: drop the variable and every assertion in this file goes back to skipping silently on
# a runner whose image lost the CLI, with a green build to show for it. These two need nothing
# but the workflow file, so they run everywhere.
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _workflow() -> dict:
    return yaml.safe_load(CI_WORKFLOW.read_text())


def test_ci_requires_docker_for_the_job_that_runs_this_file() -> None:
    """`REQUIRE_DOCKER` is what turns "no docker CLI" from a skip into a failure."""
    workflow = _workflow()
    jobs = workflow["jobs"]
    running = [
        job
        for job in jobs.values()
        if any("pytest" in str(step.get("run", "")) for step in job.get("steps") or [])
    ]
    assert running, "no job runs pytest any more"
    for job in running:
        step = next(s for s in job["steps"] if "pytest" in str(s.get("run", "")))
        # All three scopes, in the order GitHub resolves them: a variable set for the whole
        # workflow is as good as one set on the step, and rejecting that would fail a
        # configuration that works.
        env = {
            **(workflow.get("env") or {}),
            **(job.get("env") or {}),
            **(step.get("env") or {}),
        }
        assert str(env.get(REQUIRE_DOCKER_ENV, "")).strip().lower() not in (
            "",
            "0",
            "false",
            "no",
        ), f"{step.get('name')} would let these pins skip"


def test_ci_asserts_the_inside_bridge_holds_no_address() -> None:
    """The host-side half of #37, which only a real daemon can settle.

    `check` asserts the bound from inside the container; this asserts it from the host, on the
    one engine this project ever gets to run against. A job that merely *recorded* the bridge's
    addresses would leave the compose half verified nowhere.
    """
    jobs = _workflow()["jobs"]
    script = "\n".join(
        str(step.get("run", ""))
        for job in jobs.values()
        for step in job.get("steps") or []
    )
    assert "ip -4 address show" in script
    assert "exit 1" in script
    assert "python -m app.egress check" in script
