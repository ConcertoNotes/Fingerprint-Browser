"""Real Camoufox through real Mihomo: isolation, per-profile exits and fail-closed.

Each upstream node is a local SOCKS5 server whose tunnel only reaches its own
origin, and that origin answers with the node's "exit address". A page that
reads an address therefore proves which node carried it, and a page that loads
at all after its engine died would prove a fallback. Synthetic ``.test`` names
cannot be resolved by this computer or reached through the user's system proxy,
so neither a direct nor a system-proxy fallback can succeed silently.
"""

import asyncio

import pytest
from cryptography.fernet import Fernet

from camoufox_pm.config import get_settings
from camoufox_pm.core import proxy_check
from camoufox_pm.core.network_binding import NetworkBindings
from camoufox_pm.core.network_catalog import NetworkCatalog
from tests.support.network_fixtures import (
    mihomo_children,
    require_mihomo,
    start_upstream,
    subscription_yaml,
)

pytestmark = [pytest.mark.browser, pytest.mark.mihomo, pytest.mark.asyncio]

# Exit addresses the bundled GeoIP database knows, so Camoufox's own geography
# setup runs unchanged and offline.
EXITS = ("8.8.8.8", "1.1.1.1")


@pytest.fixture
async def world(profile_manager, tmp_path, monkeypatch):
    require_mihomo()
    monkeypatch.setenv("CPM_SECRET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    upstreams = [await start_upstream(tmp_path / f"u{i}", ip) for i, ip in enumerate(EXITS)]
    monkeypatch.setattr(proxy_check, "IP_ENDPOINTS", tuple(u.endpoint() for u in upstreams))
    catalog = NetworkCatalog(profile_manager)
    await catalog.create_subscription(
        "Browser nodes",
        content=subscription_yaml(*(u.node(f"node-{i}") for i, u in enumerate(upstreams))),
    )
    nodes = {n["name"]: n["id"] for n in catalog.list_nodes()["nodes"]}
    yield profile_manager, upstreams, nodes
    await profile_manager.browser_sessions.close_all()
    await profile_manager.network.shutdown()
    for upstream in upstreams:
        await upstream.close()
    get_settings.cache_clear()
    assert mihomo_children() == []


async def bound(manager, nodes, name, node):
    profile = await manager.create_profile(name, generate_fingerprint=False)
    await NetworkBindings(manager).bind(profile.id, nodes[node], binding_row_version=None)
    return profile


async def page_for(manager, profile_id):
    return await manager.browser_sessions.active_sessions[profile_id].context.new_page()


async def read_exit(page, upstream) -> str:
    response = await page.goto(upstream.endpoint(), wait_until="domcontentloaded", timeout=20000)
    assert response is not None and response.ok
    return (await page.inner_text("body")).strip()


async def test_each_profile_reaches_the_web_through_its_own_node(world):
    manager, upstreams, nodes = world
    a = await bound(manager, nodes, "A", "node-0")
    b = await bound(manager, nodes, "B", "node-1")
    await manager.launch_browser(a.id, headless=True)
    await manager.launch_browser(b.id, headless=True)
    page_a, page_b = await page_for(manager, a.id), await page_for(manager, b.id)
    assert await read_exit(page_a, upstreams[0]) == "8.8.8.8"
    assert await read_exit(page_b, upstreams[1]) == "1.1.1.1"
    # A's node cannot reach B's origin: the request really went through node A.
    with pytest.raises(Exception):  # noqa: B017 - the browser's own network error
        await page_a.goto(upstreams[1].endpoint(), timeout=10000)
    # Camoufox derived geography from each measured exit, not from the host.
    timezone_a = await page_a.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone")
    timezone_b = await page_b.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone")
    assert timezone_a == proxy_check.locate("8.8.8.8").timezone
    assert timezone_b == proxy_check.locate("1.1.1.1").timezone


async def test_a_dead_engine_fails_closed_without_any_fallback(world):
    manager, upstreams, nodes = world
    a = await bound(manager, nodes, "A", "node-0")
    b = await bound(manager, nodes, "B", "node-1")
    await manager.launch_browser(a.id, headless=True)
    await manager.launch_browser(b.id, headless=True)
    page_a, page_b = await page_for(manager, a.id), await page_for(manager, b.id)
    assert await read_exit(page_a, upstreams[0]) == "8.8.8.8"
    before = len([e for e in upstreams[0].events if e[0] == "origin"])

    runtime = manager.network.runtimes[a.id]
    runtime.engine.process.kill()
    for _ in range(60):
        if runtime.state == "degraded":
            break
        await asyncio.sleep(0.05)
    assert runtime.state == "degraded"
    with pytest.raises(Exception) as error:  # noqa: B017 - the browser's own network error
        await page_a.goto(upstreams[0].endpoint("/ip?after-death"), timeout=10000)
    assert "PROXY" in str(error.value).upper() or "REFUSED" in str(error.value).upper()
    assert len([e for e in upstreams[0].events if e[0] == "origin"]) == before
    # The other profile is untouched, and A recovers on the same node.
    assert await read_exit(page_b, upstreams[1]) == "1.1.1.1"
    await runtime.reconnect()
    assert await read_exit(page_a, upstreams[0]) == "8.8.8.8"


async def test_browser_storage_stays_with_its_profile(world):
    manager, upstreams, nodes = world
    # Same node for both: only storage could leak, not the route.
    a = await bound(manager, nodes, "A", "node-0")
    b = await bound(manager, nodes, "B", "node-0")
    origin = upstreams[0].endpoint("/")
    write = """async () => {
        document.cookie = 'cpm=profile-a; max-age=3600; path=/';
        localStorage.setItem('cpm', 'profile-a');
        await new Promise((resolve, reject) => {
            const open = indexedDB.open('cpm', 1);
            open.onupgradeneeded = () => open.result.createObjectStore('s');
            open.onsuccess = () => { const tx = open.result.transaction('s', 'readwrite');
                tx.objectStore('s').put('profile-a', 'k'); tx.oncomplete = () => resolve(); tx.onerror = reject; };
            open.onerror = reject;
        });
    }"""
    read = """async () => ({
        cookie: document.cookie,
        local: localStorage.getItem('cpm'),
        indexed: await new Promise(resolve => {
            const open = indexedDB.open('cpm', 1);
            open.onupgradeneeded = () => open.result.createObjectStore('s');
            open.onsuccess = () => { const request = open.result.transaction('s').objectStore('s').get('k');
                request.onsuccess = () => resolve(request.result ?? null); request.onerror = () => resolve(null); };
            open.onerror = () => resolve(null);
        }),
    })"""
    await manager.launch_browser(a.id, headless=True)
    page = await page_for(manager, a.id)
    await page.goto(origin, wait_until="domcontentloaded")
    await page.evaluate(write)
    await manager.close_browser(a.id)

    await manager.launch_browser(b.id, headless=True)
    page = await page_for(manager, b.id)
    await page.goto(origin, wait_until="domcontentloaded")
    assert await page.evaluate(read) == {"cookie": "", "local": None, "indexed": None}
    await manager.close_browser(b.id)

    await manager.launch_browser(a.id, headless=True)
    page = await page_for(manager, a.id)
    await page.goto(origin, wait_until="domcontentloaded")
    restored = await page.evaluate(read)
    assert restored == {"cookie": "cpm=profile-a", "local": "profile-a", "indexed": "profile-a"}
