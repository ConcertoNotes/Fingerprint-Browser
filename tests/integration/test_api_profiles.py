"""Integration tests for the profiles API."""

from datetime import datetime

import pytest

from camoufox_pm.api.dependencies import get_profile_manager
from camoufox_pm.core.isolation.chrome_presets import DEFAULT_CHROME_MAJOR
from camoufox_pm.core.models import UsageStats


@pytest.mark.asyncio
async def test_create_and_get_profile(client):
    created = await client.post("/api/profiles", json={"name": "acct-1"})
    assert created.status_code == 201
    profile_id = created.json()["id"]

    fetched = await client.get(f"/api/profiles/{profile_id}")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "acct-1"


@pytest.mark.asyncio
async def test_list_profiles_paginated(client):
    for i in range(3):
        await client.post("/api/profiles", json={"name": f"p{i}"})
    listed = await client.get("/api/profiles", params={"page": 1, "per_page": 2})
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 3
    assert len(body["profiles"]) == 2
    assert body["has_next"] is True


@pytest.mark.asyncio
async def test_search_profiles_by_name(client):
    await client.post("/api/profiles", json={"name": "facebook-main"})
    await client.post("/api/profiles", json={"name": "twitter-alt"})
    found = await client.get("/api/profiles", params={"search": "facebook"})
    names = [p["name"] for p in found.json()["profiles"]]
    assert names == ["facebook-main"]


@pytest.mark.asyncio
async def test_update_profile(client):
    created = await client.post("/api/profiles", json={"name": "before"})
    profile_id = created.json()["id"]
    updated = await client.put(f"/api/profiles/{profile_id}", json={"name": "after"})
    assert updated.status_code == 200
    assert updated.json()["name"] == "after"


@pytest.mark.asyncio
async def test_delete_profile(client):
    created = await client.post("/api/profiles", json={"name": "temp"})
    profile_id = created.json()["id"]
    deleted = await client.delete(f"/api/profiles/{profile_id}")
    assert deleted.status_code == 200
    missing = await client.get(f"/api/profiles/{profile_id}")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_unknown_profile_returns_404(client):
    response = await client.get("/api/profiles/does-not-exist")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_partial_browser_settings_update_keeps_the_rest(client):
    """Editing one field must not reset the rest of the generated fingerprint."""
    created = await client.post("/api/profiles", json={"name": "fp"})
    profile_id = created.json()["id"]
    original = created.json()["browser_settings"]

    updated = await client.put(
        f"/api/profiles/{profile_id}",
        json={"browser_settings": {"timezone": "Asia/Tokyo"}},
    )
    assert updated.status_code == 200
    settings = updated.json()["browser_settings"]

    assert settings["timezone"] == "Asia/Tokyo"
    # Values the client never sent must survive untouched.
    assert settings["screen"] == original["screen"]
    assert settings["locale"] == original["locale"]
    assert settings["device_memory"] == original["device_memory"]


@pytest.mark.asyncio
async def test_create_keeps_generated_values_for_omitted_fields(client):
    """Omitting an optional field must leave the generated fingerprint intact.

    The web form omits blanks on create; sending nulls instead produced profiles
    with no hardware and no screen. Geography is the deliberate exception — it is
    left unset so it follows the proxy — so it is asserted absent here.
    """
    created = await client.post(
        "/api/profiles",
        json={
            "name": "generated",
            "generate_fingerprint": True,
            "browser_settings": {"os": "windows", "window_width": 1280, "window_height": 720},
        },
    )
    assert created.status_code == 201
    settings = created.json()["browser_settings"]

    assert settings["os"] == "windows"
    assert settings["screen"], "screen should have been generated"
    assert settings["hardware_concurrency"], "hardware_concurrency should have been generated"
    assert settings["timezone"] == "America/Los_Angeles"
    assert settings["languages"] == ["en-US", "en"]
    assert settings["geolocation"] is None, "coordinates would turn the IP lookup off"


@pytest.mark.asyncio
async def test_create_keeps_the_notes_it_was_given(client):
    """Notes were accepted by the schema, then overwritten with a timestamp."""
    created = await client.post("/api/profiles", json={"name": "noted", "notes": "keep me"})
    assert created.json()["notes"] == "keep me"

    blank = await client.post("/api/profiles", json={"name": "unnoted"})
    assert blank.json()["notes"] is None


@pytest.mark.asyncio
async def test_explicit_null_clears_a_generated_value(client):
    """An explicit null is a deliberate 'none' — used by the geolocation switch."""
    created = await client.post(
        "/api/profiles",
        json={"name": "no-geo", "browser_settings": {"os": "windows", "geolocation": None}},
    )
    assert created.json()["browser_settings"]["geolocation"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,payload", [("notes", None), ("group", None), ("proxy_config", None)]
)
async def test_explicit_null_clears_the_field(client, field, payload):
    """Clearing a field must persist rather than silently report success."""
    created = await client.post(
        "/api/profiles",
        json={
            "name": "clearable",
            "notes": "keep me",
            "proxy_config": {"type": "http", "server": "1.2.3.4:8080"},
        },
    )
    profile_id = created.json()["id"]
    await client.put(f"/api/profiles/{profile_id}", json={"group": "g1"})

    updated = await client.put(f"/api/profiles/{profile_id}", json={field: payload})
    assert updated.status_code == 200
    assert updated.json()[field] is None


@pytest.mark.asyncio
async def test_omitted_fields_are_left_alone(client):
    """The flip side: not sending a field must not clear it."""
    created = await client.post(
        "/api/profiles",
        json={
            "name": "untouched",
            "notes": "keep me",
            "proxy_config": {"type": "http", "server": "1.2.3.4:8080"},
        },
    )
    profile_id = created.json()["id"]

    updated = await client.put(f"/api/profiles/{profile_id}", json={"name": "renamed"})
    assert updated.status_code == 200
    body = updated.json()
    assert body["name"] == "renamed"
    assert body["notes"] == "keep me"
    assert body["proxy_config"]["server"] == "1.2.3.4:8080"


@pytest.mark.asyncio
async def test_a_profile_round_trips_through_an_archive(client):
    """Export then import must reproduce the profile, with an id of its own."""
    created = await client.post(
        "/api/profiles",
        json={
            "name": "warmed-up",
            "notes": "months of history",
            "browser_settings": {"os": "macos", "timezone": "Europe/Berlin"},
            "proxy_config": {
                "type": "http",
                "server": "1.2.3.4:8080",
                "username": "fixture-user",
                "password": "secret",
            },
        },
    )
    original = created.json()

    exported = await client.get(f"/api/profiles/{original['id']}/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"] == "application/zip"
    assert "warmed-up.camoufox.zip" in exported.headers["content-disposition"]

    imported = await client.post(
        "/api/profiles/import",
        files={"file": ("warmed-up.camoufox.zip", exported.content, "application/zip")},
    )
    assert imported.status_code == 201
    restored = imported.json()

    assert restored["name"] == "warmed-up"
    assert restored["notes"] == "months of history"
    assert restored["browser_settings"]["timezone"] == "Europe/Berlin"
    assert restored["proxy_config"]["server"] == "1.2.3.4:8080"
    assert restored["id"] != original["id"], "an import must not collide with its source"
    # The source's group id is local to that instance; it must not dangle here.
    assert restored["group"] is None


@pytest.mark.asyncio
async def test_importing_a_pinned_profile_keeps_the_machine(client):
    """The point of the archive: the same identity comes back."""
    from camoufox_pm.api.dependencies import get_profile_manager

    created = await client.post("/api/profiles", json={"name": "pinned-export"})
    manager = get_profile_manager()
    profile = await manager.get_profile(created.json()["id"])
    profile.fingerprint = {
        "navigator.userAgent": "UA/1.0",
        "navigator.hardwareConcurrency": 12,
        "screen.width": 2560,
        "screen.height": 1440,
    }
    await manager.storage.update_profile(profile)

    exported = await client.get(f"/api/profiles/{profile.id}/export")
    imported = await client.post(
        "/api/profiles/import",
        files={"file": ("p.zip", exported.content, "application/zip")},
    )

    fingerprint = imported.json()["fingerprint"]
    assert fingerprint["user_agent"] == "UA/1.0"
    assert fingerprint["hardware_concurrency"] == 12
    assert fingerprint["screen"] == "2560x1440"


@pytest.mark.asyncio
async def test_import_can_rename(client):
    created = await client.post("/api/profiles", json={"name": "original"})
    exported = await client.get(f"/api/profiles/{created.json()['id']}/export")

    imported = await client.post(
        "/api/profiles/import",
        params={"name": "renamed on import"},
        files={"file": ("p.zip", exported.content, "application/zip")},
    )
    assert imported.json()["name"] == "renamed on import"


@pytest.mark.asyncio
async def test_importing_rubbish_is_a_400(client):
    response = await client.post(
        "/api/profiles/import",
        files={"file": ("notes.txt", b"this is not a zip", "text/plain")},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_exporting_an_unknown_profile_is_a_404(client):
    response = await client.get("/api/profiles/does-not-exist/export")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_preset_catalogue_is_listed(client):
    response = await client.get("/api/fingerprints/presets", params={"os": "windows"})
    assert response.status_code == 200
    presets = response.json()["data"]["presets"]
    assert presets, "Camoufox ships real device presets"
    first = presets[0]
    assert first["id"].startswith("windows:")
    assert first["os"] == "windows"
    assert first["screen"]


@pytest.mark.asyncio
async def test_creating_from_a_preset_pins_that_device(client):
    """The pinned machine must be the device the user picked, resolved fully offline."""
    listed = await client.get("/api/fingerprints/presets", params={"os": "windows"})
    preset = listed.json()["data"]["presets"][2]

    created = await client.post(
        "/api/profiles", json={"name": "real-device", "fingerprint_preset": preset["id"]}
    )
    assert created.status_code == 201
    body = created.json()

    assert body["fingerprint"] is not None, "a preset pins the profile straight away"
    assert body["fingerprint"]["screen"] == preset["screen"]
    assert body["fingerprint"]["hardware_concurrency"] == preset["hardware_concurrency"]
    assert body["browser_settings"]["os"] == "windows"


@pytest.mark.asyncio
async def test_explicit_settings_still_beat_the_preset(client):
    listed = await client.get("/api/fingerprints/presets", params={"os": "windows"})
    preset = listed.json()["data"]["presets"][0]

    created = await client.post(
        "/api/profiles",
        json={
            "name": "override-preset",
            "fingerprint_preset": preset["id"],
            "browser_settings": {"hardware_concurrency": 16},
        },
    )
    assert created.json()["fingerprint"]["hardware_concurrency"] == 16


@pytest.mark.asyncio
async def test_preset_pins_offline_without_requiring_browser_binary(client):
    """The curated Chrome preset engine resolves real device presets offline."""
    listed = await client.get("/api/fingerprints/presets", params={"os": "windows"})
    presets = listed.json()["data"]["presets"]
    assert presets, "the catalogue ships with the package"

    response = await client.post(
        "/api/profiles", json={"name": "offline-preset", "fingerprint_preset": presets[0]["id"]}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["fingerprint"] is not None
    assert body["fingerprint_hash"].startswith("FP-")
    assert body["isolation"]["preset_id"] == presets[0]["id"]


@pytest.mark.asyncio
async def test_a_preset_that_cannot_be_pinned_creates_nothing(client, monkeypatch):
    """An invalid or failing preset allocation must abort cleanly without creating a row."""
    before = (await client.get("/api/profiles")).json()["total"]
    response = await client.post(
        "/api/profiles", json={"name": "unpinnable", "fingerprint_preset": "windows:999999"}
    )

    assert response.status_code == 400
    after = (await client.get("/api/profiles")).json()["total"]
    assert after == before, "a profile that could not be pinned must not be created"


@pytest.mark.asyncio
async def test_a_refused_import_leaves_nothing_behind(client, monkeypatch):
    """A rejected archive must not strand a profile directory nobody can see."""
    from camoufox_pm.api.dependencies import get_profile_manager
    from camoufox_pm.core import profile_archive

    manager = get_profile_manager()
    profiles_dir = manager.profiles_dir

    # A real archive with real browser data in it, refused part-way through
    # extraction. Without data files the archive is empty and nothing is refused.
    created = await client.post("/api/profiles", json={"name": "source"})
    source = await manager.get_profile(created.json()["id"])
    source_dir = profiles_dir / f"profile_{source.id}"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "cookies.sqlite").write_bytes(b"C" * 4000)

    exported = await client.get(f"/api/profiles/{source.id}/export")
    monkeypatch.setattr(profile_archive, "_MAX_EXTRACTED_BYTES", 10)

    before = set(profiles_dir.glob("*"))
    response = await client.post(
        "/api/profiles/import", files={"file": ("p.zip", exported.content, "application/zip")}
    )
    assert response.status_code == 400

    after = set(profiles_dir.glob("*"))
    assert after == before, f"refused import left {after - before}"


@pytest.mark.asyncio
async def test_unknown_preset_is_rejected(client):
    response = await client.post(
        "/api/profiles", json={"name": "nope", "fingerprint_preset": "windows:99999"}
    )
    assert response.status_code == 400
    assert "preset" in response.json()["detail"].lower()


STALE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"
)


@pytest.mark.asyncio
async def test_refresh_browser_version_keeps_the_machine(client):
    """A pin must be able to move to the current Chrome version without changing hardware."""
    created = await client.post("/api/profiles", json={"name": "ageing"})
    profile_id = created.json()["id"]

    manager = get_profile_manager()
    profile = await manager.get_profile(profile_id)
    # A pin as it would look after months on an older release.
    profile.fingerprint = {
        "user_agent": STALE_UA,
        "platform": "Win32",
        "hardware_concurrency": 12,
        "screen_width": 2560,
        "screen_height": 1440,
        "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 980 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "canvas_seed": "a1b2c3d4e5f60718",
        "audio_seed": "1807f6e5d4c3b2a1",
        "webgl_seed": "0011223344556677",
    }
    await manager.storage.update_profile(profile)

    stale = (await client.get(f"/api/profiles/{profile_id}")).json()["fingerprint"]
    assert stale["browser_major"] == 110
    assert stale["browser_outdated"] is True

    response = await client.post(f"/api/profiles/{profile_id}/refresh-browser")
    assert response.status_code == 200
    refreshed = response.json()["fingerprint"]

    assert refreshed["browser_major"] == DEFAULT_CHROME_MAJOR
    assert refreshed["browser_outdated"] is False
    # The device is unchanged.
    assert refreshed["screen"] == "2560x1440"
    assert refreshed["hardware_concurrency"] == 12
    assert "GeForce GTX 980" in refreshed["gpu"]

    stored = await manager.get_profile(profile_id)
    assert stored.fingerprint["canvas_seed"] == "a1b2c3d4e5f60718"
    assert stored.fingerprint["audio_seed"] == "1807f6e5d4c3b2a1"


WINDOWS_PIN = {
    "user_agent": STALE_UA,
    "platform": "Win32",
    "hardware_concurrency": 12,
    "screen_width": 2560,
    "screen_height": 1440,
    "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 980 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "canvas_seed": "a1b2c3d4e5f60718",
    "audio_seed": "1807f6e5d4c3b2a1",
    "webgl_seed": "0011223344556677",
}


async def _pinned_windows_profile_set_to(client, os_name: str) -> str:
    """A profile whose pin says Windows while its setting says something else."""
    created = await client.post("/api/profiles", json={"name": f"drifted-{os_name}"})
    profile_id = created.json()["id"]
    manager = get_profile_manager()
    profile = await manager.get_profile(profile_id)
    profile.fingerprint = dict(WINDOWS_PIN)
    profile.browser_settings.os = os_name
    await manager.storage.update_profile(profile)
    return profile_id


@pytest.mark.asyncio
async def test_a_setting_that_disagrees_with_the_pin_is_reported(client):
    """Changing the OS of a pinned profile is allowed, but must not stay silent."""
    profile_id = await _pinned_windows_profile_set_to(client, "windows")

    updated = await client.put(f"/api/profiles/{profile_id}", json={"browser_os": "macos"})
    assert updated.status_code == 200
    summary = updated.json()["fingerprint"]
    assert summary["settings_os"] == "macos"
    assert summary["pinned_os"] == "windows"
    assert summary["os_mismatch"] is True


@pytest.mark.asyncio
async def test_keeping_the_machine_moves_the_setting_and_nothing_else(client):
    """The cheap way out: the profile is the computer it has always been."""
    profile_id = await _pinned_windows_profile_set_to(client, "macos")

    response = await client.post(
        f"/api/profiles/{profile_id}/reconcile-os", json={"keep_machine": True}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["browser_settings"]["os"] == "windows"
    assert body["fingerprint"]["os_mismatch"] is False

    stored = await get_profile_manager().get_profile(profile_id)
    assert stored.fingerprint == WINDOWS_PIN, "no fingerprint may change on this path"


@pytest.mark.asyncio
async def test_keeping_the_setting_pins_a_machine_for_it(client):
    """The expensive way out: new hardware, allocated offline for the chosen OS."""
    profile_id = await _pinned_windows_profile_set_to(client, "macos")

    response = await client.post(
        f"/api/profiles/{profile_id}/reconcile-os", json={"keep_machine": False}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["fingerprint"]["pinned_os"] == "macos"
    assert body["fingerprint"]["os_mismatch"] is False
    assert body["browser_settings"]["screen"] == body["fingerprint"]["screen"]

    stored = await get_profile_manager().get_profile(profile_id)
    assert stored.fingerprint["canvas_seed"] != WINDOWS_PIN["canvas_seed"]
    assert stored.fingerprint["platform"] == "MacIntel"


@pytest.mark.asyncio
async def test_reconciling_a_profile_that_already_agrees_is_refused(client):
    """Otherwise a stale UI could replace the hardware of a healthy profile."""
    profile_id = await _pinned_windows_profile_set_to(client, "windows")
    response = await client.post(
        f"/api/profiles/{profile_id}/reconcile-os", json={"keep_machine": False}
    )
    assert response.status_code == 400
    assert "already set to windows" in response.json()["detail"]


@pytest.mark.asyncio
async def test_reconciling_needs_a_pin_and_a_profile(client):
    created = await client.post(
        "/api/profiles", json={"name": "unpinned", "generate_fingerprint": False}
    )
    unpinned = await client.post(
        f"/api/profiles/{created.json()['id']}/reconcile-os", json={"keep_machine": True}
    )
    assert unpinned.status_code == 400
    assert "no pinned machine" in unpinned.json()["detail"]

    missing = await client.post(
        "/api/profiles/does-not-exist/reconcile-os", json={"keep_machine": True}
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_clear_geography_hands_the_location_back_to_the_proxy(client):
    """A profile from before geography followed the proxy can be freed to do so."""
    from camoufox_pm.api.dependencies import get_profile_manager

    old = await client.post(
        "/api/profiles",
        json={
            "name": "shanghai",
            "browser_settings": {
                "timezone": "Asia/Shanghai",
                "geolocation": {"lat": 31.23, "lon": 121.47},
                "languages": ["zh-CN", "zh"],
            },
        },
    )
    profile_id = old.json()["id"]

    response = await client.post(
        "/api/profiles/clear-geography", json={"profile_ids": [profile_id]}
    )
    assert response.status_code == 200
    assert response.json() == {"cleared": [profile_id], "unchanged": [], "not_found": []}

    settings = (await client.get(f"/api/profiles/{profile_id}")).json()["browser_settings"]
    assert settings["timezone"] is None
    assert settings["geolocation"] is None
    assert settings["languages"] == ["zh-CN", "zh"], "languages are identity, not geography"

    profile = await get_profile_manager().get_profile(profile_id)
    options = profile.to_camoufox_launch_options()
    assert options["geoip"] is True, "the whole point: Camoufox derives it from the exit address"


@pytest.mark.asyncio
async def test_clear_geography_reports_each_profile_it_was_given(client):
    """A selection is the unit here, so partial answers have to be readable."""
    with_geo = await client.post(
        "/api/profiles", json={"name": "berlin", "browser_settings": {"timezone": "Europe/Berlin"}}
    )
    without = await client.post(
        "/api/profiles",
        json={"name": "follows-the-proxy", "isolation": {"timezone_mode": "follow_exit"}},
    )

    response = await client.post(
        "/api/profiles/clear-geography",
        json={"profile_ids": [with_geo.json()["id"], without.json()["id"], "nope"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["cleared"] == [with_geo.json()["id"]]
    assert body["unchanged"] == [without.json()["id"]]
    assert body["not_found"] == ["nope"]


@pytest.mark.asyncio
async def test_clear_geography_needs_at_least_one_profile(client):
    response = await client.post("/api/profiles/clear-geography", json={"profile_ids": []})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_refresh_browser_version_needs_a_pin_first(client):
    """Refreshing an unpinned profile would silently invent a machine."""
    created = await client.post(
        "/api/profiles", json={"name": "unpinned", "generate_fingerprint": False}
    )
    response = await client.post(f"/api/profiles/{created.json()['id']}/refresh-browser")
    assert response.status_code == 400
    assert "launch it once" in response.json()["detail"]


@pytest.mark.asyncio
async def test_refresh_browser_version_on_a_missing_profile_is_404(client):
    response = await client.post("/api/profiles/does-not-exist/refresh-browser")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_fingerprint_is_pinned_immediately_and_deduplicated_on_create(client):
    """Stage 2 allocates and pins a unique real device immediately on creation."""
    first = await client.post("/api/profiles", json={"name": "pinned-1"})
    second = await client.post("/api/profiles", json={"name": "pinned-2"})
    assert first.status_code == 201 and second.status_code == 201
    b1, b2 = first.json(), second.json()
    assert b1["fingerprint"] is not None and b2["fingerprint"] is not None
    assert b1["fingerprint_hash"].startswith("FP-")
    assert b1["fingerprint_hash"] != b2["fingerprint_hash"]
    assert b1["isolation"]["timezone"] == "America/Los_Angeles"
    assert b1["isolation"]["languages"] == ["en-US", "en"]
    assert b1["isolation"]["install_webrtc_guard"] is True

    unpinned = await client.post(
        "/api/profiles", json={"name": "explicit-unpinned", "generate_fingerprint": False}
    )
    assert unpinned.json()["fingerprint"] is None


@pytest.mark.asyncio
async def test_regenerating_allocates_a_new_pinned_machine(client):
    """Resetting the fingerprint replaces the hardware with a new deduplicated preset."""
    from camoufox_pm.api.dependencies import get_profile_manager

    created = await client.post("/api/profiles", json={"name": "repin"})
    profile_id = created.json()["id"]

    manager = get_profile_manager()
    profile = await manager.get_profile(profile_id)
    profile.fingerprint = {"navigator.userAgent": "pinned", "screen.width": 1920}
    await manager.storage.update_profile(profile)

    stored = await client.get(f"/api/profiles/{profile_id}")
    assert stored.json()["fingerprint"]["user_agent"] == "pinned"

    reset = await client.post(f"/api/profiles/{profile_id}/reset-fingerprint")
    assert reset.status_code == 200
    assert reset.json()["fingerprint"] is not None
    assert reset.json()["fingerprint"]["user_agent"] != "pinned"
    assert reset.json()["fingerprint_hash"].startswith("FP-")


@pytest.mark.asyncio
async def test_stage_two_isolation_endpoints_and_atomic_node_bind(client, monkeypatch, tmp_path):
    """Preview, preset listing, atomic create+bind, 10-point audit, and reoptimize."""
    from cryptography.fernet import Fernet

    from camoufox_pm.config import get_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CPM_SECRET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    try:
        # 1. Isolation preview
        preview = await client.get("/api/v1/profiles/isolation/preview", params={"os": "macos"})
        assert preview.status_code == 200
        prev_data = preview.json()["data"]
        prev_iso = prev_data["isolation"]
        assert prev_iso["os"] == "macos"
        assert prev_iso["timezone"] == "America/Los_Angeles"
        assert prev_iso["languages"] == ["en-US", "en"]
        assert prev_iso["install_webrtc_guard"] is True
        assert prev_iso["fingerprint_hash"].startswith("FP-")
        assert prev_data["summary"]["fingerprint_hash"] == prev_iso["fingerprint_hash"]

        # 2. Filtered presets listing
        presets_res = await client.get(
            "/api/v1/profiles/fingerprint-presets", params={"os": "macos"}
        )
        assert presets_res.status_code == 200
        presets = presets_res.json()["data"]["presets"]
        assert presets and all(p["os"] == "macos" for p in presets)
        assert all(p["in_use"] is False for p in presets)

        # 3. Create subscription + atomic profile creation with node_id
        sub_res = await client.post(
            "/api/v1/network/subscriptions",
            json={
                "name": "Atomic Sub",
                "content": (
                    "proxies:\n"
                    '  - {name: "US-LA", type: ss, server: la.example, port: 8388, '
                    'cipher: aes-128-gcm, password: "pw"}\n'
                ),
            },
        )
        assert sub_res.status_code == 201
        nodes_res = await client.get("/api/v1/network/nodes")
        node_id = nodes_res.json()["data"]["nodes"][0]["id"]

        created = await client.post(
            "/api/v1/profiles",
            json={
                "name": "Atomic LA Profile",
                "node_id": node_id,
                "isolation": {
                    "os": "macos",
                    "preset_id": presets[0]["id"],
                    "tier": "strict",
                    "webrtc_policy": "block",
                    "canvas_policy": "stable_hardware",
                    "install_webrtc_guard": True,
                },
            },
        )
        assert created.status_code == 201
        body = created.json()
        profile_id = body["id"]
        assert body["isolation"]["preset_id"] == presets[0]["id"]
        assert body["isolation"]["timezone"] == "America/Los_Angeles"

        # Binding should exist immediately and nodes list should report bound_count=1.
        binding = (await client.get(f"/api/v1/network/bindings/{profile_id}")).json()["data"][
            "binding"
        ]
        assert binding is not None and binding["node_id"] == node_id
        nodes_after = (await client.get("/api/v1/network/nodes")).json()["data"]["nodes"]
        assert nodes_after[0]["bound_count"] == 1
        assert profile_id in nodes_after[0]["bound_profile_ids"]

        # Preset should now be marked in_use=True.
        presets_after = (
            await client.get("/api/v1/profiles/fingerprint-presets", params={"os": "macos"})
        ).json()["data"]["presets"]
        assert next(p for p in presets_after if p["id"] == presets[0]["id"])["in_use"] is True

        # 4. 11-point Isolation Audit
        audit = await client.post(f"/api/v1/profiles/{profile_id}/audit")
        assert audit.status_code == 200
        report = audit.json()["data"]
        assert report["profile_id"] == profile_id
        assert len(report["checks"]) == 11
        assert {c["key"] for c in report["checks"]} >= {
            "webrtc_guard_addon",
            "full_traffic_tunnel",
            "fingerprint_unique",
            "risk_domains_enforced",
        }

        # 5. Reoptimize preserves hardware identity
        reopt = await client.post(f"/api/v1/profiles/{profile_id}/reoptimize")
        assert reopt.status_code == 200
        assert reopt.json()["fingerprint_hash"] == body["fingerprint_hash"]
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_invalid_browser_settings_are_rejected_not_500(client):
    """browser_settings is a free-form dict, so bad values must read as 422."""
    created = await client.post("/api/profiles", json={"name": "bad"})
    profile_id = created.json()["id"]

    response = await client.put(
        f"/api/profiles/{profile_id}",
        json={"browser_settings": {"hardware_concurrency": "many"}},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_webrtc_mode_is_coerced_to_the_enum(client):
    """A raw string used to survive as str and trip response serialization."""
    created = await client.post(
        "/api/profiles",
        json={"name": "rtc", "browser_settings": {"os": "windows", "webrtc_mode": "none"}},
    )
    assert created.status_code == 201
    assert created.json()["browser_settings"]["webrtc_mode"] == "none"


@pytest.mark.asyncio
async def test_flattened_browser_fields_still_apply(client):
    """The browser_* form of the update request keeps working alongside the nested one."""
    created = await client.post("/api/profiles", json={"name": "flat"})
    profile_id = created.json()["id"]

    updated = await client.put(
        f"/api/profiles/{profile_id}",
        json={"browser_os": "linux", "browser_hardware_concurrency": 12},
    )
    assert updated.status_code == 200
    settings = updated.json()["browser_settings"]
    assert settings["os"] == "linux"
    assert settings["hardware_concurrency"] == 12


@pytest.mark.asyncio
async def test_profile_statistics_report_the_sessions_that_happened(client):
    """Regression: the endpoint read keys the manager did not return, so it
    always answered zero sessions, no last session and no actions — whatever the
    profile's history actually was.
    """
    created = await client.post("/api/profiles", json={"name": "busy"})
    profile_id = created.json()["id"]
    manager = get_profile_manager()
    for _ in range(3):
        await manager.storage.log_usage(UsageStats(profile_id=profile_id, action="launch_browser"))
    profile = await manager.get_profile(profile_id)
    profile.last_used = datetime(2026, 1, 2, 3, 4, 5)
    await manager.storage.update_profile(profile)

    body = (await client.get(f"/api/profiles/{profile_id}/stats")).json()

    assert body["total_sessions"] == 3
    assert body["last_session"].startswith("2026-01-02T03:04:05")
    assert [a["action"] for a in body["actions"]].count("launch_browser") == 3
    assert body["success_rate"] == 1.0


@pytest.mark.asyncio
async def test_statistics_for_an_unknown_profile_are_a_404(client):
    assert (await client.get("/api/profiles/nope/stats")).status_code == 404


@pytest.mark.asyncio
async def test_an_import_can_name_the_profile_from_the_form(client, tmp_path):
    """Every other part of this request travels in the multipart body.

    A client author reaches for the form field first, and used to get no error
    and no rename because only the query parameter was read.
    """
    created = await client.post("/api/v1/profiles", json={"name": "source"})
    exported = await client.get(f"/api/v1/profiles/{created.json()['id']}/export")
    archive = exported.content

    from_form = await client.post(
        "/api/v1/profiles/import",
        files={"file": ("a.zip", archive, "application/zip")},
        data={"name": "named-by-form"},
    )
    from_query = await client.post(
        "/api/v1/profiles/import?name=named-by-query",
        files={"file": ("a.zip", archive, "application/zip")},
    )

    assert from_form.json()["name"] == "named-by-form"
    assert from_query.json()["name"] == "named-by-query", "the query form must keep working"


@pytest.mark.asyncio
async def test_clearing_geography_rejects_an_empty_id(client):
    """A client building ids from empty form state used to get a cheerful 200
    reporting "" as not_found, rather than being told the request was wrong."""
    refused = await client.post("/api/v1/profiles/clear-geography", json={"profile_ids": [""]})

    assert refused.status_code == 422


@pytest.mark.asyncio
async def test_exit_ip_drift_guard_blocks_allows_override_and_cleans_up(
    client, monkeypatch, tmp_path
):
    """Bound session prepare/launch tracks last exit IP/country/city, returns 409 on drift
    with clean teardown, guards proxy credentials with desktop token, and permits override."""
    from types import SimpleNamespace

    from cryptography.fernet import Fernet

    from camoufox_pm.config import get_settings
    from camoufox_pm.core import proxy_check
    from camoufox_pm.core.models import ProxyConfig, ProxyType

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CPM_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("CPM_DESKTOP_TOKEN", "test-electron-secret-token")
    get_settings.cache_clear()
    try:
        sub = await client.post(
            "/api/v1/network/subscriptions",
            json={
                "name": "Drift Test Sub",
                "content": (
                    "proxies:\n"
                    '  - {name: "US-1", type: ss, server: us.example, port: 8388, '
                    'cipher: aes-128-gcm, password: "pw"}\n'
                ),
            },
        )
        assert sub.status_code == 201
        node_id = (await client.get("/api/v1/network/nodes")).json()["data"]["nodes"][0]["id"]

        created = await client.post(
            "/api/v1/profiles",
            json={"name": "Drift Guarded", "node_id": node_id},
        )
        assert created.status_code == 201
        body = created.json()
        profile_id = body["id"]
        assert body["ip_drift_guard"] is True
        assert body["last_exit_ip"] is None

        manager = get_profile_manager()
        closed_runtimes: list[str] = []
        current_exit = {"ip": "203.0.113.10", "country": "US", "city": "Los Angeles"}

        class FakeRuntime:
            def __init__(self, pid: str):
                self.profile_id = pid
                self.node = {"id": node_id, "name": "US-1", "check": {}}
                self.listener = SimpleNamespace(port=28888, username="u", password="p-secret-loop")
                self.browser_proxy = {
                    "server": "http://127.0.0.1:28888",
                    "username": "u",
                    "password": "p-secret-loop",
                }
                self.proxy_config = ProxyConfig(
                    type=ProxyType.HTTP,
                    server="127.0.0.1:28888",
                    username="u",
                    password="p-secret-loop",
                )
                self.engine = SimpleNamespace(alive=True)
                self.exit_ip: str | None = None
                self.exit_country: str | None = None
                self.exit_city: str | None = None

            def mark_running(self):
                pass

            async def close(self):
                closed_runtimes.append(self.profile_id)
                manager.network.runtimes.pop(self.profile_id, None)

        async def fake_prepare(pid: str):
            rt = FakeRuntime(pid)
            manager.network.runtimes[pid] = rt
            return rt

        async def fake_verify_exit(runtime, _timeout):
            runtime.exit_ip = current_exit["ip"]
            return runtime.exit_ip

        def fake_locate(ip: str):
            return SimpleNamespace(
                ip=ip,
                country=current_exit["country"],
                city=current_exit["city"],
                timezone="America/Los_Angeles",
                latitude=34.05,
                longitude=-118.24,
            )

        monkeypatch.setattr(manager.network, "prepare", fake_prepare)
        monkeypatch.setattr(manager.network, "verify_exit", fake_verify_exit)
        monkeypatch.setattr(proxy_check, "locate", fake_locate)

        # 0. Desktop session endpoints require X-Prism-Desktop-Token when configured.
        denied = await client.post(f"/api/v1/profiles/{profile_id}/session/prepare", json={})
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "desktop_token_required"

        desktop_headers = {"X-Prism-Desktop-Token": "test-electron-secret-token"}

        # 1. First session/prepare establishes baseline exit IP, country, city & returns proxy secrets.
        first = await client.post(
            f"/api/v1/profiles/{profile_id}/session/prepare",
            json={},
            headers=desktop_headers,
        )
        assert first.status_code == 200
        session_data = first.json()["data"]
        assert session_data["proxy"]["port"] == 28888
        assert session_data["proxy"]["password"] == "p-secret-loop"
        assert session_data["exit_ip"] == "203.0.113.10"
        assert session_data["exit_country"] == "US"
        assert session_data["exit_city"] == "Los Angeles"
        assert "Electron/" not in session_data["isolation"]["pinned_fingerprint"]["user_agent"]

        fetched = (await client.get(f"/api/v1/profiles/{profile_id}")).json()
        assert fetched["last_exit_ip"] == "203.0.113.10"
        assert fetched["last_exit_country"] == "US"
        assert fetched["last_exit_at"] is not None

        # Release cleanly via Electron session/release endpoint.
        released = await client.post(
            f"/api/v1/profiles/{profile_id}/session/release", headers=desktop_headers
        )
        assert released.status_code == 200
        assert released.json()["data"]["status"] == "closed"
        assert await manager.storage.get_lease(profile_id) == (None, None)

        # 2. Exit IP + country changes -> blocked with 409 exit_ip_drift, runtime closed & lease freed!
        current_exit["ip"] = "198.51.100.77"
        current_exit["country"] = "JP"
        current_exit["city"] = "Tokyo"
        drifted = await client.post(f"/api/v1/profiles/{profile_id}/launch", json={})
        assert drifted.status_code == 409
        err = drifted.json()["error"]
        assert err["code"] == "exit_ip_drift"
        assert err["details"][0] == {
            "previous_ip": "203.0.113.10",
            "previous_country": "US",
            "current_ip": "198.51.100.77",
            "current_country": "JP",
            "country_changed": True,
        }
        assert profile_id in closed_runtimes
        assert await manager.storage.get_lease(profile_id) == (None, None)

        # 3. Confirming with allow_ip_drift=True via /launch succeeds without leaking proxy password.
        confirmed = await client.post(
            f"/api/v1/profiles/{profile_id}/launch", json={"allow_ip_drift": True}
        )
        assert confirmed.status_code == 200
        assert "p-secret-loop" not in confirmed.text
        updated = (await client.get(f"/api/v1/profiles/{profile_id}")).json()
        assert updated["last_exit_ip"] == "198.51.100.77"
        assert updated["last_exit_country"] == "JP"

        # 4. Disabling ip_drift_guard via PUT allows subsequent IP change without confirmation.
        await client.post(f"/api/v1/profiles/{profile_id}/session/release", headers=desktop_headers)
        toggle = await client.put(f"/api/v1/profiles/{profile_id}", json={"ip_drift_guard": False})
        assert toggle.status_code == 200
        assert toggle.json()["ip_drift_guard"] is False

        current_exit["ip"] = "192.0.2.55"
        current_exit["country"] = "DE"
        current_exit["city"] = "Frankfurt"
        unblocked = await client.post(f"/api/v1/profiles/{profile_id}/launch")
        assert unblocked.status_code == 200
        final_prof = (await client.get(f"/api/v1/profiles/{profile_id}")).json()
        assert final_prof["last_exit_ip"] == "192.0.2.55"
        assert final_prof["last_exit_country"] == "DE"
        await client.post(f"/api/v1/profiles/{profile_id}/session/release", headers=desktop_headers)
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_terminal_and_risk_domain_probe_endpoints(client, monkeypatch, tmp_path):
    """Verify POST /profiles/{id}/terminal, POST /profiles/{id}/probe-risk-domains,
    and POST /network/nodes/{node_id}/probe-domains."""
    from types import SimpleNamespace

    from cryptography.fernet import Fernet

    from camoufox_pm.config import get_settings
    from camoufox_pm.core import isolated_terminal
    from camoufox_pm.core.models import ProxyConfig, ProxyType

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CPM_SECRET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    try:
        # Unbound profile -> terminal returns 409, risk probe returns 409 no_bound_node.
        unbound = (await client.post("/api/v1/profiles", json={"name": "Unbound"})).json()
        t_stopped = await client.post(f"/api/v1/profiles/{unbound['id']}/terminal")
        assert t_stopped.status_code == 409
        assert t_stopped.json()["error"]["code"] == "terminal_requires_running_node"

        p_unbound = await client.post(f"/api/v1/profiles/{unbound['id']}/probe-risk-domains")
        assert p_unbound.status_code == 409
        assert p_unbound.json()["error"]["code"] == "no_bound_node"

        # Create node + bound profile.
        sub = await client.post(
            "/api/v1/network/subscriptions",
            json={
                "name": "Risk Sub",
                "content": (
                    "proxies:\n"
                    '  - {name: "US-LA", type: ss, server: la.example, port: 8388, '
                    'cipher: aes-128-gcm, password: "pw"}\n'
                ),
            },
        )
        assert sub.status_code == 201
        node_id = (await client.get("/api/v1/network/nodes")).json()["data"]["nodes"][0]["id"]
        bound = (
            await client.post("/api/v1/profiles", json={"name": "Bound", "node_id": node_id})
        ).json()
        profile_id = bound["id"]

        # Attach fake live runtime so both terminal and risk probes reuse it offline.
        manager = get_profile_manager()
        secret_pw = "SuperSecretLoopback99"
        manager.network.runtimes[profile_id] = SimpleNamespace(
            profile_id=profile_id,
            node={
                "id": node_id,
                "name": "US-LA",
                "check": {"exit_ip": "203.0.113.5", "country_iso": "US"},
            },
            listener=SimpleNamespace(port=29999, username="loop-u", password=secret_pw),
            proxy_config=ProxyConfig(
                type=ProxyType.HTTP, server="127.0.0.1:29999", username="loop-u", password=secret_pw
            ),
            engine=SimpleNamespace(alive=True),
            exit_ip="203.0.113.5",
            exit_country="US",
        )

        # Mock native terminal spawn + risk domain prober.
        spawned_paths = []
        monkeypatch.setattr(
            isolated_terminal,
            "spawn_terminal",
            lambda script_path, _cmd, **_kw: spawned_paths.append(script_path),
        )

        term_res = await client.post(f"/api/v1/profiles/{profile_id}/terminal")
        assert term_res.status_code == 200
        term_data = term_res.json()["data"]
        assert term_data["profile_id"] == profile_id
        assert term_data["proxy_port"] == 29999
        assert term_data["exit_ip"] == "203.0.113.5"
        assert term_data["exit_country"] == "US"
        assert term_data["launched"] is True
        assert secret_pw not in term_res.text
        assert spawned_paths and spawned_paths[0].is_file()

        async def fake_prober(_proxy, domain: str, category: str, _timeout: float):
            if domain == "withpersona.com":
                return {
                    "domain": domain,
                    "category": category,
                    "reachable": False,
                    "status_code": None,
                    "latency_ms": 500,
                    "error": "timeout",
                }
            return {
                "domain": domain,
                "category": category,
                "reachable": True,
                "status_code": 200,
                "latency_ms": 42,
                "error": None,
            }

        manager.network_probe.risk_domain_prober = fake_prober

        node_probe = await client.post(f"/api/v1/network/nodes/{node_id}/probe-domains")
        assert node_probe.status_code == 200
        node_report = node_probe.json()["data"]
        assert node_report["node_id"] == node_id
        assert node_report["exit_ip"] == "203.0.113.5"
        assert node_report["total_enforced_domains"] == 38
        assert node_report["total_enforced_cidrs"] == 4
        assert node_report["summary"] == {"ok": 9, "total": 10}
        assert len(node_report["probes"]) == 10

        prof_probe = await client.post(f"/api/v1/profiles/{profile_id}/probe-risk-domains")
        assert prof_probe.status_code == 200
        prof_report = prof_probe.json()["data"]
        assert prof_report["node_id"] == node_id
        assert prof_report["summary"] == {"ok": 9, "total": 10}
        assert set(prof_report["risk_probe_groups"]) == {
            "core",
            "antifraud_telemetry",
            "challenge_kyc",
        }
    finally:
        get_settings.cache_clear()
