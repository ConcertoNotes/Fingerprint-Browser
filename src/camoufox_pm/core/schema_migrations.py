"""Numbered schema boundary around the legacy idempotent schema migration.

Version zero denotes pre-updater databases. Version one records the complete
current schema only after tables, column additions and indexes have succeeded.
New versions must add an explicit numbered migration before increasing CURRENT.
"""

import sqlite3

CURRENT_SCHEMA = 6

# Subscription nodes, their measured exits and per-profile bindings. Secrets
# (subscription URLs, node configurations, provider keys) are stored sealed.
# A binding references a node that is retired rather than deleted, so a
# refreshed subscription can never silently move a profile onto another exit.
NETWORK_TABLES = (
    """CREATE TABLE IF NOT EXISTS network_subscriptions (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        source_kind TEXT NOT NULL CHECK(source_kind IN ('url','file')),
        source_url_enc TEXT,
        source_host TEXT,
        source_file_name TEXT,
        status TEXT NOT NULL DEFAULT 'idle' CHECK(status IN ('idle','syncing','ok','error')),
        last_synced_at TEXT,
        last_error_code TEXT,
        last_error TEXT,
        usage TEXT,
        revision INTEGER NOT NULL DEFAULT 0,
        row_version BIGINT NOT NULL DEFAULT 0,
        deleted_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS network_nodes (
        id TEXT PRIMARY KEY,
        subscription_id TEXT NOT NULL REFERENCES network_subscriptions(id) ON DELETE CASCADE,
        node_key TEXT NOT NULL,
        display_name TEXT NOT NULL,
        protocol TEXT NOT NULL,
        entry TEXT NOT NULL,
        config_enc TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','retired','invalid')),
        invalid_reason TEXT,
        position INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        retired_at TEXT
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_network_nodes_source_key "
    "ON network_nodes(subscription_id, node_key)",
    """CREATE TABLE IF NOT EXISTS network_node_checks (
        node_id TEXT PRIMARY KEY REFERENCES network_nodes(id) ON DELETE CASCADE,
        health TEXT NOT NULL DEFAULT 'unknown' CHECK(health IN ('unknown','ok','error')),
        exit_ip TEXT,
        country_iso TEXT,
        city TEXT,
        region TEXT,
        latitude REAL,
        longitude REAL,
        timezone TEXT,
        precision TEXT NOT NULL DEFAULT 'unknown'
            CHECK(precision IN ('unknown','country','coordinates','city')),
        location_source TEXT,
        latency_ms INTEGER,
        error_code TEXT,
        error TEXT,
        checked_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_network_checks_exit_ip ON network_node_checks(exit_ip)",
    """CREATE TABLE IF NOT EXISTS network_ip_intel (
        ip TEXT PRIMARY KEY,
        data TEXT NOT NULL,
        checked_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS profile_network_bindings (
        profile_id TEXT PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
        node_id TEXT NOT NULL REFERENCES network_nodes(id) ON DELETE RESTRICT,
        row_version BIGINT NOT NULL DEFAULT 0,
        bound_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_profile_binding_node ON profile_network_bindings(node_id)",
    """CREATE TABLE IF NOT EXISTS network_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
)


def check_schema(connection: sqlite3.Connection) -> int:
    current = connection.execute("PRAGMA user_version").fetchone()[0]
    if current > CURRENT_SCHEMA:
        raise ValueError(f"Database schema {current} requires a newer manager")
    if current < 0:
        raise ValueError("Invalid database schema")
    return current


def complete_migration(connection: sqlite3.Connection) -> None:
    current = check_schema(connection)
    if current == 0:
        # Check the essential baseline before publishing migration 1. Legacy
        # upgrades are idempotent and resumable when interrupted before here.
        required = {"profiles", "users", "sessions", "schedules", "proxy_inventory"}
        actual = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not required <= actual:
            raise ValueError("Incomplete database migration")
        connection.execute("PRAGMA user_version = 1")
        connection.commit()

    if current < 2:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            columns = {r[1] for r in connection.execute("PRAGMA table_info(users)")}
            if "role" not in columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'administrator' CHECK(role IN ('administrator','operator','viewer'))"
                )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS profile_grants (username TEXT NOT NULL COLLATE NOCASE REFERENCES users(username) ON DELETE CASCADE, profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE, can_export INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(username,profile_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, actor_kind TEXT NOT NULL, actor_username TEXT, action TEXT NOT NULL, object_id TEXT, result TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            columns = {r[1] for r in connection.execute("PRAGMA table_info(schedules)")}
            if "actor_kind" not in columns:
                connection.execute(
                    "ALTER TABLE schedules ADD COLUMN actor_kind TEXT NOT NULL DEFAULT 'system'"
                )
            if "actor_username" not in columns:
                connection.execute("ALTER TABLE schedules ADD COLUMN actor_username TEXT")
            if "actor_user_id" not in columns:
                connection.execute("ALTER TABLE schedules ADD COLUMN actor_user_id TEXT")
            connection.execute("PRAGMA user_version = 2")

    if current < 3:
        # Older schema-aware managers must refuse this database before touching
        # profile paths: they do not enforce distributed checkout or apply fences.
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS manager_features (feature TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version > 0))"
            )
            connection.execute(
                "INSERT OR REPLACE INTO manager_features(feature, version) VALUES ('sync-admission', 1)"
            )
            connection.execute("PRAGMA user_version = 3")

    if current < 4:
        # Schema-3 managers cannot enforce authenticated Trash lifecycle fences.
        # Fail their startup rather than let them reopen a retained deleted profile.
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR REPLACE INTO manager_features(feature, version) VALUES ('sync-lifecycle', 1)"
            )
            connection.execute("PRAGMA user_version = 4")

    if current < 5:
        # A pre-workflow manager cannot reconcile durable active-run receipts.
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS workflows (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS workflow_runs (id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE, active INTEGER NOT NULL, body TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS workflow_one_active_run ON workflow_runs(workflow_id) WHERE active=1"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS workflow_receipts (id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE)"
            )
            connection.execute("PRAGMA user_version = 5")

    if current < 6:
        # A pre-network manager would launch a bound profile with its stale
        # manual proxy (or none), so it must refuse this database instead.
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            for statement in NETWORK_TABLES:
                connection.execute(statement)
            connection.execute(
                "INSERT OR REPLACE INTO manager_features(feature, version) VALUES ('network-nodes', 1)"
            )
            connection.execute("PRAGMA user_version = 6")
