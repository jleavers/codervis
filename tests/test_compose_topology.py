"""The compose topology that makes the egress proxy unavoidable rather than advisory.

An allow-list bounds egress only while there is no route around it, so these assert the shape
of the rendered compose file: the dashboard's container joins internal networks alone, the
proxy is the one service with a leg on each kind, and only the ingress relay publishes a port.
Rendering needs the Docker CLI but no daemon; the test is skipped where Docker is not installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.egress import ALLOW_ENV, PROXY_ENV_NAMES

ROOT = Path(__file__).resolve().parents[1]
PROXY_URL = "http://egress:3128"


@pytest.fixture(scope="module")
def config(tmp_path_factory) -> dict:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not installed")
    home = tmp_path_factory.mktemp("userprofile")
    # Only what the CLI needs to find its plugins, so nothing in the developer's shell
    # (DASHBOARD_PORT, CLAUDE_HOME, ...) leaks into the interpolation under test.
    keep = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "XDG_RUNTIME_DIR")
    env = {name: os.environ[name] for name in keep if name in os.environ}
    env["USERPROFILE"] = str(home)
    result = subprocess.run(
        ["docker", "compose", "--env-file", ".env.example", "config", "--format", "json"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0 and "is not a docker command" in result.stderr:
        pytest.skip("docker compose plugin not installed")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _internal(config: dict, service: str) -> dict[str, bool]:
    networks = config["services"][service].get("networks") or {}
    return {name: bool(config["networks"][name].get("internal")) for name in networks}


def test_the_dashboard_container_joins_internal_networks_alone(config: dict) -> None:
    joined = _internal(config, "codervis")
    assert joined, "codervis must name its networks, not fall back to the default bridge"
    assert all(joined.values()), joined


def test_the_dashboard_container_publishes_nothing_itself(config: dict) -> None:
    """Docker ignores a published port on an internal-only container, so the port lives on
    the relay, and a `ports:` line here would be a silent no-op."""
    assert not config["services"]["codervis"].get("ports")


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


@pytest.mark.parametrize("service", ["egress", "ingress"])
def test_the_gateway_services_run_with_nothing_to_spare(config: dict, service: str) -> None:
    """They hold no credential and are the two processes with a leg on the outside."""
    svc = config["services"][service]
    assert svc.get("user") not in (None, "", "root", "0", "0:0")
    assert svc.get("read_only") is True
    assert svc.get("cap_drop") == ["ALL"]
    assert "no-new-privileges:true" in (svc.get("security_opt") or [])
    assert "volumes" not in svc
