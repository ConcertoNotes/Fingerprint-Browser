"""Numbered schema boundary around SQLite migrations.

Current schema version is 9 (isolation engine, cloud delivery tables, and exit IP drift guard).
Databases created by older versions (v0..v8) migrate forward cleanly while
preserving all existing profiles, groups, subscriptions, nodes, and bindings.
"""

import json
import sqlite3

CURRENT_SCHEMA = 9

NETWORK_TABLES = (
    """CREATE TABLE IF NOT EXISTS network_subscriptions (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        source_kind TEXT NOT NULL CHECK(source_kind IN ('url','file','manual')),
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
        updated_at TEXT NOT NULL,
        managed_by TEXT NOT NULL DEFAULT 'local',
        cloud_source_id TEXT
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

CLOUD_TABLES = (
    """CREATE TABLE IF NOT EXISTS cloud_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS cloud_cache (
        kind TEXT PRIMARY KEY,
        version TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        payload TEXT NOT NULL,
        fetched_at TEXT NOT NULL
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
        required = {"profiles", "profile_groups", "usage_stats"}
        actual = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not required <= actual:
            raise ValueError("Incomplete database migration")

    if current < 6:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS manager_features "
                "(feature TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version > 0))"
            )
            for statement in NETWORK_TABLES:
                connection.execute(statement)
            connection.execute(
                "INSERT OR REPLACE INTO manager_features(feature, version) VALUES ('network-nodes', 2)"
            )
            connection.execute("PRAGMA user_version = 6")

    if current < 7:
        _allow_manual_subscriptions(connection)

    if current < 8:
        _migrate_to_v8(connection)

    if current < 9:
        _migrate_to_v9(connection)


def _allow_manual_subscriptions(connection: sqlite3.Connection) -> None:
    sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='network_subscriptions'"
    ).fetchone()
    if sql_row is None:
        return
    if "'manual'" in sql_row[0]:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS manager_features "
                "(feature TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version > 0))"
            )
            connection.execute(
                "INSERT OR REPLACE INTO manager_features(feature, version) VALUES ('network-nodes', 2)"
            )
            connection.execute("PRAGMA user_version = 7")
        return
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            columns = [
                row[1] for row in connection.execute("PRAGMA table_info(network_subscriptions)")
            ]
            create = (
                NETWORK_TABLES[0]
                .replace("network_subscriptions (", "network_subscriptions_rebuild (", 1)
                .replace("IF NOT EXISTS ", "", 1)
            )
            connection.execute(create)
            names = ", ".join(columns)
            connection.execute(
                f"INSERT INTO network_subscriptions_rebuild ({names}) "
                f"SELECT {names} FROM network_subscriptions"
            )
            connection.execute("DROP TABLE network_subscriptions")
            connection.execute(
                "ALTER TABLE network_subscriptions_rebuild RENAME TO network_subscriptions"
            )
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Network subscription migration broke a reference")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS manager_features "
                "(feature TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version > 0))"
            )
            connection.execute(
                "INSERT OR REPLACE INTO manager_features(feature, version) VALUES ('network-nodes', 2)"
            )
            connection.execute("PRAGMA user_version = 7")
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _migrate_to_v8(connection: sqlite3.Connection) -> None:
    """Upgrade to schema v8: profile isolation columns, cloud tables, and subscription metadata."""
    from .isolation.models import isolation_from_legacy_row

    with connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS manager_features "
            "(feature TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version > 0))"
        )

        # Ensure profiles has v8 columns.
        profile_cols = {row[1] for row in connection.execute("PRAGMA table_info(profiles)")}
        for col_name, col_def in (
            ("isolation_config", "TEXT"),
            ("fingerprint_hash", "TEXT"),
            ("cloud_revision", "INTEGER NOT NULL DEFAULT 0"),
            ("cloud_synced_at", "TEXT"),
        ):
            if col_name not in profile_cols:
                connection.execute(f"ALTER TABLE profiles ADD COLUMN {col_name} {col_def}")

        # Ensure network_subscriptions has v8 cloud management columns.
        sub_cols = {
            row[1] for row in connection.execute("PRAGMA table_info(network_subscriptions)")
        }
        for col_name, col_def in (
            ("managed_by", "TEXT NOT NULL DEFAULT 'local'"),
            ("cloud_source_id", "TEXT"),
        ):
            if col_name not in sub_cols:
                connection.execute(
                    f"ALTER TABLE network_subscriptions ADD COLUMN {col_name} {col_def}"
                )

        # Create cloud delivery tables.
        for statement in CLOUD_TABLES:
            connection.execute(statement)

        # Create v8 indexes.
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_profiles_fingerprint_hash ON profiles(fingerprint_hash)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_network_subscriptions_cloud_source "
            "ON network_subscriptions(cloud_source_id)"
        )

        # Backfill legacy profiles that lack isolation_config without altering hardware pins.
        legacy_rows = connection.execute(
            "SELECT id, browser_settings, fingerprint FROM profiles WHERE isolation_config IS NULL"
        ).fetchall()
        for row in legacy_rows:
            row_id = row[0]
            try:
                bs = json.loads(row[1]) if row[1] else {}
            except ValueError:
                bs = {}
            try:
                fp = json.loads(row[2]) if row[2] else None
            except ValueError:
                fp = None
            iso = isolation_from_legacy_row(bs, fp)
            connection.execute(
                "UPDATE profiles SET isolation_config = ?, fingerprint_hash = ? WHERE id = ?",
                (
                    iso.model_dump_json(),
                    iso.fingerprint_hash or None,
                    row_id,
                ),
            )

        connection.execute(
            "INSERT OR REPLACE INTO manager_features(feature, version) VALUES ('network-nodes', 2)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO manager_features(feature, version) VALUES ('isolation-cloud', 1)"
        )
        connection.execute("PRAGMA user_version = 8")


def _migrate_to_v9(connection: sqlite3.Connection) -> None:
    """Upgrade to schema v9: exit IP drift guard tracking columns on profiles."""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS manager_features "
            "(feature TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version > 0))"
        )

        profile_cols = {row[1] for row in connection.execute("PRAGMA table_info(profiles)")}
        for col_name, col_def in (
            ("last_exit_ip", "TEXT"),
            ("last_exit_country", "TEXT"),
            ("last_exit_at", "TEXT"),
            ("ip_drift_guard", "INTEGER NOT NULL DEFAULT 1"),
        ):
            if col_name not in profile_cols:
                connection.execute(f"ALTER TABLE profiles ADD COLUMN {col_name} {col_def}")

        connection.execute(
            "INSERT OR REPLACE INTO manager_features(feature, version) "
            "VALUES ('exit-ip-drift-guard', 1)"
        )
        connection.execute("PRAGMA user_version = 9")
