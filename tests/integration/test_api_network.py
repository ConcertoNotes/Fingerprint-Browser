"""The /network API: envelope, secrecy, error codes, CAS bindings and access control."""

import pytest
from cryptography.fernet import Fernet

from camoufox_pm.api import dependencies
from camoufox_pm.config import get_settings
from camoufox_pm.core import auth

SECRET = "api-network-node-secret"
YAML = f"""
proxies:
  - {{name: "JP 01", type: trojan, server: jp.example, port: 443, password: "{SECRET}"}}
  - {{name: "US 01", type: ss, server: us.example, port: 8388, cipher: aes-128-gcm, password: "{SECRET}"}}
"""
PASSWORD = "strong-test-password"


@pytest.fixture
async def api(client, monkeypatch):
    monkeypatch.setenv("CPM_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("CPM_API_KEY", raising=False)
    get_settings.cache_clear()
    yield client
    get_settings.cache_clear()


async def import_yaml(api, name="Local import", content=YAML):
    response = await api.post(
        "/api/v1/network/subscriptions",
        json={"name": name, "content": content, "file_name": "clash.yaml"},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def nodes(api):
    response = await api.get("/api/v1/network/nodes")
    assert response.status_code == 200
    return {node["name"]: node for node in response.json()["data"]["nodes"]}


@pytest.mark.asyncio
async def test_import_list_and_responses_never_contain_secrets(api):
    created = await import_yaml(api)
    assert created["report"]["created"] == 2
    assert created["subscription"]["node_counts"] == {"active": 2, "retired": 0, "invalid": 0}
    for path in ("/subscriptions", "/nodes", "/map", "/bindings", "/settings", "/status"):
        response = await api.get("/api/v1/network" + path)
        assert response.status_code == 200, path
        assert response.json()["success"] is True
        assert response.headers["cache-control"] == "no-store"
        assert SECRET not in response.text and "enc:" not in response.text
        assert "config_enc" not in response.text
    listed = await nodes(api)
    assert listed["JP 01"]["protocol"] == "trojan" and listed["JP 01"]["check"] is None
    # The unversioned alias serves the same routes.
    assert (await api.get("/api/network/nodes")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body, code",
    [
        ({"url": "http://sub.example/x"}, "https_required"),
        ({"url": "https://127.0.0.1/x"}, "blocked_address"),
        ({"content": "port: 7890\n"}, "no_proxies"),
        ({"content": "proxy-providers: {a: {type: http}}\n"}, "providers_only"),
        ({"content": "dm1lc3M6Ly9leUoySWpvaU1pSjk=\n"}, "no_valid_proxies"),
    ],
)
async def test_unusable_sources_fail_with_their_code_and_store_nothing(api, body, code):
    response = await api.post("/api/v1/network/subscriptions", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == code
    assert (await api.get("/api/v1/network/subscriptions")).json()["data"]["subscriptions"] == []


@pytest.mark.asyncio
async def test_both_or_neither_source_is_a_validation_error(api):
    for body in ({}, {"url": "https://a.example/", "content": "x"}):
        response = await api.post("/api/v1/network/subscriptions", json=body)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_without_an_encryption_key_nothing_is_imported(api, monkeypatch):
    monkeypatch.delenv("CPM_SECRET_KEY")
    get_settings.cache_clear()
    response = await api.post("/api/v1/network/subscriptions", json={"content": YAML})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "secrets_unavailable"


@pytest.mark.asyncio
async def test_binding_is_cas_checked_and_refused_while_running(api):
    await import_yaml(api)
    listed = await nodes(api)
    manager = dependencies.get_profile_manager()
    profile = await manager.create_profile(
        "Bound",
        generate_fingerprint=False,
        browser_settings={"timezone": "Europe/Berlin"},
        proxy_config={"type": "http", "server": "manual.example:8080"},
    )
    path = f"/api/v1/network/bindings/{profile.id}"
    bound = await api.put(
        path, json={"node_id": listed["JP 01"]["id"], "binding_row_version": None}
    )
    assert bound.status_code == 200, bound.text
    data = bound.json()["data"]
    assert data["row_version"] == 0 and data["usable"]
    # Reported for the user to decide; nothing is changed behind their back.
    assert data["geography_conflict"] is True and data["overrides_manual_proxy"] is True
    assert (await manager.get_profile(profile.id)).browser_settings.timezone == "Europe/Berlin"

    stale = await api.put(
        path, json={"node_id": listed["US 01"]["id"], "binding_row_version": None}
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_write"
    moved = await api.put(path, json={"node_id": listed["US 01"]["id"], "binding_row_version": 0})
    assert moved.status_code == 200 and moved.json()["data"]["row_version"] == 1

    # A leased (running) profile is refused, and its binding is untouched.
    assert await manager.storage.acquire_lease(profile.id, "another-instance", 120)
    locked = await api.put(path, json={"node_id": listed["JP 01"]["id"], "binding_row_version": 1})
    assert locked.status_code == 409 and locked.json()["error"]["code"] == "conflict"
    unbind_locked = await api.delete(path, params={"binding_row_version": 1})
    assert unbind_locked.status_code == 409
    await manager.storage.release_lease(profile.id, "another-instance")

    current = (await api.get(path)).json()["data"]
    assert current["binding"]["node_id"] == listed["US 01"]["id"]
    assert (
        current["runtime"]["state"] == "stopped" and current["runtime"]["adapter_kind"] == "mihomo"
    )
    assert (await api.delete(path, params={"binding_row_version": 0})).status_code == 409
    assert (await api.delete(path, params={"binding_row_version": 1})).status_code == 200
    assert (await api.get(path)).json()["data"]["binding"] is None
    assert (await api.delete(path, params={"binding_row_version": 1})).status_code == 404


@pytest.mark.asyncio
async def test_bindings_reject_missing_profiles_nodes_and_retired_nodes(api):
    created = await import_yaml(api)
    listed = await nodes(api)
    manager = dependencies.get_profile_manager()
    profile = await manager.create_profile("P", generate_fingerprint=False)
    missing_node = await api.put(f"/api/v1/network/bindings/{profile.id}", json={"node_id": "nope"})
    assert missing_node.status_code == 404
    missing_profile = await api.put(
        "/api/v1/network/bindings/missing1", json={"node_id": listed["JP 01"]["id"]}
    )
    assert missing_profile.status_code == 404
    sub_id = created["subscription"]["id"]
    only_us = YAML.split("\n  - ")[0] + "\n  - " + YAML.split("\n  - ")[2]
    sync = await api.post(f"/api/v1/network/subscriptions/{sub_id}/sync", json={"content": only_us})
    assert sync.status_code == 200 and sync.json()["data"]["report"]["retired"] == 1
    retired = await api.put(
        f"/api/v1/network/bindings/{profile.id}", json={"node_id": listed["JP 01"]["id"]}
    )
    assert retired.status_code == 422 and retired.json()["error"]["code"] == "node_unavailable"


@pytest.mark.asyncio
async def test_deleting_a_bound_subscription_needs_explicit_retirement(api):
    created = await import_yaml(api)
    sub_id = created["subscription"]["id"]
    listed = await nodes(api)
    profile = await dependencies.get_profile_manager().create_profile(
        "P", generate_fingerprint=False
    )
    await api.put(f"/api/v1/network/bindings/{profile.id}", json={"node_id": listed["JP 01"]["id"]})
    refused = await api.delete(f"/api/v1/network/subscriptions/{sub_id}")
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "subscription_bound"
    assert refused.json()["error"]["details"] == [{"bound_profiles": 1}]
    retired = await api.delete(
        f"/api/v1/network/subscriptions/{sub_id}", params={"retire_bound": True}
    )
    assert retired.status_code == 200 and retired.json()["data"]["retired_bound_nodes"] == 1
    binding = (await api.get(f"/api/v1/network/bindings/{profile.id}")).json()["data"]["binding"]
    assert binding["usable"] is False and binding["node"]["status"] == "retired"


@pytest.mark.asyncio
async def test_rename_is_version_checked(api):
    created = await import_yaml(api)
    sub_id = created["subscription"]["id"]
    path = f"/api/v1/network/subscriptions/{sub_id}"
    assert (await api.patch(path, json={"name": "New", "row_version": 0})).status_code == 200
    stale = await api.patch(path, json={"name": "Other", "row_version": 0})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_write"
    assert (await api.get(path)).json()["data"]["name"] == "New"


@pytest.mark.asyncio
async def test_settings_keep_keys_write_only(api):
    response = await api.patch(
        "/api/v1/network/settings",
        json={
            "probe_concurrency": 4,
            "intel_scamalytics_key": "SCAM-KEY-123",
            "intel_scamalytics_url": "https://api11.scamalytics.com/v3/me/",
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["probe_concurrency"] == 4 and data["has_intel_scamalytics_key"] is True
    assert "SCAM-KEY-123" not in response.text
    stored = (
        dependencies.get_storage_manager()
        .db._connection.execute(
            "SELECT value FROM network_settings WHERE key='intel_scamalytics_key'"
        )
        .fetchone()[0]
    )
    assert "SCAM-KEY-123" not in stored
    bad = await api.patch("/api/v1/network/settings", json={"intel_scamalytics_url": "http://x"})
    assert bad.status_code == 422
    out_of_range = await api.patch("/api/v1/network/settings", json={"probe_concurrency": 99})
    assert out_of_range.status_code == 422
    cleared = await api.patch("/api/v1/network/settings", json={"intel_scamalytics_key": None})
    assert cleared.json()["data"]["has_intel_scamalytics_key"] is False


@pytest.mark.asyncio
async def test_runtime_actions_need_a_running_profile(api):
    profile = await dependencies.get_profile_manager().create_profile(
        "P", generate_fingerprint=False
    )
    for path, body in (
        (f"/runtime/{profile.id}/reconnect", None),
        (f"/runtime/{profile.id}/open-site", {"site": "ippure"}),
    ):
        response = await api.post("/api/v1/network" + path, json=body)
        assert response.status_code == 409 and response.json()["error"]["code"] == "not_running"
    bad_site = await api.post(
        f"/api/v1/network/runtime/{profile.id}/open-site", json={"site": "https://evil.example"}
    )
    assert bad_site.status_code == 422


@pytest.mark.asyncio
async def test_batch_check_validates_its_input(api):
    assert (
        await api.post("/api/v1/network/nodes/check-batch", json={"node_ids": []})
    ).status_code == 422
    response = await api.post("/api/v1/network/nodes/check-batch", json={"node_ids": ["nope"]})
    assert response.status_code == 404
    assert (await api.get("/api/v1/network/jobs/nope")).status_code == 404


@pytest.mark.asyncio
async def test_operators_cannot_reach_instance_network_routes(api, monkeypatch):
    await import_yaml(api)
    storage = dependencies.get_storage_manager()
    manager = dependencies.get_profile_manager()
    own = await manager.create_profile("Own", generate_fingerprint=False)
    await storage.create_user(auth.new_user_id(), "admin", auth.hash_password(PASSWORD))
    await storage.create_user(
        auth.new_user_id(), "member", auth.hash_password(PASSWORD), role="operator"
    )
    await storage.set_profile_grant("member", own.id)
    anonymous = await api.get("/api/v1/network/nodes")
    assert anonymous.status_code == 401
    login = await api.post("/api/v1/auth/login", json={"username": "member", "password": PASSWORD})
    assert login.status_code == 200
    for method, path in (
        ("GET", "/subscriptions"),
        ("GET", "/nodes"),
        ("GET", "/map"),
        ("GET", "/settings"),
        ("GET", "/bindings"),
    ):
        response = await api.request(method, "/api/v1/network" + path)
        assert response.status_code == 403, path
        assert SECRET not in response.text
    # Their own profile's runtime is visible; another's is indistinguishable from missing.
    assert (await api.get(f"/api/v1/network/runtime/{own.id}")).status_code == 200
    assert (await api.get("/api/v1/network/runtime/missing1")).status_code == 404


@pytest.mark.asyncio
async def test_launching_a_bound_profile_without_an_engine_is_refused_cleanly(api, monkeypatch):
    from camoufox_pm.core import mihomo_manager, network_adapter

    def missing(configured, data_dir):
        raise mihomo_manager.MihomoUnavailable("binary_missing", "Mihomo is not installed.")

    monkeypatch.setattr(network_adapter, "discover_binary", missing)
    await import_yaml(api)
    listed = await nodes(api)
    manager = dependencies.get_profile_manager()
    profile = await manager.create_profile("P", generate_fingerprint=False)
    await api.put(f"/api/v1/network/bindings/{profile.id}", json={"node_id": listed["JP 01"]["id"]})
    launch = await api.post(f"/api/v1/profiles/{profile.id}/launch", json={})
    assert launch.status_code == 503
    assert launch.json()["error"]["code"] == "binary_missing"
    lease = await manager.storage.get_lease(profile.id)
    assert lease is None or lease[0] is None
    assert not manager.browser_sessions.is_live(profile.id)


@pytest.mark.asyncio
async def test_manual_nodes_from_a_form_or_pasted_lines(api):
    created = await api.post(
        "/api/v1/network/manual-nodes",
        json={
            "group_name": "Static seller",
            "node": {
                "name": "Seller 1",
                "type": "socks5",
                "server": "203.0.113.50",
                "port": 1080,
                "username": "buyer",
                "password": SECRET,
            },
        },
    )
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    group_id = data["group"]["id"]
    assert data["group"]["manual"] is True and data["created"][0]["protocol"] == "socks5"
    assert SECRET not in created.text
    pasted = await api.post(
        "/api/v1/network/manual-nodes",
        json={
            "group_id": group_id,
            "default_type": "http",
            "text": f"198.51.100.7:3128:buyer:{SECRET}\nnot a proxy\n",
        },
    )
    assert pasted.status_code == 201
    assert pasted.json()["data"]["skipped"] == [{"line": 2, "reason": "unrecognised proxy line"}]
    node_id = pasted.json()["data"]["created"][0]["id"]
    renamed = await api.patch(f"/api/v1/network/nodes/{node_id}", json={"name": "Seller 2"})
    assert renamed.status_code == 200 and renamed.json()["data"]["name"] == "Seller 2"
    profile = await dependencies.get_profile_manager().create_profile(
        "P", generate_fingerprint=False
    )
    await api.put(f"/api/v1/network/bindings/{profile.id}", json={"node_id": node_id})
    refused = await api.delete(f"/api/v1/network/nodes/{node_id}")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "node_bound"
    listed = (await api.get("/api/v1/network/subscriptions")).json()["data"]["subscriptions"]
    assert listed[0]["node_counts"]["active"] == 2
    sync = await api.post(f"/api/v1/network/subscriptions/{group_id}/sync", json={})
    assert sync.status_code == 422 and sync.json()["error"]["code"] == "manual_group"
    assert (await api.get("/api/v1/network/jobs/current")).json()["data"] == {"job": None}


@pytest.mark.asyncio
async def test_manual_nodes_need_one_source_and_a_group(api):
    for body in (
        {"group_name": "x"},
        {"text": "1.2.3.4:80"},
        {"group_name": "x", "text": "1.2.3.4:80", "node": {"server": "a", "port": 1}},
    ):
        response = await api.post("/api/v1/network/manual-nodes", json=body)
        assert response.status_code == 422
    imported = await import_yaml(api)
    node_id = (await nodes(api))["JP 01"]["id"]
    edited = await api.patch(f"/api/v1/network/nodes/{node_id}", json={"name": "x"})
    assert edited.status_code == 422 and edited.json()["error"]["code"] == "not_manual"
    assert imported["subscription"]["manual"] is False
