"""Unit tests for cloud delivery protocol, SSRF-safe client, and CloudDeliveryService."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from cloud_server.app import create_app as create_cloud_app
from cloud_server.store import canonical_sha256
from cryptography.fernet import Fernet
from httpx import ASGITransport

from camoufox_pm.config import get_settings
from camoufox_pm.core.cloud import (
    CloudClient,
    CloudClientError,
    CloudConfigUpdate,
    CloudDeliveryService,
    CloudSyncError,
    validate_cloud_base_url,
)
from camoufox_pm.core.isolation.policy import (
    BUILTIN_POLICY,
    load_active_policy,
    validate_policy,
)
from camoufox_pm.core.network_catalog import NetworkCatalog
from camoufox_pm.core.network_subscription import FetchedSubscription

SAMPLE_YAML = """
proxies:
  - {name: "US-LA-01", type: trojan, server: us-la.example.com, port: 443, password: "sub-secret-password"}
  - {name: "JP-TYO-01", type: ss, server: jp-tyo.example.com, port: 8388, cipher: aes-128-gcm, password: "sub-secret-password"}
"""

LOCAL_YAML = """
proxies:
  - {name: "Local-SG-01", type: socks5, server: sg-local.example.com, port: 1080}
"""


@pytest.fixture
def secret_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("CPM_SECRET_KEY", key)
    monkeypatch.delenv("CPM_CLOUD_URL", raising=False)
    monkeypatch.delenv("CPM_CLOUD_TOKEN", raising=False)
    monkeypatch.delenv("CPM_CLOUD_ALLOW_LOCAL", raising=False)
    get_settings.cache_clear()
    yield key
    get_settings.cache_clear()


def test_seed_isolation_policy_passes_strict_validation():
    seed_path = (
        Path(__file__).resolve().parents[2] / "cloud_server" / "seed" / "isolation_policy.json"
    )
    raw_text = seed_path.read_text(encoding="utf-8")
    for forbidden in ("camoufox", "firefox", "mihomo", "daijro", "lulu", "噜噜"):
        assert forbidden not in raw_text.lower()
    raw = json.loads(raw_text)
    validated = validate_policy(raw)
    assert validated.policy_version == "prism-cloud-2026.11.26-r3"
    assert validated.default_timezone_mode == "fixed"
    assert validated.default_timezone == "America/Los_Angeles"
    assert validated.default_locale_mode == "fixed"
    assert validated.default_languages == ["en-US", "en"]
    assert validated.install_webrtc_guard is True
    assert validated.preset_filters.min_chrome_major == 125
    # ECH / OCSP / full-protocol proxy / battery hardening prefs
    prefs = validated.hardening_prefs
    assert prefs["network.proxy.share_proxy_settings"] is True
    assert prefs["network.dns.echconfig.enabled"] is False
    assert prefs["network.dns.http3_echconfig.enabled"] is False
    assert prefs["security.OCSP.enabled"] == 0
    assert prefs["security.OCSP.require"] is False
    assert prefs["dom.battery.enabled"] is False
    # Cloud-deliverable risk & verification domain suite
    assert set(validated.risk_probe_groups) == {"core", "antifraud_telemetry", "challenge_kyc"}
    assert "claude.ai" in validated.risk_probe_groups["core"]
    assert "r.stripe.com" in validated.risk_probe_groups["antifraud_telemetry"]
    assert "challenges.cloudflare.com" in validated.risk_probe_groups["challenge_kyc"]
    total_domains = sum(len(v) for v in validated.risk_probe_groups.values())
    assert total_domains >= 36
    assert validated.risk_probe_cidrs == [
        "34.8.0.0/16",
        "160.79.104.0/21",
        "153.61.208.0/20",
        "153.61.224.0/22",
    ]
    assert len(validated.risk_probe_targets) == 10


@pytest.mark.parametrize(
    "url, allow_local, expected_code",
    [
        ("http://cloud.example.com", False, "https_required"),
        ("http://127.0.0.1:8787", False, "https_required"),
        ("http://localhost:8787", False, "https_required"),
        ("https://127.0.0.1:8787", False, "blocked_address"),
        ("https://localhost:8787", False, "blocked_address"),
        ("https://169.254.169.254", False, "blocked_address"),
        ("https://169.254.169.254", True, "blocked_address"),
        ("https://10.0.0.5", True, "blocked_address"),
        ("https://user:pass@cloud.example.com", False, "invalid_url"),
        ("https://cloud.example.com/?q=1", False, "invalid_url"),
    ],
)
def test_validate_cloud_base_url_rejects_unsafe_urls(url, allow_local, expected_code):
    with pytest.raises(CloudClientError) as exc_info:
        validate_cloud_base_url(url, allow_local=allow_local)
    assert exc_info.value.code == expected_code


def test_validate_cloud_base_url_accepts_https_and_opted_in_local():
    assert validate_cloud_base_url("https://cloud.example.com/") == "https://cloud.example.com"
    assert (
        validate_cloud_base_url("http://127.0.0.1:8787/", allow_local=True)
        == "http://127.0.0.1:8787"
    )
    assert (
        validate_cloud_base_url("http://localhost:8787", allow_local=True)
        == "http://localhost:8787"
    )


@pytest.mark.asyncio
async def test_client_enforces_auth_etag_redirect_and_size_limit(tmp_path):
    cloud_app = create_cloud_app(
        db_path=tmp_path / "cloud.db",
        token="client-secret-token",
        admin_token="admin-secret-token",
    )
    transport = ASGITransport(app=cloud_app)

    unauth_client = CloudClient("https://cloud.example.test", transport=transport)
    with pytest.raises(CloudClientError) as exc_info:
        await unauth_client.get_manifest()
    assert exc_info.value.code == "unauthorized"

    client = CloudClient(
        "https://cloud.example.test",
        token="client-secret-token",
        transport=transport,
    )
    first = await client.get_manifest()
    assert not first.not_modified
    assert first.data is not None
    assert first.etag is not None

    second = await client.get_manifest(etag=first.etag)
    assert second.not_modified is True
    assert second.data is None

    # Enforce strict response size limit.
    tiny_client = CloudClient(
        "https://cloud.example.test",
        token="client-secret-token",
        max_bytes=16,
        transport=transport,
    )
    with pytest.raises(CloudClientError) as size_exc:
        await tiny_client.get_isolation_policy()
    assert size_exc.value.code == "too_large"

    # Reject HTTP redirects so Bearer tokens cannot be redirected.
    def _redirect_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://evil.example/steal"})

    redirect_client = CloudClient(
        "https://cloud.example.test",
        token="client-secret-token",
        transport=httpx.MockTransport(_redirect_handler),
    )
    with pytest.raises(CloudClientError) as redir_exc:
        await redirect_client.get_manifest()
    assert redir_exc.value.code == "redirect_blocked"


@pytest.mark.asyncio
async def test_client_guarded_transport_blocks_dns_rebinding_to_private_ips():
    async def rebinding_resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34", "169.254.169.254"]

    client = CloudClient(
        "https://rebind.example.test",
        resolver=rebinding_resolver,
    )
    with pytest.raises(CloudClientError) as exc_info:
        await client.get_manifest()
    assert exc_info.value.code == "blocked_address"

    async def loopback_resolver(_host: str, _port: int) -> list[str]:
        return ["127.0.0.1"]

    client_no_local = CloudClient(
        "https://local-alias.example.test",
        allow_local=False,
        resolver=loopback_resolver,
    )
    with pytest.raises(CloudClientError) as exc_loop:
        await client_no_local.get_manifest()
    assert exc_loop.value.code == "blocked_address"


@pytest.mark.asyncio
async def test_config_seals_token_and_never_exposes_plaintext(profile_manager, secret_key):
    service = CloudDeliveryService(profile_manager)
    token = "super-secret-cloud-bearer-token"
    cfg = service.update_config(
        CloudConfigUpdate(
            base_url="https://cloud.example.test/",
            token=token,
            auto_sync_on_startup=True,
            device_label="workstation-a",
        )
    )
    assert cfg.base_url == "https://cloud.example.test"
    assert cfg.has_token is True
    assert token not in cfg.model_dump_json()

    status = service.get_status()
    assert status.configured is True
    assert status.has_token is True
    assert token not in status.model_dump_json()

    # Stored SQLite value must be Fernet-encrypted ("enc:..."), never plaintext.
    row = profile_manager.storage.db._connection.execute(
        "SELECT value FROM cloud_settings WHERE key = 'token_enc'"
    ).fetchone()
    assert row is not None
    assert token not in row["value"]
    assert "enc:" in row["value"]

    # Clearing token removes it cleanly.
    cleared = service.update_config(CloudConfigUpdate(clear_token=True))
    assert cleared.has_token is False


@pytest.mark.asyncio
async def test_sync_policy_verifies_sha256_whitelist_and_offline_fallback(
    profile_manager, secret_key, tmp_path
):
    cloud_app = create_cloud_app(db_path=tmp_path / "cloud.db", token="tok-1")
    transport = ASGITransport(app=cloud_app)
    service = CloudDeliveryService(profile_manager, transport=transport)
    service.update_config(CloudConfigUpdate(base_url="https://cloud.example.test", token="tok-1"))

    # Before sync, builtin policy is active.
    active, source = load_active_policy(profile_manager.storage.db._connection)
    assert source == "builtin"
    assert active.policy_version == BUILTIN_POLICY.policy_version

    # First sync pulls and caches the cloud seed policy.
    res = await service.sync_policy()
    assert res.status == "updated"
    assert res.updated is True
    active_after, source_after = load_active_policy(profile_manager.storage.db._connection)
    assert source_after == "cloud"
    assert active_after.policy_version == "prism-cloud-2026.11.26-r3"
    assert active_after.default_timezone == "America/Los_Angeles"
    assert active_after.hardening_prefs["network.proxy.share_proxy_settings"] is True
    assert active_after.hardening_prefs["network.dns.echconfig.enabled"] is False
    assert active_after.hardening_prefs["security.OCSP.enabled"] == 0
    assert len(active_after.risk_probe_cidrs) == 4
    assert len(active_after.risk_probe_targets) == 10

    policy_info = service.get_active_policy_info()
    assert policy_info["summary"]["risk_domains_count"] >= 36
    assert policy_info["summary"]["risk_cidrs_count"] == 4
    assert policy_info["summary"]["risk_probe_targets_count"] == 10

    # Second sync hits ETag 304 Not Modified.
    res_again = await service.sync_policy()
    assert res_again.status == "unchanged"
    assert res_again.updated is False

    # Tampered SHA-256 is rejected and preserves previously cached cloud policy.
    bad_sha_payload = {
        "version": "cloud-evil-sha",
        "sha256": "0" * 64,
        "policy": BUILTIN_POLICY.model_dump(mode="json"),
    }
    bad_sha_service = CloudDeliveryService(
        profile_manager,
        transport=httpx.MockTransport(lambda _req: httpx.Response(200, json=bad_sha_payload)),
    )
    with pytest.raises(CloudSyncError) as exc_sha:
        await bad_sha_service.sync_policy(force=True)
    assert exc_sha.value.code == "sha256_mismatch"

    # Malicious policy weakening proxy isolation is rejected even with valid SHA-256.
    evil_policy = BUILTIN_POLICY.model_dump(mode="json")
    evil_policy["policy_version"] = "cloud-evil-prefs"
    evil_policy["hardening_prefs"]["network.proxy.no_proxies_on"] = "*"
    evil_payload = {
        "version": "cloud-evil-prefs",
        "sha256": canonical_sha256(evil_policy),
        "policy": evil_policy,
    }
    evil_service = CloudDeliveryService(
        profile_manager,
        transport=httpx.MockTransport(lambda _req: httpx.Response(200, json=evil_payload)),
    )
    with pytest.raises(CloudSyncError) as exc_evil:
        await evil_service.sync_policy(force=True)
    assert exc_evil.value.code == "invalid_policy"

    # Weakening ECH or share_proxy_settings is also rejected by the security whitelist.
    ech_policy = BUILTIN_POLICY.model_dump(mode="json")
    ech_policy["policy_version"] = "cloud-evil-ech"
    ech_policy["hardening_prefs"]["network.dns.echconfig.enabled"] = True
    ech_payload = {
        "version": "cloud-evil-ech",
        "sha256": canonical_sha256(ech_policy),
        "policy": ech_policy,
    }
    ech_service = CloudDeliveryService(
        profile_manager,
        transport=httpx.MockTransport(lambda _req: httpx.Response(200, json=ech_payload)),
    )
    with pytest.raises(CloudSyncError) as exc_ech:
        await ech_service.sync_policy(force=True)
    assert exc_ech.value.code == "invalid_policy"

    # Offline / network failure keeps the cached cloud policy intact.
    def _offline(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network unreachable")

    offline_service = CloudDeliveryService(
        profile_manager,
        transport=httpx.MockTransport(_offline),
    )
    with pytest.raises(CloudSyncError) as exc_off:
        await offline_service.sync_policy(force=True)
    assert exc_off.value.code == "connection_failed"

    still_active, still_source = load_active_policy(profile_manager.storage.db._connection)
    assert still_source == "cloud"
    assert still_active.policy_version == active_after.policy_version


@pytest.mark.asyncio
async def test_sync_subscriptions_preserves_local_and_active_bindings(
    profile_manager, secret_key, tmp_path
):
    cloud_app = create_cloud_app(
        db_path=tmp_path / "cloud.db",
        token="client-tok",
        admin_token="admin-tok",
    )
    transport = ASGITransport(app=cloud_app)

    # Create a local user subscription that must NEVER be touched by cloud sync.
    catalog = NetworkCatalog(profile_manager)
    local_created = await catalog.create_subscription(
        "My Local Airport", content=LOCAL_YAML.encode("utf-8"), file_name="local.yaml"
    )
    local_sub_id = local_created["subscription"]["id"]

    # Seed two cloud subscriptions (one URL, one manual) on cloud_server.
    async with httpx.AsyncClient(
        transport=transport, base_url="https://cloud.example.test"
    ) as admin:
        resp = await admin.put(
            "/v1/admin/subscriptions",
            headers={"Authorization": "Bearer admin-tok"},
            json={
                "version": "subs-1",
                "subscriptions": [
                    {
                        "cloud_source_id": "cloud-airport-url",
                        "name": "Cloud Airport",
                        "kind": "url",
                        "url": "https://sub.example.com/clash.yaml",
                        "auto_sync": True,
                    },
                    {
                        "cloud_source_id": "cloud-static-ips",
                        "name": "Cloud Static IPs",
                        "kind": "manual",
                        "manual_nodes": ["socks5://u:p@203.0.113.10:1080#US-Static-01"],
                        "auto_sync": True,
                    },
                ],
            },
        )
        assert resp.status_code == 200

    async def fake_fetch(url: str) -> FetchedSubscription:
        assert url == "https://sub.example.com/clash.yaml"
        return FetchedSubscription(
            content=SAMPLE_YAML.encode("utf-8"), usage=None, suggested_name=None
        )

    service = CloudDeliveryService(
        profile_manager,
        transport=transport,
        subscription_fetcher=fake_fetch,
    )
    service.update_config(
        CloudConfigUpdate(base_url="https://cloud.example.test", token="client-tok")
    )

    result = await service.sync_subscriptions()
    assert result.status == "updated"
    assert result.created == 2
    assert result.retired == 0

    conn = profile_manager.storage.db._connection
    subs = conn.execute(
        "SELECT id, name, managed_by, cloud_source_id FROM network_subscriptions WHERE deleted_at IS NULL"
    ).fetchall()
    by_id = {row["id"]: dict(row) for row in subs}
    assert by_id[local_sub_id]["managed_by"] == "local"
    assert by_id[local_sub_id]["cloud_source_id"] is None
    cloud_sources = {
        row["cloud_source_id"]: row["id"] for row in subs if row["managed_by"] == "cloud"
    }
    assert set(cloud_sources) == {"cloud-airport-url", "cloud-static-ips"}

    # Bind a profile to the manual cloud node before the cloud removes that subscription.
    profile = await profile_manager.create_profile("Bound Browser")
    manual_node = conn.execute(
        "SELECT id FROM network_nodes WHERE subscription_id = ?",
        (cloud_sources["cloud-static-ips"],),
    ).fetchone()
    assert manual_node is not None
    with conn:
        conn.execute(
            "INSERT INTO profile_network_bindings (profile_id, node_id, row_version, bound_at) "
            "VALUES (?, ?, 0, '2026-11-26T00:00:00+00:00')",
            (profile.id, manual_node["id"]),
        )

    # Admin removes both cloud subscriptions from server.
    async with httpx.AsyncClient(
        transport=transport, base_url="https://cloud.example.test"
    ) as admin:
        resp2 = await admin.put(
            "/v1/admin/subscriptions",
            headers={"Authorization": "Bearer admin-tok"},
            json={"version": "subs-2", "subscriptions": []},
        )
        assert resp2.status_code == 200

    result2 = await service.sync_subscriptions()
    assert result2.retired == 2

    # Local subscription is untouched!
    assert (
        conn.execute(
            "SELECT deleted_at FROM network_subscriptions WHERE id = ?", (local_sub_id,)
        ).fetchone()["deleted_at"]
        is None
    )
    # Active profile binding is NOT clobbered, and its bound node is marked retired rather than deleted.
    binding_still = conn.execute(
        "SELECT node_id FROM profile_network_bindings WHERE profile_id = ?", (profile.id,)
    ).fetchone()
    assert binding_still is not None
    assert binding_still["node_id"] == manual_node["id"]
    node_status = conn.execute(
        "SELECT status FROM network_nodes WHERE id = ?", (manual_node["id"],)
    ).fetchone()["status"]
    assert node_status == "retired"


@pytest.mark.asyncio
async def test_push_encrypts_secrets_and_pull_skips_running_browsers(
    profile_manager, secret_key, tmp_path, monkeypatch
):
    cloud_app = create_cloud_app(db_path=tmp_path / "cloud.db", token="tok-sec")
    transport = ASGITransport(app=cloud_app)

    cookie_secret = "session_cookie_secret_98765"
    note_secret = "account_password_memo_xyz"
    exported_cookies = json.dumps(
        [
            {
                "name": "sid",
                "value": cookie_secret,
                "domain": "example.com",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ]
    )
    imported_cookies_log: list[tuple[str, str]] = []

    async def fake_export(_pid: str) -> str:
        return exported_cookies

    async def fake_import(pid: str, data: str) -> None:
        imported_cookies_log.append((pid, data))

    service = CloudDeliveryService(
        profile_manager,
        transport=transport,
        cookie_exporter=fake_export,
        cookie_importer=fake_import,
    )
    service.update_config(CloudConfigUpdate(base_url="https://cloud.example.test", token="tok-sec"))

    profile = await profile_manager.create_profile(
        "Sensitive Account",
        group="Finance",
        tags=["us", "vip"],
        notes=note_secret,
    )

    # Without CPM_SECRET_KEY, cookie upload is refused.
    monkeypatch.setenv("CPM_SECRET_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(CloudSyncError) as no_key_exc:
        await service.push_profile(profile.id, include_cookies=True)
    assert no_key_exc.value.code == "encryption_key_required"

    # Restore key and push with cookies.
    monkeypatch.setenv("CPM_SECRET_KEY", secret_key)
    get_settings.cache_clear()
    pushed = await service.push_profile(profile.id, include_cookies=True)
    assert pushed["profile_id"] == profile.id
    assert pushed["revision"] == 1
    assert pushed["included_cookies"] is True

    # Verify the cloud server holds ZERO plaintext secrets!
    raw_snapshot = cloud_app.state.store.get_profile(profile.id)
    assert raw_snapshot is not None
    raw_dump = json.dumps(raw_snapshot)
    assert cookie_secret not in raw_dump
    assert note_secret not in raw_dump
    assert raw_snapshot["notes"] is None
    assert raw_snapshot["encrypted_cookies_blob"].startswith("enc:")
    assert raw_snapshot["encrypted_metadata_blob"].startswith("enc:")

    # Bump remote revision on cloud server and mark profile as running locally.
    updated_remote = {**raw_snapshot, "name": "Remote Renamed", "revision": 5}
    cloud_app.state.store.upsert_profile(profile.id, updated_remote)
    monkeypatch.setattr(
        profile_manager.browser_sessions,
        "is_running",
        lambda pid: pid == profile.id,
    )

    pull_while_running = await service.pull_profiles(include_cookies=True)
    assert pull_while_running["skipped_running"] == 1
    assert pull_while_running["skipped_running_ids"] == [profile.id]
    assert pull_while_running["updated"] == 0
    still_local = await profile_manager.get_profile(profile.id)
    assert still_local is not None
    assert still_local.name == "Sensitive Account"
    assert imported_cookies_log == []

    # Once browser stops, pull succeeds and restores encrypted cookies and remote name.
    monkeypatch.setattr(profile_manager.browser_sessions, "is_running", lambda _pid: False)
    pull_after_stop = await service.pull_profiles(include_cookies=True)
    assert pull_after_stop["skipped_running"] == 0
    assert pull_after_stop["updated"] == 1
    assert pull_after_stop["cookies_restored"] == 1
    assert imported_cookies_log == [(profile.id, exported_cookies)]
    reloaded = await profile_manager.get_profile(profile.id)
    assert reloaded is not None
    assert reloaded.name == "Remote Renamed"
    assert reloaded.notes == note_secret


@pytest.mark.asyncio
async def test_sync_all_records_status_and_startup_sync_respects_toggle(
    profile_manager, secret_key, tmp_path
):
    cloud_app = create_cloud_app(db_path=tmp_path / "cloud_all.db")
    transport = ASGITransport(app=cloud_app)
    service = CloudDeliveryService(profile_manager, transport=transport)

    # Unconfigured startup sync is a safe no-op.
    assert await service.maybe_sync_on_startup() is None

    service.update_config(
        CloudConfigUpdate(
            base_url="https://cloud.example.test",
            auto_sync_on_startup=False,
            sync_profiles_enabled=True,
        )
    )
    # Disabled auto_sync_on_startup skips startup sync.
    assert await service.maybe_sync_on_startup() is None

    # Enable auto_sync_on_startup -> runs sync_all cleanly.
    service.update_config(CloudConfigUpdate(auto_sync_on_startup=True))
    report = await service.maybe_sync_on_startup()
    assert report is not None
    assert report.ok is True
    assert report.policy is not None and report.policy.updated is True
    assert report.subscriptions is not None
    assert report.profiles is not None

    status = service.get_status()
    assert status.connected is True
    assert status.policy_source == "cloud"
    assert status.last_synced_at == report.synced_at
    assert status.last_error is None

    # Startup sync absorbs offline errors without crashing app boot.
    def _fail(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    offline_svc = CloudDeliveryService(
        profile_manager,
        transport=httpx.MockTransport(_fail),
    )
    assert await offline_svc.maybe_sync_on_startup() is None
    status_after_fail = offline_svc.get_status()
    assert status_after_fail.connected is False
    assert status_after_fail.last_error is not None
    assert status_after_fail.policy_source == "cloud"


@pytest.mark.asyncio
async def test_cloud_server_dev_mode_and_single_token_auth(tmp_path):
    # No tokens configured -> local dev mode allows both client & admin endpoints.
    dev_app = create_cloud_app(db_path=tmp_path / "dev.db", token="", admin_token="")
    async with httpx.AsyncClient(
        transport=ASGITransport(app=dev_app), base_url="http://127.0.0.1:8787"
    ) as client:
        assert (await client.get("/v1/manifest")).status_code == 200
        assert (
            await client.put("/v1/admin/subscriptions", json={"subscriptions": []})
        ).status_code == 200

    # Single token configured -> protects both client and admin endpoints.
    single_app = create_cloud_app(
        db_path=tmp_path / "single.db", token="shared-tok", admin_token=""
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(app=single_app), base_url="http://127.0.0.1:8787"
    ) as client:
        assert (await client.get("/v1/manifest")).status_code == 401
        assert (
            await client.put("/v1/admin/subscriptions", json={"subscriptions": []})
        ).status_code == 401
        headers = {"Authorization": "Bearer shared-tok"}
        assert (await client.get("/v1/manifest", headers=headers)).status_code == 200
        assert (
            await client.put("/v1/admin/subscriptions", headers=headers, json={"subscriptions": []})
        ).status_code == 200


@pytest.mark.asyncio
async def test_prism_branding_and_env_token_fallback(tmp_path, monkeypatch):
    # Verify Prism client User-Agent header.
    seen_ua: list[str] = []

    def _capture_ua(request: httpx.Request) -> httpx.Response:
        seen_ua.append(request.headers.get("user-agent", ""))
        return httpx.Response(
            200,
            json={
                "protocol_version": 1,
                "isolation_policy": {"version": "v1", "sha256": "a" * 64},
                "subscriptions": {"version": "s1", "sha256": "b" * 64},
                "profiles_sync_enabled": True,
                "profiles_count": 0,
            },
        )

    client = CloudClient(
        "https://cloud.example.test",
        transport=httpx.MockTransport(_capture_ua),
    )
    await client.get_manifest()
    assert seen_ua == ["Prism-Browser/1.0"]

    # Legacy LULU_CLOUD_* env vars still work as backwards-compatible fallbacks.
    monkeypatch.delenv("PRISM_CLOUD_TOKEN", raising=False)
    monkeypatch.delenv("PRISM_CLOUD_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("LULU_CLOUD_TOKEN", "legacy-client-tok")
    monkeypatch.setenv("LULU_CLOUD_ADMIN_TOKEN", "legacy-admin-tok")
    legacy_app = create_cloud_app(db_path=tmp_path / "legacy_env.db")
    assert legacy_app.title == "Prism Cloud Config Server"
    async with httpx.AsyncClient(
        transport=ASGITransport(app=legacy_app), base_url="http://127.0.0.1:8787"
    ) as http:
        health = await http.get("/health")
        assert health.status_code == 200
        assert health.json()["service"] == "prism-cloud-server"
        assert (await http.get("/v1/manifest")).status_code == 401
        assert (
            await http.get("/v1/manifest", headers={"Authorization": "Bearer legacy-client-tok"})
        ).status_code == 200
        assert (
            await http.put(
                "/v1/admin/subscriptions",
                headers={"Authorization": "Bearer legacy-admin-tok"},
                json={"subscriptions": []},
            )
        ).status_code == 200

    # PRISM_CLOUD_* takes precedence over legacy LULU_CLOUD_* when both are set.
    monkeypatch.setenv("PRISM_CLOUD_TOKEN", "prism-client-tok")
    monkeypatch.setenv("PRISM_CLOUD_ADMIN_TOKEN", "prism-admin-tok")
    prism_app = create_cloud_app(db_path=tmp_path / "prism_env.db")
    async with httpx.AsyncClient(
        transport=ASGITransport(app=prism_app), base_url="http://127.0.0.1:8787"
    ) as http:
        assert (
            await http.get("/v1/manifest", headers={"Authorization": "Bearer legacy-client-tok"})
        ).status_code == 401
        assert (
            await http.get("/v1/manifest", headers={"Authorization": "Bearer prism-client-tok"})
        ).status_code == 200
        assert (
            await http.put(
                "/v1/admin/subscriptions",
                headers={"Authorization": "Bearer prism-admin-tok"},
                json={"subscriptions": []},
            )
        ).status_code == 200
