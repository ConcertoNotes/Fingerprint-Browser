"""Comprehensive unit and API tests for multi-tenant cloud_server, resolver, and /admin panel."""

from __future__ import annotations

import os
import re
import sqlite3
import stat
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cloud_server.app import create_app as create_cloud_app
from cloud_server.resolver import filter_nodes, normalize_node_filter, resolve_source
from cloud_server.store import CloudStore, canonical_json, canonical_sha256
from cryptography.fernet import Fernet
from httpx import ASGITransport

from camoufox_pm.core.network_subscription import FetchedSubscription, validate_node

ADMIN_TOKEN = "super-secret-admin-token-42"

UPSTREAM_AIRPORT_YAML = """
proxies:
  - {name: "剩余流量：888 GB", type: ss, server: info.airport.example, port: 443, cipher: aes-128-gcm, password: "x"}
  - {name: "套餐到期：2027-01-01", type: ss, server: expire.airport.example, port: 443, cipher: aes-128-gcm, password: "x"}
  - {name: "US-LosAngeles-01", type: trojan, server: us-la-1.upstream.example, port: 443, password: "pw-us-1"}
  - {name: "US-NewYork-02", type: trojan, server: us-ny-2.upstream.example, port: 443, password: "pw-us-2"}
  - {name: "JP-Tokyo-01", type: ss, server: jp-tyo-1.upstream.example, port: 8388, cipher: aes-128-gcm, password: "pw-jp-1"}
  - {name: "JP-Beta-02", type: ss, server: jp-beta-2.upstream.example, port: 8388, cipher: aes-128-gcm, password: "pw-jp-2"}
  - {name: "SG-Singapore-01", type: vless, server: sg-1.upstream.example, port: 443, uuid: "11111111-1111-1111-1111-111111111111"}
"""

UPSTREAM_URL = (
    "https://secret-airport.example.com/api/v1/client/subscribe?token=SUPER_SECRET_AIRPORT_999"
)


def _make_fake_fetcher(yaml_text: str = UPSTREAM_AIRPORT_YAML):
    calls: list[str] = []

    async def _fetcher(url: str) -> FetchedSubscription:
        calls.append(url)
        return FetchedSubscription(
            content=yaml_text.encode("utf-8"),
            usage={"upload": 10, "download": 20, "total": 1000},
            suggested_name="SecretAirport",
        )

    return _fetcher, calls


def _admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def test_store_auto_migrates_legacy_cloud_profiles_and_creates_secret_key(tmp_path):
    db_file = tmp_path / "legacy_cloud.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        """
        CREATE TABLE cloud_profiles (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            snapshot_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    legacy_snapshot = {"id": "legacy-p1", "name": "Legacy Profile", "revision": 3}
    conn.execute(
        "INSERT INTO cloud_profiles (id, name, revision, snapshot_json, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            "legacy-p1",
            "Legacy Profile",
            3,
            canonical_json(legacy_snapshot),
            "2026-01-01T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    store = CloudStore(db_file)
    try:
        # Secret key auto-created with 0600 permissions beside db_path.
        key_file = tmp_path / "cloud_secret.key"
        assert key_file.is_file()
        if os.name == "posix":
            assert stat.S_IMODE(key_file.stat().st_mode) == 0o600

        # Legacy row migrated under tenant_id='default' cleanly.
        migrated = store.get_profile("legacy-p1")
        assert migrated is not None
        assert migrated["name"] == "Legacy Profile"
        assert migrated["revision"] == 3

        # Re-initializing on an already-migrated DB is idempotent.
        store2 = CloudStore(db_file)
        assert store2.get_profile("legacy-p1") == migrated
        store2.close()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_resolver_strips_informational_and_filters_keywords_and_keys():
    fetcher, calls = _make_fake_fetcher()
    result = await resolve_source("url", {"url": UPSTREAM_URL}, fetcher=fetcher)
    assert calls == [UPSTREAM_URL]
    names = [n["name"] for n in result.nodes]
    assert len(names) == 5
    assert all("剩余流量" not in name and "套餐到期" not in name for name in names)
    for outbound in result.nodes:
        validated = validate_node(outbound)
        assert validated.name == outbound["name"]

    # Keyword include + exclude filtering.
    filtered = filter_nodes(
        result.nodes,
        {"include_keywords": ["US", "JP"], "exclude_keywords": ["Beta"]},
    )
    assert [n["name"] for n in filtered] == ["US-LosAngeles-01", "US-NewYork-02", "JP-Tokyo-01"]

    # Pipe-delimited string keyword support.
    norm = normalize_node_filter({"include_keywords": "美国|US|SG"})
    assert norm["include_keywords"] == ["美国", "US", "SG"]
    filtered_pipe = filter_nodes(result.nodes, {"include_keywords": "US|SG"})
    assert [n["name"] for n in filtered_pipe] == [
        "US-LosAngeles-01",
        "US-NewYork-02",
        "SG-Singapore-01",
    ]


@pytest.mark.asyncio
async def test_tenant_token_hashing_rotation_and_strict_isolation(tmp_path):
    db_file = tmp_path / "mt_isolation.db"
    app = create_cloud_app(
        db_path=db_file,
        admin_token=ADMIN_TOKEN,
        secret_key=Fernet.generate_key().decode(),
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="https://cloud.example.test"
    ) as client:
        # Multi-tenant mode with admin_token configured MUST reject unauthenticated client requests.
        no_auth = await client.get("/v1/manifest")
        assert no_auth.status_code == 401
        assert no_auth.json()["code"] == "invalid_token"

        # Create Tenant A (profiles_sync_enabled=True) and Tenant B (profiles_sync_enabled=False).
        res_a = await client.post(
            "/v1/admin/tenants",
            headers=_admin_headers(),
            json={"name": "Tenant Alpha", "profiles_sync_enabled": True, "max_devices": 3},
        )
        assert res_a.status_code == 201, res_a.text
        token_a = res_a.json()["token"]
        tenant_a_id = res_a.json()["tenant"]["id"]
        assert re.fullmatch(r"prism_[A-Za-z0-9_-]{32}", token_a)
        assert res_a.json()["tenant"]["token_prefix"] == token_a[:14]
        assert "token_hash" not in res_a.json()["tenant"]

        res_b = await client.post(
            "/v1/admin/tenants",
            headers=_admin_headers(),
            json={"name": "Tenant Beta", "profiles_sync_enabled": False, "max_devices": 3},
        )
        assert res_b.status_code == 201
        token_b = res_b.json()["token"]
        tenant_b_id = res_b.json()["tenant"]["id"]

        # Confirm raw tokens are NEVER stored anywhere in SQLite.
        raw_db_bytes = db_file.read_bytes()
        assert token_a.encode("utf-8") not in raw_db_bytes
        assert token_b.encode("utf-8") not in raw_db_bytes

        # Confirm Tenant B gets 403 profiles_sync_disabled on /v1/profiles*.
        b_headers = {"Authorization": f"Bearer {token_b}"}
        b_prof = await client.get("/v1/profiles", headers=b_headers)
        assert b_prof.status_code == 403
        assert b_prof.json()["code"] == "profiles_sync_disabled"

        b_manifest = await client.get("/v1/manifest", headers=b_headers)
        assert b_manifest.status_code == 200
        assert b_manifest.json()["profiles_sync_enabled"] is False
        assert b_manifest.json()["account"]["profiles_sync_enabled"] is False
        assert b_manifest.json()["account"]["tenant_id"] == tenant_b_id

        # Enable profiles_sync on Tenant B and verify strict profile isolation with same profile_id.
        await client.patch(
            f"/v1/admin/tenants/{tenant_b_id}",
            headers=_admin_headers(),
            json={"profiles_sync_enabled": True},
        )
        a_headers = {"Authorization": f"Bearer {token_a}"}
        put_a = await client.put(
            "/v1/profiles/shared-id-01",
            headers=a_headers,
            json={"name": "Alpha Secret Profile", "notes": "only-for-alpha"},
        )
        assert put_a.status_code == 200

        put_b = await client.put(
            "/v1/profiles/shared-id-01",
            headers=b_headers,
            json={"name": "Beta Private Profile", "notes": "only-for-beta"},
        )
        assert put_b.status_code == 200

        # Tenant A only sees Alpha's profile; Tenant B only sees Beta's profile.
        get_a = await client.get("/v1/profiles/shared-id-01", headers=a_headers)
        get_b = await client.get("/v1/profiles/shared-id-01", headers=b_headers)
        assert get_a.json()["name"] == "Alpha Secret Profile"
        assert get_b.json()["name"] == "Beta Private Profile"
        assert (await client.get("/v1/profiles", headers=a_headers)).json()["count"] == 1
        assert (await client.get("/v1/profiles", headers=b_headers)).json()["count"] == 1

        # Tenant B deleting shared-id-01 does NOT affect Tenant A!
        assert (
            await client.delete("/v1/profiles/shared-id-01", headers=b_headers)
        ).status_code == 200
        assert (await client.get("/v1/profiles/shared-id-01", headers=b_headers)).status_code == 404
        assert (await client.get("/v1/profiles/shared-id-01", headers=a_headers)).status_code == 200

        # Rotate Tenant A token: old token fails immediately, new token works with new seal_salt.
        m_before = (await client.get("/v1/manifest", headers=a_headers)).json()
        old_salt = m_before["account"]["seal_salt"]

        rot = await client.post(
            f"/v1/admin/tenants/{tenant_a_id}/rotate-token",
            headers=_admin_headers(),
        )
        assert rot.status_code == 200
        new_token_a = rot.json()["token"]
        assert new_token_a != token_a

        old_resp = await client.get("/v1/manifest", headers=a_headers)
        assert old_resp.status_code == 401
        assert old_resp.json()["code"] == "invalid_token"

        m_after = await client.get(
            "/v1/manifest", headers={"Authorization": f"Bearer {new_token_a}"}
        )
        assert m_after.status_code == 200
        assert m_after.json()["account"]["seal_salt"] != old_salt


@pytest.mark.asyncio
async def test_expiry_disable_and_device_limit_revocation(tmp_path):
    app = create_cloud_app(db_path=tmp_path / "mt_guards.db", admin_token=ADMIN_TOKEN)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="https://cloud.example.test"
    ) as client:
        future_exp = (datetime.now(timezone.utc) + timedelta(days=15)).isoformat()
        created = await client.post(
            "/v1/admin/tenants",
            headers=_admin_headers(),
            json={"name": "Guarded Customer", "expires_at": future_exp, "max_devices": 2},
        )
        assert created.status_code == 201
        tid = created.json()["tenant"]["id"]
        token = created.json()["token"]

        def dev_headers(dev_id: str, label: str = "Laptop") -> dict[str, str]:
            return {
                "Authorization": f"Bearer {token}",
                "X-Prism-Device-Id": dev_id,
                "X-Prism-Device-Label": label,
                "X-Prism-Client-Version": "0.6.0",
            }

        # Device 1 & Device 2 succeed.
        r1 = await client.get("/v1/manifest", headers=dev_headers("dev-001", "MacBook Pro"))
        assert r1.status_code == 200
        assert r1.json()["account"]["active_devices"] == 1
        assert r1.json()["account"]["max_devices"] == 2
        etag1 = r1.headers["etag"]

        # Same device re-checking returns 304 Not Modified on ETag match.
        r1_304 = await client.get(
            "/v1/manifest",
            headers={**dev_headers("dev-001", "MacBook Pro"), "If-None-Match": etag1},
        )
        assert r1_304.status_code == 304

        r2 = await client.get("/v1/manifest", headers=dev_headers("dev-002", "Windows PC"))
        assert r2.status_code == 200
        assert r2.json()["account"]["active_devices"] == 2

        # Device 3 exceeds max_devices=2 -> 403 device_limit_exceeded!
        r3 = await client.get("/v1/manifest", headers=dev_headers("dev-003", "Third Machine"))
        assert r3.status_code == 403
        r3_body = r3.json()
        assert r3_body["code"] == "device_limit_exceeded"
        assert r3_body["max_devices"] == 2
        assert r3_body["active_devices"] == 2

        # Admin lists devices and kicks dev-001.
        devs = await client.get(f"/v1/admin/tenants/{tid}/devices", headers=_admin_headers())
        assert devs.status_code == 200
        assert devs.json()["active_devices"] == 2
        assert len(devs.json()["devices"]) == 2

        kick = await client.delete(
            f"/v1/admin/tenants/{tid}/devices/dev-001", headers=_admin_headers()
        )
        assert kick.status_code == 200
        assert kick.json()["active_devices"] == 1

        # Kicked dev-001 is rejected with 403 device_revoked.
        r1_revoked = await client.get("/v1/manifest", headers=dev_headers("dev-001"))
        assert r1_revoked.status_code == 403
        assert r1_revoked.json()["code"] == "device_revoked"

        # Freed slot allows dev-003 to connect!
        r3_ok = await client.get("/v1/manifest", headers=dev_headers("dev-003", "Third Machine"))
        assert r3_ok.status_code == 200
        assert r3_ok.json()["account"]["active_devices"] == 2

        # Manual disable -> 403 tenant_disabled.
        await client.patch(
            f"/v1/admin/tenants/{tid}",
            headers=_admin_headers(),
            json={"status": "disabled"},
        )
        dis_resp = await client.get("/v1/manifest", headers=dev_headers("dev-002"))
        assert dis_resp.status_code == 403
        assert dis_resp.json()["code"] == "tenant_disabled"

        # Re-enable + set past expires_at -> 403 tenant_expired with expires_at.
        past_exp = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        await client.patch(
            f"/v1/admin/tenants/{tid}",
            headers=_admin_headers(),
            json={"status": "active", "expires_at": past_exp},
        )
        exp_resp = await client.get("/v1/manifest", headers=dev_headers("dev-002"))
        assert exp_resp.status_code == 403
        exp_body = exp_resp.json()
        assert exp_body["code"] == "tenant_expired"
        assert exp_body["expires_at"] is not None

        # Quick renew via extend_days=30 restores access and updates ETag!
        renewed = await client.patch(
            f"/v1/admin/tenants/{tid}",
            headers=_admin_headers(),
            json={"extend_days": 30},
        )
        assert renewed.status_code == 200
        assert renewed.json()["effective_status"] == "active"

        ok_again = await client.get("/v1/manifest", headers=dev_headers("dev-002"))
        assert ok_again.status_code == 200
        assert ok_again.headers["etag"] != etag1


@pytest.mark.asyncio
async def test_server_side_url_resolution_never_leaks_url_and_isolates_nodes(tmp_path):
    db_file = tmp_path / "mt_sources.db"
    fetcher, calls = _make_fake_fetcher()
    app = create_cloud_app(
        db_path=db_file,
        admin_token=ADMIN_TOKEN,
        subscription_fetcher=fetcher,
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="https://cloud.example.test"
    ) as client:
        # 1. Create shared upstream URL source (fetched server-side!).
        src_resp = await client.post(
            "/v1/admin/sources",
            headers=_admin_headers(),
            json={"name": "Global Airport Pool", "kind": "url", "url": UPSTREAM_URL},
        )
        assert src_resp.status_code == 201, src_resp.text
        src_data = src_resp.json()
        src_id = src_data["id"]
        assert src_data["node_count"] == 5
        assert len(calls) == 1
        assert "SUPER_SECRET_AIRPORT_999" not in src_resp.text
        assert src_data["payload_preview"] == "https://secret-airport.example.com/…"

        # Confirm DB stores upstream URL and resolved nodes encrypted (not plaintext!).
        raw_db_bytes = db_file.read_bytes()
        assert b"SUPER_SECRET_AIRPORT_999" not in raw_db_bytes
        assert b"pw-us-1" not in raw_db_bytes

        # 2. Create two tenants.
        t_a = (
            await client.post("/v1/admin/tenants", headers=_admin_headers(), json={"name": "Alice"})
        ).json()
        t_b = (
            await client.post("/v1/admin/tenants", headers=_admin_headers(), json={"name": "Bob"})
        ).json()
        tid_a, tok_a = t_a["tenant"]["id"], t_a["token"]
        tid_b, tok_b = t_b["tenant"]["id"], t_b["token"]

        # 3. Assign shared source to Alice (US only) and Bob (SG only via node_keys).
        src_detail = (
            await client.get(f"/v1/admin/sources/{src_id}", headers=_admin_headers())
        ).json()
        sg_node_key = next(
            n["key"] for n in src_detail["nodes_preview"] if n["name"] == "SG-Singapore-01"
        )
        assert "password" not in src_detail["nodes_preview"][0]

        await client.put(
            f"/v1/admin/tenants/{tid_a}/assignments",
            headers=_admin_headers(),
            json={
                "assignments": [
                    {
                        "source_id": src_id,
                        "display_name_override": "VIP 美国专线",
                        "node_filter": {
                            "include_keywords": ["US"],
                            "exclude_keywords": ["NewYork"],
                        },
                    }
                ]
            },
        )
        await client.put(
            f"/v1/admin/tenants/{tid_b}/assignments",
            headers=_admin_headers(),
            json={
                "assignments": [
                    {
                        "source_id": src_id,
                        "node_filter": {"node_keys": [sg_node_key]},
                    }
                ]
            },
        )

        # 4. Add a private static IP ONLY for Alice.
        priv_resp = await client.post(
            f"/v1/admin/tenants/{tid_a}/private-nodes",
            headers=_admin_headers(),
            json={
                "name": "Alice 独享静态 IP",
                "nodes_text": "socks5://alice:s3cr3t@203.0.113.55:1080#Alice-LA-Static",
            },
        )
        assert priv_resp.status_code == 201, priv_resp.text
        assert priv_resp.json()["node_count"] == 1

        # Re-saving Alice's shared assignments must NOT wipe her private static IP!
        await client.put(
            f"/v1/admin/tenants/{tid_a}/assignments",
            headers=_admin_headers(),
            json={
                "assignments": [
                    {
                        "source_id": src_id,
                        "display_name_override": "VIP 美国专线",
                        "node_filter": {
                            "include_keywords": ["US"],
                            "exclude_keywords": ["NewYork"],
                        },
                    }
                ]
            },
        )

        # 5. Verify Alice's /v1/subscriptions: sealed_nodes only, NO upstream URL, both shared & private!
        subs_a_resp = await client.get(
            "/v1/subscriptions", headers={"Authorization": f"Bearer {tok_a}"}
        )
        assert subs_a_resp.status_code == 200
        assert "secret-airport.example.com" not in subs_a_resp.text
        assert "SUPER_SECRET_AIRPORT_999" not in subs_a_resp.text
        subs_a = subs_a_resp.json()
        assert subs_a["sha256"] == canonical_sha256(subs_a["subscriptions"])
        assert len(subs_a["subscriptions"]) == 2
        assert all(item["kind"] == "sealed_nodes" for item in subs_a["subscriptions"])
        assert subs_a["subscriptions"][0]["name"] == "VIP 美国专线"
        assert [n["name"] for n in subs_a["subscriptions"][0]["nodes"]] == ["US-LosAngeles-01"]
        assert subs_a["subscriptions"][1]["name"] == "Alice 独享静态 IP"
        assert [n["name"] for n in subs_a["subscriptions"][1]["nodes"]] == ["Alice-LA-Static"]

        # 6. Verify Bob's /v1/subscriptions: ONLY SG-Singapore-01, NEVER Alice's static IP or US nodes!
        subs_b_resp = await client.get(
            "/v1/subscriptions", headers={"Authorization": f"Bearer {tok_b}"}
        )
        assert subs_b_resp.status_code == 200
        subs_b = subs_b_resp.json()
        assert subs_b["sha256"] != subs_a["sha256"]
        assert len(subs_b["subscriptions"]) == 1
        assert subs_b["subscriptions"][0]["kind"] == "sealed_nodes"
        assert [n["name"] for n in subs_b["subscriptions"][0]["nodes"]] == ["SG-Singapore-01"]
        assert "Alice-LA-Static" not in subs_b_resp.text

        # 7. Refresh upstream source via POST /v1/admin/sources/{id}/refresh.
        ref_resp = await client.post(
            f"/v1/admin/sources/{src_id}/refresh", headers=_admin_headers()
        )
        assert ref_resp.status_code == 200
        assert ref_resp.json()["node_count"] == 5
        assert len(calls) == 2

        # Manifests have strictly distinct per-tenant ETags and subscription digests.
        m_a = await client.get("/v1/manifest", headers={"Authorization": f"Bearer {tok_a}"})
        m_b = await client.get("/v1/manifest", headers={"Authorization": f"Bearer {tok_b}"})
        assert m_a.headers["etag"] != m_b.headers["etag"]
        assert m_a.json()["subscriptions"]["sha256"] == subs_a["sha256"]
        assert m_b.json()["subscriptions"]["sha256"] == subs_b["sha256"]


@pytest.mark.asyncio
async def test_admin_cookie_login_rate_limit_crud_and_admin_html(tmp_path):
    app = create_cloud_app(db_path=tmp_path / "mt_admin.db", admin_token=ADMIN_TOKEN)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="https://cloud.example.test"
    ) as client:
        # 1. /admin HTML serving with security headers & zero external CDN dependencies.
        html_resp = await client.get("/admin")
        assert html_resp.status_code == 200
        assert "text/html" in html_resp.headers["content-type"]
        assert "no-store" in html_resp.headers["cache-control"]
        assert "content-security-policy" in html_resp.headers
        assert html_resp.headers["x-frame-options"] == "DENY"
        assert "棱镜云下发中心" in html_resp.text
        assert "新建客户" in html_resp.text
        for cdn in ("cdn.jsdelivr.net", "unpkg.com", "fonts.googleapis.com", "cdnjs.cloudflare"):
            assert cdn not in html_resp.text

        # 2. Unauthenticated session check & protected admin endpoint check.
        sess0 = await client.get("/v1/admin/session")
        assert sess0.status_code == 200
        assert sess0.json() == {"authenticated": False, "auth_required": True}
        assert (await client.get("/v1/admin/overview")).status_code == 401

        # 3. Wrong password fails, and 10 failed attempts trigger 429 rate limit.
        for _ in range(10):
            bad = await client.post("/v1/admin/login", json={"token": "wrong-token"})
            assert bad.status_code == 401
        blocked = await client.post("/v1/admin/login", json={"token": ADMIN_TOKEN})
        assert blocked.status_code == 429
        assert blocked.json()["code"] == "rate_limited"

        # 4. Use a distinct X-Forwarded-For IP to log in with valid admin token & session cookie.
        ip_hdr = {"X-Forwarded-For": "198.51.100.99"}
        login_resp = await client.post(
            "/v1/admin/login", json={"token": ADMIN_TOKEN}, headers=ip_hdr
        )
        assert login_resp.status_code == 200
        set_cookie = login_resp.headers.get("set-cookie", "")
        assert "prism_admin_session=" in set_cookie
        assert "httponly" in set_cookie.lower()
        assert "samesite=strict" in set_cookie.lower()

        # Session cookie authenticates all admin API endpoints without Bearer header!
        sess1 = await client.get("/v1/admin/session")
        assert sess1.json()["authenticated"] is True

        ov = await client.get("/v1/admin/overview")
        assert ov.status_code == 200
        assert ov.json()["total_tenants"] == 0

        pol = await client.get("/v1/admin/isolation-policy")
        assert pol.status_code == 200
        assert pol.json()["policy"]["policy_version"]

        # Create, search, and delete a tenant using cookie auth.
        created = await client.post(
            "/v1/admin/tenants", json={"name": "Cookie Customer", "notes": "vip-test"}
        )
        assert created.status_code == 201
        tid = created.json()["tenant"]["id"]

        searched = await client.get("/v1/admin/tenants?search=vip-test")
        assert searched.status_code == 200
        assert searched.json()["count"] == 1

        logs = await client.get("/v1/admin/audit-logs?limit=20")
        assert logs.status_code == 200
        actions = {row["action"] for row in logs.json()["logs"]}
        assert "admin_login" in actions
        assert "tenant_created" in actions

        assert (await client.delete(f"/v1/admin/tenants/{tid}")).status_code == 200

        # Logout clears cookie with matching HttpOnly / SameSite=strict / Secure flags.
        out = await client.post("/v1/admin/logout")
        assert out.status_code == 200
        logout_cookie = out.headers.get("set-cookie", "").lower()
        assert "prism_admin_session=" in logout_cookie
        assert "httponly" in logout_cookie
        assert "samesite=strict" in logout_cookie
        assert "secure" in logout_cookie
        assert (await client.get("/v1/admin/overview")).status_code == 401


def test_client_ip_only_trusts_proxy_headers_from_trusted_peers():
    from types import SimpleNamespace

    from cloud_server.app import _client_ip, _is_https_request

    def _req(peer: str, headers: dict[str, str], scheme: str = "http"):
        return SimpleNamespace(
            client=SimpleNamespace(host=peer),
            headers={k.lower(): v for k, v in headers.items()},
            url=SimpleNamespace(scheme=scheme),
        )

    # Direct public peer cannot spoof X-Real-IP, X-Forwarded-For, or X-Forwarded-Proto.
    public_req = _req(
        "203.0.113.45",
        {
            "x-real-ip": "1.2.3.4",
            "x-forwarded-for": "5.6.7.8",
            "x-forwarded-proto": "https",
        },
    )
    assert _client_ip(public_req) == "203.0.113.45"
    assert _is_https_request(public_req) is False

    # Private Docker / Caddy peer prefers X-Real-IP over X-Forwarded-For.
    caddy_req = _req(
        "172.18.0.2",
        {
            "x-real-ip": "198.51.100.12",
            "x-forwarded-for": "9.9.9.9, 198.51.100.12",
            "x-forwarded-proto": "https",
        },
    )
    assert _client_ip(caddy_req) == "198.51.100.12"
    assert _is_https_request(caddy_req) is True

    # Loopback proxy without X-Real-IP uses rightmost X-Forwarded-For entry (blocks leftmost spoof).
    xff_req = _req("127.0.0.1", {"x-forwarded-for": "9.9.9.9, 198.51.100.77"})
    assert _client_ip(xff_req) == "198.51.100.77"


def test_audit_log_pruning_caps_at_2000_rows():
    store = CloudStore(":memory:")
    try:
        for idx in range(2050):
            store.log_audit("admin", "bulk_test", target_id=str(idx))
        count = store._conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
        assert count == 2000
    finally:
        store.close()
