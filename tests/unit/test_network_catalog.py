"""Node catalog: sealed storage, ID-stable incremental sync, retirement and the map."""

import pytest
from cryptography.fernet import Fernet

from camoufox_pm.config import get_settings
from camoufox_pm.core.network_catalog import (
    NetworkCatalog,
    NetworkSecretsUnavailable,
    SubscriptionHasBindings,
)
from camoufox_pm.core.network_subscription import FetchedSubscription, SubscriptionError

SECRET = "catalog-node-secret"
SUB_SECRET = "https://sub.example/api/v1/client/subscribe?token=TOKEN-123456"


def doc(*nodes: tuple[str, str, int, str]) -> bytes:
    lines = ["proxies:"]
    for name, server, port, password in nodes:
        lines.append(
            f"  - {{name: '{name}', type: trojan, server: {server}, port: {port}, "
            f"password: '{password}'}}"
        )
    return "\n".join(lines).encode()


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("CPM_SECRET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def catalog(profile_manager, keyed):
    return NetworkCatalog(profile_manager)


def fetcher(*documents: bytes, usage=None):
    queue = list(documents)

    async def fetch(url):
        assert url == SUB_SECRET
        content = queue.pop(0)
        if isinstance(content, Exception):
            raise content
        return FetchedSubscription(content=content, usage=usage, suggested_name="Airport")

    return fetch


def raw(catalog, sql):
    return [tuple(row) for row in catalog.connection.execute(sql).fetchall()]


@pytest.mark.asyncio
async def test_secrets_are_sealed_and_never_returned(catalog):
    created = await catalog.create_subscription(
        None,
        url=SUB_SECRET,
        fetch=fetcher(doc(("JP 01", "jp.example", 443, SECRET)), usage={"total": 10}),
    )
    subscription = created["subscription"]
    assert subscription["name"] == "Airport"
    assert subscription["source_display"] == "https://sub.example/…"
    assert subscription["usage"] == {"total": 10}
    dump = "\n".join(catalog.connection.iterdump())
    assert SECRET not in dump and "TOKEN-123456" not in dump
    listed = repr(catalog.list_subscriptions()) + repr(catalog.list_nodes())
    assert SECRET not in listed and "TOKEN" not in listed
    node = catalog.list_nodes()["nodes"][0]
    _, outbound = catalog.node_outbound(node["id"])
    assert outbound["password"] == SECRET and outbound["type"] == "trojan"


@pytest.mark.asyncio
async def test_nothing_is_stored_without_an_encryption_key(profile_manager, monkeypatch):
    monkeypatch.delenv("CPM_SECRET_KEY", raising=False)
    get_settings.cache_clear()
    catalog = NetworkCatalog(profile_manager)
    with pytest.raises(NetworkSecretsUnavailable):
        await catalog.create_subscription("x", content=doc(("a", "a.example", 1, SECRET)))
    with pytest.raises(NetworkSecretsUnavailable):
        await catalog.create_subscription("x", url=SUB_SECRET, fetch=fetcher(doc()))
    assert raw(catalog, "SELECT COUNT(*) FROM network_subscriptions") == [(0,)]
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_failed_first_import_creates_nothing(catalog):
    with pytest.raises(SubscriptionError):
        await catalog.create_subscription("x", content=b"port: 1\n")
    assert raw(catalog, "SELECT COUNT(*) FROM network_subscriptions") == [(0,)]


@pytest.mark.asyncio
async def test_sync_keeps_ids_retires_missing_and_reports(catalog):
    a, b, c = (
        ("A", "a.example", 1, SECRET),
        ("B", "b.example", 2, SECRET),
        ("C", "c.example", 3, SECRET),
    )
    created = await catalog.create_subscription(
        "S",
        url=SUB_SECRET,
        fetch=fetcher(doc(a, b)),
    )
    sub_id = created["subscription"]["id"]
    assert created["report"]["created"] == 2
    before = {n["name"]: n["id"] for n in catalog.list_nodes()["nodes"]}

    result = await catalog.sync_subscription(sub_id, fetch=fetcher(doc(a, c)))
    report = result["report"]
    assert (report["created"], report["retired"], report["unchanged"]) == (1, 1, 1)
    after = {n["name"]: (n["id"], n["status"]) for n in catalog.list_nodes()["nodes"]}
    assert after["A"] == (before["A"], "active")
    assert after["B"] == (before["B"], "retired")
    assert after["C"][1] == "active"
    assert result["subscription"]["revision"] == 2

    # B comes back: same ID, active again.
    report = (await catalog.sync_subscription(sub_id, fetch=fetcher(doc(a, b, c))))["report"]
    assert report["updated"] == 1
    assert catalog.get_node(before["B"])["status"] == "active"


@pytest.mark.asyncio
async def test_changed_secret_updates_in_place_and_forgets_the_old_measurement(catalog):
    created = await catalog.create_subscription(
        "S", url=SUB_SECRET, fetch=fetcher(doc(("A", "a.example", 1, SECRET)))
    )
    node_id = catalog.list_nodes()["nodes"][0]["id"]
    catalog.save_check(
        node_id,
        {
            "health": "ok",
            "exit_ip": "203.0.113.5",
            "latitude": 1.0,
            "longitude": 2.0,
            "precision": "coordinates",
        },
    )
    report = (
        await catalog.sync_subscription(
            created["subscription"]["id"], fetch=fetcher(doc(("A", "a.example", 1, "rotated")))
        )
    )["report"]
    assert report["updated"] == 1
    assert catalog.get_node(node_id)["check"] is None
    assert catalog.node_outbound(node_id)[1]["password"] == "rotated"


@pytest.mark.asyncio
async def test_failed_sync_keeps_every_node(catalog):
    created = await catalog.create_subscription(
        "S", url=SUB_SECRET, fetch=fetcher(doc(("A", "a.example", 1, SECRET)))
    )
    sub_id = created["subscription"]["id"]
    for failure in (SubscriptionError("timeout", "slow"), b"not: [valid"):
        with pytest.raises(SubscriptionError):
            await catalog.sync_subscription(sub_id, fetch=fetcher(failure))
        nodes = catalog.list_nodes()["nodes"]
        assert [(n["name"], n["status"]) for n in nodes] == [("A", "active")]
    subscription = catalog.get_subscription(sub_id)
    assert subscription["status"] == "error" and subscription["last_error_code"] == "invalid_yaml"


@pytest.mark.asyncio
async def test_file_imports_refresh_only_with_a_new_file(catalog):
    created = await catalog.create_subscription(
        None, content=doc(("A", "a.example", 1, SECRET)), file_name="local.yaml"
    )
    sub_id = created["subscription"]["id"]
    assert created["subscription"]["source_kind"] == "file"
    assert created["subscription"]["source_display"] == "local.yaml"
    with pytest.raises(SubscriptionError) as error:
        await catalog.sync_subscription(sub_id)
    assert error.value.code == "file_required"
    report = await catalog.sync_subscription(sub_id, content=doc(("B", "b.example", 2, SECRET)))
    assert report["report"]["retired"] == 1


async def bind(manager, catalog, profile_name="P"):
    profile = await manager.create_profile(profile_name, generate_fingerprint=False)
    node_id = catalog.list_nodes()["nodes"][0]["id"]
    catalog.connection.execute(
        "INSERT INTO profile_network_bindings (profile_id, node_id, bound_at) VALUES (?,?,?)",
        (profile.id, node_id, "2026-01-01T00:00:00+00:00"),
    )
    catalog.connection.commit()
    return profile, node_id


@pytest.mark.asyncio
async def test_bound_subscriptions_are_only_retired_on_request(catalog, profile_manager):
    created = await catalog.create_subscription(
        "S", content=doc(("A", "a.example", 1, SECRET), ("B", "b.example", 2, SECRET))
    )
    sub_id = created["subscription"]["id"]
    profile, node_id = await bind(profile_manager, catalog)
    with pytest.raises(SubscriptionHasBindings) as error:
        catalog.delete_subscription(sub_id)
    assert error.value.count == 1
    assert catalog.delete_subscription(sub_id, retire_bound=True) == {
        "deleted": False,
        "retired_bound_nodes": 1,
    }
    # The bound node survives, retired, for its binding to report; the other is gone.
    assert raw(catalog, "SELECT id, status FROM network_nodes") == [(node_id, "retired")]
    assert catalog.list_subscriptions() == []
    sealed = raw(catalog, "SELECT source_url_enc FROM network_subscriptions")
    assert sealed == [(None,)]


@pytest.mark.asyncio
async def test_unbound_subscriptions_are_deleted_with_their_nodes(catalog):
    created = await catalog.create_subscription("S", content=doc(("A", "a.example", 1, SECRET)))
    assert catalog.delete_subscription(created["subscription"]["id"])["deleted"]
    assert raw(catalog, "SELECT COUNT(*) FROM network_nodes") == [(0,)]


@pytest.mark.asyncio
async def test_permanently_deleting_a_profile_cascades_its_binding(catalog, profile_manager):
    await catalog.create_subscription("S", content=doc(("A", "a.example", 1, SECRET)))
    profile, _ = await bind(profile_manager, catalog)
    await profile_manager.delete_profile(profile.id)  # Trash keeps the binding
    assert raw(catalog, "SELECT COUNT(*) FROM profile_network_bindings") == [(1,)]
    assert await profile_manager.permanently_delete_profile(profile.id)
    assert raw(catalog, "SELECT COUNT(*) FROM profile_network_bindings") == [(0,)]


@pytest.mark.asyncio
async def test_map_places_only_measured_exits_and_counts_shared_ips(catalog):
    await catalog.create_subscription(
        "S",
        content=doc(
            ("JP-1", "entry1.example", 1, SECRET),
            ("JP-2", "entry2.example", 2, SECRET),
            ("US", "us.example", 3, SECRET),
        ),
    )
    nodes = {n["name"]: n["id"] for n in catalog.list_nodes()["nodes"]}
    tokyo = {
        "health": "ok",
        "exit_ip": "203.0.113.7",
        "country_iso": "JP",
        "latitude": 35.69,
        "longitude": 139.69,
        "precision": "coordinates",
        "latency_ms": 120,
    }
    catalog.save_check(nodes["JP-1"], tokyo)
    catalog.save_check(nodes["JP-2"], tokyo)
    catalog.save_check(nodes["US"], {"health": "error", "error_code": "exit_unreachable"})
    data = catalog.map_data()
    features = data["features"]["features"]
    assert {f["properties"]["label"] for f in features} == {"JP-1", "JP-2"}
    assert all(f["geometry"]["coordinates"] == [139.69, 35.69] for f in features)
    assert all(f["properties"]["shared_exit_count"] == 2 for f in features)
    assert data["unknown_location_count"] == 1
    assert data["unknown_location"][0]["label"] == "US"
    assert (data["checked_count"], data["total_nodes"]) == (3, 3)
    # Filters work on measured data, not names.
    assert catalog.list_nodes(country="jp")["total"] == 2
    assert catalog.list_nodes(health="error")["total"] == 1
    assert catalog.list_nodes(located=False)["total"] == 1
    assert catalog.list_nodes(search="203.0.113")["total"] == 2


@pytest.mark.asyncio
async def test_a_wrong_key_is_reported_not_returned_as_ciphertext(catalog, monkeypatch):
    await catalog.create_subscription("S", content=doc(("A", "a.example", 1, SECRET)))
    node_id = catalog.list_nodes()["nodes"][0]["id"]
    monkeypatch.setenv("CPM_SECRET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    with pytest.raises(NetworkSecretsUnavailable):
        catalog.node_outbound(node_id)
