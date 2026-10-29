import sqlite3
from contextlib import closing

import pytest

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
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.asyncio
async def test_stage_two_existing_accounts_become_administrators(tmp_path):
    path = tmp_path / "legacy.sqlite"
    storage = StorageManager(str(path))
    await storage.initialize()
    await storage.close()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP TABLE profile_grants")
        db.execute("DROP TABLE audit_events")
        db.execute("ALTER TABLE users DROP COLUMN role")
        db.execute("ALTER TABLE schedules DROP COLUMN actor_kind")
        db.execute("ALTER TABLE schedules DROP COLUMN actor_username")
        db.execute("ALTER TABLE schedules DROP COLUMN actor_user_id")
        db.execute(
            "INSERT INTO users(id,username,password_hash,created_at) VALUES ('existing','host-owner','hash','2026-01-01')"
        )
        db.execute("PRAGMA user_version=1")
    storage = StorageManager(str(path))
    await storage.initialize()
    assert (await storage.get_user_by_username("host-owner"))["role"] == "administrator"
    await storage.close()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA


@pytest.mark.asyncio
async def test_partial_schedule_actor_migration_resumes(tmp_path):
    path = tmp_path / "partial.sqlite"
    storage = StorageManager(str(path))
    await storage.initialize()
    await storage.close()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("ALTER TABLE schedules DROP COLUMN actor_username")
        db.execute("ALTER TABLE schedules DROP COLUMN actor_user_id")
        db.execute("PRAGMA user_version=1")
    storage = StorageManager(str(path))
    await storage.initialize()
    assert await storage.list_schedules() == []
    await storage.close()
    with closing(sqlite3.connect(path)) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(schedules)")}
        assert {"actor_username", "actor_user_id"} <= columns


@pytest.mark.asyncio
async def test_sync_schema_refuses_pre_sync_manager(tmp_path, monkeypatch):
    from camoufox_pm.core import schema_migrations

    path = tmp_path / "sync.sqlite"
    storage = StorageManager(str(path))
    await storage.initialize()
    await storage.close()
    with closing(sqlite3.connect(path)) as db:
        assert (
            db.execute(
                "SELECT version FROM manager_features WHERE feature='sync-admission'"
            ).fetchone()[0]
            == 1
        )
        monkeypatch.setattr(schema_migrations, "CURRENT_SCHEMA", 2)
        with pytest.raises(ValueError, match="newer manager"):
            schema_migrations.check_schema(db)


@pytest.mark.asyncio
async def test_lifecycle_schema_refuses_pre_lifecycle_manager(tmp_path, monkeypatch):
    from camoufox_pm.core import schema_migrations

    path = tmp_path / "lifecycle.sqlite"
    storage = StorageManager(str(path))
    await storage.initialize()
    await storage.close()
    with closing(sqlite3.connect(path)) as db:
        assert (
            db.execute(
                "SELECT version FROM manager_features WHERE feature='sync-lifecycle'"
            ).fetchone()[0]
            == 1
        )
        monkeypatch.setattr(schema_migrations, "CURRENT_SCHEMA", 3)
        with pytest.raises(ValueError, match="newer manager"):
            schema_migrations.check_schema(db)


@pytest.mark.asyncio
async def test_schema_five_databases_gain_the_network_tables_once(tmp_path):
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
        db.execute("DELETE FROM manager_features WHERE feature='network-nodes'")
        db.execute("PRAGMA user_version=5")
    for _ in range(2):  # resumable and idempotent
        storage = StorageManager(str(path))
        await storage.initialize()
        await storage.close()
    with closing(sqlite3.connect(path)) as db, db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA == 7
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert set(network_tables) <= tables
        assert db.execute(
            "SELECT version FROM manager_features WHERE feature='network-nodes'"
        ).fetchone() == (2,)
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
        assert db.execute("PRAGMA user_version").fetchone()[0] == 7
        assert db.execute("SELECT id, name FROM network_subscriptions").fetchall() == [
            ("s1", "Airport")
        ]
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
