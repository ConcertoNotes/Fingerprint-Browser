"""Unit tests for cloud credential protection, enc2 sealing, Mihomo shredding, entitlements, and customer edition."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import psutil
import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport

from camoufox_pm.api import dependencies
from camoufox_pm.api.routes import cloud as cloud_routes
from camoufox_pm.config import Settings, get_settings
from camoufox_pm.core import crypto
from camoufox_pm.core.cloud import (
    CloudAccountEntitlement,
    CloudClient,
    CloudClientError,
    CloudConfigUpdate,
    CloudDeliveryService,
    CloudEntitlementError,
    CloudSyncError,
    canonical_sha256,
    check_cloud_entitlement,
    clear_active_seal_salt,
    evaluate_entitlement,
    get_active_seal_salt,
    save_entitlement,
)
from camoufox_pm.core.isolation.policy import BUILTIN_POLICY
from camoufox_pm.core.mihomo_manager import MihomoProcess, reap_orphans, validate_binary
from camoufox_pm.core.network_adapter import NetworkLaunchError, ProfileNetwork
from camoufox_pm.core.network_binding import NetworkBindings
from camoufox_pm.core.network_catalog import (
    CloudManagedReadOnly,
    NetworkCatalog,
    NetworkSecretsUnavailable,
)
from camoufox_pm.main import create_app

UPSTREAM_SERVER = "secret-origin-node.upstream.example"
UPSTREAM_SNI = "camouflage.sni.upstream.example"
UPSTREAM_HOST = "cdn-edge.host.upstream.example"
UPSTREAM_PASSWORD = "ultra-secret-trojan-password-999"
UPSTREAM_UUID = "11111111-2222-3333-4444-555555555555"
UPSTREAM_PUBKEY = "PublicKeySecretBase64Value123456"
UPSTREAM_SHORTID = "a1b2c3d4"
UPSTREAM_PATH = "/secret-ws-tunnel-path"
UPSTREAM_SVC = "secret-grpc-service"


@pytest.fixture
def secret_env(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("CPM_SECRET_KEY", key)
    monkeypatch.delenv("CPM_API_KEY", raising=False)
    monkeypatch.delenv("CPM_CLOUD_URL", raising=False)
    monkeypatch.delenv("CPM_CLOUD_TOKEN", raising=False)
    monkeypatch.delenv("CPM_CLOUD_ALLOW_LOCAL", raising=False)
    monkeypatch.delenv("CPM_CLIENT_EDITION", raising=False)
    monkeypatch.delenv("CPM_BRAND_NAME", raising=False)
    monkeypatch.delenv("CPM_REQUIRE_ACTIVATION", raising=False)
    get_settings.cache_clear()
    yield key
    clear_active_seal_salt(None)
    get_settings.cache_clear()


def _build_mock_cloud_transport(
    *,
    expected_token: str = "prism_test_token_123",
    seal_salt: str = "tenant-salt-v1-xyz",
    tenant_id: str = "tenant-alpha",
    tenant_name: str = "Acme Customer",
    status: str = "active",
    expires_at: str | None = None,
    max_devices: int = 3,
    active_devices: int = 1,
    offline_grace_hours: int = 24,
    profiles_sync_enabled: bool = False,
    sealed_nodes: list[dict[str, Any]] | None = None,
    extra_subscriptions: list[dict[str, Any]] | None = None,
    error_response: tuple[int, dict[str, Any]] | None = None,
    captured_headers: list[dict[str, str]] | None = None,
) -> httpx.MockTransport:
    policy_obj = BUILTIN_POLICY.model_dump(mode="json")
    policy_sha = canonical_sha256(policy_obj)

    nodes_payload = (
        sealed_nodes
        if sealed_nodes is not None
        else [
            {
                "name": "Cloud-US-01",
                "type": "trojan",
                "server": UPSTREAM_SERVER,
                "port": 443,
                "password": UPSTREAM_PASSWORD,
                "sni": UPSTREAM_SNI,
            },
            {
                "name": "Cloud-JP-02",
                "type": "ss",
                "server": "jp-hidden.upstream.example",
                "port": 8388,
                "cipher": "aes-128-gcm",
                "password": "jp-hidden-password-777",
            },
        ]
    )
    subs_items: list[dict[str, Any]] = [
        {
            "cloud_source_id": "pool-sealed-01",
            "name": "VIP Dedicated Pool",
            "kind": "sealed_nodes",
            "nodes": nodes_payload,
            "auto_sync": True,
            "enabled": True,
        }
    ]
    if extra_subscriptions:
        subs_items.extend(extra_subscriptions)
    subs_sha = canonical_sha256(subs_items)

    def _handler(request: httpx.Request) -> httpx.Response:
        if captured_headers is not None:
            captured_headers.append(dict(request.headers))
        if error_response is not None:
            code_status, body = error_response
            return httpx.Response(code_status, json=body)
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {expected_token}":
            return httpx.Response(
                401,
                json={"code": "invalid_token", "message": "Invalid or expired activation token."},
            )
        now_iso = datetime.now(timezone.utc).isoformat()
        if request.url.path == "/v1/manifest":
            return httpx.Response(
                200,
                headers={"ETag": f'"{subs_sha[:16]}"'},
                json={
                    "protocol_version": 1,
                    "server_time": now_iso,
                    "isolation_policy": {
                        "version": policy_obj["policy_version"],
                        "sha256": policy_sha,
                    },
                    "subscriptions": {
                        "version": "subs-sealed-1",
                        "sha256": subs_sha,
                        "count": len(subs_items),
                    },
                    "profiles_sync_enabled": profiles_sync_enabled,
                    "profiles_count": 0,
                    "account": {
                        "tenant_id": tenant_id,
                        "tenant_name": tenant_name,
                        "status": status,
                        "expires_at": expires_at,
                        "server_time": now_iso,
                        "max_devices": max_devices,
                        "active_devices": active_devices,
                        "offline_grace_hours": offline_grace_hours,
                        "profiles_sync_enabled": profiles_sync_enabled,
                        "seal_salt": seal_salt,
                    },
                },
            )
        if request.url.path == "/v1/isolation-policy":
            return httpx.Response(
                200,
                headers={"ETag": f'"{policy_sha[:16]}"'},
                json={
                    "version": policy_obj["policy_version"],
                    "sha256": policy_sha,
                    "policy": policy_obj,
                },
            )
        if request.url.path == "/v1/subscriptions":
            return httpx.Response(
                200,
                headers={"ETag": f'"{subs_sha[:16]}"'},
                json={
                    "version": "subs-sealed-1",
                    "sha256": subs_sha,
                    "subscriptions": subs_items,
                },
            )
        if request.url.path == "/v1/profiles":
            if not profiles_sync_enabled:
                return httpx.Response(
                    403,
                    json={
                        "code": "profiles_sync_disabled",
                        "message": "Profiles sync is disabled for this tenant.",
                    },
                )
            return httpx.Response(200, json={"profiles": [], "count": 0})
        return httpx.Response(404, json={"detail": "Not found"})

    return httpx.MockTransport(_handler)


# ---------------------------------------------------------------------------
# 1. HKDF-SHA256 salted encryption (enc2:)
# ---------------------------------------------------------------------------


def test_enc2_hkdf_encryption_roundtrip_and_salt_failures(secret_env, monkeypatch, tmp_path):
    plaintext = json.dumps({"server": UPSTREAM_SERVER, "password": UPSTREAM_PASSWORD})
    sealed = crypto.encrypt_with_salt(plaintext, "tenant-salt-alpha")
    assert sealed.startswith("enc2:")
    assert UPSTREAM_SERVER not in sealed
    assert UPSTREAM_PASSWORD not in sealed
    assert crypto.decrypt_with_salt(sealed, "tenant-salt-alpha") == plaintext

    # Legacy enc: remains untouched and compatible.
    legacy = crypto.encrypt("local-secret")
    assert legacy.startswith("enc:")
    assert crypto.decrypt(legacy) == "local-secret"

    # Wrong salt fails loudly.
    with pytest.raises(ValueError):
        crypto.decrypt_with_salt(sealed, "wrong-salt")

    # Empty salt fails loudly.
    with pytest.raises(ValueError):
        crypto.encrypt_with_salt(plaintext, "")
    with pytest.raises(ValueError):
        crypto.decrypt_with_salt(sealed, "   ")

    # Non-enc2 value fails loudly.
    with pytest.raises(ValueError):
        crypto.decrypt_with_salt(legacy, "tenant-salt-alpha")

    # Wrong CPM_SECRET_KEY fails loudly.
    monkeypatch.setenv("CPM_SECRET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    with pytest.raises(ValueError):
        crypto.decrypt_with_salt(sealed, "tenant-salt-alpha")

    # Missing CPM_SECRET_KEY fails loudly.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CPM_SECRET_KEY", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ValueError):
        crypto.encrypt_with_salt(plaintext, "tenant-salt-alpha")


# ---------------------------------------------------------------------------
# 2. CloudClient headers & structured 401/403 error parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_code, body, expected_code, expected_detail_key",
    [
        (401, {"code": "invalid_token", "message": "Token rejected"}, "invalid_token", None),
        (403, {"code": "tenant_disabled", "message": "Account disabled"}, "tenant_disabled", None),
        (
            403,
            {
                "code": "tenant_expired",
                "message": "Expired",
                "expires_at": "2026-01-01T00:00:00+00:00",
            },
            "tenant_expired",
            "expires_at",
        ),
        (403, {"code": "device_revoked", "message": "Device kicked"}, "device_revoked", None),
        (
            403,
            {
                "code": "device_limit_exceeded",
                "message": "Too many devices",
                "max_devices": 3,
                "active_devices": 3,
            },
            "device_limit_exceeded",
            "max_devices",
        ),
        (
            403,
            {
                "detail": {
                    "code": "profiles_sync_disabled",
                    "message": "Disabled for tenant",
                    "tenant_id": "t1",
                }
            },
            "profiles_sync_disabled",
            "tenant_id",
        ),
    ],
)
async def test_cloud_client_headers_and_structured_auth_errors(
    status_code, body, expected_code, expected_detail_key
):
    seen: list[dict[str, str]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return httpx.Response(status_code, json=body)

    client = CloudClient(
        "https://cloud.example.test",
        token="prism_tok_abc",
        device_id="dev-uuid-1234",
        device_label="测试工作站-01",
        client_version="0.6.0",
        transport=httpx.MockTransport(_handler),
    )
    with pytest.raises(CloudClientError) as exc_info:
        await client.get_manifest()

    assert exc_info.value.code == expected_code
    assert exc_info.value.status_code == status_code
    if expected_detail_key:
        assert expected_detail_key in exc_info.value.details
    assert len(seen) == 1
    assert seen[0]["x-prism-device-id"] == "dev-uuid-1234"
    assert seen[0]["x-prism-client-version"] == "0.6.0"
    assert seen[0]["x-prism-device-label"]


# ---------------------------------------------------------------------------
# 3. API redaction + DB zero-plaintext + 403 readonly enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cloud_api_redaction_and_403_readonly_enforcement(profile_manager, secret_env):
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    transport = _build_mock_cloud_transport(
        expires_at=future,
        extra_subscriptions=[
            {
                "cloud_source_id": "cloud-manual-static",
                "name": "Cloud Static Pack",
                "kind": "manual",
                "manual_nodes": ["socks5://clouduser:cloudpass@203.0.113.55:1080#Cloud-Static-01"],
                "auto_sync": True,
                "enabled": True,
            }
        ],
    )

    service = CloudDeliveryService(profile_manager, transport=transport)
    service.update_config(
        CloudConfigUpdate(base_url="https://cloud.example.test", token="prism_test_token_123")
    )
    report = await service.sync_all()
    assert report.ok is True
    assert report.subscriptions is not None
    assert report.subscriptions.created == 2

    # Also add a local manual node to verify local workflows stay 100% intact.
    catalog = NetworkCatalog(profile_manager)
    local_group = catalog.create_manual_group("My Local Static")
    local_added = catalog.add_manual_nodes(
        local_group["id"],
        [
            (
                1,
                {
                    "name": "Local-Static-01",
                    "type": "socks5",
                    "server": "198.51.100.88",
                    "port": 1080,
                    "username": "localu",
                    "password": "local-password-123",
                },
                None,
            )
        ],
    )
    local_node_id = local_added["created"][0]["id"]

    # Check raw SQLite rows: cloud nodes store entry='' and enc2: ciphertext.
    conn = profile_manager.storage.db._connection
    cloud_db_rows = conn.execute(
        "SELECT n.id, n.display_name, n.entry, n.config_enc, s.source_url_enc, s.source_host, s.source_file_name "
        "FROM network_nodes n JOIN network_subscriptions s ON s.id = n.subscription_id "
        "WHERE s.managed_by = 'cloud'"
    ).fetchall()
    assert len(cloud_db_rows) == 3
    for row in cloud_db_rows:
        assert row["entry"] == ""
        assert str(row["config_enc"]).startswith("enc2:")
        assert UPSTREAM_SERVER not in str(row["config_enc"])
        assert UPSTREAM_PASSWORD not in str(row["config_enc"])
        assert "203.0.113.55" not in str(row["config_enc"])
        assert row["source_url_enc"] is None
        assert row["source_host"] is None
        assert row["source_file_name"] is None

    # Runtime node_outbound transparently unseals enc2: with the active seal_salt.
    cloud_node_row = next(r for r in cloud_db_rows if r["display_name"] == "Cloud-US-01")
    public_summary, outbound = catalog.node_outbound(cloud_node_row["id"])
    assert public_summary["entry"] is None
    assert public_summary["protocol"] == "protected"
    assert public_summary["credentials_protected"] is True
    assert outbound["type"] == "trojan"
    assert outbound["server"] == UPSTREAM_SERVER
    assert outbound["password"] == UPSTREAM_PASSWORD

    # Without the active seal_salt (e.g. offline DB exfiltration), enc2: cannot be unsealed!
    saved_salt = get_active_seal_salt(conn)
    assert saved_salt == "tenant-salt-v1-xyz"
    clear_active_seal_salt(conn)
    with pytest.raises(NetworkSecretsUnavailable):
        catalog.node_outbound(cloud_node_row["id"])
    # Restore salt via entitlement cache for remaining API checks.
    save_entitlement(
        conn,
        CloudAccountEntitlement(
            tenant_id="tenant-alpha",
            tenant_name="Acme Customer",
            status="active",
            expires_at=future,
            seal_salt=saved_salt,
        ),
    )

    # Wire local FastAPI app to check REST API redaction & 403 read-only enforcement.
    dependencies.set_storage_manager(profile_manager.storage)
    dependencies.set_profile_manager(profile_manager)
    app = create_app(Settings(_env_file=None))
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://local") as api:
        subs_resp = await api.get("/api/v1/network/subscriptions")
        assert subs_resp.status_code == 200
        subs_by_name = {s["name"]: s for s in subs_resp.json()["data"]["subscriptions"]}

        vip_sub = subs_by_name["VIP Dedicated Pool"]
        cloud_static_sub = subs_by_name["Cloud Static Pack"]
        local_sub = subs_by_name["My Local Static"]

        assert vip_sub["managed_by"] == "cloud"
        assert vip_sub["source_display"] == "云端托管"
        assert vip_sub["source_file_name"] is None
        assert vip_sub["manual"] is False
        assert vip_sub["readonly"] is True

        assert cloud_static_sub["managed_by"] == "cloud"
        assert cloud_static_sub["manual"] is False
        assert cloud_static_sub["readonly"] is True

        assert local_sub["managed_by"] == "local"
        assert local_sub["manual"] is True
        assert local_sub["readonly"] is False

        nodes_resp = await api.get("/api/v1/network/nodes")
        assert nodes_resp.status_code == 200
        raw_text = nodes_resp.text
        for leaked in (
            UPSTREAM_SERVER,
            UPSTREAM_PASSWORD,
            UPSTREAM_SNI,
            "jp-hidden.upstream.example",
            "203.0.113.55",
            "cloudpass",
        ):
            assert leaked not in raw_text

        nodes_by_name = {n["name"]: n for n in nodes_resp.json()["data"]["nodes"]}
        cloud_us = nodes_by_name["Cloud-US-01"]
        cloud_static_node = nodes_by_name["Cloud-Static-01"]
        local_node = nodes_by_name["Local-Static-01"]

        for cn in (cloud_us, cloud_static_node):
            assert cn["entry"] is None
            assert cn["protocol"] == "protected"
            assert cn["credentials_protected"] is True
            assert cn["editable"] is False
            assert cn["deletable"] is False

        assert local_node["entry"] == "198.51.100.88:1080"
        assert local_node["protocol"] == "socks5"
        assert local_node["credentials_protected"] is False
        assert local_node["editable"] is True
        assert local_node["deletable"] is True

        # Every mutation on cloud subscriptions and cloud nodes returns 403 cloud_managed_readonly.
        r_rename = await api.patch(
            f"/api/v1/network/subscriptions/{vip_sub['id']}",
            json={"name": "Hacked Name", "row_version": vip_sub["row_version"]},
        )
        assert r_rename.status_code == 403
        assert r_rename.json()["error"]["code"] == "cloud_managed_readonly"

        r_delete = await api.delete(f"/api/v1/network/subscriptions/{vip_sub['id']}")
        assert r_delete.status_code == 403
        assert r_delete.json()["error"]["code"] == "cloud_managed_readonly"

        r_sync = await api.post(f"/api/v1/network/subscriptions/{vip_sub['id']}/sync", json={})
        assert r_sync.status_code == 403
        assert r_sync.json()["error"]["code"] == "cloud_managed_readonly"

        r_add_manual = await api.post(
            "/api/v1/network/manual-nodes",
            json={
                "group_id": cloud_static_sub["id"],
                "text": "socks5://1.2.3.4:1080",
            },
        )
        assert r_add_manual.status_code == 403
        assert r_add_manual.json()["error"]["code"] == "cloud_managed_readonly"

        r_edit_node = await api.patch(
            f"/api/v1/network/nodes/{cloud_static_node['id']}",
            json={"name": "Edited Cloud Node"},
        )
        assert r_edit_node.status_code == 403
        assert r_edit_node.json()["error"]["code"] == "cloud_managed_readonly"

        r_del_node = await api.delete(f"/api/v1/network/nodes/{cloud_static_node['id']}")
        assert r_del_node.status_code == 403
        assert r_del_node.json()["error"]["code"] == "cloud_managed_readonly"

        # Local node edits continue working normally.
        r_edit_local = await api.patch(
            f"/api/v1/network/nodes/{local_node_id}",
            json={"name": "Local-Static-Renamed"},
        )
        assert r_edit_local.status_code == 200
        assert r_edit_local.json()["data"]["name"] == "Local-Static-Renamed"

    with pytest.raises(CloudManagedReadOnly):
        catalog.delete_subscription(vip_sub["id"])


# ---------------------------------------------------------------------------
# 4. Mihomo config.yaml shredding, owner.json sanitization & full log redaction
# ---------------------------------------------------------------------------


def _make_fake_mihomo_executable(directory: Path) -> Path:
    """Create a tiny offline fake Mihomo script that binds its listener port and logs secrets."""
    script = directory / "fake_mihomo.py"
    script.write_text(
        """
import json, signal, socket, sys, time
if "-v" in sys.argv:
    print("Mihomo Meta v1.19.5 test")
    sys.exit(0)
cfg_path = sys.argv[sys.argv.index("-f") + 1]
with open(cfg_path, "r", encoding="utf-8") as fh:
    cfg = json.load(fh)
socks = []
for lst in cfg.get("listeners", []):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((lst["listen"], lst["port"]))
    s.listen(4)
    socks.append(s)
proxy = cfg["proxies"][0]
print(
    f"dial error server={proxy['server']} sni={proxy.get('sni')} "
    f"servername={proxy.get('servername')} password={proxy.get('password')} "
    f"uuid={proxy.get('uuid')} host={proxy.get('ws-opts', {}).get('headers', {}).get('Host')} "
    f"path={proxy.get('ws-opts', {}).get('path')} pub={proxy.get('reality-opts', {}).get('public-key')} "
    f"sid={proxy.get('reality-opts', {}).get('short-id')} svc={proxy.get('grpc-opts', {}).get('service-name')}",
    flush=True,
)
def _stop(_sig, _frame):
    for s in socks:
        s.close()
    sys.exit(0)
signal.signal(signal.SIGTERM, _stop)
while True:
    time.sleep(0.2)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    wrapper = directory / "mihomo"
    wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} {script} "$@"\n', encoding="utf-8")
    wrapper.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    return wrapper


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell wrapper fake mihomo")
@pytest.mark.asyncio
async def test_mihomo_shreds_config_redacts_upstream_fields_and_sanitizes_owner(tmp_path):
    bin_path = _make_fake_mihomo_executable(tmp_path)
    binary = validate_binary(bin_path)

    outbound = {
        "type": "vless",
        "server": UPSTREAM_SERVER,
        "port": 443,
        "uuid": UPSTREAM_UUID,
        "password": UPSTREAM_PASSWORD,
        "sni": UPSTREAM_SNI,
        "servername": UPSTREAM_SNI,
        "ws-opts": {
            "path": UPSTREAM_PATH,
            "headers": {"Host": UPSTREAM_HOST},
        },
        "reality-opts": {
            "public-key": UPSTREAM_PUBKEY,
            "short-id": UPSTREAM_SHORTID,
        },
        "grpc-opts": {
            "service-name": UPSTREAM_SVC,
        },
    }
    runtime_root = tmp_path / "network-runtime"
    proc = MihomoProcess(
        binary,
        {"node-0": outbound},
        ["node-0"],
        runtime_root,
        purpose="profile-test",
    )
    await proc.start()
    try:
        assert proc.workdir is not None
        # config.yaml MUST be shredded & removed immediately after _wait_ready()!
        assert not (proc.workdir / "config.yaml").exists()

        # owner.json MUST omit config path and only keep PID/start/workdir metadata.
        owner_data = json.loads((proc.workdir / "owner.json").read_text(encoding="utf-8"))
        assert set(owner_data.keys()) == {
            "manager_pid",
            "manager_started",
            "pid",
            "pid_started",
            "workdir",
        }
        assert "config" not in owner_data
        assert owner_data["workdir"] == str(proc.workdir)

        # Wait briefly for reader task to consume stdout line and verify full redaction.
        for _ in range(40):
            if proc._log:
                break
            await asyncio.sleep(0.05)
        joined_log = "\n".join(proc._log)
        assert "[redacted]" in joined_log
        for sensitive in (
            UPSTREAM_SERVER,
            UPSTREAM_SNI,
            UPSTREAM_HOST,
            UPSTREAM_PASSWORD,
            UPSTREAM_UUID,
            UPSTREAM_PUBKEY,
            UPSTREAM_SHORTID,
            UPSTREAM_PATH,
            UPSTREAM_SVC,
        ):
            assert sensitive not in joined_log

        # Restart re-writes transiently from self.outbounds and shreds config.yaml again!
        assert proc.process is not None
        proc.process.terminate()
        await proc.process.wait()
        await proc.restart()
        assert proc.alive is True
        assert not (proc.workdir / "config.yaml").exists()
    finally:
        await proc.close()


def test_reap_orphans_works_with_workdir_only_owner_json(tmp_path):
    root = tmp_path / "network-runtime"
    orphan_dir = root / "profile-orphan"
    orphan_dir.mkdir(parents=True)

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_started = psutil.Process(dead.pid).create_time()
    dead.wait()

    # Simulated mihomo command line containing `-d <workdir>` (without config.yaml in owner.json).
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "-d", str(orphan_dir)]
    )
    try:
        (orphan_dir / "owner.json").write_text(
            json.dumps(
                {
                    "manager_pid": dead.pid,
                    "manager_started": dead_started,
                    "pid": child.pid,
                    "pid_started": psutil.Process(child.pid).create_time(),
                    "workdir": str(orphan_dir),
                }
            ),
            encoding="utf-8",
        )
        assert reap_orphans(root) == 1
        child.wait(5)
        assert not orphan_dir.exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


# ---------------------------------------------------------------------------
# 5. Entitlement expiry / clock-rollback / offline-grace & selective revocation
# ---------------------------------------------------------------------------


def test_entitlement_rules_expiry_expiring_soon_clock_rollback_and_offline_grace(
    profile_manager, secret_env
):
    conn = profile_manager.storage.db._connection
    base_now = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)

    # Legacy mode (no entitlement record) is allowed and reports status='none'.
    legacy = evaluate_entitlement(conn, now_utc=base_now)
    assert legacy.allowed is True
    assert legacy.status == "none"
    assert legacy.legacy_mode is True

    # Active license (30 days remaining).
    save_entitlement(
        conn,
        CloudAccountEntitlement(
            tenant_id="t1",
            tenant_name="Alpha",
            status="active",
            expires_at=(base_now + timedelta(days=30)).isoformat(),
            server_time=base_now.isoformat(),
            max_devices=3,
            active_devices=1,
            offline_grace_hours=24,
            profiles_sync_enabled=False,
            seal_salt="salt-1",
        ),
        now_utc=base_now,
    )
    res_active = check_cloud_entitlement(conn, now_utc=base_now)
    assert res_active.status == "active"
    assert res_active.days_remaining == 30


def test_entitlement_time_guards_exact_boundaries(profile_manager, secret_env):
    conn = profile_manager.storage.db._connection
    sync_time = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)

    # 1) Expiring soon (5 days remaining, synced now).
    save_entitlement(
        conn,
        CloudAccountEntitlement(
            tenant_id="t1",
            tenant_name="Alpha",
            status="active",
            expires_at=(sync_time + timedelta(days=5)).isoformat(),
            server_time=sync_time.isoformat(),
            offline_grace_hours=24,
            seal_salt="salt-1",
        ),
        now_utc=sync_time,
    )
    soon = check_cloud_entitlement(conn, now_utc=sync_time + timedelta(hours=1))
    assert soon.allowed is True
    assert soon.status == "expiring_soon"
    assert soon.days_remaining == 5

    # 2) Clock rollback (> 5 minutes backwards).
    with pytest.raises(CloudEntitlementError) as exc_clock:
        check_cloud_entitlement(conn, now_utc=sync_time - timedelta(minutes=10))
    assert exc_clock.value.code == "clock_tampered_reverify_required"

    # Mild skew (2 minutes backwards) is tolerated.
    assert check_cloud_entitlement(conn, now_utc=sync_time - timedelta(minutes=2)).allowed is True

    # 3) Offline grace exceeded (> 24 hours since last sync).
    with pytest.raises(CloudEntitlementError) as exc_offline:
        check_cloud_entitlement(conn, now_utc=sync_time + timedelta(hours=25))
    assert exc_offline.value.code == "offline_grace_exceeded"

    # 4) Expired (expires_at passed).
    save_entitlement(
        conn,
        CloudAccountEntitlement(
            tenant_id="t1",
            tenant_name="Alpha",
            status="active",
            expires_at=(sync_time + timedelta(hours=2)).isoformat(),
            server_time=sync_time.isoformat(),
            offline_grace_hours=24,
            seal_salt="salt-1",
        ),
        now_utc=sync_time,
    )
    with pytest.raises(CloudEntitlementError) as exc_exp:
        check_cloud_entitlement(conn, now_utc=sync_time + timedelta(hours=3))
    assert exc_exp.value.code == "cloud_entitlement_expired"


@pytest.mark.asyncio
async def test_entitlement_blocks_cloud_adapter_and_probe_while_local_works(
    profile_manager, secret_env, monkeypatch
):
    future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    transport = _build_mock_cloud_transport(expires_at=future)
    service = CloudDeliveryService(profile_manager, transport=transport)
    service.update_config(
        CloudConfigUpdate(base_url="https://cloud.example.test", token="prism_test_token_123")
    )
    assert (await service.sync_all()).ok is True

    catalog = NetworkCatalog(profile_manager)
    cloud_node = next(n for n in catalog.list_nodes()["nodes"] if n["name"] == "Cloud-US-01")

    local_group = catalog.create_manual_group("Local Group")
    local_node = catalog.add_manual_nodes(
        local_group["id"],
        [
            (
                1,
                {
                    "name": "Local-Node-01",
                    "type": "socks5",
                    "server": "198.51.100.10",
                    "port": 1080,
                },
                None,
            )
        ],
    )["created"][0]

    cloud_profile = await profile_manager.create_profile(
        "Cloud Profile", generate_fingerprint=False
    )
    local_profile = await profile_manager.create_profile(
        "Local Profile", generate_fingerprint=False
    )
    bindings = NetworkBindings(profile_manager)
    await bindings.bind(cloud_profile.id, cloud_node["id"], binding_row_version=None)
    await bindings.bind(local_profile.id, local_node["id"], binding_row_version=None)

    # Expire the entitlement locally!
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    save_entitlement(
        profile_manager.storage.db._connection,
        CloudAccountEntitlement(
            tenant_id="tenant-alpha",
            tenant_name="Acme Customer",
            status="active",
            expires_at=past,
            offline_grace_hours=24,
            seal_salt="tenant-salt-v1-xyz",
        ),
    )

    # 1) NetworkAdapter.prepare rejects cloud profile with 403 cloud_entitlement_expired.
    with pytest.raises(NetworkLaunchError) as exc_prep:
        await profile_manager.network.prepare(cloud_profile.id)
    assert exc_prep.value.status == 403
    assert exc_prep.value.code == "cloud_entitlement_expired"

    # 2) ProfileNetwork.reconnect rejects cloud runtime with 403 cloud_entitlement_expired.
    class _StubEngine:
        listeners = [type("L", (), {"port": 19999, "username": "u", "password": "p"})()]
        alive = False

        def on_exit(self, _cb):
            pass

        async def restart(self):
            raise AssertionError("restart should not be called when entitlement is expired")

        async def close(self):
            pass

    cloud_runtime = ProfileNetwork(
        profile_manager.network,
        cloud_profile.id,
        cloud_node,
        _StubEngine(),  # type: ignore[arg-type]
    )
    with pytest.raises(NetworkLaunchError) as exc_reconn:
        await cloud_runtime.reconnect()
    assert exc_reconn.value.status == 403
    assert exc_reconn.value.code == "cloud_entitlement_expired"

    # 3) NetworkProbe.check_node, probe_risk_domains, and start_batch reject cloud node.
    with pytest.raises(NetworkLaunchError) as exc_check:
        await profile_manager.network_probe.check_node(cloud_node["id"])
    assert exc_check.value.status == 403
    assert exc_check.value.code == "cloud_entitlement_expired"

    with pytest.raises(NetworkLaunchError) as exc_risk:
        await profile_manager.network_probe.probe_risk_domains(cloud_node["id"])
    assert exc_risk.value.status == 403
    assert exc_risk.value.code == "cloud_entitlement_expired"

    with pytest.raises(NetworkLaunchError) as exc_batch:
        profile_manager.network_probe.start_batch([cloud_node["id"]])
    assert exc_batch.value.status == 403
    assert exc_batch.value.code == "cloud_entitlement_expired"

    # 4) Local profile & local node bypass entitlement guard completely!
    from camoufox_pm.core import mihomo_manager, network_adapter

    def _sentinel_binary(_configured, _data_dir):
        raise mihomo_manager.MihomoUnavailable("reached_local_engine", "passed guard")

    monkeypatch.setattr(network_adapter, "discover_binary", _sentinel_binary)
    with pytest.raises(NetworkLaunchError) as exc_local:
        await profile_manager.network.prepare(local_profile.id)
    assert exc_local.value.code == "reached_local_engine"


@pytest.mark.asyncio
async def test_server_revocation_closes_cloud_sessions_wipes_creds_and_preserves_local(
    profile_manager, secret_env
):
    future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    service = CloudDeliveryService(
        profile_manager,
        transport=_build_mock_cloud_transport(expires_at=future),
    )
    service.update_config(
        CloudConfigUpdate(base_url="https://cloud.example.test", token="prism_test_token_123")
    )
    assert (await service.sync_all()).ok is True

    catalog = NetworkCatalog(profile_manager)
    cloud_us = next(n for n in catalog.list_nodes()["nodes"] if n["name"] == "Cloud-US-01")
    cloud_jp = next(n for n in catalog.list_nodes()["nodes"] if n["name"] == "Cloud-JP-02")

    # Add a local subscription & node.
    local_group = catalog.create_manual_group("My Local Group")
    local_node = catalog.add_manual_nodes(
        local_group["id"],
        [
            (
                1,
                {
                    "name": "Local-Keep-01",
                    "type": "socks5",
                    "server": "198.51.100.77",
                    "port": 1080,
                    "password": "keep-local-password",
                },
                None,
            )
        ],
    )["created"][0]

    p_cloud = await profile_manager.create_profile("Cloud Browser", generate_fingerprint=False)
    p_local = await profile_manager.create_profile("Local Browser", generate_fingerprint=False)
    bindings = NetworkBindings(profile_manager)
    await bindings.bind(p_cloud.id, cloud_us["id"], binding_row_version=None)
    await bindings.bind(p_local.id, local_node["id"], binding_row_version=None)

    class _RecordingRuntime:
        def __init__(self, pid: str, node: dict[str, Any]):
            self.profile_id = pid
            self.node = node
            self.closed = False

        def mark_running(self):
            pass

        async def close(self):
            self.closed = True
            profile_manager.network.runtimes.pop(self.profile_id, None)

    cloud_rt = _RecordingRuntime(p_cloud.id, cloud_us)
    local_rt = _RecordingRuntime(p_local.id, local_node)
    profile_manager.network.runtimes[p_cloud.id] = cloud_rt  # type: ignore[assignment]
    profile_manager.network.runtimes[p_local.id] = local_rt  # type: ignore[assignment]
    profile_manager.browser_sessions.register_session(p_cloud.id, network=cloud_rt)
    profile_manager.browser_sessions.register_session(p_local.id, network=local_rt)

    # Server returns 403 tenant_expired on next sync!
    expired_service = CloudDeliveryService(
        profile_manager,
        transport=_build_mock_cloud_transport(
            error_response=(
                403,
                {
                    "code": "tenant_expired",
                    "message": "Your subscription has expired.",
                    "expires_at": "2026-11-20T00:00:00+00:00",
                },
            )
        ),
    )
    with pytest.raises(CloudSyncError) as exc_sync:
        await expired_service.sync_all(force=True)
    assert exc_sync.value.code == "tenant_expired"
    assert exc_sync.value.status_code == 403

    # Cloud session & runtime were closed; local session & runtime keep running!
    assert cloud_rt.closed is True
    assert not profile_manager.browser_sessions.is_running(p_cloud.id)
    assert local_rt.closed is False
    assert profile_manager.browser_sessions.is_running(p_local.id)

    conn = profile_manager.storage.db._connection
    # Unbound cloud node (Cloud-JP-02) was deleted.
    assert (
        conn.execute("SELECT 1 FROM network_nodes WHERE id = ?", (cloud_jp["id"],)).fetchone()
        is None
    )
    # Bound cloud node (Cloud-US-01) was retired AND credentials wiped!
    bound_row = conn.execute(
        "SELECT status, config_enc, entry FROM network_nodes WHERE id = ?",
        (cloud_us["id"],),
    ).fetchone()
    assert bound_row["status"] == "retired"
    assert bound_row["config_enc"] == "revoked"
    assert bound_row["entry"] == ""
    # Subscription cache & seal_salt cleared.
    assert conn.execute("SELECT 1 FROM cloud_cache WHERE kind = 'subscriptions'").fetchone() is None
    assert get_active_seal_salt(conn) is None
    # Status reflects expired entitlement.
    assert expired_service.get_status().entitlement_status == "expired"

    # Local node & its decrypted credentials are 100% intact!
    _, local_outbound = catalog.node_outbound(local_node["id"])
    assert local_outbound["password"] == "keep-local-password"

    # Renewal: once admin extends expiry, re-sync restores Cloud-US-01 in place with same node ID!
    renewed_service = CloudDeliveryService(
        profile_manager,
        transport=_build_mock_cloud_transport(
            expires_at=future,
            seal_salt="tenant-salt-v2-rotated",
        ),
    )
    renewed_report = await renewed_service.sync_all(force=True)
    assert renewed_report.ok is True
    restored_row = conn.execute(
        "SELECT status, config_enc FROM network_nodes WHERE id = ?",
        (cloud_us["id"],),
    ).fetchone()
    assert restored_row["status"] == "active"
    assert str(restored_row["config_enc"]).startswith("enc2:")
    _, restored_outbound = catalog.node_outbound(cloud_us["id"])
    assert restored_outbound["password"] == UPSTREAM_PASSWORD

    # Cleanup local session cleanly.
    await profile_manager.browser_sessions.close_all()


# ---------------------------------------------------------------------------
# 6. Customer edition: /docs disabled, base_url locked/masked, /activate & /logout, /system/status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_customer_edition_docs_disabled_url_locked_activate_and_logout(
    profile_manager, secret_env, monkeypatch
):
    monkeypatch.setenv("CPM_CLIENT_EDITION", "customer")
    monkeypatch.setenv("CPM_CLOUD_URL", "https://locked-cloud.example.test")
    monkeypatch.setenv("CPM_BRAND_NAME", "极光专享浏览器")
    monkeypatch.setenv("CPM_REQUIRE_ACTIVATION", "1")
    get_settings.cache_clear()

    dependencies.set_storage_manager(profile_manager.storage)
    dependencies.set_profile_manager(profile_manager)

    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    seen_headers: list[dict[str, str]] = []
    mock_transport = _build_mock_cloud_transport(
        expected_token="prism_customer_key_001",
        tenant_name="VIP Customer Co",
        expires_at=future,
        max_devices=3,
        active_devices=1,
        profiles_sync_enabled=False,
        captured_headers=seen_headers,
    )
    cloud_routes.set_cloud_service_factory(
        lambda mgr: CloudDeliveryService(mgr, transport=mock_transport)
    )

    # Create a local node to verify logout never touches local nodes.
    catalog = NetworkCatalog(profile_manager)
    local_grp = catalog.create_manual_group("Customer Own Proxy")
    local_node = catalog.add_manual_nodes(
        local_grp["id"],
        [(1, {"name": "Own-IP", "type": "socks5", "server": "198.51.100.2", "port": 1080}, None)],
    )["created"][0]

    customer_app = create_app(get_settings())
    standard_app = create_app(Settings(_env_file=None, client_edition="standard"))

    try:
        # Standard edition exposes /docs; customer edition disables /docs, /redoc, /openapi.json.
        async with httpx.AsyncClient(
            transport=ASGITransport(app=standard_app), base_url="http://std"
        ) as std_api:
            assert (await std_api.get("/docs")).status_code == 200

        async with httpx.AsyncClient(
            transport=ASGITransport(app=customer_app), base_url="http://cust"
        ) as api:
            assert (await api.get("/docs")).status_code == 404
            assert (await api.get("/redoc")).status_code == 404
            assert (await api.get("/openapi.json")).status_code == 404

            # Initial cloud status hides base_url and reports customer edition metadata.
            st0 = (await api.get("/api/v1/cloud/status")).json()["data"]
            assert st0["edition"] == "customer"
            assert st0["brand_name"] == "极光专享浏览器"
            assert st0["cloud_url_locked"] is True
            assert st0["require_activation"] is True
            assert st0["configured"] is True
            assert st0["base_url"] is None
            assert st0["has_token"] is False
            assert st0["entitlement_status"] == "none"
            assert st0["device_id"]
            assert "locked-cloud.example.test" not in json.dumps(st0)

            # Attempting to override base_url or allow_local fails with 403 cloud_url_locked.
            r_url = await api.put(
                "/api/v1/cloud/config",
                json={"base_url": "https://evil.example.com"},
            )
            assert r_url.status_code == 403
            assert r_url.json()["error"]["code"] == "cloud_url_locked"

            r_local = await api.put(
                "/api/v1/cloud/config",
                json={"allow_local": True},
            )
            assert r_local.status_code == 403
            assert r_local.json()["error"]["code"] == "cloud_url_locked"

            # Activate with invalid token -> 401 invalid_token and state stays cleanly unactivated!
            r_bad = await api.post(
                "/api/v1/cloud/activate",
                json={"token": "prism_wrong_token"},
            )
            assert r_bad.status_code == 401
            assert r_bad.json()["error"]["code"] == "invalid_token"
            st_bad = (await api.get("/api/v1/cloud/status")).json()["data"]
            assert st_bad["has_token"] is False
            assert st_bad["entitlement_status"] == "none"

            # Activate with valid customer token -> syncs sealed_nodes & entitlement!
            r_act = await api.post(
                "/api/v1/cloud/activate",
                json={"token": "prism_customer_key_001"},
            )
            assert r_act.status_code == 200, r_act.text
            act_data = r_act.json()["data"]
            assert act_data["report"]["ok"] is True
            assert act_data["status"]["has_token"] is True
            assert act_data["status"]["base_url"] is None
            assert act_data["status"]["tenant_name"] == "VIP Customer Co"
            assert act_data["status"]["entitlement_status"] == "active"
            assert act_data["status"]["days_remaining"] == 30
            assert act_data["status"]["max_devices"] == 3
            assert act_data["status"]["active_devices"] == 1
            assert act_data["status"]["profiles_sync_allowed"] is False
            assert act_data["status"]["cloud_subscription_count"] == 1
            # Device headers were sent to cloud server.
            assert any("x-prism-device-id" in h for h in seen_headers)

            # System status reflects customer edition & live entitlement!
            sys_resp = await api.get("/api/v1/system/status")
            assert sys_resp.status_code == 200, sys_resp.text
            sys_body = sys_resp.json()
            assert sys_body["client_edition"] == "customer"
            assert sys_body["brand_name"] == "极光专享浏览器"
            assert sys_body["require_activation"] is True
            assert sys_body["entitlement_status"] == "active"
            assert sys_body["entitlement_tenant_name"] == "VIP Customer Co"
            assert sys_body["entitlement_expires_at"] == future
            assert sys_body["entitlement_days_remaining"] == 30

            # Logout clears token, revokes cloud nodes, resets entitlement, keeps local nodes!
            r_out = await api.post("/api/v1/cloud/logout", json={})
            assert r_out.status_code == 200, r_out.text
            out_status = r_out.json()["data"]
            assert out_status["has_token"] is False
            assert out_status["entitlement_status"] == "none"
            assert out_status["tenant_name"] is None
            assert get_active_seal_salt(profile_manager.storage.db._connection) is None

            # Cloud nodes wiped; local node untouched!
            all_nodes = catalog.list_nodes()["nodes"]
            assert [n["id"] for n in all_nodes] == [local_node["id"]]
    finally:
        cloud_routes.set_cloud_service_factory(None)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_live_multitenant_cloud_server_sealed_nodes_and_disable_revocation(
    profile_manager, secret_env, tmp_path
):
    """End-to-end check against the real multi-tenant ``cloud_server`` FastAPI app."""
    from cloud_server.app import create_app as create_cloud_app

    admin_token = "live-e2e-admin-token"
    cloud_app = create_cloud_app(
        db_path=tmp_path / "live_mt_cloud.db",
        admin_token=admin_token,
        secret_key=Fernet.generate_key().decode(),
    )
    cloud_transport = ASGITransport(app=cloud_app)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()

    async with httpx.AsyncClient(
        transport=cloud_transport, base_url="https://cloud.example.test"
    ) as admin:
        t_resp = await admin.post(
            "/v1/admin/tenants",
            headers=admin_headers,
            json={
                "name": "Live Tenant A",
                "expires_at": future,
                "max_devices": 2,
                "offline_grace_hours": 24,
                "profiles_sync_enabled": False,
            },
        )
        assert t_resp.status_code == 201, t_resp.text
        tenant_data = t_resp.json()
        tenant_id = tenant_data["tenant"]["id"]
        tenant_token = tenant_data["token"]

        priv_resp = await admin.post(
            f"/v1/admin/tenants/{tenant_id}/private-nodes",
            headers=admin_headers,
            json={
                "name": "Tenant A Private Pool",
                "nodes_text": f"socks5://privuser:{UPSTREAM_PASSWORD}@203.0.113.99:1080#Private-US-01",
            },
        )
        assert priv_resp.status_code in {200, 201}, priv_resp.text

    # Customer client activates against real cloud_server!
    service = CloudDeliveryService(profile_manager, transport=cloud_transport)
    service.update_config(CloudConfigUpdate(base_url="https://cloud.example.test"))
    act = await service.activate(tenant_token)
    assert act["report"]["ok"] is True
    assert act["status"]["entitlement_status"] == "active"
    assert act["status"]["tenant_name"] == "Live Tenant A"
    assert act["status"]["max_devices"] == 2
    assert act["status"]["active_devices"] == 1
    assert act["status"]["profiles_sync_allowed"] is False

    catalog = NetworkCatalog(profile_manager)
    nodes = catalog.list_nodes()["nodes"]
    assert len(nodes) == 1
    node = nodes[0]
    assert node["name"] == "Private-US-01"
    assert node["entry"] is None
    assert node["protocol"] == "protected"
    assert node["credentials_protected"] is True

    # Verify DB stores enc2: and empty entry!
    conn = profile_manager.storage.db._connection
    raw_row = conn.execute(
        "SELECT entry, config_enc FROM network_nodes WHERE id = ?", (node["id"],)
    ).fetchone()
    assert raw_row["entry"] == ""
    assert str(raw_row["config_enc"]).startswith("enc2:")

    # Bind a profile, then admin disables the tenant on cloud_server.
    prof = await profile_manager.create_profile("Live Cloud Browser", generate_fingerprint=False)
    await NetworkBindings(profile_manager).bind(prof.id, node["id"], binding_row_version=None)

    async with httpx.AsyncClient(
        transport=cloud_transport, base_url="https://cloud.example.test"
    ) as admin:
        dis_resp = await admin.patch(
            f"/v1/admin/tenants/{tenant_id}",
            headers=admin_headers,
            json={"status": "disabled"},
        )
        assert dis_resp.status_code == 200, dis_resp.text

    # Next client sync triggers real 403 tenant_disabled and wipes local credentials!
    with pytest.raises(CloudSyncError) as exc_dis:
        await service.sync_all(force=True)
    assert exc_dis.value.code == "tenant_disabled"
    assert service.get_status().entitlement_status == "disabled"
    wiped_row = conn.execute(
        "SELECT status, config_enc, entry FROM network_nodes WHERE id = ?", (node["id"],)
    ).fetchone()
    assert wiped_row["status"] == "retired"
    assert wiped_row["config_enc"] == "revoked"
    assert wiped_row["entry"] == ""
    assert get_active_seal_salt(conn) is None

    # Admin re-enables tenant -> client sync restores the node seamlessly!
    async with httpx.AsyncClient(
        transport=cloud_transport, base_url="https://cloud.example.test"
    ) as admin:
        en_resp = await admin.patch(
            f"/v1/admin/tenants/{tenant_id}",
            headers=admin_headers,
            json={"status": "active"},
        )
        assert en_resp.status_code == 200, en_resp.text

    restored_report = await service.sync_all(force=True)
    assert restored_report.ok is True
    assert service.get_status().entitlement_status == "active"
    _, restored_outbound = catalog.node_outbound(node["id"])
    assert restored_outbound["password"] == UPSTREAM_PASSWORD

    # Regression #1: A subsequent non-forced sync hitting 304 Not Modified on /v1/manifest
    # MUST NOT call GET /v1/profiles when tenant.profiles_sync_enabled is False!
    second_sync = await service.sync_all(force=False)
    assert second_sync.ok is True
    assert second_sync.errors == []
    assert second_sync.profiles is None
    assert "profiles" not in second_sync.scopes
    st_after_304 = service.get_status()
    assert st_after_304.connected is True
    assert st_after_304.last_sync_ok is True
    assert st_after_304.last_error is None


@pytest.mark.asyncio
async def test_activate_typo_preserves_existing_active_session_and_nodes(
    profile_manager, secret_env
):
    """Regression #2: A bad candidate token during re-activation must not revoke active nodes."""
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    transport = _build_mock_cloud_transport(
        expected_token="prism_valid_active_token",
        expires_at=future,
        profiles_sync_enabled=False,
    )
    service = CloudDeliveryService(profile_manager, transport=transport)
    service.update_config(CloudConfigUpdate(base_url="https://cloud.example.test"))

    # Activate cleanly with the valid token first.
    act = await service.activate("prism_valid_active_token")
    assert act["report"]["ok"] is True
    assert act["status"]["entitlement_status"] == "active"

    catalog = NetworkCatalog(profile_manager)
    cloud_node = next(n for n in catalog.list_nodes()["nodes"] if n["name"] == "Cloud-US-01")

    # Attempt to replace with a typo token -> 401 invalid_token, existing active state untouched!
    with pytest.raises(CloudSyncError) as exc_typo:
        await service.activate("prism_typo_token")
    assert exc_typo.value.code == "invalid_token"
    assert exc_typo.value.status_code == 401

    st_after = service.get_status()
    assert st_after.has_token is True
    assert st_after.entitlement_status == "active"
    assert st_after.tenant_name == "Acme Customer"
    assert get_active_seal_salt(profile_manager.storage.db._connection) == "tenant-salt-v1-xyz"
    _, outbound = catalog.node_outbound(cloud_node["id"])
    assert outbound["password"] == UPSTREAM_PASSWORD


@pytest.mark.asyncio
async def test_customer_edition_startup_and_heartbeat_skip_when_unactivated(
    profile_manager, secret_env, monkeypatch
):
    """Regression #3: Unactivated customer installs must not fire 401 syncs on startup or heartbeat."""
    from camoufox_pm.main import _cloud_sync_heartbeat_loop, _startup_cloud_sync

    monkeypatch.setenv("CPM_CLIENT_EDITION", "customer")
    monkeypatch.setenv("CPM_CLOUD_URL", "https://locked-cloud.example.test")
    get_settings.cache_clear()

    sync_calls: list[bool] = []

    async def _recording_sync_all(self, *args, **kwargs):
        sync_calls.append(self.get_status().has_token)
        return None

    monkeypatch.setattr(CloudDeliveryService, "sync_all", _recording_sync_all)

    # Unactivated (has_token=False): startup sync, maybe_sync_on_startup, and heartbeat ALL skip.
    await _startup_cloud_sync(profile_manager)
    assert await CloudDeliveryService(profile_manager).maybe_sync_on_startup() is None

    task = asyncio.create_task(_cloud_sync_heartbeat_loop(profile_manager, interval_seconds=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sync_calls == []

    # Once a token is saved (has_token=True): startup and heartbeat DO run sync_all!
    CloudDeliveryService(profile_manager).update_config(
        CloudConfigUpdate(token="prism_activated_token")
    )
    await _startup_cloud_sync(profile_manager)
    assert sync_calls == [True]

    task2 = asyncio.create_task(_cloud_sync_heartbeat_loop(profile_manager, interval_seconds=0.01))
    await asyncio.sleep(0.05)
    task2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task2
    assert len(sync_calls) >= 2
    assert all(sync_calls)
