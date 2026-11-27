"""Cloning, deleting, refreshing, and session preparation in ProfileManager."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from camoufox_pm.core.isolation import DEFAULT_CHROME_MAJOR, browser_major

PINNED = {
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "chrome_major": 120,
    "platform": "Win32",
    "platform_version": "15.0.0",
    "architecture": "x86",
    "bitness": "64",
    "model": "",
    "mobile": False,
    "screen_width": 2560,
    "screen_height": 1440,
    "avail_width": 2560,
    "avail_height": 1392,
    "color_depth": 24,
    "device_pixel_ratio": 1,
    "hardware_concurrency": 12,
    "device_memory": 8,
    "webgl_vendor": "Google Inc. (NVIDIA)",
    "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "canvas_seed": "a" * 32,
    "audio_seed": "b" * 32,
    "webgl_seed": "c" * 32,
    "fonts": ["Segoe UI", "Arial"],
}


async def pin(profile_manager, profile):
    profile.fingerprint = dict(PINNED)
    await profile_manager.storage.update_profile(profile)
    return profile


@pytest.mark.asyncio
async def test_a_clone_carries_the_browser_data_into_a_directory_of_its_own(profile_manager):
    source = await profile_manager.create_profile(name="warm")
    source_dir = Path(source.get_storage_path(str(profile_manager.profiles_dir)))
    (source_dir / "cookies.json").write_bytes(b"logged in")

    clone = await profile_manager.clone_profile(source.id, "warm copy")

    assert clone is not None and clone.id != source.id
    clone_dir = Path(clone.get_storage_path(str(profile_manager.profiles_dir)))
    assert clone_dir != source_dir
    assert (clone_dir / "cookies.json").read_bytes() == b"logged in"
    assert (source_dir / "cookies.json").exists()


@pytest.mark.asyncio
async def test_a_clone_is_a_different_machine(profile_manager):
    source = await pin(profile_manager, await profile_manager.create_profile(name="source"))

    clone = await profile_manager.clone_profile(source.id, "copy")

    assert clone is not None
    assert clone.fingerprint is not None
    assert clone.fingerprint != PINNED
    assert clone.fingerprint_hash and clone.fingerprint_hash != source.fingerprint_hash
    assert (await profile_manager.get_profile(source.id)).fingerprint == PINNED


@pytest.mark.asyncio
async def test_a_clone_can_be_asked_to_keep_the_machine(profile_manager):
    source = await pin(profile_manager, await profile_manager.create_profile(name="source"))

    clone = await profile_manager.clone_profile(source.id, "copy", regenerate_fingerprint=False)

    assert clone is not None
    assert clone.fingerprint == PINNED
    assert clone.browser_settings == source.browser_settings


@pytest.mark.asyncio
async def test_cloning_an_unknown_profile_returns_nothing(profile_manager):
    assert await profile_manager.clone_profile("nope", "copy") is None


@pytest.mark.asyncio
async def test_deleting_a_profile_removes_its_directory_directly(profile_manager):
    profile = await profile_manager.create_profile(name="gone")
    directory = Path(profile.get_storage_path(str(profile_manager.profiles_dir)))
    (directory / "cookies.json").write_bytes(b"session")

    assert await profile_manager.delete_profile(profile.id) is True
    assert not directory.exists()
    assert await profile_manager.get_profile(profile.id) is None


@pytest.mark.asyncio
async def test_the_data_can_be_kept_when_the_profile_is_deleted(profile_manager):
    profile = await profile_manager.create_profile(name="gone")
    directory = Path(profile.get_storage_path(str(profile_manager.profiles_dir)))
    (directory / "cookies.json").write_bytes(b"session")

    await profile_manager.delete_profile(profile.id, remove_data=False)

    assert (directory / "cookies.json").read_bytes() == b"session"


@pytest.mark.asyncio
async def test_a_profile_whose_directory_is_already_gone_still_deletes(profile_manager):
    profile = await profile_manager.create_profile(name="gone")
    shutil.rmtree(profile.get_storage_path(str(profile_manager.profiles_dir)))

    assert await profile_manager.delete_profile(profile.id) is True


@pytest.mark.asyncio
async def test_deleting_an_unknown_profile_reports_it_rather_than_raising(profile_manager):
    assert await profile_manager.delete_profile("nope") is False


@pytest.mark.asyncio
async def test_deleting_a_group_ungroups_its_profiles_and_keeps_them(profile_manager):
    group = await profile_manager.create_group("Social")
    first = await profile_manager.create_profile(name="a", group=group["id"])
    second = await profile_manager.create_profile(name="b", group=group["id"])

    assert await profile_manager.delete_group(group["id"]) is True

    remaining = await profile_manager.list_profiles()
    assert {p.id for p in remaining} == {first.id, second.id}
    assert [p.group for p in remaining] == [None, None]


@pytest.mark.asyncio
async def test_deleting_an_unknown_group_reports_it(profile_manager):
    assert await profile_manager.delete_group("nope") is False


@pytest.mark.asyncio
async def test_a_group_reports_how_many_profiles_it_holds(profile_manager):
    group = await profile_manager.create_group("Social", description="socials")
    await profile_manager.create_profile(name="a", group=group["id"])
    await profile_manager.create_profile(name="b")

    assert (await profile_manager.get_group(group["id"]))["profile_count"] == 1
    assert [g["profile_count"] for g in await profile_manager.list_groups()] == [1]


@pytest.mark.asyncio
async def test_bulk_update_counts_only_the_profiles_that_exist(profile_manager):
    first = await profile_manager.create_profile(name="a")
    second = await profile_manager.create_profile(name="b")

    updated = await profile_manager.bulk_update_profiles(
        [first.id, "missing", second.id], {"status": "inactive"}
    )

    assert updated == 2
    assert [p.status for p in await profile_manager.list_profiles()] == ["inactive", "inactive"]


@pytest.mark.asyncio
async def test_launching_an_unknown_profile_says_so(profile_manager):
    with pytest.raises(ValueError, match="not found"):
        await profile_manager.launch_browser("nope")


@pytest.mark.asyncio
async def test_prepare_and_release_session_are_idempotent_and_replay_pin(profile_manager):
    profile = await pin(profile_manager, await profile_manager.create_profile(name="p"))

    first = await profile_manager.prepare_session(profile.id)
    assert first["status"] == "ready"
    assert first["isolation"]["pinned_fingerprint"]["webgl_renderer"] == PINNED["webgl_renderer"]
    assert first["isolation"]["pinned_fingerprint"]["screen_width"] == 2560
    assert first["isolation"]["pinned_fingerprint"]["canvas_seed"] == "a" * 32

    again = await profile_manager.launch_browser(profile.id)
    assert again["status"] == "already_running"

    released = await profile_manager.release_session(profile.id)
    assert released["status"] == "closed"
    assert not profile_manager.browser_sessions.is_running(profile.id)


@pytest.mark.asyncio
async def test_first_session_prepare_pins_unpinned_profile(profile_manager):
    profile = await profile_manager.create_profile(name="unpinned", generate_fingerprint=False)
    assert profile.fingerprint is None

    payload = await profile_manager.prepare_session(profile.id)
    assert payload["isolation"]["pinned_fingerprint"]["chrome_major"] == DEFAULT_CHROME_MAJOR

    stored = await profile_manager.get_profile(profile.id)
    assert stored is not None and stored.fingerprint is not None
    assert stored.last_used is not None
    await profile_manager.release_session(profile.id)


@pytest.mark.asyncio
async def test_profile_explicit_settings_beat_the_pin(profile_manager):
    profile = await profile_manager.create_profile(
        name="p", browser_settings={"timezone": "Asia/Tokyo", "hardware_concurrency": 4}
    )
    await pin(profile_manager, profile)

    payload = await profile_manager.prepare_session(profile.id)
    assert payload["isolation"]["timezone"] == "Asia/Tokyo"
    assert payload["isolation"]["pinned_fingerprint"]["hardware_concurrency"] == 4
    await profile_manager.release_session(profile.id)


@pytest.mark.asyncio
async def test_closing_a_browser_that_is_not_running_says_so(profile_manager):
    result = await profile_manager.close_browser("nope")
    assert result["status"] == "not_running"


@pytest.mark.asyncio
async def test_rotating_the_fingerprint_allocates_a_new_pinned_machine(profile_manager):
    profile = await pin(profile_manager, await profile_manager.create_profile(name="p"))
    old_hash = profile.fingerprint_hash

    rotated = await profile_manager.rotate_profile_fingerprint(profile.id)

    assert rotated is not None
    assert rotated.fingerprint is not None
    assert rotated.fingerprint != PINNED
    assert rotated.fingerprint_hash and rotated.fingerprint_hash != old_hash


@pytest.mark.asyncio
async def test_refreshing_a_browser_version_without_a_pin_says_to_launch_first(profile_manager):
    profile = await profile_manager.create_profile(name="p", generate_fingerprint=False)

    with pytest.raises(ValueError, match="no pinned machine"):
        await profile_manager.refresh_browser_version(profile.id)


@pytest.mark.asyncio
async def test_refreshing_keeps_pinned_os_and_hardware_while_updating_chrome_major(profile_manager):
    """OS dropdown change must not put a macOS UA onto Windows hardware during refresh."""
    profile = await profile_manager.create_profile(name="p", browser_settings={"os": "macos"})
    await pin(profile_manager, profile)

    refreshed = await profile_manager.refresh_browser_version(profile.id)
    assert refreshed is not None and refreshed.fingerprint is not None
    assert browser_major(refreshed.fingerprint) == DEFAULT_CHROME_MAJOR
    assert "Windows NT 10.0" in refreshed.fingerprint["user_agent"]
    assert refreshed.fingerprint["webgl_renderer"] == PINNED["webgl_renderer"]
    assert refreshed.fingerprint["canvas_seed"] == PINNED["canvas_seed"]


@pytest.mark.asyncio
async def test_absolute_paths_from_an_adopted_data_directory_are_re_pointed(
    profile_manager, tmp_path
):
    profile = await profile_manager.create_profile(name="adopted")
    own_dir = profile_manager.profiles_dir.resolve() / f"profile_{profile.id}"
    own_dir.mkdir(parents=True, exist_ok=True)
    old_location = tmp_path / "Old App Name" / "profiles" / f"profile_{profile.id}"
    conn = profile_manager.storage.db._connection
    conn.execute(
        "UPDATE profiles SET storage_path = ? WHERE id = ?", (str(old_location), profile.id)
    )
    conn.commit()

    assert profile_manager._relocate_moved_storage_paths() == 1
    reloaded = await profile_manager.get_profile(profile.id)
    assert reloaded is not None
    assert Path(reloaded.storage_path) == own_dir
    # Idempotent: a second pass changes nothing.
    assert profile_manager._relocate_moved_storage_paths() == 0
