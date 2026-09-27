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
    """The four front-door bounds are armed by the command the image starts (#43), so a
    `command:` here that replaced it would disarm all four while every other pin stayed green.

    Asserted as the exact rendered shape -- `SERVICE_COMMANDS` below, which says `codervis`
    carries no override at all -- and not as "the override mentions `app.server`". That
    substring reading passed `["python", "-m", "uvicorn", "app.main:app", "--header",
    "x=app.server"]`, which arms none of them (#78). This is the rendered half of
    `test_each_service_runs_exactly_the_command_named_here`; interpolation cannot reach a
    `command:` that is not there, but a pin on the bound the image's `CMD` carries belongs on
    what the daemon would actually be handed too.
    """
    service = config["services"]["codervis"]
    assert service.get("command") == SERVICE_COMMANDS["codervis"], service.get("command")
    assert "entrypoint" not in service, service.get("entrypoint")


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
    co-resident container, neither of which a host firewall stops (#15). Checked with no
    `.env` as well, because that is the reading `.env.example` would otherwise cover up."""
    (port,) = request.getfixturevalue(rendered)["services"]["ingress"]["ports"]
    assert port.get("host_ip") == "127.0.0.1"


@pytest.mark.parametrize("rendered", ["config", "bare_config"])
def test_the_dashboard_answers_only_loopback_names_by_default(request, rendered: str) -> None:
    """The other half: a loopback publish alone still answers a rebound page as same-origin."""
    setting = request.getfixturevalue(rendered)["services"]["codervis"]["environment"]
    assert parse_allowed_hosts(setting[ALLOWED_HOSTS_ENV]) == {"localhost", "127.0.0.1", "::1"}


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
    """They hold no credential and are the two processes with a leg on the outside.

    Every value is an equality against `GATEWAY_PRIVILEGE` below. What was here before refused
    `None`, `""`, `"root"`, `"0"` and `"0:0"` and so admitted `"0:65534"` and `"root:root"`,
    which are the same uid under two spellings the list did not think of (#78), and asked
    whether `no-new-privileges:true` was *among* the `security_opt` entries rather than
    whether it was the only one.
    """
    svc = config["services"][service]
    for key, permitted in GATEWAY_PRIVILEGE.items():
        assert svc.get(key) == permitted, (service, key, svc.get(key))
    assert "volumes" not in svc

# The pins above need the Docker CLI to render the compose file, so they skip where it is
# absent unless `REQUIRE_DOCKER` says they must not. That makes CI's own configuration part of
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


# ─── The shape of the file itself, as an allow-list written here ─────────────
#
# The pins above read the *rendered* config, which needs the Docker CLI and so skips wherever
# it is absent. These read `docker-compose.yml` directly, for two reasons. They cover what a
# rendered pin cannot state cheaply -- the exact set of keys a service carries, which is the
# only form of this check that a *widening* has to go through rather than around (#78) -- and
# they run everywhere, including the developer checkout with no Docker installed, where every
# pin above is a skip.
#
# What is asserted is an equality against a table written out below, never a "not one of these
# bad values" and never a substring. A privilege pin that refuses `root` and `0` says nothing
# about `0:65534`; a command pin that greps for `app.server` says nothing about a bare uvicorn
# invocation that merely mentions it in an argument. Both of those widenings were green.
#
# `yaml.safe_load` rather than the CLI: the file uses one anchor (`x-logging`), which the
# loader resolves, and no `extends`, `include` or profile, so what is parsed here is what the
# CLI would render minus the `${VAR:-default}` interpolation the rendered pins above cover.

COMPOSE_FILE = ROOT / "docker-compose.yml"


@pytest.fixture(scope="module")
def source() -> dict:
    """The compose file as committed, with its anchors resolved and nothing interpolated."""
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


#: Every key each service may carry, exactly. A key absent from its service's set is a
#: widening whoever adds it has to come here and declare, which is the whole point: `privileged`,
#: `cap_add`, `pid: host`, `ipc: host`, `userns_mode`, `devices`, `group_add`, `sysctls`,
#: `network_mode` and `ports` are all keys that grant something, and not one of them is listed.
#: Naming the permitted keys rather than the forbidden ones is what makes that true of the
#: next such key as well as of today's.
SERVICE_KEYS: dict[str, frozenset[str]] = {
    "codervis": frozenset(
        {
            "build",
            "container_name",
            "networks",
            "environment",
            "volumes",
            "depends_on",
            "restart",
            "logging",
        }
    ),
    "egress": frozenset(
        {
            "build",
            "command",
            "networks",
            "environment",
            "healthcheck",
            "user",
            "read_only",
            "cap_drop",
            "security_opt",
            "restart",
            "logging",
        }
    ),
    "ingress": frozenset(
        {
            "build",
            "command",
            "networks",
            "ports",
            "depends_on",
            "user",
            "read_only",
            "cap_drop",
            "security_opt",
            "restart",
            "logging",
        }
    ),
}


def test_the_file_declares_these_three_services_and_no_others(source: dict) -> None:
    assert set(source["services"]) == set(SERVICE_KEYS)


@pytest.mark.parametrize("service", sorted(SERVICE_KEYS))
def test_each_service_carries_exactly_the_keys_named_here(source: dict, service: str) -> None:
    """The permitted shape, not a list of the spellings someone thought of.

    `cap_add`, `privileged`, `pid: host` and the rest of the keys that widen what a container
    holds were each added to a scratch copy and left the suite green (#78), because every pin
    on this file asked whether a *good* key was still there. An equality on the key set is
    what makes adding one of them red without anybody having had to predict it.
    """
    assert set(source["services"][service]) == SERVICE_KEYS[service], (
        f"{service} no longer carries exactly the keys this test names. A key that grants "
        "something -- capabilities, a namespace, a device, a published port -- is a widening "
        "to make on purpose: add it here, in the same change, and say why."
    )


#: What the two gateway services run as, exactly. They hold no credential and are the two
#: processes with a leg on the outside, so this is an equality: `0:65534` and `root:root` are
#: both root, and both passed the refuse-`root`-and-`0` check this replaces.
GATEWAY_PRIVILEGE: dict[str, object] = {
    "user": "65534:65534",
    "read_only": True,
    "cap_drop": ["ALL"],
    "security_opt": ["no-new-privileges:true"],
}


@pytest.mark.parametrize("service", ["egress", "ingress"])
@pytest.mark.parametrize("key", sorted(GATEWAY_PRIVILEGE))
def test_the_gateway_services_run_as_exactly_what_is_named_here(
    source: dict, service: str, key: str
) -> None:
    assert source["services"][service][key] == GATEWAY_PRIVILEGE[key], (
        f"{service}.{key} is not what this test names. Any other value is a widening: "
        "a uid of 0 under a second name is still uid 0."
    )


#: What each service runs, exactly. `None` means the service carries no `command:` at all and
#: so runs the image's own `CMD`.
#:
#: `codervis` is `None` on purpose and is the one that matters: `CMD ["python", "-m",
#: "app.server", ...]` is where the head cap, the head deadline, the body deadline and the
#: connection ceiling are armed, and none of them has a default (CLAUDE.md, "Keep the bound
#: whole"). The check this replaces asked whether the override *mentioned* `app.server`, so
#: `command: ["python", "-m", "uvicorn", "app.main:app", "--header", "x=app.server"]` passed
#: with all four bounds disarmed. An override that genuinely still launches the module is
#: welcome -- it just has to be written out here, where a reviewer reads it.
SERVICE_COMMANDS: dict[str, list[str] | None] = {
    "codervis": None,
    "egress": ["python", "-m", "app.egress", "serve", "--bind", "0.0.0.0", "--port", "3128"],
    "ingress": [
        "python",
        "-m",
        "app.ingress",
        "--bind",
        "0.0.0.0",
        "--port",
        "8000",
        "--target",
        "codervis:8000",
    ],
}


@pytest.mark.parametrize("service", sorted(SERVICE_COMMANDS))
def test_each_service_runs_exactly_the_command_named_here(source: dict, service: str) -> None:
    assert source["services"][service].get("command") == SERVICE_COMMANDS[service]


@pytest.mark.parametrize("service", sorted(SERVICE_KEYS))
def test_no_service_replaces_the_images_entrypoint(source: dict, service: str) -> None:
    """An `entrypoint:` replaces the `CMD` as surely as a `command:` does, and nothing asked.

    It is absent from `SERVICE_KEYS` above too; this says which key it was and why, so the
    failure names the bound rather than a set difference.
    """
    assert "entrypoint" not in source["services"][service]
