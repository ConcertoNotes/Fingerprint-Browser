"""Bound profiles through the real Mihomo engine, against controlled upstream nodes.

Only the browser binary is replaced (by a recording stand-in); the launch path,
leases, sessions, the engine, its listeners and every proxied request are real.
Each upstream answers with its own exit address, so a response proves which node
carried it.
"""

import asyncio
from pathlib import Path

import psutil
import pytest
from cryptography.fernet import Fernet

from camoufox_pm.config import get_settings
from camoufox_pm.core import browser_session, proxy_check
from camoufox_pm.core.network_adapter import NetworkLaunchError
from camoufox_pm.core.network_binding import NetworkBindings
from camoufox_pm.core.network_catalog import NetworkCatalog
from tests.support.network_fixtures import (
    mihomo_children,
    require_mihomo,
    start_upstream,
    subscription_yaml,
)

pytestmark = [pytest.mark.mihomo, pytest.mark.asyncio]


class FakeContext:
    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler

    async def new_page(self):
        raise AssertionError("not used")


class FakeCamoufox:
    launches: list[dict] = []

    def __init__(self, **options):
        self.options = options
        self.closed = False

    async def start(self):
        FakeCamoufox.launches.append(self.options)
        return FakeContext()

    async def __aexit__(self, *args):
        self.closed = True


@pytest.fixture
async def world(profile_manager, tmp_path, monkeypatch):
    require_mihomo()
    monkeypatch.setenv("CPM_SECRET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    FakeCamoufox.launches = []
    monkeypatch.setattr(browser_session, "AsyncCamoufox", FakeCamoufox)
    monkeypatch.setattr(browser_session, "CAMOUFOX_AVAILABLE", True)
    monkeypatch.setattr(
        proxy_check,
        "locate",
        lambda ip: proxy_check.ProxyLocation(
            ip=ip, country="JP", timezone="Asia/Tokyo", latitude=35.69, longitude=139.69
        ),
    )
    upstreams = [
        await start_upstream(tmp_path / name, ip)
        for name, ip in (("a", "198.51.100.10"), ("b", "198.51.100.20"), ("c", "198.51.100.30"))
    ]
    # Each node's own origin answers only through that node.
    monkeypatch.setattr(proxy_check, "IP_ENDPOINTS", tuple(u.endpoint() for u in upstreams))
    catalog = NetworkCatalog(profile_manager)
    created = await catalog.create_subscription(
        "Test nodes",
        content=subscription_yaml(*(u.node(f"node-{i}") for i, u in enumerate(upstreams))),
    )
    nodes = {n["name"]: n["id"] for n in catalog.list_nodes()["nodes"]}
    yield {
        "manager": profile_manager,
        "catalog": catalog,
        "bindings": NetworkBindings(profile_manager),
        "upstreams": upstreams,
        "nodes": nodes,
        "subscription": created["subscription"]["id"],
    }
    await profile_manager.browser_sessions.close_all()
    await profile_manager.network.shutdown()
    for upstream in upstreams:
        await upstream.close()
    get_settings.cache_clear()
    assert mihomo_children() == []


async def bound_profile(world, name, node):
    manager = world["manager"]
    profile = await manager.create_profile(name, generate_fingerprint=False)
    await world["bindings"].bind(profile.id, world["nodes"][node], binding_row_version=None)
    return profile


async def exit_through(proxy) -> str:
    ip, _ = await proxy_check.resolve_exit_ip(proxy, timeout=3)
    return ip


async def test_two_profiles_run_through_their_own_nodes_and_ports(world):
    manager = world["manager"]
    a = await bound_profile(world, "A", "node-0")
    b = await bound_profile(world, "B", "node-1")
    await manager.launch_browser(a.id)
    await manager.launch_browser(b.id)
    run_a, run_b = manager.network.runtimes[a.id], manager.network.runtimes[b.id]
    assert (run_a.exit_ip, run_b.exit_ip) == ("198.51.100.10", "198.51.100.20")
    assert run_a.listener.port != run_b.listener.port
    assert run_a.engine.process.pid != run_b.engine.process.pid
    options_a, options_b = FakeCamoufox.launches
    # The browser gets the profile's own authenticated loopback listener...
    assert options_a["proxy"]["server"] == f"http://127.0.0.1:{run_a.listener.port}"
    assert options_a["proxy"]["username"] and options_a["proxy"]["password"]
    assert options_a["proxy"] != options_b["proxy"]
    # ...and Camoufox derives geography from the exit measured through it.
    assert options_a["geoip"] == "198.51.100.10" and options_b["geoip"] == "198.51.100.20"
    assert (await exit_through(run_a.proxy_config), await exit_through(run_b.proxy_config)) == (
        "198.51.100.10",
        "198.51.100.20",
    )
    # One listener's credentials do not open the other's port.
    crossed = run_a.proxy_config.model_copy(update={"server": f"127.0.0.1:{run_b.listener.port}"})
    with pytest.raises(ConnectionError):
        await exit_through(crossed)
    # A second launch of A is the same instance, not a second engine.
    again = await manager.launch_browser(a.id)
    assert again["status"] == "already_running" and len(FakeCamoufox.launches) == 2


async def test_engine_death_fails_closed_for_that_profile_only_and_reconnects(world):
    manager = world["manager"]
    a = await bound_profile(world, "A", "node-0")
    b = await bound_profile(world, "B", "node-1")
    await manager.launch_browser(a.id)
    await manager.launch_browser(b.id)
    run_a, run_b = manager.network.runtimes[a.id], manager.network.runtimes[b.id]
    run_a.engine.process.kill()
    for _ in range(50):
        if run_a.state == "degraded":
            break
        await asyncio.sleep(0.05)
    assert run_a.state == "degraded" and run_a.error["code"] == "engine_exited"
    with pytest.raises(ConnectionError):  # no fallback: the listener is simply gone
        await exit_through(run_a.proxy_config)
    assert run_b.state == "running" and await exit_through(run_b.proxy_config) == "198.51.100.20"
    await run_a.reconnect()
    assert run_a.state == "running"
    assert await exit_through(run_a.proxy_config) == "198.51.100.10"


async def test_a_dead_node_stops_the_launch_and_leaves_nothing_behind(world):
    manager = world["manager"]
    c = await bound_profile(world, "C", "node-2")
    await world["upstreams"][2].close()
    world["upstreams"][2].servers.clear()
    with pytest.raises(NetworkLaunchError) as error:
        await manager.launch_browser(c.id)
    assert error.value.status == 502
    assert FakeCamoufox.launches == []  # no browser ever started
    assert c.id not in manager.network.runtimes
    lease = await manager.storage.get_lease(c.id)
    assert lease is None or lease[0] is None  # the launch handed its lease back
    assert mihomo_children() == []


async def test_closing_a_browser_stops_its_engine_and_keeps_the_binding(world):
    manager = world["manager"]
    a = await bound_profile(world, "A", "node-0")
    b = await bound_profile(world, "B", "node-1")
    await manager.launch_browser(a.id)
    await manager.launch_browser(b.id)
    run_a = manager.network.runtimes[a.id]
    pid, workdir = run_a.engine.process.pid, run_a.engine.workdir
    await manager.close_browser(a.id)
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    assert not Path(workdir).exists()
    assert a.id not in manager.network.runtimes
    assert world["bindings"].get(a.id)["node_id"] == world["nodes"]["node-0"]
    run_b = manager.network.runtimes[b.id]
    assert await exit_through(run_b.proxy_config) == "198.51.100.20"


async def test_running_profiles_cannot_be_rebound(world):
    from camoufox_pm.core.leases import ProfileLocked

    manager = world["manager"]
    a = await bound_profile(world, "A", "node-0")
    await manager.launch_browser(a.id)
    binding = world["bindings"].get(a.id)
    with pytest.raises(ProfileLocked):
        await world["bindings"].bind(
            a.id, world["nodes"]["node-1"], binding_row_version=binding["row_version"]
        )
    assert world["bindings"].get(a.id)["node_id"] == world["nodes"]["node-0"]
    assert manager.network.runtimes[a.id].exit_ip == "198.51.100.10"


async def test_a_retired_node_is_refused_instead_of_replaced(world):
    manager = world["manager"]
    a = await bound_profile(world, "A", "node-0")
    upstream_b = world["upstreams"][1]
    await world["catalog"].sync_subscription(
        world["subscription"], content=subscription_yaml(upstream_b.node("node-1"))
    )
    with pytest.raises(NetworkLaunchError) as error:
        await manager.launch_browser(a.id)
    assert (error.value.status, error.value.code) == (422, "node_retired")
    assert FakeCamoufox.launches == [] and mihomo_children() == []


async def test_unbound_profiles_keep_their_manual_proxy(world):
    manager = world["manager"]
    profile = await manager.create_profile(
        "Manual",
        generate_fingerprint=False,
        proxy_config={"type": "socks5", "server": f"127.0.0.1:{world['upstreams'][0].proxy_port}"},
    )
    await manager.launch_browser(profile.id)
    (options,) = FakeCamoufox.launches
    assert options["proxy"]["server"] == f"socks5://127.0.0.1:{world['upstreams'][0].proxy_port}"
    assert profile.id not in manager.network.runtimes
    assert mihomo_children() == []


async def test_batch_checks_use_one_engine_for_all_nodes(world):
    manager = world["manager"]
    probe = manager.network_probe
    probe.endpoints = tuple(u.endpoint() for u in world["upstreams"])
    seen_engines = set()
    from camoufox_pm.core import network_probe as np

    real_start = np.MihomoProcess.start

    async def counting_start(self, ports=None):
        engine = await real_start(self, ports)
        seen_engines.add(engine.process.pid)
        assert len(mihomo_children()) == 1, "a batch must run a single engine process"
        return engine

    np.MihomoProcess.start = counting_start
    try:
        job = probe.start_batch(list(world["nodes"].values()))
        for _ in range(200):
            job = probe.get_job(job["id"])
            if job["state"] not in {"queued", "running"}:
                break
            await asyncio.sleep(0.05)
    finally:
        np.MihomoProcess.start = real_start
    assert job["state"] == "finished" and (job["ok"], job["failed"]) == (3, 0)
    assert job["engine_processes"] == 1 and len(seen_engines) == 1
    checks = {n["name"]: n["check"]["exit_ip"] for n in world["catalog"].list_nodes()["nodes"]}
    assert checks == {
        "node-0": "198.51.100.10",
        "node-1": "198.51.100.20",
        "node-2": "198.51.100.30",
    }
    assert mihomo_children() == []


async def test_a_node_mihomo_rejects_is_set_aside_without_a_process_per_node(world):
    manager = world["manager"]
    catalog = world["catalog"]
    upstream = world["upstreams"][0]
    bad = {
        "name": "bad",
        "type": "ss",
        "server": "127.0.0.1",
        "port": 9,
        "cipher": "not-a-cipher",
        "password": "x",
    }
    await catalog.sync_subscription(
        world["subscription"],
        content=subscription_yaml(
            upstream.node("node-0"), bad, world["upstreams"][1].node("node-1")
        ),
    )
    probe = manager.network_probe
    probe.endpoints = tuple(u.endpoint() for u in world["upstreams"])
    active = [n["id"] for n in catalog.list_nodes(status="active")["nodes"]]
    job = probe.start_batch(active)
    for _ in range(200):
        job = probe.get_job(job["id"])
        if job["state"] not in {"queued", "running"}:
            break
        await asyncio.sleep(0.05)
    assert job["state"] == "finished"
    assert (job["ok"], job["failed"], job["engine_processes"]) == (2, 1, 2)
    by_name = {n["name"]: n["check"] for n in catalog.list_nodes(status="active")["nodes"]}
    assert by_name["bad"]["error_code"] == "config_invalid"
    assert by_name["node-0"]["exit_ip"] == "198.51.100.10"


async def test_single_check_reuses_a_running_profiles_listener(world):
    manager = world["manager"]
    a = await bound_profile(world, "A", "node-0")
    await manager.launch_browser(a.id)
    probe = manager.network_probe
    probe.endpoints = tuple(u.endpoint() for u in world["upstreams"])
    before = {p.pid for p in mihomo_children()}
    node = await probe.check_node(world["nodes"]["node-0"], include_intel=False)
    assert node["check"]["exit_ip"] == "198.51.100.10"
    assert {p.pid for p in mihomo_children()} == before


async def test_single_check_with_intel_goes_through_the_node(world):
    manager = world["manager"]
    probe = manager.network_probe
    upstream = world["upstreams"][1]
    probe.endpoints = (upstream.endpoint(),)
    probe.intel_urls = {
        "ipinfo": upstream.endpoint("/json"),
        "ippure": upstream.endpoint("/v1/info"),
    }
    from camoufox_pm.core import ip_intel

    ip_intel._MIN_INTERVAL = 0
    node = await probe.check_node(world["nodes"]["node-1"])
    assert node["check"]["exit_ip"] == "198.51.100.20"
    assert node["check"]["city"] == "Tokyo"
    composite = node["intel"]["composite"]
    assert composite["purity_score"] == 90 and composite["grade"] == "excellent"
    origin_paths = [e[1] for e in upstream.events if e[0] == "origin"]
    assert any(b"/json" in path for path in origin_paths)
    assert any(b"/v1/info" in path for path in origin_paths)
    assert mihomo_children() == []
