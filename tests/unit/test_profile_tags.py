"""Tag metadata must be bounded, persistent, filterable and safe under leases."""

import json
import sqlite3
from datetime import datetime

import pytest
from pydantic import ValidationError

from camoufox_pm.core.database import StorageManager
from camoufox_pm.core.models import Profile


def test_tags_normalize_unicode_case_whitespace_and_duplicate_labels():
    profile = Profile(
        name="Tagged", tags=[" Work ", "WORK", " Client   A ", "CAFE\u0301", "café", "Straße"]
    )
    assert profile.tags == ["work", "client a", "café", "strasse"]
    assert Profile(name="Old profile").tags == []


@pytest.mark.parametrize(
    "tags",
    [
        [""],
        ["x" * 33],
        [f"tag-{index}" for index in range(21)],
        ["line\nbreak"],
        ["x\u200dy"],
        ["a,b"],
        [123],
        "work",
        None,
    ],
)
def test_invalid_tag_lists_are_rejected(tags):
    with pytest.raises((ValidationError, ValueError)):
        Profile(name="Invalid", tags=tags)


async def test_old_database_migration_retains_identity_and_adds_empty_tags(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute("""CREATE TABLE profiles (
        id TEXT PRIMARY KEY, name TEXT, group_id TEXT, status TEXT,
        browser_settings TEXT, proxy_config TEXT, extensions TEXT, storage_path TEXT,
        notes TEXT, created_at TEXT, updated_at TEXT, last_used TEXT
    )""")
    now = datetime.now().isoformat()
    connection.execute(
        "INSERT INTO profiles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy",
            "Existing",
            None,
            "active",
            json.dumps({"os": "linux"}),
            None,
            "[]",
            "/kept/path",
            "keep notes",
            now,
            now,
            None,
        ),
    )
    connection.commit()
    connection.close()
    storage = StorageManager(str(path))
    try:
        await storage.initialize()
        profile = await storage.get_profile("legacy")
        assert profile.tags == [] and profile.name == "Existing"
        assert profile.storage_path == "/kept/path" and profile.notes == "keep notes"
        await storage.db.initialize()
        assert (await storage.get_profile("legacy")).tags == []
    finally:
        await storage.close()


async def test_tag_filter_count_and_pagination_share_all_matching_predicates(storage):
    profiles = [
        Profile(id="a", name="Alpha % literal", group="client", tags=["Work", "US"]),
        Profile(id="b", name="Alpha normal", group="client", tags=["work"]),
        Profile(id="c", name="Other", tags=["work", "us"], status="inactive"),
    ]
    for profile in profiles:
        await storage.save_profile(profile)
    filters = {"tags": ["WORK", "us"], "group": "client", "status": "active", "search": "%"}
    assert await storage.count_profiles(filters) == 1
    assert [profile.id for profile in await storage.list_profiles(filters, limit=1)] == ["a"]
    assert await storage.list_profiles(filters, limit=1, offset=1) == []
    assert [profile.id for profile in await storage.list_profiles({"group": "__ungrouped__"})] == [
        "c"
    ]
    assert await storage.count_profiles({"search": "a"}) == len(
        await storage.list_profiles({"search": "a"})
    )
    assert await storage.list_profiles({"tags": ["x' OR 1=1--"]}) == []


async def test_metadata_only_tag_write_preserves_lease_pin_proxy_and_data(profile_manager):
    profile = await profile_manager.create_profile(
        name="Running",
        generate_fingerprint=False,
        tags=["old"],
        proxy_config={"type": "http", "server": "proxy.example:8080"},
    )
    profile.fingerprint = {"navigator.hardwareConcurrency": 8}
    await profile_manager.storage.update_profile(profile, expected_row_version=profile.row_version)
    expected = profile.row_version
    assert await profile_manager.storage.acquire_lease(profile.id, "another-browser", 300)
    result = await profile_manager.bulk_update_tags(
        [{"id": profile.id, "row_version": expected}], ["New"], ["old"]
    )
    assert result["skipped"] == [] and result["updated"][0].tags == ["new"]
    latest = await profile_manager.get_profile(profile.id)
    assert latest.fingerprint == profile.fingerprint and latest.proxy == profile.proxy
    assert latest.browser_settings == profile.browser_settings
    row = profile_manager.storage.db._connection.execute(
        "SELECT locked_by FROM profiles WHERE id = ?", (profile.id,)
    ).fetchone()
    assert row["locked_by"] == "another-browser"
    assert latest.row_version == expected + 1


async def test_bulk_tags_refuse_stale_targets_without_losing_successes(profile_manager):
    first = await profile_manager.create_profile(
        name="Fresh", generate_fingerprint=False, tags=["keep"]
    )
    stale = await profile_manager.create_profile(
        name="Stale", generate_fingerprint=False, tags=["original"]
    )
    await profile_manager.update_profile(
        stale.id, {"notes": "changed elsewhere"}, stale.row_version
    )
    result = await profile_manager.bulk_update_tags(
        [
            {"id": first.id, "row_version": first.row_version},
            {"id": stale.id, "row_version": stale.row_version},
            {"id": "missing", "row_version": 0},
        ],
        ["added"],
        [],
    )
    assert [profile.id for profile in result["updated"]] == [first.id]
    assert {item["id"] for item in result["skipped"]} == {stale.id, "missing"}
    assert (await profile_manager.get_profile(stale.id)).tags == ["original"]
    assert not profile_manager.storage.db._connection.in_transaction


async def test_tags_survive_clone_and_archive(profile_manager, tmp_path):
    profile = await profile_manager.create_profile(
        name="Tagged", generate_fingerprint=False, tags=["Work", "client-a"]
    )
    clone = await profile_manager.clone_profile(profile.id, "Clone", regenerate_fingerprint=False)
    assert clone.tags == ["work", "client-a"]
    archive = tmp_path / "profile.zip"
    await profile_manager.export_profile(profile.id, archive)
    imported = await profile_manager.import_profile(archive)
    assert imported.tags == profile.tags and imported.id != profile.id
