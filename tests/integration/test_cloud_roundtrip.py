"""Full end-to-end integration tests against the real standalone ``cloud_server`` FastAPI app."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from cloud_server.app import create_app as create_cloud_app
from cryptography.fernet import Fernet
from httpx import ASGITransport

from camoufox_pm.api import dependencies
from camoufox_pm.api.routes import cloud as cloud_routes
from camoufox_pm.config import get_settings
from camoufox_pm.core.cloud import CloudDeliveryService
from camoufox_pm.core.database import StorageManager
from camoufox_pm.core.isolation.policy import BUILTIN_POLICY, load_active_policy
from camoufox_pm.core.network_binding import NetworkBindings
from camoufox_pm.core.network_catalog import NetworkCatalog
from camoufox_pm.core.profile_manager import ProfileManager
from camoufox_pm.main import app as local_app

CLOUD_CLASH_YAML = """
proxies:
  - {name: "US-LosAngeles-01", type: trojan, server: la.cloud-pool.example, port: 443, password: "pool-secret-password"}
  - {name: "SG-Singapore-01", type: ss, server: sg.cloud-pool.example, port: 8388, cipher: aes-128-gcm, password: "pool-secret-password"}
"""

CLIENT_TOKEN = "integration-client-token"
ADMIN_TOKEN = "integration-admin-token"


async def _make_local_machine(tmp_path, name: str):
    machine_dir = tmp_path / name
    machine_dir.mkdir(parents=True, exist_ok=True)
    storage = StorageManager(str(machine_dir / "profiles.db"))
    await storage.initialize()
    manager = ProfileManager(storage, str(machine_dir))
    await manager.initialize()
    return storage, manager


@pytest.mark.asyncio
async def test_cloud_server_auth_etag_and_profile_crud(tmp_path):
    cloud_app = create_cloud_app(
        db_path=tmp_path / "cloud_crud.db",
        token=CLIENT_TOKEN,
        admin_token=ADMIN_TOKEN,
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(app=cloud_app),
        base_url="https://cloud.example.test",
    ) as client:
        # Unauthenticated healthcheck works.
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert health.json()["service"] == "prism-cloud-server"

        # Client endpoints require client token; admin endpoints require admin token.
        assert (await client.get("/v1/manifest")).status_code == 401
        assert (
            await client.put(
                "/v1/admin/subscriptions",
                headers={"Authorization": f"Bearer {CLIENT_TOKEN}"},
                json={"subscriptions": []},
            )
        ).status_code == 401

        client_headers = {"Authorization": f"Bearer {CLIENT_TOKEN}"}
        manifest_resp = await client.get("/v1/manifest", headers=client_headers)
        assert manifest_resp.status_code == 200
        etag = manifest_resp.headers["etag"]
        assert manifest_resp.json()["profiles_sync_enabled"] is True

        # Conditional ETag returns 304 Not Modified.
        not_mod = await client.get(
            "/v1/manifest",
            headers={**client_headers, "If-None-Match": etag},
        )
        assert not_mod.status_code == 304

        # Profile CRUD roundtrip.
        put_resp = await client.put(
            "/v1/profiles/prof01",
            headers=client_headers,
            json={"name": "Alpha", "tags": ["us"], "revision": 0},
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["revision"] == 1

        list_resp = await client.get("/v1/profiles", headers=client_headers)
        assert list_resp.status_code == 200
        assert list_resp.json()["count"] == 1

        del_resp = await client.delete("/v1/profiles/prof01", headers=client_headers)
        assert del_resp.status_code == 200
        assert del_resp.json()["deleted"] is True
        assert (await client.get("/v1/profiles/prof01", headers=client_headers)).status_code == 404


@pytest.mark.asyncio
async def test_full_cloud_roundtrip_two_machines_via_local_api(tmp_path, monkeypatch):
    """Full loop: cloud publish -> machine A sync/create/bind/push -> machine B sync/pull/rebind."""
    shared_secret = Fernet.generate_key().decode()
    monkeypatch.setenv("CPM_SECRET_KEY", shared_secret)
    monkeypatch.delenv("CPM_API_KEY", raising=False)
    monkeypatch.delenv("CPM_CLOUD_URL", raising=False)
    monkeypatch.delenv("CPM_CLOUD_TOKEN", raising=False)
    monkeypatch.setenv("CPM_CLOUD_ALLOW_LOCAL", "1")
    monkeypatch.setattr(
        "camoufox_pm.core.browser_install.ensure_browser_ready", lambda: None, raising=False
    )
    get_settings.cache_clear()

    cloud_app = create_cloud_app(
        db_path=tmp_path / "cloud_server.db",
        token=CLIENT_TOKEN,
        admin_token=ADMIN_TOKEN,
    )
    cloud_transport = ASGITransport(app=cloud_app)

    # 1. Admin publishes an updated isolation policy and two cloud subscriptions.
    custom_policy = BUILTIN_POLICY.model_dump(mode="json")
    custom_policy["policy_version"] = "prism-cloud-2026.12.01"
    custom_policy["default_timezone_mode"] = "fixed"
    custom_policy["default_timezone"] = "America/Los_Angeles"
    custom_policy["default_languages"] = ["en-US", "en"]
    custom_policy["install_webrtc_guard"] = True

    async with httpx.AsyncClient(
        transport=cloud_transport, base_url="http://127.0.0.1:8787"
    ) as cloud_http:
        p_resp = await cloud_http.put(
            "/v1/admin/isolation-policy",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            json={"version": "prism-cloud-2026.12.01", "policy": custom_policy},
        )
        assert p_resp.status_code == 200, p_resp.text

        s_resp = await cloud_http.put(
            "/v1/admin/subscriptions",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            json={
                "version": "subs-v1",
                "subscriptions": [
                    {
                        "cloud_source_id": "corp-clash-pool",
                        "name": "Corp Cloud Pool",
                        "kind": "content",
                        "content": CLOUD_CLASH_YAML,
                        "auto_sync": True,
                    },
                    {
                        "cloud_source_id": "corp-static-la",
                        "name": "Corp Static LA",
                        "kind": "manual",
                        "manual_nodes": ["socks5://alice:s3cr3t@203.0.113.77:1080#LA-Static-01"],
                        "auto_sync": True,
                    },
                ],
            },
        )
        assert s_resp.status_code == 200, s_resp.text

    # 2. Boot Machine A wired into the real local FastAPI app.
    storage_a, manager_a = await _make_local_machine(tmp_path, "machine_a")
    secret_cookie_json = json.dumps(
        [
            {
                "name": "auth_session",
                "value": "ultra-secret-cookie-value-777",
                "domain": "shop.example.com",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ]
    )
    machine_b_restored_cookies: dict[str, str] = {}

    try:
        dependencies.set_storage_manager(storage_a)
        dependencies.set_profile_manager(manager_a)
        cloud_routes.set_cloud_service_factory(
            lambda mgr: CloudDeliveryService(
                mgr,
                transport=cloud_transport,
                cookie_exporter=lambda _pid: _async_val(secret_cookie_json),
            )
        )

        async with httpx.AsyncClient(
            transport=ASGITransport(app=local_app), base_url="http://test-a"
        ) as api_a:
            # Configure local HTTP cloud endpoint (allowed via CPM_CLOUD_ALLOW_LOCAL=1).
            cfg_resp = await api_a.put(
                "/api/v1/cloud/config",
                json={
                    "base_url": "http://127.0.0.1:8787",
                    "token": CLIENT_TOKEN,
                    "auto_sync_on_startup": True,
                    "sync_profiles_enabled": True,
                    "device_label": "machine-a",
                },
            )
            assert cfg_resp.status_code == 200, cfg_resp.text
            assert cfg_resp.headers["cache-control"] == "no-store"
            assert CLIENT_TOKEN not in cfg_resp.text
            assert cfg_resp.json()["data"]["has_token"] is True

            # Sync policy + subscriptions on Machine A.
            sync_resp = await api_a.post(
                "/api/v1/cloud/sync",
                json={"scopes": ["policy", "subscriptions"]},
            )
            assert sync_resp.status_code == 200, sync_resp.text
            sync_data = sync_resp.json()["data"]
            assert sync_data["ok"] is True
            assert sync_data["policy"]["updated"] is True
            assert sync_data["policy"]["version"] == "prism-cloud-2026.12.01"
            assert sync_data["subscriptions"]["created"] == 2

            # Verify active policy endpoint reflects the cloud policy.
            pol_resp = await api_a.get("/api/v1/cloud/policy")
            assert pol_resp.status_code == 200
            pol_data = pol_resp.json()["data"]
            assert pol_data["source"] == "cloud"
            assert pol_data["policy"]["policy_version"] == "prism-cloud-2026.12.01"
            assert pol_data["policy"]["default_timezone"] == "America/Los_Angeles"
            assert (
                pol_data["policy"]["hardening_prefs"]["network.proxy.share_proxy_settings"] is True
            )
            assert pol_data["policy"]["hardening_prefs"]["network.dns.echconfig.enabled"] is False
            assert pol_data["policy"]["hardening_prefs"]["security.OCSP.enabled"] == 0
            assert len(pol_data["policy"]["risk_probe_cidrs"]) == 4
            assert pol_data["summary"]["risk_domains_count"] >= 36
            assert pol_data["summary"]["risk_cidrs_count"] == 4

            # Verify cloud subscriptions & nodes exist on Machine A.
            catalog_a = NetworkCatalog(manager_a)
            nodes_a = catalog_a.list_nodes()["nodes"]
            la_node = next(n for n in nodes_a if n["name"] == "US-LosAngeles-01")

            # Create a browser profile bound to the cloud node.
            secret_memo = "merchant-login-email-secret@example.com"
            profile_a = await manager_a.create_profile(
                "LA Storefront 01",
                group="Commerce",
                tags=["us", "store"],
                notes=secret_memo,
            )
            await NetworkBindings(manager_a).bind(
                profile_a.id,
                la_node["id"],
                binding_row_version=None,
            )

            # Push the profile + encrypted cookies to cloud_server via local REST API.
            push_resp = await api_a.post(
                f"/api/v1/cloud/profiles/{profile_a.id}/push",
                json={"include_cookies": True},
            )
            assert push_resp.status_code == 200, push_resp.text
            push_data = push_resp.json()["data"]
            assert push_data["revision"] == 1
            assert push_data["included_cookies"] is True
            assert push_data["bound_subscription_cloud_id"] == "corp-clash-pool"
            assert push_data["bound_node_key"]

            # Confirm the cloud server never sees plaintext cookies or private memo.
            stored_cloud = cloud_app.state.store.get_profile(profile_a.id)
            assert stored_cloud is not None
            stored_text = json.dumps(stored_cloud)
            assert "ultra-secret-cookie-value-777" not in stored_text
            assert secret_memo not in stored_text

        # 3. Boot brand-new Machine B (empty DB, same CPM_SECRET_KEY) and sync everything from cloud!
        storage_b, manager_b = await _make_local_machine(tmp_path, "machine_b")
        try:
            dependencies.set_storage_manager(storage_b)
            dependencies.set_profile_manager(manager_b)

            async def record_cookie_import(pid: str, cookies_json: str) -> None:
                machine_b_restored_cookies[pid] = cookies_json

            cloud_routes.set_cloud_service_factory(
                lambda mgr: CloudDeliveryService(
                    mgr,
                    transport=cloud_transport,
                    cookie_importer=record_cookie_import,
                )
            )

            async with httpx.AsyncClient(
                transport=ASGITransport(app=local_app), base_url="http://test-b"
            ) as api_b:
                assert (
                    await api_b.put(
                        "/api/v1/cloud/config",
                        json={
                            "base_url": "http://127.0.0.1:8787",
                            "token": CLIENT_TOKEN,
                            "device_label": "machine-b",
                        },
                    )
                ).status_code == 200

                # Single full sync pulls policy -> subscriptions -> profiles (with cookies) in order!
                full_sync = await api_b.post(
                    "/api/v1/cloud/sync",
                    json={"scopes": ["all"], "include_cookies": True},
                )
                assert full_sync.status_code == 200, full_sync.text
                rep = full_sync.json()["data"]
                assert rep["ok"] is True
                assert rep["policy"]["version"] == "prism-cloud-2026.12.01"
                assert rep["subscriptions"]["created"] == 2
                assert rep["profiles"]["created"] == 1
                assert rep["profiles"]["rebound"] == 1
                assert rep["profiles"]["cookies_restored"] == 1

                # Verify Machine B has active cloud policy, restored profile, decrypted notes,
                # decrypted cookies, and automatically restored node binding!
                policy_b, source_b = load_active_policy(storage_b.db._connection)
                assert source_b == "cloud"
                assert policy_b.policy_version == "prism-cloud-2026.12.01"

                restored = await manager_b.get_profile(profile_a.id)
                assert restored is not None
                assert restored.name == "LA Storefront 01"
                assert restored.group == "Commerce"
                assert restored.tags == ["us", "store"]
                assert restored.notes == secret_memo
                assert restored.fingerprint_hash == profile_a.fingerprint_hash
                assert machine_b_restored_cookies[profile_a.id] == secret_cookie_json

                binding_b = NetworkBindings(manager_b).get(profile_a.id)
                assert binding_b is not None
                assert binding_b["usable"] is True
                assert binding_b["node"]["name"] == "US-LosAngeles-01"

                status_b = (await api_b.get("/api/v1/cloud/status")).json()["data"]
                assert status_b["configured"] is True
                assert status_b["connected"] is True
                assert status_b["policy_source"] == "cloud"
                assert status_b["cloud_subscription_count"] == 2
                assert status_b["synced_profiles_count"] == 1

                # Direct /profiles/pull endpoint also succeeds cleanly (idempotent when unchanged).
                pull_again = await api_b.post("/api/v1/cloud/profiles/pull", json={})
                assert pull_again.status_code == 200, pull_again.text
                assert pull_again.json()["data"]["unchanged"] == 1
                assert pull_again.json()["data"]["skipped_running"] == 0

                # System status reflects the synced cloud policy version.
                sys_status = await api_b.get("/api/v1/system/status")
                assert sys_status.status_code == 200, sys_status.text
                sys_body = sys_status.json()
                assert sys_body["cloud_policy_source"] == "cloud"
                assert sys_body["cloud_policy_version"] == "prism-cloud-2026.12.01"
        finally:
            await storage_b.close()
    finally:
        cloud_routes.set_cloud_service_factory(None)
        await storage_a.close()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_local_cloud_routes_error_handling(tmp_path, monkeypatch):
    monkeypatch.setenv("CPM_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("CPM_API_KEY", raising=False)
    monkeypatch.delenv("CPM_CLOUD_URL", raising=False)
    monkeypatch.delenv("CPM_CLOUD_TOKEN", raising=False)
    monkeypatch.delenv("CPM_CLOUD_ALLOW_LOCAL", raising=False)
    get_settings.cache_clear()

    storage, manager = await _make_local_machine(tmp_path, "err_machine")
    try:
        dependencies.set_storage_manager(storage)
        dependencies.set_profile_manager(manager)
        cloud_routes.set_cloud_service_factory(None)

        async with httpx.AsyncClient(
            transport=ASGITransport(app=local_app), base_url="http://test-err"
        ) as api:
            # Syncing before configuring URL returns 400 not_configured.
            resp = await api.post("/api/v1/cloud/sync", json={})
            assert resp.status_code == 400
            assert resp.json()["error"]["code"] == "not_configured"

            # Reject plain HTTP remote URLs with 422 https_required.
            bad_url = await api.put(
                "/api/v1/cloud/config",
                json={"base_url": "http://remote.example.com"},
            )
            assert bad_url.status_code == 422
            assert bad_url.json()["error"]["code"] == "https_required"

            # Pushing a missing profile returns 404 profile_not_found.
            missing = await api.post("/api/v1/cloud/profiles/nonexistent/push", json={})
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "profile_not_found"
    finally:
        cloud_routes.set_cloud_service_factory(None)
        await storage.close()
        get_settings.cache_clear()


async def _async_val(value: Any) -> Any:
    return value
