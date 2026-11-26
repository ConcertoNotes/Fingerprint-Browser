"""Unit tests for the curated Chrome fingerprint pool, isolation engine, and 11-point auditor."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from camoufox_pm.core.isolation import (
    BUILTIN_POLICY,
    CHROME_PRESET_POOL,
    PolicyValidationError,
    ProfileIsolation,
    allocate_isolation,
    audit_profile,
    compile_session_payload,
    compute_fingerprint_hash,
    list_filtered_presets,
    load_active_policy,
    validate_policy,
)
from camoufox_pm.core.models import Profile


def _fake_network(port: int = 18888, exit_ip: str = "203.0.113.42", country_iso: str = "JP"):
    return SimpleNamespace(
        listener=SimpleNamespace(port=port, username="cpm-test", password="secret"),
        browser_proxy={
            "server": f"http://127.0.0.1:{port}",
            "username": "cpm-test",
            "password": "secret",
        },
        exit_ip=exit_ip,
        exit_country=country_iso,
        exit_city="Tokyo",
        node={"id": "n-test", "name": "Tokyo-1", "check": {"country_iso": country_iso}},
        engine=SimpleNamespace(alive=True),
    )


# --- Policy & Security Whitelist ---------------------------------------------


def test_builtin_policy_defaults_match_fixed_la_english_guard():
    assert BUILTIN_POLICY.schema_version == 1
    assert BUILTIN_POLICY.policy_version == "builtin-2026.11.26-r3"
    assert BUILTIN_POLICY.default_tier == "strict"
    assert BUILTIN_POLICY.default_webrtc_policy == "block"
    assert BUILTIN_POLICY.default_canvas_policy == "stable_hardware"
    assert BUILTIN_POLICY.default_locale_mode == "fixed"
    assert BUILTIN_POLICY.default_languages == ["en-US", "en"]
    assert BUILTIN_POLICY.default_timezone_mode == "fixed"
    assert BUILTIN_POLICY.default_timezone == "America/Los_Angeles"
    assert BUILTIN_POLICY.install_webrtc_guard is True
    assert BUILTIN_POLICY.hardening_prefs["network.trr.mode"] == 5
    assert BUILTIN_POLICY.hardening_prefs["network.dns.disableIPv6"] is True
    assert BUILTIN_POLICY.hardening_prefs["network.http.http3.enable"] is False
    assert BUILTIN_POLICY.hardening_prefs["network.proxy.no_proxies_on"] == ""
    assert BUILTIN_POLICY.hardening_prefs["network.proxy.socks_remote_dns"] is True
    assert BUILTIN_POLICY.hardening_prefs["network.proxy.failover_direct"] is False
    assert BUILTIN_POLICY.hardening_prefs["network.proxy.share_proxy_settings"] is True
    assert BUILTIN_POLICY.hardening_prefs["network.dns.echconfig.enabled"] is False
    assert BUILTIN_POLICY.hardening_prefs["network.dns.http3_echconfig.enabled"] is False
    assert BUILTIN_POLICY.hardening_prefs["security.OCSP.enabled"] == 0
    assert type(BUILTIN_POLICY.hardening_prefs["security.OCSP.enabled"]) is int
    assert BUILTIN_POLICY.hardening_prefs["security.OCSP.require"] is False
    assert BUILTIN_POLICY.hardening_prefs["dom.battery.enabled"] is False
    assert BUILTIN_POLICY.total_enforced_domains() == 38
    assert len(BUILTIN_POLICY.risk_probe_cidrs) == 4
    assert len(BUILTIN_POLICY.risk_probe_targets) == 10


def test_validate_policy_accepts_seed_and_rejects_dangerous_overrides():
    seed = Path(__file__).resolve().parents[2] / "cloud_server" / "seed" / "isolation_policy.json"
    if seed.is_file():
        validated = validate_policy(json.loads(seed.read_text(encoding="utf-8")))
        assert validated.default_timezone == "America/Los_Angeles"
        assert validated.default_languages == ["en-US", "en"]
        assert validated.install_webrtc_guard is True

    for bad_key in ("addons", "extensions", "executable_path", "user_data_dir", "proxy", "env"):
        raw = BUILTIN_POLICY.model_dump()
        raw[bad_key] = ["/tmp/evil"]
        with pytest.raises(PolicyValidationError):
            validate_policy(raw)

    raw = BUILTIN_POLICY.model_dump()
    raw["hardening_prefs"]["xpinstall.signatures.required"] = False
    with pytest.raises(PolicyValidationError):
        validate_policy(raw)

    for pref_key, bad_val in (
        ("network.proxy.no_proxies_on", "localhost,127.0.0.1"),
        ("network.proxy.socks_remote_dns", False),
        ("network.proxy.failover_direct", True),
        ("network.proxy.share_proxy_settings", False),
        ("network.trr.mode", 2),
        ("network.dns.disableIPv6", False),
        ("network.dns.echconfig.enabled", True),
        ("network.dns.http3_echconfig.enabled", True),
        ("network.http.http3.enable", True),
        ("security.OCSP.enabled", 1),
        ("security.OCSP.enabled", False),
        ("security.OCSP.require", True),
        ("dom.battery.enabled", True),
        ("media.peerconnection.ice.no_host", False),
        ("media.peerconnection.ice.proxy_only", False),
    ):
        raw = BUILTIN_POLICY.model_dump()
        raw["hardening_prefs"][pref_key] = bad_val
        with pytest.raises(PolicyValidationError):
            validate_policy(raw)

    raw = BUILTIN_POLICY.model_dump()
    raw["risk_probe_groups"]["core"] = ["https://bad-url.example/path"]
    with pytest.raises(PolicyValidationError):
        validate_policy(raw)

    raw = BUILTIN_POLICY.model_dump()
    raw["risk_probe_cidrs"] = ["not-a-cidr"]
    with pytest.raises(PolicyValidationError):
        validate_policy(raw)

    raw = BUILTIN_POLICY.model_dump()
    raw["default_timezone"] = "Mars/Olympus_Mons"
    with pytest.raises(PolicyValidationError):
        validate_policy(raw)


@pytest.mark.asyncio
async def test_load_active_policy_uses_cloud_cache_and_falls_back_safely(storage):
    conn = storage.db._connection
    pol, src = load_active_policy(conn)
    assert src == "builtin"
    assert pol.policy_version == BUILTIN_POLICY.policy_version

    cloud_raw = BUILTIN_POLICY.model_dump()
    cloud_raw["policy_version"] = "cloud-2026.12.01"
    payload = json.dumps(cloud_raw, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO cloud_cache (kind, version, sha256, payload, fetched_at) "
            "VALUES ('isolation_policy', ?, ?, ?, '2026-12-01T00:00:00+00:00')",
            ("cloud-2026.12.01", digest, payload),
        )
    pol, src = load_active_policy(conn)
    assert src == "cloud"
    assert pol.policy_version == "cloud-2026.12.01"

    with conn:
        conn.execute(
            "UPDATE cloud_cache SET sha256 = ? WHERE kind = 'isolation_policy'",
            ("0" * 64,),
        )
    pol, src = load_active_policy(conn)
    assert src == "builtin"


# --- Curated 60+ Chrome Preset Pool & Deduplicated Allocation ----------------


def test_curated_chrome_preset_pool_has_60_plus_clean_desktop_devices():
    total = sum(len(group) for group in CHROME_PRESET_POOL.values())
    assert total >= 60
    assert len(CHROME_PRESET_POOL["macos"]) >= 20
    assert len(CHROME_PRESET_POOL["windows"]) >= 20
    assert len(CHROME_PRESET_POOL["linux"]) >= 15

    all_filtered = list_filtered_presets(None, BUILTIN_POLICY)
    assert len(all_filtered) == total
    for item in all_filtered:
        assert "Chrome/" in item["user_agent"]
        assert "Electron/" not in item["user_agent"]
        assert "Camoufox" not in item["user_agent"]


def test_list_filtered_presets_drops_software_renderers_and_low_res():
    policy = validate_policy(
        {
            **BUILTIN_POLICY.model_dump(),
            "curated_presets": [
                {
                    "id": "cloud:windows:good",
                    "os": "windows",
                    "screen_width": 1920,
                    "screen_height": 1080,
                    "hardware_concurrency": 8,
                    "webgl_vendor": "Google Inc. (NVIDIA)",
                    "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060)",
                },
                {
                    "id": "cloud:windows:swiftshader",
                    "os": "windows",
                    "screen_width": 1920,
                    "screen_height": 1080,
                    "hardware_concurrency": 8,
                    "webgl_vendor": "Google Inc.",
                    "webgl_renderer": "Google SwiftShader",
                },
                {
                    "id": "cloud:windows:tiny",
                    "os": "windows",
                    "screen_width": 800,
                    "screen_height": 600,
                    "hardware_concurrency": 8,
                    "webgl_vendor": "AMD",
                    "webgl_renderer": "AMD Radeon RX 6700",
                },
            ],
        }
    )

    presets = list_filtered_presets("windows", policy, used_preset_ids={"cloud:windows:good"})
    ids = {p["id"] for p in presets}
    assert "cloud:windows:good" in ids
    assert "cloud:windows:swiftshader" not in ids
    assert "cloud:windows:tiny" not in ids
    good_entry = next(p for p in presets if p["id"] == "cloud:windows:good")
    assert good_entry["in_use"] is True


def test_allocate_isolation_deduplicates_and_generates_chrome_cdp_pin_with_seeds():
    seen_hashes: set[str] = set()
    seen_presets: set[str] = set()
    seen_seeds: set[str] = set()
    for _ in range(8):
        iso = allocate_isolation(
            "windows",
            "strict",
            existing_hashes=seen_hashes,
            policy=BUILTIN_POLICY,
            existing_preset_ids=seen_presets,
        )
        assert iso.fingerprint_hash.startswith("FP-")
        assert len(iso.fingerprint_hash) == 11
        assert iso.fingerprint_hash not in seen_hashes
        assert iso.preset_id not in seen_presets
        assert iso.timezone_mode == "fixed"
        assert iso.timezone == "America/Los_Angeles"
        assert iso.languages == ["en-US", "en"]
        assert iso.install_webrtc_guard is True

        pin = iso.pinned_fingerprint
        assert "Chrome/" in pin["user_agent"]
        assert "Electron/" not in pin["user_agent"]
        assert pin["platform"] == "Win32"
        assert pin["screen_width"] >= 1280
        assert pin["screen_height"] >= 720
        assert pin["mobile"] is False
        assert len(pin["canvas_seed"]) == 32
        assert len(pin["audio_seed"]) == 32
        assert len(pin["webgl_seed"]) == 32
        assert pin["canvas_seed"] not in seen_seeds
        assert compute_fingerprint_hash(pin) == iso.fingerprint_hash

        seen_hashes.add(iso.fingerprint_hash)
        seen_seeds.add(pin["canvas_seed"])
        if iso.preset_id:
            seen_presets.add(iso.preset_id)


def test_allocate_isolation_rejects_unknown_preset():
    with pytest.raises(ValueError, match="Unknown fingerprint preset"):
        allocate_isolation("windows", "strict", set(), BUILTIN_POLICY, preset_id="windows:999999")


# --- Electron Session Payload Compiler ---------------------------------------


def test_compile_session_payload_matches_electron_contract_and_sanitizes_when_requested():
    iso = allocate_isolation("macos", "strict", set(), BUILTIN_POLICY)
    profile = Profile(name="isolated-1")
    network = _fake_network(port=21234, exit_ip="203.0.113.42", country_iso="JP")

    full = compile_session_payload(
        profile,
        iso,
        BUILTIN_POLICY,
        network=network,
        exit_ip="203.0.113.42",
        exit_country="JP",
        exit_city="Tokyo",
        include_proxy_secrets=True,
    )
    assert full["profile_id"] == profile.id
    assert full["status"] == "ready"
    assert full["proxy"] == {
        "server": "http://127.0.0.1:21234",
        "host": "127.0.0.1",
        "port": 21234,
        "username": "cpm-test",
        "password": "secret",
    }
    assert full["exit_ip"] == "203.0.113.42"
    assert full["exit_country"] == "JP"
    assert full["exit_city"] == "Tokyo"
    assert full["isolation"]["timezone"] == "America/Los_Angeles"
    assert full["isolation"]["languages"] == ["en-US", "en"]
    assert full["isolation"]["webrtc_policy"] == "block"
    assert full["isolation"]["canvas_policy"] == "stable_hardware"
    pin = full["isolation"]["pinned_fingerprint"]
    for key in (
        "user_agent",
        "chrome_major",
        "platform",
        "platform_version",
        "architecture",
        "bitness",
        "model",
        "mobile",
        "screen_width",
        "screen_height",
        "avail_width",
        "avail_height",
        "color_depth",
        "device_pixel_ratio",
        "hardware_concurrency",
        "device_memory",
        "webgl_vendor",
        "webgl_renderer",
        "canvas_seed",
        "audio_seed",
        "webgl_seed",
        "fonts",
    ):
        assert key in pin

    sanitized = compile_session_payload(
        profile,
        iso,
        BUILTIN_POLICY,
        network=network,
        include_proxy_secrets=False,
    )
    assert "password" not in sanitized["proxy"]
    assert "username" not in sanitized["proxy"]


def test_compile_session_payload_follow_exit_languages():
    iso = allocate_isolation(
        "windows",
        "standard",
        set(),
        BUILTIN_POLICY,
        locale_mode="follow_exit",
        timezone_mode="fixed",
        timezone="Asia/Tokyo",
    )
    profile = Profile(name="dynamic-1")
    payload = compile_session_payload(
        profile, iso, BUILTIN_POLICY, exit_ip="198.51.100.7", exit_country="JP"
    )
    assert payload["isolation"]["languages"] == ["ja-JP", "ja", "en-US"]
    assert payload["isolation"]["timezone"] == "Asia/Tokyo"


# --- 11-Point Isolation Auditor ----------------------------------------------


@pytest.mark.asyncio
async def test_audit_profile_reports_all_eleven_checks_and_detects_collisions(
    profile_manager, monkeypatch
):
    from cryptography.fernet import Fernet

    from camoufox_pm.config import get_settings
    from camoufox_pm.core.network_catalog import NetworkCatalog

    monkeypatch.setenv("CPM_SECRET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    try:
        catalog = NetworkCatalog(profile_manager)
        group = catalog.create_manual_group("Static IPs")
        added = catalog.add_manual_nodes(
            group["id"],
            [
                (1, {"name": "US-1", "type": "http", "server": "198.51.100.1", "port": 8080}, None),
                (2, {"name": "US-2", "type": "http", "server": "198.51.100.2", "port": 8080}, None),
            ],
        )
        node1_id = added["created"][0]["id"]
        node2_id = added["created"][1]["id"]
        catalog.save_check(
            node1_id,
            {"health": "ok", "exit_ip": "203.0.113.10", "country_iso": "US", "latency_ms": 42},
        )
        catalog.save_check(
            node2_id,
            {"health": "ok", "exit_ip": "203.0.113.10", "country_iso": "US", "latency_ms": 50},
        )

        p1 = await profile_manager.create_profile(name="Alpha", node_id=node1_id)
        report1 = await audit_profile(profile_manager, p1.id)
        check_map1 = {c["key"]: c for c in report1["checks"]}
        expected_keys = {
            "fingerprint_pinned",
            "fingerprint_unique",
            "browser_version",
            "storage_isolated",
            "node_bound_and_alive",
            "exit_ip_exclusivity",
            "leak_protection",
            "webrtc_guard_addon",
            "full_traffic_tunnel",
            "cloud_policy_fresh",
            "risk_domains_enforced",
        }
        assert set(check_map1) == expected_keys
        assert check_map1["risk_domains_enforced"]["status"] == "pass"
        assert "38" in check_map1["risk_domains_enforced"]["summary"]
        assert report1["status"] == "healthy"
        assert report1["score"] == 100

        unbound = await profile_manager.create_profile(name="Unbound")
        unbound_map = {
            c["key"]: c for c in (await audit_profile(profile_manager, unbound.id))["checks"]
        }
        assert unbound_map["risk_domains_enforced"]["status"] == "fail"
        assert unbound_map["risk_domains_enforced"]["action"] == "bind_node"

        p2 = await profile_manager.create_profile(name="Beta", node_id=node2_id)
        assert p1.fingerprint_hash != p2.fingerprint_hash
        report2 = await audit_profile(profile_manager, p2.id)
        check_map2 = {c["key"]: c for c in report2["checks"]}
        assert check_map2["fingerprint_unique"]["status"] == "pass"
        assert check_map2["exit_ip_exclusivity"]["status"] == "warn"

        iso2 = ProfileIsolation.model_validate(p2.isolation_config)
        iso2.policy_version = "old-2025.01.01"
        iso2.fingerprint_hash = p1.fingerprint_hash or ""
        p2.isolation_config = iso2.model_dump(mode="json")
        p2.fingerprint_hash = p1.fingerprint_hash
        await profile_manager.storage.update_profile(p2)

        report_bad = await audit_profile(profile_manager, p2.id)
        bad_map = {c["key"]: c for c in report_bad["checks"]}
        assert bad_map["fingerprint_unique"]["status"] == "fail"
        assert bad_map["cloud_policy_fresh"]["status"] == "warn"
        assert bad_map["cloud_policy_fresh"]["action"] == "reoptimize"

        hw_before = dict(p2.fingerprint or {})
        reopt = await profile_manager.reoptimize_profile(p2.id)
        assert reopt is not None
        assert reopt.isolation_config["policy_version"] == BUILTIN_POLICY.policy_version
        assert reopt.fingerprint["webgl_renderer"] == hw_before["webgl_renderer"]
    finally:
        get_settings.cache_clear()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink root resolution test")
@pytest.mark.asyncio
async def test_profile_lock_handles_symlinked_root_and_rejects_planted_lock_symlink(tmp_path):
    from camoufox_pm.core.fs_permissions import PermissionProtectionError
    from camoufox_pm.core.process_lock import profile_lock

    real_root = tmp_path / "real-data"
    real_root.mkdir()
    alias_root = tmp_path / "alias-data"
    alias_root.symlink_to(real_root, target_is_directory=True)

    fake_manager = SimpleNamespace(
        storage=SimpleNamespace(db=SimpleNamespace(db_path=str(alias_root / "app.db"))),
        data_dir=alias_root,
    )
    async with profile_lock(fake_manager, "prof-1"):
        assert (real_root / "profile-locks" / "prof-1.lock").is_file()

    decoy = tmp_path / "decoy.lock"
    decoy.write_text("decoy")
    planted = real_root / "profile-locks" / "prof-2.lock"
    planted.symlink_to(decoy)
    with pytest.raises(PermissionProtectionError):
        async with profile_lock(fake_manager, "prof-2"):
            pass


@pytest.mark.asyncio
async def test_probe_risk_target_handles_405_fallback_403_reachable_and_sanitized_errors():
    import httpx

    from camoufox_pm.core.network_probe import probe_risk_target

    seen_methods: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_methods.append((request.method, request.url.host))
        if request.url.host == "claude.ai":
            return httpx.Response(403)
        if request.url.host == "intercom.io":
            if request.method == "HEAD":
                return httpx.Response(405)
            return httpx.Response(200)
        if request.url.host == "timeout.example":
            raise httpx.ReadTimeout("timed out connecting via http://user:secretpw@127.0.0.1:9999")
        raise httpx.ProxyError("proxy rejected http://user:secretpw@127.0.0.1:9999")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        r_cf = await probe_risk_target(client, "claude.ai", "core")
        assert r_cf["reachable"] is True
        assert r_cf["status_code"] == 403

        r_405 = await probe_risk_target(client, "intercom.io", "antifraud_telemetry")
        assert r_405["reachable"] is True
        assert r_405["status_code"] == 200
        assert ("HEAD", "intercom.io") in seen_methods
        assert ("GET", "intercom.io") in seen_methods

        r_timeout = await probe_risk_target(client, "timeout.example", "core")
        assert r_timeout["reachable"] is False
        assert r_timeout["error"] == "timeout"
        assert "secretpw" not in str(r_timeout)

        r_proxy = await probe_risk_target(client, "broken.example", "challenge_kyc")
        assert r_proxy["reachable"] is False
        assert r_proxy["error"] == "proxy_error"
        assert "secretpw" not in str(r_proxy)
