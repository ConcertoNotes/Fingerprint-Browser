"""SQLite storage layer for Prism Browser."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from loguru import logger
from pydantic import ValidationError

from .crypto import decrypt, encrypt
from .leases import lease_expired
from .models import (
    Profile,
    ProfileGroup,
    ProfileStatus,
    ProxyCheckRecord,
    ProxyConfig,
    UsageStats,
    normalize_tags,
)


def _serialize_proxy(proxy: ProxyConfig) -> dict:
    """Serialize a proxy for storage, encrypting the password."""
    data = proxy.model_dump()
    if data.get("password"):
        data["password"] = encrypt(data["password"])
    return data


def _deserialize_proxy(data: dict) -> ProxyConfig:
    """Rebuild a proxy from storage, decrypting the password."""
    if data.get("password"):
        data["password"] = decrypt(data["password"])
    return ProxyConfig(**data)


class StaleWriteError(Exception):
    """A save built on an older version of the row than the one stored."""

    def __init__(self, profile_id: str, expected_row_version: int | None = None):
        self.profile_id = profile_id
        self.expected_row_version = expected_row_version
        super().__init__(
            f"Profile {profile_id} was changed by someone else since it was read; "
            "reload it and apply the edit again"
        )


class DatabaseManager:
    """Async-friendly SQLite database manager."""

    def __init__(self, db_path: str = "data/profiles.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection: sqlite3.Connection = None  # type: ignore[assignment]
        logger.info(f"DatabaseManager initialized with database: {self.db_path}")

    async def initialize(self):
        """Initialize the database and create tables (safe to call twice)."""
        if self._connection is not None:
            return
        self._connection = sqlite3.connect(str(self.db_path), timeout=30.0)
        self._connection.row_factory = sqlite3.Row
        from .schema_migrations import check_schema, complete_migration

        try:
            check_schema(self._connection)
        except Exception:
            self._connection.close()
            self._connection = None
            raise
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")

        await self._create_tables()
        await self._migrate()
        await self._create_indexes()
        complete_migration(self._connection)
        logger.info("Database initialized")

    async def _migrate(self):
        """Ensure legacy tables carry required columns prior to numbered migrations."""
        added_columns = {
            "profiles": [
                ("fingerprint", "TEXT"),
                ("proxy_check", "TEXT"),
                ("locked_by", "TEXT"),
                ("lock_expires", "TIMESTAMP"),
                ("row_version", "BIGINT NOT NULL DEFAULT 0"),
                ("deleted_at", "TIMESTAMP"),
                ("tags", "TEXT NOT NULL DEFAULT '[]'"),
                ("isolation_config", "TEXT"),
                ("fingerprint_hash", "TEXT"),
                ("cloud_revision", "INTEGER NOT NULL DEFAULT 0"),
                ("cloud_synced_at", "TEXT"),
                ("last_exit_ip", "TEXT"),
                ("last_exit_country", "TEXT"),
                ("last_exit_at", "TEXT"),
                ("ip_drift_guard", "INTEGER NOT NULL DEFAULT 1"),
            ],
        }
        for table, columns in added_columns.items():
            cursor = self._connection.execute(f"PRAGMA table_info({table})")
            existing = {row["name"] for row in cursor.fetchall()}
            for name, column_type in columns:
                if name in existing:
                    continue
                self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")
                logger.info(f"Migrated {table}: added column {name}")
        self._connection.commit()

    async def _create_tables(self):
        """Create the core database tables."""
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                group_id TEXT,
                status TEXT DEFAULT 'active',
                browser_settings TEXT NOT NULL,
                proxy_config TEXT,
                extensions TEXT,
                tags TEXT NOT NULL DEFAULT '[]',
                storage_path TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used TIMESTAMP,
                fingerprint TEXT,
                proxy_check TEXT,
                locked_by TEXT,
                lock_expires TIMESTAMP,
                row_version BIGINT NOT NULL DEFAULT 0,
                deleted_at TIMESTAMP,
                isolation_config TEXT,
                fingerprint_hash TEXT,
                cloud_revision INTEGER NOT NULL DEFAULT 0,
                cloud_synced_at TEXT,
                last_exit_ip TEXT,
                last_exit_country TEXT,
                last_exit_at TEXT,
                ip_drift_guard INTEGER NOT NULL DEFAULT 1
            )
        """)

        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS profile_groups (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                description TEXT,
                profile_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS usage_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id TEXT NOT NULL,
                action TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                duration INTEGER,
                success BOOLEAN DEFAULT 1,
                details TEXT
            )
        """)
        self._connection.commit()

    async def _create_indexes(self):
        """Create indexes to optimize queries."""
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_profiles_group ON profiles(group_id)",
            "CREATE INDEX IF NOT EXISTS idx_profiles_status ON profiles(status)",
            "CREATE INDEX IF NOT EXISTS idx_profiles_created ON profiles(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_profiles_fingerprint_hash ON profiles(fingerprint_hash)",
            "CREATE INDEX IF NOT EXISTS idx_usage_stats_profile ON usage_stats(profile_id)",
            "CREATE INDEX IF NOT EXISTS idx_usage_stats_timestamp ON usage_stats(timestamp)",
        ]
        for index_sql in indexes:
            self._connection.execute(index_sql)
        self._connection.commit()

    # --- Profiles ---

    def _profile_columns(self, profile: Profile) -> tuple:
        """Column values owned by a save, shared across upsert and guarded update."""
        profile.tags = normalize_tags(profile.tags)
        iso_json = (
            json.dumps(profile.isolation_config, ensure_ascii=False)
            if profile.isolation_config is not None
            else None
        )
        return (
            profile.id,
            profile.name,
            profile.group,
            profile.status.value if hasattr(profile.status, "value") else profile.status,
            json.dumps(profile.browser_settings.model_dump()),
            json.dumps(_serialize_proxy(profile.proxy)) if profile.proxy else None,
            json.dumps(profile.extensions),
            profile.storage_path,
            profile.notes,
            profile.created_at.isoformat(),
            profile.updated_at.isoformat(),
            profile.last_used.isoformat() if profile.last_used else None,
            json.dumps(profile.fingerprint) if profile.fingerprint else None,
            profile.proxy_check.model_dump_json() if profile.proxy_check else None,
            json.dumps(profile.tags, ensure_ascii=False),
            iso_json,
            profile.fingerprint_hash,
            profile.cloud_revision,
            profile.cloud_synced_at.isoformat() if profile.cloud_synced_at else None,
            profile.last_exit_ip,
            profile.last_exit_country,
            profile.last_exit_at.isoformat() if profile.last_exit_at else None,
            1 if profile.ip_drift_guard else 0,
        )

    def _upsert_profile(self, profile: Profile) -> None:
        self._connection.execute(
            """
            INSERT INTO profiles (
                id, name, group_id, status, browser_settings, proxy_config,
                extensions, storage_path, notes, created_at, updated_at, last_used,
                fingerprint, proxy_check, tags, isolation_config, fingerprint_hash,
                cloud_revision, cloud_synced_at, last_exit_ip, last_exit_country,
                last_exit_at, ip_drift_guard
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                group_id = excluded.group_id,
                status = excluded.status,
                browser_settings = excluded.browser_settings,
                proxy_config = excluded.proxy_config,
                extensions = excluded.extensions,
                storage_path = excluded.storage_path,
                notes = excluded.notes,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                last_used = excluded.last_used,
                fingerprint = excluded.fingerprint,
                proxy_check = excluded.proxy_check,
                tags = excluded.tags,
                isolation_config = excluded.isolation_config,
                fingerprint_hash = excluded.fingerprint_hash,
                cloud_revision = excluded.cloud_revision,
                cloud_synced_at = excluded.cloud_synced_at,
                last_exit_ip = excluded.last_exit_ip,
                last_exit_country = excluded.last_exit_country,
                last_exit_at = excluded.last_exit_at,
                ip_drift_guard = excluded.ip_drift_guard
        """,
            self._profile_columns(profile),
        )

    async def insert_profile_if_absent(self, profile: Profile) -> bool:
        """Reserve a new local identity without overwriting an existing row."""
        connection = self._connection
        if connection.in_transaction:
            raise ValueError("Profile insertion requires an idle transaction")
        previous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
        connection.execute("PRAGMA synchronous=FULL")
        try:
            cursor = connection.execute(
                """INSERT INTO profiles (
                    id, name, group_id, status, browser_settings, proxy_config,
                    extensions, storage_path, notes, created_at, updated_at, last_used,
                    fingerprint, proxy_check, tags, isolation_config, fingerprint_hash,
                    cloud_revision, cloud_synced_at, last_exit_ip, last_exit_country,
                    last_exit_at, ip_drift_guard
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING""",
                self._profile_columns(profile),
            )
            inserted = cursor.rowcount == 1
            connection.commit()
            return inserted
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.execute(f"PRAGMA synchronous={previous}")

    async def save_profile(self, profile: Profile, expected_row_version: int | None = None):
        """Save a profile, optionally checking ``expected_row_version``."""
        profile.updated_at = datetime.now()
        if expected_row_version is None:
            self._upsert_profile(profile)
        else:
            self._save_profile_if_unchanged(profile, expected_row_version)
        self._connection.commit()
        logger.debug(f"Profile {profile.name} saved")

    def _save_profile_if_unchanged(self, profile: Profile, expected_row_version: int) -> None:
        cursor = self._connection.execute(
            """
            UPDATE profiles SET
                name = ?, group_id = ?, status = ?, browser_settings = ?,
                proxy_config = ?, extensions = ?, storage_path = ?, notes = ?,
                created_at = ?, updated_at = ?, last_used = ?, fingerprint = ?,
                proxy_check = ?, tags = ?, isolation_config = ?, fingerprint_hash = ?,
                cloud_revision = ?, cloud_synced_at = ?, last_exit_ip = ?,
                last_exit_country = ?, last_exit_at = ?, ip_drift_guard = ?,
                row_version = row_version + 1
            WHERE id = ? AND row_version = ? AND deleted_at IS NULL
            """,
            (*self._profile_columns(profile)[1:], profile.id, expected_row_version),
        )
        if cursor.rowcount == 0:
            raise StaleWriteError(profile.id, expected_row_version)
        profile.row_version = expected_row_version + 1

    async def get_profile(self, profile_id: str, include_deleted: bool = False) -> Profile | None:
        """Get a profile by ID."""
        query = "SELECT * FROM profiles WHERE id = ?"
        if not include_deleted:
            query += " AND deleted_at IS NULL"
        cursor = self._connection.execute(query, (profile_id,))
        row = cursor.fetchone()
        if row:
            return self._row_to_profile(row)
        return None

    async def update_profile(self, profile: Profile, expected_row_version: int | None = None):
        """Update a profile, optionally guarded against a concurrent save."""
        await self.save_profile(profile, expected_row_version)
        logger.debug(f"Profile {profile.name} updated")

    async def durable_update_profile(self, profile: Profile, expected_row_version: int) -> None:
        """Publish a profile update with a FULL-synchronous CAS commit."""
        if type(expected_row_version) is not int or expected_row_version < 0:
            raise ValueError("A current profile version is required")
        connection = self._connection
        if connection.in_transaction:
            raise ValueError("Durable profile publication requires an idle transaction")
        previous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
        original_version = profile.row_version
        connection.execute("PRAGMA synchronous=FULL")
        try:
            profile.updated_at = datetime.now()
            self._save_profile_if_unchanged(profile, expected_row_version)
            connection.commit()
        except BaseException:
            connection.rollback()
            profile.row_version = original_version
            raise
        finally:
            connection.execute(f"PRAGMA synchronous={previous}")

    async def set_proxy_check(self, profile_id: str, record: ProxyCheckRecord | None) -> None:
        """Write only the proxy check column."""
        self._connection.execute(
            "UPDATE profiles SET proxy_check = ? WHERE id = ?",
            (record.model_dump_json() if record else None, profile_id),
        )
        self._connection.commit()

    async def set_last_exit(
        self,
        profile_id: str,
        exit_ip: str,
        exit_country: str | None,
        exit_at: datetime,
    ) -> None:
        """Persist the latest verified exit IP/country after a bound browser launch."""
        self._connection.execute(
            "UPDATE profiles SET last_exit_ip = ?, last_exit_country = ?, last_exit_at = ? "
            "WHERE id = ?",
            (exit_ip, exit_country, exit_at.isoformat(), profile_id),
        )
        self._connection.commit()

    async def set_launch_pin(
        self,
        profile_id: str,
        fingerprint: dict | None,
        last_used: datetime,
        isolation_config: dict | None = None,
        fingerprint_hash: str | None = None,
    ) -> None:
        """Write only what a launch owns: pinned hardware, isolation snapshot, and timestamp."""
        if isolation_config is not None or fingerprint_hash is not None:
            self._connection.execute(
                "UPDATE profiles SET fingerprint = ?, isolation_config = COALESCE(?, isolation_config), "
                "fingerprint_hash = COALESCE(?, fingerprint_hash), last_used = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    json.dumps(fingerprint) if fingerprint else None,
                    json.dumps(isolation_config, ensure_ascii=False)
                    if isolation_config is not None
                    else None,
                    fingerprint_hash,
                    last_used.isoformat(),
                    last_used.isoformat(),
                    profile_id,
                ),
            )
        else:
            self._connection.execute(
                "UPDATE profiles SET fingerprint = ?, last_used = ?, updated_at = ? WHERE id = ?",
                (
                    json.dumps(fingerprint) if fingerprint else None,
                    last_used.isoformat(),
                    last_used.isoformat(),
                    profile_id,
                ),
            )
        self._connection.commit()

    async def delete_profile(self, profile_id: str) -> bool:
        """Permanently delete a profile row (cascades its network binding)."""
        cursor = self._connection.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        self._connection.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.debug(f"Profile {profile_id} deleted")
        return deleted

    def list_fingerprint_hashes(self, exclude_profile_id: str | None = None) -> set[str]:
        """Return all non-empty fingerprint hashes currently in use across local profiles."""
        if exclude_profile_id:
            rows = self._connection.execute(
                "SELECT fingerprint_hash FROM profiles "
                "WHERE deleted_at IS NULL AND id != ? AND fingerprint_hash IS NOT NULL AND fingerprint_hash != ''",
                (exclude_profile_id,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT fingerprint_hash FROM profiles "
                "WHERE deleted_at IS NULL AND fingerprint_hash IS NOT NULL AND fingerprint_hash != ''"
            ).fetchall()
        return {row[0] for row in rows}

    def _profile_filters(self, filters: dict | None = None) -> tuple[str, list]:
        filters = filters or {}
        predicate = "1=1" if filters.get("include_deleted") else "deleted_at IS NULL"
        params: list = []
        if "group" in filters:
            if filters["group"] == "__ungrouped__" or filters["group"] is None:
                predicate += " AND group_id IS NULL"
            else:
                predicate += " AND group_id = ?"
                params.append(filters["group"])
        if "status" in filters:
            predicate += " AND status = ?"
            params.append(filters["status"])
        if "name_like" in filters:
            predicate += " AND name LIKE ?"
            params.append(f"%{filters['name_like']}%")
        if filters.get("search"):
            value = (
                str(filters["search"]).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            predicate += " AND (name LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\')"
            params.extend([f"%{value}%", f"%{value}%"])
        requested = filters.get("tags", [])
        if filters.get("tag"):
            requested = [*requested, filters["tag"]]
        for tag in normalize_tags(requested):
            predicate += " AND EXISTS (SELECT 1 FROM json_each(profiles.tags) WHERE value = ?)"
            params.append(tag)
        return predicate, params

    async def list_profiles(
        self, filters: dict | None = None, limit: int | None = None, offset: int = 0
    ) -> list[Profile]:
        """List profiles with filtering and pagination."""
        predicate, params = self._profile_filters(filters)
        query = f"SELECT * FROM profiles WHERE {predicate} ORDER BY created_at DESC, id"
        if limit is not None or offset:
            query += " LIMIT ?"
            params.append(limit if limit is not None else -1)
        if offset:
            query += " OFFSET ?"
            params.append(offset)
        rows = self._connection.execute(query, params).fetchall()
        return [self._row_to_profile(row) for row in rows]

    async def count_profiles(self, filters: dict | None = None) -> int:
        predicate, params = self._profile_filters(filters)
        return self._connection.execute(
            f"SELECT COUNT(*) FROM profiles WHERE {predicate}", params
        ).fetchone()[0]

    async def set_profile_tags(
        self, profile_id: str, tags: list[str], expected_row_version: int
    ) -> Profile:
        """Metadata-only CAS for profile tags."""
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE profiles SET tags = ?, updated_at = ?, row_version = row_version + 1 "
                "WHERE id = ? AND row_version = ? AND deleted_at IS NULL",
                (
                    json.dumps(normalize_tags(tags), ensure_ascii=False),
                    datetime.now().isoformat(),
                    profile_id,
                    expected_row_version,
                ),
            )
            if not cursor.rowcount:
                raise StaleWriteError(profile_id, expected_row_version)
        profile = await self.get_profile(profile_id)
        assert profile is not None
        return profile

    # --- Profile groups ---

    async def save_profile_group(self, group: ProfileGroup):
        """Save a profile group."""
        self._connection.execute(
            """
            INSERT OR REPLACE INTO profile_groups (
                id, name, description, profile_count, created_at
            ) VALUES (?, ?, ?, ?, ?)
        """,
            (
                group.id,
                group.name,
                group.description,
                group.profile_count,
                group.created_at.isoformat(),
            ),
        )
        self._connection.commit()
        logger.debug(f"Group {group.name} saved")

    async def list_profile_groups(self) -> list[ProfileGroup]:
        """List all groups."""
        cursor = self._connection.execute("""
            SELECT pg.*, COUNT(p.id) as actual_count
            FROM profile_groups pg
            LEFT JOIN profiles p ON pg.id = p.group_id AND p.deleted_at IS NULL
            GROUP BY pg.id
            ORDER BY pg.created_at DESC
        """)
        rows = cursor.fetchall()
        return [
            ProfileGroup(
                id=row["id"],
                name=row["name"],
                description=row["description"],
                profile_count=row["actual_count"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    async def delete_profile_group(self, group_id: str) -> bool:
        """Delete a profile group."""
        self._connection.execute(
            "UPDATE profiles SET group_id = NULL WHERE group_id = ?", (group_id,)
        )
        cursor = self._connection.execute("DELETE FROM profile_groups WHERE id = ?", (group_id,))
        self._connection.commit()
        return cursor.rowcount > 0

    # --- Statistics ---

    async def log_usage(self, usage_stats: UsageStats):
        """Record a usage statistic."""
        self._connection.execute(
            """
            INSERT INTO usage_stats (
                profile_id, action, timestamp, duration, success, details
            ) VALUES (?, ?, ?, ?, ?, ?)
        """,
            (
                usage_stats.profile_id,
                usage_stats.action,
                usage_stats.timestamp.isoformat(),
                usage_stats.duration,
                usage_stats.success,
                json.dumps(usage_stats.details) if usage_stats.details else None,
            ),
        )
        self._connection.commit()

    async def get_profile_usage_stats(self, profile_id: str, limit: int = 100) -> list[UsageStats]:
        """Get usage statistics for a profile."""
        cursor = self._connection.execute(
            """
            SELECT * FROM usage_stats
            WHERE profile_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """,
            (profile_id, limit),
        )
        rows = cursor.fetchall()
        return [
            UsageStats(
                id=row["id"],
                profile_id=row["profile_id"],
                action=row["action"],
                timestamp=datetime.fromisoformat(row["timestamp"]),
                duration=row["duration"],
                success=bool(row["success"]),
                details=json.loads(row["details"]) if row["details"] else None,
            )
            for row in rows
        ]

    # --- Leases ---

    async def acquire_lease(
        self, profile_id: str, holder: str, ttl_seconds: int, include_deleted: bool = False
    ) -> bool:
        cursor = self._connection.execute(
            """
            UPDATE profiles
               SET locked_by = ?,
                   lock_expires = datetime('now', '+' || ? || ' seconds')
             WHERE id = ? AND (deleted_at IS NULL OR ?)
               AND (locked_by IS NULL
                    OR locked_by = ?
                    OR julianday(lock_expires) < julianday('now'))
            """,
            (holder, ttl_seconds, profile_id, include_deleted, holder),
        )
        self._connection.commit()
        return cursor.rowcount > 0

    async def renew_lease(self, profile_ids: list[str], holder: str, ttl_seconds: int) -> int:
        if not profile_ids:
            return 0
        placeholders = ",".join("?" for _ in profile_ids)
        cursor = self._connection.execute(
            f"""
            UPDATE profiles
               SET lock_expires = datetime('now', '+' || ? || ' seconds')
             WHERE id IN ({placeholders}) AND locked_by = ?
            """,
            (ttl_seconds, *profile_ids, holder),
        )
        self._connection.commit()
        return cursor.rowcount

    async def release_lease(self, profile_id: str, holder: str) -> bool:
        cursor = self._connection.execute(
            "UPDATE profiles SET locked_by = NULL, lock_expires = NULL "
            "WHERE id = ? AND locked_by = ?",
            (profile_id, holder),
        )
        self._connection.commit()
        return cursor.rowcount > 0

    async def force_release_lease(self, profile_id: str) -> str | None:
        row = self._connection.execute(
            "SELECT locked_by FROM profiles WHERE id = ? AND locked_by IS NOT NULL",
            (profile_id,),
        ).fetchone()
        if row is None:
            return None
        self._connection.execute(
            "UPDATE profiles SET locked_by = NULL, lock_expires = NULL WHERE id = ?",
            (profile_id,),
        )
        self._connection.commit()
        return row["locked_by"]

    async def get_lease(self, profile_id: str) -> tuple[str | None, str | None] | None:
        cursor = self._connection.execute(
            "SELECT locked_by, lock_expires FROM profiles WHERE id = ?", (profile_id,)
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return row["locked_by"], row["lock_expires"]

    async def get_lease_holders(self) -> list[dict]:
        cursor = self._connection.execute(
            """
            SELECT id, name, locked_by, lock_expires
              FROM profiles
             WHERE locked_by IS NOT NULL
             ORDER BY id
            """
        )
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "locked_by": row["locked_by"],
                "lock_expires": row["lock_expires"],
                "expired": lease_expired(row["lock_expires"]),
            }
            for row in cursor.fetchall()
        ]

    # --- Utilities ---

    def _row_to_profile(self, row) -> Profile:
        from .models import BrowserSettings

        browser_settings_data = json.loads(row["browser_settings"])
        browser_settings = BrowserSettings(**browser_settings_data)

        proxy = None
        if row["proxy_config"]:
            proxy = _deserialize_proxy(json.loads(row["proxy_config"]))

        extensions = json.loads(row["extensions"]) if row["extensions"] else []

        keys = row.keys()
        fingerprint = (
            json.loads(row["fingerprint"]) if "fingerprint" in keys and row["fingerprint"] else None
        )
        isolation_config = None
        if "isolation_config" in keys and row["isolation_config"]:
            try:
                isolation_config = json.loads(row["isolation_config"])
            except ValueError:
                logger.warning(f"Profile {row['id']}: unreadable isolation_config, ignoring")

        fingerprint_hash = row["fingerprint_hash"] if "fingerprint_hash" in keys else None
        cloud_revision = (
            int(row["cloud_revision"])
            if "cloud_revision" in keys and row["cloud_revision"] is not None
            else 0
        )
        cloud_synced_at = (
            datetime.fromisoformat(row["cloud_synced_at"])
            if "cloud_synced_at" in keys and row["cloud_synced_at"]
            else None
        )
        ip_drift_guard = (
            bool(row["ip_drift_guard"])
            if "ip_drift_guard" in keys and row["ip_drift_guard"] is not None
            else True
        )
        last_exit_ip = row["last_exit_ip"] if "last_exit_ip" in keys else None
        last_exit_country = row["last_exit_country"] if "last_exit_country" in keys else None
        last_exit_at = (
            datetime.fromisoformat(row["last_exit_at"])
            if "last_exit_at" in keys and row["last_exit_at"]
            else None
        )

        stored_check = None
        if "proxy_check" in keys and row["proxy_check"]:
            try:
                stored_check = ProxyCheckRecord.model_validate_json(row["proxy_check"])
            except ValidationError as error:
                logger.warning(f"Profile {row['id']}: unreadable proxy check, ignoring ({error})")

        return Profile(
            id=row["id"],
            name=row["name"],
            group=row["group_id"],
            tags=json.loads(row["tags"] or "[]"),
            status=ProfileStatus(row["status"]),
            browser_settings=browser_settings,
            proxy=proxy,
            extensions=extensions,
            storage_path=row["storage_path"],
            notes=row["notes"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            last_used=datetime.fromisoformat(row["last_used"]) if row["last_used"] else None,
            fingerprint=fingerprint,
            isolation_config=isolation_config,
            fingerprint_hash=fingerprint_hash,
            ip_drift_guard=ip_drift_guard,
            last_exit_ip=last_exit_ip,
            last_exit_country=last_exit_country,
            last_exit_at=last_exit_at,
            cloud_revision=cloud_revision,
            cloud_synced_at=cloud_synced_at,
            proxy_check=stored_check,
            row_version=row["row_version"],
            deleted_at=datetime.fromisoformat(row["deleted_at"]) if row["deleted_at"] else None,
        )

    async def close(self):
        """Close the database connection."""
        if self._connection:
            self._connection.close()
            self._connection: sqlite3.Connection = None  # type: ignore[assignment]
        logger.info("Database connection closed")


class StorageManager:
    """StorageManager backed by the SQLite database."""

    def __init__(self, db_path: str = "data/profiles.db"):
        self.db = DatabaseManager(db_path)
        logger.info(f"StorageManager initialized with database: {db_path}")

    async def initialize(self):
        await self.db.initialize()

    # Profile methods
    async def insert_profile_if_absent(self, profile: Profile) -> bool:
        return await self.db.insert_profile_if_absent(profile)

    async def save_profile(self, profile: Profile, expected_row_version: int | None = None):
        await self.db.save_profile(profile, expected_row_version)

    async def get_profile(self, profile_id: str, include_deleted: bool = False) -> Profile | None:
        return await self.db.get_profile(profile_id, include_deleted)

    async def update_profile(self, profile: Profile, expected_row_version: int | None = None):
        await self.db.update_profile(profile, expected_row_version)

    async def durable_update_profile(self, profile: Profile, expected_row_version: int) -> None:
        await self.db.durable_update_profile(profile, expected_row_version)

    async def set_proxy_check(self, profile_id: str, record: ProxyCheckRecord | None) -> None:
        await self.db.set_proxy_check(profile_id, record)

    async def set_last_exit(
        self,
        profile_id: str,
        exit_ip: str,
        exit_country: str | None,
        exit_at: datetime,
    ) -> None:
        await self.db.set_last_exit(profile_id, exit_ip, exit_country, exit_at)

    async def set_launch_pin(
        self,
        profile_id: str,
        fingerprint: dict | None,
        last_used: datetime,
        isolation_config: dict | None = None,
        fingerprint_hash: str | None = None,
    ) -> None:
        await self.db.set_launch_pin(
            profile_id, fingerprint, last_used, isolation_config, fingerprint_hash
        )

    async def delete_profile(self, profile_id: str) -> bool:
        return await self.db.delete_profile(profile_id)

    def list_fingerprint_hashes(self, exclude_profile_id: str | None = None) -> set[str]:
        return self.db.list_fingerprint_hashes(exclude_profile_id)

    async def list_profiles(
        self, filters: dict | None = None, limit: int | None = None, offset: int = 0
    ) -> list[Profile]:
        return await self.db.list_profiles(filters, limit, offset)

    async def count_profiles(self, filters: dict | None = None) -> int:
        return await self.db.count_profiles(filters)

    # Group methods
    async def save_profile_group(self, group: ProfileGroup):
        await self.db.save_profile_group(group)

    async def list_profile_groups(self) -> list[ProfileGroup]:
        return await self.db.list_profile_groups()

    async def delete_profile_group(self, group_id: str) -> bool:
        return await self.db.delete_profile_group(group_id)

    # Statistics methods
    async def log_usage(self, usage_stats: UsageStats):
        await self.db.log_usage(usage_stats)

    async def get_profile_usage_stats(self, profile_id: str) -> list[UsageStats]:
        return await self.db.get_profile_usage_stats(profile_id)

    # Lease methods
    async def acquire_lease(
        self, profile_id: str, holder: str, ttl_seconds: int, include_deleted: bool = False
    ) -> bool:
        return await self.db.acquire_lease(profile_id, holder, ttl_seconds, include_deleted)

    async def renew_lease(self, profile_ids: list[str], holder: str, ttl_seconds: int) -> int:
        return await self.db.renew_lease(profile_ids, holder, ttl_seconds)

    async def release_lease(self, profile_id: str, holder: str) -> bool:
        return await self.db.release_lease(profile_id, holder)

    async def force_release_lease(self, profile_id: str) -> str | None:
        return await self.db.force_release_lease(profile_id)

    async def get_lease(self, profile_id: str) -> tuple[str | None, str | None] | None:
        return await self.db.get_lease(profile_id)

    async def get_lease_holders(self) -> list[dict]:
        return await self.db.get_lease_holders()

    async def close(self):
        await self.db.close()
