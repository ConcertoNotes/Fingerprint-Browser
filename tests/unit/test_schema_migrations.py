import json
import sqlite3
from contextlib import closing

import pytest

from camoufox_pm.core import schema_migrations
from camoufox_pm.core.database import StorageManager
from camoufox_pm.core.schema_migrations import CURRENT_SCHEMA


@pytest.mark.asyncio
async def test_future_schema_refused_without_changes(tmp_path):
    path = tmp_path / "db.sqlite"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA user_version=99")
    storage = StorageManager(str(path))
    for _ in range(2):
        with pytest.raises(ValueError, match="newer manager"):
            await storage.initialize()
    with closing(sqlite3.connect(path)) as db, db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99


@pytest.mark.asyncio
async def test_baseline_migration_numbered_and_retryable(tmp_path):
    path = tmp_path / "db.sqlite"
    for _ in range(2):
        storage = StorageManager(str(path))
        await storage.initialize()
        await storage.close()
    with closing(sqlite3.connect(path)) as db, db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA == 9
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.asyncio
async def test_v8_feature_refuses_older_manager(tmp_path, monkeypatch):
    path = tmp_path / "v8.sqlite"
    storage = StorageManager(str(path))
    await storage.initialize()
    await storage.close()
    with closing(sqlite3.connect(path)) as db:
        assert (
            db.execute(
                "SELECT version FROM manager_features WHERE feature='isolation-cloud'"
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT version FROM manager_features WHERE feature='exit-ip-drift-guard'"
            ).fetchone()[0]
            == 1
        )
        monkeypatch.setattr(schema_migrations, "CURRENT_SCHEMA", 8)
        with pytest.raises(ValueError, match="newer manager"):
            schema_migrations.check_schema(db)


@pytest.mark.asyncio
async def test_schema_five_databases_gain_network_and_cloud_tables(tmp_path):
    from camoufox_pm.core.models import Profile

    path = tmp_path / "v5.sqlite"
    storage = StorageManager(str(path))
    await storage.initialize()
    await storage.save_profile(Profile(name="Existing"))
    await storage.close()
    network_tables = (
        "network_subscriptions",
        "network_nodes",
        "network_node_checks",
        "network_ip_intel",
        "profile_network_bindings",
        "network_settings",
    )
    with closing(sqlite3.connect(path)) as db, db:
        for table in network_tables:
            db.execute(f"DROP TABLE {table}")
        db.execute("DROP TABLE cloud_settings")
        db.execute("DROP TABLE cloud_cache")
        db.execute(
            "DELETE FROM manager_features WHERE feature IN ('network-nodes', 'isolation-cloud')"
        )
        db.execute("PRAGMA user_version=5")
    for _ in range(2):  # resumable and idempotent
        storage = StorageManager(str(path))
        await storage.initialize()
        await storage.close()
    with closing(sqlite3.connect(path)) as db, db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA == 9
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert set(network_tables) | {"cloud_settings", "cloud_cache"} <= tables
        assert db.execute(
            "SELECT version FROM manager_features WHERE feature='network-nodes'"
        ).fetchone() == (2,)
        assert db.execute(
            "SELECT version FROM manager_features WHERE feature='isolation-cloud'"
        ).fetchone() == (1,)
        assert db.execute("SELECT name FROM profiles").fetchall() == [("Existing",)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.asyncio
async def test_schema_six_rebuild_keeps_subscriptions_and_allows_manual_groups(tmp_path):
    path = tmp_path / "v6.sqlite"
    storage = StorageManager(str(path))
    await storage.initialize()
    await storage.close()
    stamp = "2026-10-29T00:00:00+00:00"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys = OFF")
        db.execute("DROP TABLE network_subscriptions")
        db.execute(
            "CREATE TABLE network_subscriptions (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
            "source_kind TEXT NOT NULL CHECK(source_kind IN ('url','file')), source_url_enc TEXT, "
            "source_host TEXT, source_file_name TEXT, status TEXT NOT NULL DEFAULT 'idle', "
            "last_synced_at TEXT, last_error_code TEXT, last_error TEXT, usage TEXT, "
            "revision INTEGER NOT NULL DEFAULT 0, row_version BIGINT NOT NULL DEFAULT 0, "
            "deleted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO network_subscriptions (id, name, source_kind, created_at, updated_at) "
            "VALUES ('s1', 'Airport', 'file', ?, ?)",
            (stamp, stamp),
        )
        db.execute(
            "INSERT INTO network_nodes (id, subscription_id, node_key, display_name, protocol, "
            "entry, config_enc, created_at, updated_at) VALUES ('n1','s1','k','JP','ss','a:1','enc:x',?,?)",
            (stamp, stamp),
        )
        db.execute("UPDATE manager_features SET version=1 WHERE feature='network-nodes'")
        db.execute("PRAGMA user_version=6")
    storage = StorageManager(str(path))
    await storage.initialize()
    await storage.close()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys = ON")
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA == 9
        assert db.execute(
            "SELECT id, name, managed_by, cloud_source_id FROM network_subscriptions"
        ).fetchall() == [("s1", "Airport", "local", None)]
        assert db.execute("SELECT subscription_id FROM network_nodes").fetchall() == [("s1",)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        db.execute(
            "INSERT INTO network_subscriptions (id, name, source_kind, created_at, updated_at) "
            "VALUES ('m1', 'Static', 'manual', ?, ?)",
            (stamp, stamp),
        )
        # Cascades still work after the rebuild.
        db.execute("DELETE FROM network_subscriptions WHERE id='s1'")
        assert db.execute("SELECT COUNT(*) FROM network_nodes").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_schema_seven_migrates_to_eight_with_legacy_isolation_backfill(tmp_path):
    path = tmp_path / "v7.sqlite"
    storage = StorageManager(str(path))
    await storage.initialize()
    await storage.close()
    stamp = "2026-11-01T00:00:00+00:00"
    legacy_pin = {
        "navigator.userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:146.0) Gecko/20100101 Firefox/146.0",
        "navigator.platform": "MacIntel",
        "screen.width": 1440,
        "screen.height": 900,
        "navigator.hardwareConcurrency": 8,
        "webGl:renderer": "Apple M2",
    }
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP TABLE cloud_settings")
        db.execute("DROP TABLE cloud_cache")
        db.execute("DROP INDEX IF EXISTS idx_profiles_fingerprint_hash")
        db.execute("DROP INDEX IF EXISTS idx_network_subscriptions_cloud_source")
        db.execute("ALTER TABLE profiles DROP COLUMN isolation_config")
        db.execute("ALTER TABLE profiles DROP COLUMN fingerprint_hash")
        db.execute("ALTER TABLE profiles DROP COLUMN cloud_revision")
        db.execute("ALTER TABLE profiles DROP COLUMN cloud_synced_at")
        db.execute("ALTER TABLE network_subscriptions DROP COLUMN managed_by")
        db.execute("ALTER TABLE network_subscriptions DROP COLUMN cloud_source_id")
        db.execute(
            "INSERT INTO profiles (id, name, status, browser_settings, fingerprint, storage_path, "
            "created_at, updated_at) VALUES (?, ?, 'active', ?, ?, ?, ?, ?)",
            (
                "legacy01",
                "Legacy Profile",
                json.dumps({"os": "macos", "languages": ["en-US", "en"]}),
                json.dumps(legacy_pin),
                str(tmp_path / "profiles" / "legacy01"),
                stamp,
                stamp,
            ),
        )
        db.execute(
            "INSERT INTO network_subscriptions (id, name, source_kind, created_at, updated_at) "
            "VALUES ('sub7', 'Local v7', 'file', ?, ?)",
            (stamp, stamp),
        )
        db.execute("DELETE FROM manager_features WHERE feature='isolation-cloud'")
        db.execute("PRAGMA user_version=7")

    storage = StorageManager(str(path))
    await storage.initialize()
    profile = await storage.get_profile("legacy01")
    await storage.close()

    assert profile is not None
    assert profile.fingerprint_hash and profile.fingerprint_hash.startswith("FP-")
    assert profile.isolation_config is not None
    assert profile.isolation_config["fingerprint_hash"] == profile.fingerprint_hash
    assert profile.isolation_config["os"] == "macos"
    with closing(sqlite3.connect(path)) as db, db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 9
        row = db.execute(
            "SELECT managed_by, cloud_source_id FROM network_subscriptions WHERE id='sub7'"
        ).fetchone()
        assert row == ("local", None)
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"cloud_settings", "cloud_cache"} <= tables


@pytest.mark.asyncio
async def test_schema_eight_migrates_to_nine_with_ip_drift_guard_defaults(tmp_path):
    path = tmp_path / "v8_to_v9.sqlite"
    storage = StorageManager(str(path))
    await storage.initialize()
    await storage.close()
    stamp = "2026-11-20T00:00:00+00:00"
    with closing(sqlite3.connect(path)) as db, db:
        for col in ("last_exit_ip", "last_exit_country", "last_exit_at", "ip_drift_guard"):
            db.execute(f"ALTER TABLE profiles DROP COLUMN {col}")
        db.execute("DELETE FROM manager_features WHERE feature='exit-ip-drift-guard'")
        db.execute(
            "INSERT INTO profiles (id, name, status, browser_settings, storage_path, "
            "created_at, updated_at) VALUES (?, ?, 'active', ?, ?, ?, ?)",
            (
                "v8prof01",
                "V8 Profile",
                json.dumps({"os": "macos", "languages": ["en-US", "en"]}),
                str(tmp_path / "profiles" / "v8prof01"),
                stamp,
                stamp,
            ),
        )
        db.execute("PRAGMA user_version=8")

    storage = StorageManager(str(path))
    await storage.initialize()
    profile = await storage.get_profile("v8prof01")
    await storage.close()

    assert profile is not None
    assert profile.ip_drift_guard is True
    assert profile.last_exit_ip is None
    assert profile.last_exit_country is None
    assert profile.last_exit_at is None
    with closing(sqlite3.connect(path)) as db, db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 9
        assert db.execute(
            "SELECT version FROM manager_features WHERE feature='exit-ip-drift-guard'"
        ).fetchone() == (1,)
