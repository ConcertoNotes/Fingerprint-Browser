"""Multi-tenant SQLite storage backend for the standalone Prism cloud configuration server."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .resolver import filter_nodes, mask_source_payload, normalize_node_filter, summarize_nodes

DEFAULT_SEED_POLICY_PATH = Path(__file__).resolve().parent / "seed" / "isolation_policy.json"
DEFAULT_TENANT_ID = "default"
MAX_AUDIT_LOGS = 2000
EXPIRING_SOON_DAYS = 7

_UNSET: Any = object()


def utc_now() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def parse_iso_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp (including trailing ``Z``) into an aware UTC datetime."""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    normalized = cleaned[:-1] + "+00:00" if cleaned.endswith(("Z", "z")) else cleaned
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize_expires_at(value: str | None) -> str | None:
    """Validate and normalize an optional expiration timestamp to ISO-8601 UTC."""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        dt = parse_iso_datetime(cleaned)
    except ValueError as exc:
        raise ValueError(f"Invalid ISO-8601 expires_at timestamp: {value}") from exc
    return dt.isoformat() if dt is not None else None


def is_expired(expires_at: str | None, *, now: datetime | None = None) -> bool:
    """Return True when ``expires_at`` is set and at or before ``now`` (UTC)."""
    dt = parse_iso_datetime(expires_at)
    if dt is None:
        return False
    ref = now or datetime.now(timezone.utc)
    return ref >= dt


def compute_days_remaining(expires_at: str | None, *, now: datetime | None = None) -> int | None:
    """Compute whole remaining days until ``expires_at`` (0 if expired, None if permanent)."""
    dt = parse_iso_datetime(expires_at)
    if dt is None:
        return None
    ref = now or datetime.now(timezone.utc)
    delta_seconds = (dt - ref).total_seconds()
    if delta_seconds <= 0:
        return 0
    return max(0, int((delta_seconds + 86399) // 86400))


def canonical_json(data: Any) -> str:
    """Serialize JSON deterministically for SHA-256 verification across client and server."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_sha256(data: Any) -> str:
    """Compute the SHA-256 hex digest of the canonical JSON representation of ``data``."""
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def hash_token(token: str) -> str:
    """Compute the SHA-256 hex digest of a raw client bearer token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_tenant_token() -> tuple[str, str, str]:
    """Generate ``(raw_token, token_hash, token_prefix)`` for a tenant.

    Raw tokens use ``prism_<32 urlsafe chars>``. Only the SHA-256 hash and a short
    non-secret prefix (e.g. ``prism_a1b2c3d4``) are persisted in SQLite.
    """
    raw_token = f"prism_{secrets.token_urlsafe(24)}"
    return raw_token, hash_token(raw_token), raw_token[:14]


def generate_seal_salt() -> str:
    """Generate a random 32-byte hex salt used for client-side cloud node sealing."""
    return secrets.token_hex(32)


def _env_first(*names: str) -> str:
    for name in names:
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return ""


class CloudStore:
    """Thread-safe multi-tenant SQLite store for cloud policies, subscriptions, and profiles."""

    def __init__(
        self,
        db_path: str | Path = "data/cloud_server.db",
        *,
        seed_policy_path: str | Path | None = None,
        secret_key: str | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self._is_memory = str(self.db_path) == ":memory:"
        if not self._is_memory:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.seed_policy_path = (
            Path(seed_policy_path) if seed_policy_path else DEFAULT_SEED_POLICY_PATH
        )
        self._secret_key_bytes = self._load_or_create_secret_key(secret_key)
        self._fernet = Fernet(self._secret_key_bytes)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._initialize()

    # --- Secret Key & Encryption --------------------------------------------

    def _load_or_create_secret_key(self, explicit_key: str | None) -> bytes:
        candidate = (explicit_key or "").strip() or _env_first(
            "PRISM_CLOUD_SECRET_KEY", "LULU_CLOUD_SECRET_KEY"
        )
        if candidate:
            key_bytes = candidate.encode("utf-8")
            Fernet(key_bytes)
            return key_bytes

        if self._is_memory:
            return Fernet.generate_key()

        key_path = self.db_path.parent / "cloud_secret.key"
        if key_path.is_file():
            stored = key_path.read_text(encoding="utf-8").strip().encode("utf-8")
            Fernet(stored)
            return stored

        generated = Fernet.generate_key()
        tmp_path = key_path.with_suffix(".key.tmp")
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, generated + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, key_path)
        os.chmod(key_path, 0o600)
        return generated

    def derive_hmac_key(self, purpose: bytes) -> bytes:
        """Derive a domain-separated HMAC key from the server's secret key."""
        return hmac.new(self._secret_key_bytes, purpose, hashlib.sha256).digest()

    def _encrypt_json(self, data: Any) -> str:
        raw = canonical_json(data).encode("utf-8")
        return self._fernet.encrypt(raw).decode("ascii")

    def _decrypt_json(self, token: str | None) -> Any:
        if not token:
            return None
        try:
            raw = self._fernet.decrypt(token.encode("ascii"))
        except InvalidToken as exc:
            raise ValueError(
                "Failed to decrypt stored subscription payload; check PRISM_CLOUD_SECRET_KEY"
            ) from exc
        return json.loads(raw.decode("utf-8"))

    # --- Schema & Auto-Migration --------------------------------------------

    def _initialize(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA foreign_keys = ON")

            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS isolation_policies (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    version TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    policy_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS managed_subscriptions (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    version TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    subscriptions_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_cloud_profiles_schema()

            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tenants (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    token_hash TEXT UNIQUE,
                    token_prefix TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'disabled')),
                    expires_at TEXT,
                    max_devices INTEGER NOT NULL DEFAULT 3,
                    offline_grace_hours INTEGER NOT NULL DEFAULT 24,
                    profiles_sync_enabled INTEGER NOT NULL DEFAULT 0,
                    seal_salt TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tenant_devices (
                    tenant_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    device_label TEXT NOT NULL DEFAULT '',
                    client_version TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (tenant_id, device_id),
                    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subscription_sources (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'shared' CHECK(scope IN ('shared', 'tenant_private')),
                    owner_tenant_id TEXT,
                    kind TEXT NOT NULL CHECK(kind IN ('url', 'content', 'manual')),
                    payload_enc TEXT NOT NULL,
                    resolved_nodes_enc TEXT,
                    node_count INTEGER NOT NULL DEFAULT 0,
                    last_fetched_at TEXT,
                    last_error TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (owner_tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tenant_assignments (
                    tenant_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    display_name_override TEXT,
                    node_filter_json TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, source_id),
                    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
                    FOREIGN KEY (source_id) REFERENCES subscription_sources(id) ON DELETE CASCADE
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_id TEXT,
                    detail_json TEXT,
                    ip TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cloud_profiles_tenant ON cloud_profiles(tenant_id, updated_at DESC)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sources_scope_owner ON subscription_sources(scope, owner_tenant_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at DESC)"
            )
            self._conn.commit()
            self._seed_defaults()

    def _ensure_cloud_profiles_schema(self) -> None:
        table = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cloud_profiles'"
        ).fetchone()
        if table is None:
            self._conn.execute(
                """
                CREATE TABLE cloud_profiles (
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    snapshot_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, id)
                )
                """
            )
            return

        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(cloud_profiles)").fetchall()
        }
        if "tenant_id" in columns:
            return

        # Lossless upgrade from single-tenant cloud_profiles(id PK) to (tenant_id, id) PK.
        self._conn.execute("ALTER TABLE cloud_profiles RENAME TO cloud_profiles_legacy")
        self._conn.execute(
            """
            CREATE TABLE cloud_profiles (
                tenant_id TEXT NOT NULL DEFAULT 'default',
                id TEXT NOT NULL,
                name TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                snapshot_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, id)
            )
            """
        )
        self._conn.execute(
            """
            INSERT INTO cloud_profiles (tenant_id, id, name, revision, snapshot_json, updated_at)
            SELECT 'default', id, name, revision, snapshot_json, updated_at
            FROM cloud_profiles_legacy
            """
        )
        self._conn.execute("DROP TABLE cloud_profiles_legacy")

    def _seed_defaults(self) -> None:
        stamp = utc_now()
        policy_row = self._conn.execute("SELECT 1 FROM isolation_policies WHERE id = 1").fetchone()
        if policy_row is None:
            policy = self._load_seed_policy()
            version = str(policy.get("policy_version") or "prism-cloud-seed-1")
            digest = canonical_sha256(policy)
            self._conn.execute(
                "INSERT INTO isolation_policies (id, version, sha256, policy_json, updated_at) "
                "VALUES (1, ?, ?, ?, ?)",
                (version, digest, canonical_json(policy), stamp),
            )
        subs_row = self._conn.execute("SELECT 1 FROM managed_subscriptions WHERE id = 1").fetchone()
        if subs_row is None:
            empty_subs: list[dict[str, Any]] = []
            self._conn.execute(
                "INSERT INTO managed_subscriptions (id, version, sha256, subscriptions_json, updated_at) "
                "VALUES (1, ?, ?, ?, ?)",
                ("subs-0", canonical_sha256(empty_subs), canonical_json(empty_subs), stamp),
            )
        self._conn.commit()

    def _load_seed_policy(self) -> dict[str, Any]:
        if self.seed_policy_path.is_file():
            raw = json.loads(self.seed_policy_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        return {
            "schema_version": 1,
            "policy_version": "prism-cloud-seed-1",
            "default_tier": "strict",
            "default_webrtc_policy": "block",
            "default_canvas_policy": "stable_hardware",
            "default_locale_mode": "fixed",
            "default_languages": ["en-US", "en"],
            "default_timezone_mode": "fixed",
            "default_timezone": "America/Los_Angeles",
            "install_webrtc_guard": True,
            "hardening_prefs": {
                "network.trr.mode": 5,
                "network.dns.disableIPv6": True,
                "network.dns.echconfig.enabled": False,
                "network.dns.http3_echconfig.enabled": False,
                "network.http.http3.enable": False,
                "network.proxy.socks_remote_dns": True,
                "network.proxy.no_proxies_on": "",
                "network.proxy.failover_direct": False,
                "network.proxy.share_proxy_settings": True,
                "security.OCSP.enabled": 0,
                "security.OCSP.require": False,
                "dom.battery.enabled": False,
            },
            "preset_filters": {
                "min_width": 1280,
                "min_height": 720,
                "min_cores": 4,
                "min_chrome_major": 125,
                "blocked_renderers": ["SwiftShader", "llvmpipe"],
            },
            "curated_presets": [],
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- Isolation Policy ---------------------------------------------------

    def get_isolation_policy(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT version, sha256, policy_json, updated_at FROM isolation_policies WHERE id = 1"
            ).fetchone()
            assert row is not None
            return {
                "version": row["version"],
                "sha256": row["sha256"],
                "updated_at": row["updated_at"],
                "policy": json.loads(row["policy_json"]),
            }

    def set_isolation_policy(
        self, policy: dict[str, Any], *, version: str | None = None
    ) -> dict[str, Any]:
        if not isinstance(policy, dict):
            raise ValueError("Policy payload must be a JSON object")
        normalized = dict(policy)
        resolved_version = (version or str(normalized.get("policy_version") or "")).strip()
        if not resolved_version:
            resolved_version = (
                f"prism-cloud-{datetime.now(timezone.utc).strftime('%Y.%m.%d.%H%M%S')}"
            )
        normalized["policy_version"] = resolved_version
        digest = canonical_sha256(normalized)
        stamp = utc_now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO isolation_policies (id, version, sha256, policy_json, updated_at) "
                "VALUES (1, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET version=excluded.version, sha256=excluded.sha256, "
                "policy_json=excluded.policy_json, updated_at=excluded.updated_at",
                (resolved_version, digest, canonical_json(normalized), stamp),
            )
            self._conn.commit()
        return {
            "version": resolved_version,
            "sha256": digest,
            "updated_at": stamp,
            "policy": normalized,
        }

    # --- Legacy Single-Tenant Managed Subscriptions -------------------------

    def get_subscriptions(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT version, sha256, subscriptions_json, updated_at "
                "FROM managed_subscriptions WHERE id = 1"
            ).fetchone()
            assert row is not None
            return {
                "version": row["version"],
                "sha256": row["sha256"],
                "updated_at": row["updated_at"],
                "subscriptions": json.loads(row["subscriptions_json"]),
            }

    def set_subscriptions(
        self, subscriptions: list[dict[str, Any]], *, version: str | None = None
    ) -> dict[str, Any]:
        seen_ids: set[str] = set()
        for item in subscriptions:
            source_id = str(item.get("cloud_source_id", "")).strip()
            if not source_id:
                raise ValueError("Every subscription must have a non-empty cloud_source_id")
            if source_id in seen_ids:
                raise ValueError(f"Duplicate cloud_source_id: {source_id}")
            seen_ids.add(source_id)

        digest = canonical_sha256(subscriptions)
        stamp = utc_now()
        with self._lock:
            current = self._conn.execute(
                "SELECT version FROM managed_subscriptions WHERE id = 1"
            ).fetchone()
            resolved_version = (version or "").strip()
            if not resolved_version:
                next_num = 1
                if current and isinstance(current["version"], str):
                    prefix, _, suffix = current["version"].partition("-")
                    if prefix == "subs" and suffix.isdigit():
                        next_num = int(suffix) + 1
                resolved_version = f"subs-{next_num}"
            self._conn.execute(
                "INSERT INTO managed_subscriptions (id, version, sha256, subscriptions_json, updated_at) "
                "VALUES (1, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET version=excluded.version, sha256=excluded.sha256, "
                "subscriptions_json=excluded.subscriptions_json, updated_at=excluded.updated_at",
                (resolved_version, digest, canonical_json(subscriptions), stamp),
            )
            self._conn.commit()
        return {
            "version": resolved_version,
            "sha256": digest,
            "updated_at": stamp,
            "subscriptions": subscriptions,
        }

    # --- Tenants ------------------------------------------------------------

    def ensure_default_tenant(self) -> dict[str, Any]:
        """Ensure the built-in ``default`` tenant exists for single-tenant / dev mode."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tenants WHERE id = ?", (DEFAULT_TENANT_ID,)
            ).fetchone()
            if row is not None:
                return self._tenant_internal_dict(row)
            stamp = utc_now()
            salt = generate_seal_salt()
            self._conn.execute(
                """
                INSERT INTO tenants (
                    id, name, notes, token_hash, token_prefix, status, expires_at,
                    max_devices, offline_grace_hours, profiles_sync_enabled, seal_salt,
                    created_at, updated_at, last_seen_at
                ) VALUES (?, ?, ?, NULL, ?, 'active', NULL, 0, 24, 1, ?, ?, ?, NULL)
                """,
                (
                    DEFAULT_TENANT_ID,
                    "默认租户",
                    "兼容单租户与本地开发模式的内置租户",
                    "env-token",
                    salt,
                    stamp,
                    stamp,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM tenants WHERE id = ?", (DEFAULT_TENANT_ID,)
            ).fetchone()
            assert row is not None
            return self._tenant_internal_dict(row)

    def has_managed_tenants(self) -> bool:
        """Return True if at least one non-default customer tenant exists."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM tenants WHERE id != ? LIMIT 1", (DEFAULT_TENANT_ID,)
            ).fetchone()
            return row is not None

    def _tenant_internal_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "notes": row["notes"],
            "token_prefix": row["token_prefix"],
            "status": row["status"],
            "expires_at": row["expires_at"],
            "max_devices": int(row["max_devices"]),
            "offline_grace_hours": int(row["offline_grace_hours"]),
            "profiles_sync_enabled": bool(row["profiles_sync_enabled"]),
            "seal_salt": row["seal_salt"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_seen_at": row["last_seen_at"],
        }

    def _tenant_public_dict(
        self, row: sqlite3.Row, *, now: datetime | None = None
    ) -> dict[str, Any]:
        ref = now or datetime.now(timezone.utc)
        tenant_id = row["id"]
        dev_stats = self._conn.execute(
            """
            SELECT
                SUM(CASE WHEN revoked = 0 THEN 1 ELSE 0 END) AS active_cnt,
                COUNT(*) AS total_cnt
            FROM tenant_devices
            WHERE tenant_id = ?
            """,
            (tenant_id,),
        ).fetchone()
        active_devices = int(dev_stats["active_cnt"] or 0) if dev_stats else 0
        total_devices = int(dev_stats["total_cnt"] or 0) if dev_stats else 0

        prof_row = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM cloud_profiles WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
        profiles_count = int(prof_row["cnt"] or 0) if prof_row else 0

        subs_payload = self._build_tenant_sealed_subscriptions(tenant_id)
        assigned_sources_count = len(subs_payload["subscriptions"])
        assigned_nodes_count = sum(
            len(item.get("nodes") or []) for item in subs_payload["subscriptions"]
        )

        status = row["status"]
        expires_at = row["expires_at"]
        days_remaining = compute_days_remaining(expires_at, now=ref)
        if status == "disabled":
            effective_status = "disabled"
        elif is_expired(expires_at, now=ref):
            effective_status = "expired"
        elif days_remaining is not None and days_remaining <= EXPIRING_SOON_DAYS:
            effective_status = "expiring_soon"
        else:
            effective_status = "active"

        return {
            "id": tenant_id,
            "name": row["name"],
            "notes": row["notes"],
            "token_prefix": row["token_prefix"],
            "status": status,
            "effective_status": effective_status,
            "expires_at": expires_at,
            "days_remaining": days_remaining,
            "max_devices": int(row["max_devices"]),
            "active_devices": active_devices,
            "total_devices": total_devices,
            "offline_grace_hours": int(row["offline_grace_hours"]),
            "profiles_sync_enabled": bool(row["profiles_sync_enabled"]),
            "assigned_sources_count": assigned_sources_count,
            "assigned_nodes_count": assigned_nodes_count,
            "profiles_count": profiles_count,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_seen_at": row["last_seen_at"],
        }

    def create_tenant(
        self,
        *,
        name: str,
        notes: str = "",
        expires_at: str | None = None,
        max_devices: int = 3,
        offline_grace_hours: int = 24,
        profiles_sync_enabled: bool = False,
        status: str = "active",
        tenant_id: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("客户名称不能为空")
        if status not in {"active", "disabled"}:
            raise ValueError("status must be 'active' or 'disabled'")
        if max_devices < 0:
            raise ValueError("max_devices must be >= 0")
        if offline_grace_hours < 0:
            raise ValueError("offline_grace_hours must be >= 0")
        normalized_expiry = normalize_expires_at(expires_at)
        resolved_id = (tenant_id or f"tnt_{uuid.uuid4().hex[:12]}").strip()
        if resolved_id == DEFAULT_TENANT_ID:
            raise ValueError("Tenant id 'default' is reserved")

        raw_token, token_hash, token_prefix = generate_tenant_token()
        seal_salt = generate_seal_salt()
        stamp = utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO tenants (
                    id, name, notes, token_hash, token_prefix, status, expires_at,
                    max_devices, offline_grace_hours, profiles_sync_enabled, seal_salt,
                    created_at, updated_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    resolved_id,
                    clean_name,
                    notes.strip(),
                    token_hash,
                    token_prefix,
                    status,
                    normalized_expiry,
                    int(max_devices),
                    int(offline_grace_hours),
                    1 if profiles_sync_enabled else 0,
                    seal_salt,
                    stamp,
                    stamp,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM tenants WHERE id = ?", (resolved_id,)
            ).fetchone()
            assert row is not None
            return self._tenant_public_dict(row), raw_token

    def list_tenants(
        self,
        *,
        include_default: bool = False,
        search: str | None = None,
        status_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            query = "SELECT * FROM tenants"
            params: list[Any] = []
            if not include_default:
                query += " WHERE id != ?"
                params.append(DEFAULT_TENANT_ID)
            query += " ORDER BY created_at DESC, id ASC"
            rows = self._conn.execute(query, params).fetchall()
            now = datetime.now(timezone.utc)
            items = [self._tenant_public_dict(row, now=now) for row in rows]

        if search and search.strip():
            q = search.strip().casefold()
            items = [
                item
                for item in items
                if q in item["name"].casefold()
                or q in item["notes"].casefold()
                or q in item["token_prefix"].casefold()
                or q in item["id"].casefold()
            ]
        if status_filter and status_filter.strip() and status_filter != "all":
            wanted = status_filter.strip().lower()
            if wanted == "active":
                items = [i for i in items if i["effective_status"] in {"active", "expiring_soon"}]
            else:
                items = [i for i in items if i["effective_status"] == wanted]
        return items

    def get_tenant(self, tenant_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
            if row is None:
                return None
            return self._tenant_public_dict(row)

    def get_tenant_internal(self, tenant_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
            return self._tenant_internal_dict(row) if row is not None else None

    def get_tenant_by_token(self, raw_token: str) -> dict[str, Any] | None:
        digest = hash_token(raw_token)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tenants WHERE token_hash = ?", (digest,)
            ).fetchone()
            return self._tenant_internal_dict(row) if row is not None else None

    def update_tenant(
        self,
        tenant_id: str,
        *,
        name: str = _UNSET,
        notes: str = _UNSET,
        status: str = _UNSET,
        expires_at: str | None = _UNSET,
        extend_days: int | None = None,
        max_devices: int = _UNSET,
        offline_grace_hours: int = _UNSET,
        profiles_sync_enabled: bool = _UNSET,
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
            if row is None:
                return None

            new_name = row["name"] if name is _UNSET else name.strip()
            if not new_name:
                raise ValueError("客户名称不能为空")
            new_notes = row["notes"] if notes is _UNSET else notes.strip()
            new_status = row["status"] if status is _UNSET else status
            if new_status not in {"active", "disabled"}:
                raise ValueError("status must be 'active' or 'disabled'")

            if extend_days is not None:
                if extend_days <= 0:
                    raise ValueError("extend_days must be > 0")
                now = datetime.now(timezone.utc)
                current_dt = parse_iso_datetime(row["expires_at"])
                base = current_dt if (current_dt is not None and current_dt > now) else now
                new_expires_at: str | None = (base + timedelta(days=extend_days)).isoformat()
            elif expires_at is _UNSET:
                new_expires_at = row["expires_at"]
            else:
                new_expires_at = normalize_expires_at(expires_at)

            new_max_devices = int(row["max_devices"]) if max_devices is _UNSET else int(max_devices)
            if new_max_devices < 0:
                raise ValueError("max_devices must be >= 0")

            new_grace = (
                int(row["offline_grace_hours"])
                if offline_grace_hours is _UNSET
                else int(offline_grace_hours)
            )
            if new_grace < 0:
                raise ValueError("offline_grace_hours must be >= 0")

            new_sync = (
                int(row["profiles_sync_enabled"])
                if profiles_sync_enabled is _UNSET
                else (1 if profiles_sync_enabled else 0)
            )

            stamp = utc_now()
            self._conn.execute(
                """
                UPDATE tenants
                SET name = ?, notes = ?, status = ?, expires_at = ?, max_devices = ?,
                    offline_grace_hours = ?, profiles_sync_enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    new_name,
                    new_notes,
                    new_status,
                    new_expires_at,
                    new_max_devices,
                    new_grace,
                    new_sync,
                    stamp,
                    tenant_id,
                ),
            )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM tenants WHERE id = ?", (tenant_id,)
            ).fetchone()
            assert updated is not None
            return self._tenant_public_dict(updated)

    def delete_tenant(self, tenant_id: str) -> bool:
        if tenant_id == DEFAULT_TENANT_ID:
            raise ValueError("Built-in default tenant cannot be deleted")
        with self._lock:
            self._conn.execute("DELETE FROM cloud_profiles WHERE tenant_id = ?", (tenant_id,))
            cursor = self._conn.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))
            self._conn.commit()
            return cursor.rowcount > 0

    def rotate_tenant_token(self, tenant_id: str) -> tuple[dict[str, Any], str] | None:
        if tenant_id == DEFAULT_TENANT_ID:
            raise ValueError("Built-in default tenant token is managed via environment variables")
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
            if row is None:
                return None
            raw_token, token_hash, token_prefix = generate_tenant_token()
            new_salt = generate_seal_salt()
            stamp = utc_now()
            self._conn.execute(
                """
                UPDATE tenants
                SET token_hash = ?, token_prefix = ?, seal_salt = ?, updated_at = ?
                WHERE id = ?
                """,
                (token_hash, token_prefix, new_salt, stamp, tenant_id),
            )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM tenants WHERE id = ?", (tenant_id,)
            ).fetchone()
            assert updated is not None
            return self._tenant_public_dict(updated), raw_token

    def touch_tenant_seen(self, tenant_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tenants SET last_seen_at = ? WHERE id = ?",
                (utc_now(), tenant_id),
            )
            self._conn.commit()

    # --- Devices ------------------------------------------------------------

    def count_active_devices(self, tenant_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM tenant_devices WHERE tenant_id = ? AND revoked = 0",
                (tenant_id,),
            ).fetchone()
            return int(row["cnt"] or 0) if row else 0

    def verify_and_record_device(
        self,
        tenant: dict[str, Any],
        *,
        device_id: str | None,
        device_label: str | None = None,
        client_version: str | None = None,
    ) -> tuple[str | None, dict[str, int]]:
        """Validate device revocation & limits, recording/updating the device heartbeat."""
        tenant_id = str(tenant["id"])
        max_devices = int(tenant["max_devices"])
        clean_device_id = (device_id or "").strip()[:128]
        clean_label = (device_label or "").strip()[:128]
        clean_ver = (client_version or "").strip()[:64]

        with self._lock:
            active_count = self.count_active_devices(tenant_id)
            if not clean_device_id:
                return None, {"active_devices": active_count, "max_devices": max_devices}

            existing = self._conn.execute(
                """
                SELECT device_label, client_version, revoked
                FROM tenant_devices
                WHERE tenant_id = ? AND device_id = ?
                """,
                (tenant_id, clean_device_id),
            ).fetchone()
            stamp = utc_now()

            if existing is not None:
                if int(existing["revoked"]) == 1:
                    return "device_revoked", {
                        "active_devices": active_count,
                        "max_devices": max_devices,
                    }
                self._conn.execute(
                    """
                    UPDATE tenant_devices
                    SET device_label = ?, client_version = ?, last_seen_at = ?
                    WHERE tenant_id = ? AND device_id = ?
                    """,
                    (
                        clean_label or existing["device_label"],
                        clean_ver or existing["client_version"],
                        stamp,
                        tenant_id,
                        clean_device_id,
                    ),
                )
                self._conn.commit()
                return None, {"active_devices": active_count, "max_devices": max_devices}

            if max_devices > 0 and active_count >= max_devices:
                return "device_limit_exceeded", {
                    "active_devices": active_count,
                    "max_devices": max_devices,
                }

            self._conn.execute(
                """
                INSERT INTO tenant_devices (
                    tenant_id, device_id, device_label, client_version,
                    first_seen_at, last_seen_at, revoked
                ) VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
                (tenant_id, clean_device_id, clean_label, clean_ver, stamp, stamp),
            )
            self._conn.commit()
            return None, {"active_devices": active_count + 1, "max_devices": max_devices}

    def list_tenant_devices(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT tenant_id, device_id, device_label, client_version,
                       first_seen_at, last_seen_at, revoked
                FROM tenant_devices
                WHERE tenant_id = ?
                ORDER BY revoked ASC, last_seen_at DESC, device_id ASC
                """,
                (tenant_id,),
            ).fetchall()
            return [
                {
                    "tenant_id": row["tenant_id"],
                    "device_id": row["device_id"],
                    "device_label": row["device_label"],
                    "client_version": row["client_version"],
                    "first_seen_at": row["first_seen_at"],
                    "last_seen_at": row["last_seen_at"],
                    "revoked": bool(row["revoked"]),
                }
                for row in rows
            ]

    def revoke_tenant_device(self, tenant_id: str, device_id: str, *, purge: bool = False) -> bool:
        """Revoke a device (freeing its slot while blocking reconnect) or purge its row."""
        with self._lock:
            if purge:
                cursor = self._conn.execute(
                    "DELETE FROM tenant_devices WHERE tenant_id = ? AND device_id = ?",
                    (tenant_id, device_id),
                )
            else:
                cursor = self._conn.execute(
                    "UPDATE tenant_devices SET revoked = 1 WHERE tenant_id = ? AND device_id = ?",
                    (tenant_id, device_id),
                )
            self._conn.commit()
            return cursor.rowcount > 0

    # --- Subscription Sources (Encrypted at Rest) ---------------------------

    def _source_public_dict(
        self, row: sqlite3.Row, *, include_nodes_summary: bool = False
    ) -> dict[str, Any]:
        payload = self._decrypt_json(row["payload_enc"]) or {}
        result: dict[str, Any] = {
            "id": row["id"],
            "name": row["name"],
            "scope": row["scope"],
            "owner_tenant_id": row["owner_tenant_id"],
            "kind": row["kind"],
            "payload_preview": mask_source_payload(row["kind"], payload),
            "node_count": int(row["node_count"]),
            "last_fetched_at": row["last_fetched_at"],
            "last_error": row["last_error"],
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if include_nodes_summary:
            nodes = self._decrypt_json(row["resolved_nodes_enc"]) or []
            result["nodes_preview"] = summarize_nodes(nodes)
        return result

    def create_source(
        self,
        *,
        name: str,
        kind: str,
        payload: dict[str, Any],
        scope: str = "shared",
        owner_tenant_id: str | None = None,
        enabled: bool = True,
        resolved_nodes: list[dict[str, Any]] | None = None,
        last_error: str | None = None,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("上游源名称不能为空")
        if scope not in {"shared", "tenant_private"}:
            raise ValueError("scope must be 'shared' or 'tenant_private'")
        if kind not in {"url", "content", "manual"}:
            raise ValueError("kind must be 'url', 'content', or 'manual'")
        if scope == "tenant_private" and not owner_tenant_id:
            raise ValueError("tenant_private source requires owner_tenant_id")
        if scope == "shared":
            owner_tenant_id = None

        resolved_id = (source_id or f"src_{uuid.uuid4().hex[:12]}").strip()
        payload_enc = self._encrypt_json(payload)
        resolved_nodes_enc = (
            self._encrypt_json(resolved_nodes) if resolved_nodes is not None else None
        )
        node_count = len(resolved_nodes) if resolved_nodes is not None else 0
        stamp = utc_now()
        fetched_at = stamp if resolved_nodes is not None else None

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO subscription_sources (
                    id, name, scope, owner_tenant_id, kind, payload_enc, resolved_nodes_enc,
                    node_count, last_fetched_at, last_error, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolved_id,
                    clean_name,
                    scope,
                    owner_tenant_id,
                    kind,
                    payload_enc,
                    resolved_nodes_enc,
                    node_count,
                    fetched_at,
                    last_error,
                    1 if enabled else 0,
                    stamp,
                    stamp,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM subscription_sources WHERE id = ?", (resolved_id,)
            ).fetchone()
            assert row is not None
            return self._source_public_dict(row, include_nodes_summary=True)

    def list_sources(
        self,
        *,
        scope: str | None = None,
        owner_tenant_id: str | None = None,
        include_nodes_summary: bool = False,
    ) -> list[dict[str, Any]]:
        with self._lock:
            clauses: list[str] = []
            params: list[Any] = []
            if scope:
                clauses.append("scope = ?")
                params.append(scope)
            if owner_tenant_id is not None:
                clauses.append("owner_tenant_id = ?")
                params.append(owner_tenant_id)
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = self._conn.execute(
                f"SELECT * FROM subscription_sources{where} ORDER BY created_at DESC, id ASC",
                params,
            ).fetchall()
            return [
                self._source_public_dict(row, include_nodes_summary=include_nodes_summary)
                for row in rows
            ]

    def get_source(
        self, source_id: str, *, include_nodes_summary: bool = True
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM subscription_sources WHERE id = ?", (source_id,)
            ).fetchone()
            if row is None:
                return None
            return self._source_public_dict(row, include_nodes_summary=include_nodes_summary)

    def get_source_secret_payload(
        self, source_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Internal helper: return ``(metadata_dict, decrypted_payload_dict)`` for server resolution."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM subscription_sources WHERE id = ?", (source_id,)
            ).fetchone()
            if row is None:
                return None
            payload = self._decrypt_json(row["payload_enc"]) or {}
            meta = {
                "id": row["id"],
                "name": row["name"],
                "scope": row["scope"],
                "owner_tenant_id": row["owner_tenant_id"],
                "kind": row["kind"],
                "enabled": bool(row["enabled"]),
            }
            return meta, payload

    def update_source(
        self,
        source_id: str,
        *,
        name: str = _UNSET,
        enabled: bool = _UNSET,
        kind: str = _UNSET,
        payload: dict[str, Any] = _UNSET,
        resolved_nodes: list[dict[str, Any]] | None = _UNSET,
        last_error: str | None = _UNSET,
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM subscription_sources WHERE id = ?", (source_id,)
            ).fetchone()
            if row is None:
                return None
            new_name = row["name"] if name is _UNSET else name.strip()
            if not new_name:
                raise ValueError("上游源名称不能为空")
            new_enabled = row["enabled"] if enabled is _UNSET else (1 if enabled else 0)
            new_kind = row["kind"] if kind is _UNSET else kind
            if new_kind not in {"url", "content", "manual"}:
                raise ValueError("kind must be 'url', 'content', or 'manual'")
            new_payload_enc = (
                row["payload_enc"] if payload is _UNSET else self._encrypt_json(payload)
            )
            stamp = utc_now()
            if resolved_nodes is _UNSET:
                new_resolved_enc = row["resolved_nodes_enc"]
                new_node_count = int(row["node_count"])
                new_fetched_at = row["last_fetched_at"]
            elif resolved_nodes is None:
                new_resolved_enc = None
                new_node_count = 0
                new_fetched_at = row["last_fetched_at"]
            else:
                new_resolved_enc = self._encrypt_json(resolved_nodes)
                new_node_count = len(resolved_nodes)
                new_fetched_at = stamp
            new_error = row["last_error"] if last_error is _UNSET else last_error

            self._conn.execute(
                """
                UPDATE subscription_sources
                SET name = ?, enabled = ?, kind = ?, payload_enc = ?, resolved_nodes_enc = ?,
                    node_count = ?, last_fetched_at = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    new_name,
                    new_enabled,
                    new_kind,
                    new_payload_enc,
                    new_resolved_enc,
                    new_node_count,
                    new_fetched_at,
                    new_error,
                    stamp,
                    source_id,
                ),
            )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM subscription_sources WHERE id = ?", (source_id,)
            ).fetchone()
            assert updated is not None
            return self._source_public_dict(updated, include_nodes_summary=True)

    def set_source_resolution(
        self,
        source_id: str,
        *,
        resolved_nodes: list[dict[str, Any]] | None,
        last_error: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM subscription_sources WHERE id = ?", (source_id,)
            ).fetchone()
            if row is None:
                return None
            stamp = utc_now()
            if resolved_nodes is not None:
                self._conn.execute(
                    """
                    UPDATE subscription_sources
                    SET resolved_nodes_enc = ?, node_count = ?, last_fetched_at = ?,
                        last_error = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        self._encrypt_json(resolved_nodes),
                        len(resolved_nodes),
                        stamp,
                        stamp,
                        source_id,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE subscription_sources
                    SET last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (last_error, stamp, source_id),
                )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM subscription_sources WHERE id = ?", (source_id,)
            ).fetchone()
            assert updated is not None
            return self._source_public_dict(updated, include_nodes_summary=True)

    def delete_source(self, source_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM subscription_sources WHERE id = ?", (source_id,)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    # --- Tenant Assignments & Sealed Subscriptions --------------------------

    def list_tenant_assignments(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._lock:
            assigned_rows = self._conn.execute(
                """
                SELECT ta.tenant_id, ta.source_id, ta.display_name_override,
                       ta.node_filter_json, ta.enabled AS assign_enabled, ta.created_at,
                       s.name AS source_name, s.scope, s.owner_tenant_id, s.kind,
                       s.resolved_nodes_enc, s.node_count, s.enabled AS source_enabled,
                       s.last_fetched_at, s.last_error
                FROM tenant_assignments ta
                JOIN subscription_sources s ON s.id = ta.source_id
                WHERE ta.tenant_id = ?
                ORDER BY ta.created_at ASC, ta.source_id ASC
                """,
                (tenant_id,),
            ).fetchall()

            seen_ids: set[str] = set()
            results: list[dict[str, Any]] = []
            for row in assigned_rows:
                seen_ids.add(row["source_id"])
                node_filter = normalize_node_filter(
                    json.loads(row["node_filter_json"]) if row["node_filter_json"] else None
                )
                all_nodes = self._decrypt_json(row["resolved_nodes_enc"]) or []
                matched = filter_nodes(all_nodes, node_filter)
                results.append(
                    {
                        "source_id": row["source_id"],
                        "source_name": row["source_name"],
                        "scope": row["scope"],
                        "kind": row["kind"],
                        "display_name_override": row["display_name_override"],
                        "node_filter": node_filter,
                        "enabled": bool(row["assign_enabled"]),
                        "source_enabled": bool(row["source_enabled"]),
                        "total_source_nodes": int(row["node_count"]),
                        "matched_nodes_count": len(matched),
                        "matched_nodes_preview": summarize_nodes(matched),
                        "last_fetched_at": row["last_fetched_at"],
                        "last_error": row["last_error"],
                    }
                )

            # Include owned tenant_private sources that do not have explicit assignment rows.
            private_rows = self._conn.execute(
                """
                SELECT * FROM subscription_sources
                WHERE scope = 'tenant_private' AND owner_tenant_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (tenant_id,),
            ).fetchall()
            for row in private_rows:
                if row["id"] in seen_ids:
                    continue
                all_nodes = self._decrypt_json(row["resolved_nodes_enc"]) or []
                results.append(
                    {
                        "source_id": row["id"],
                        "source_name": row["name"],
                        "scope": row["scope"],
                        "kind": row["kind"],
                        "display_name_override": None,
                        "node_filter": normalize_node_filter(None),
                        "enabled": bool(row["enabled"]),
                        "source_enabled": bool(row["enabled"]),
                        "total_source_nodes": int(row["node_count"]),
                        "matched_nodes_count": len(all_nodes),
                        "matched_nodes_preview": summarize_nodes(all_nodes),
                        "last_fetched_at": row["last_fetched_at"],
                        "last_error": row["last_error"],
                    }
                )
            return results

    def set_tenant_assignments(
        self,
        tenant_id: str,
        assignments: list[dict[str, Any]],
        *,
        replace_private: bool = False,
    ) -> list[dict[str, Any]]:
        with self._lock:
            tenant = self._conn.execute(
                "SELECT 1 FROM tenants WHERE id = ?", (tenant_id,)
            ).fetchone()
            if tenant is None:
                raise KeyError(f"Tenant not found: {tenant_id}")

            validated: list[tuple[str, str | None, str | None, int]] = []
            seen: set[str] = set()
            for item in assignments:
                source_id = str(item.get("source_id") or "").strip()
                if not source_id:
                    raise ValueError("source_id is required for every assignment")
                if source_id in seen:
                    raise ValueError(f"Duplicate assignment for source_id: {source_id}")
                seen.add(source_id)
                src = self._conn.execute(
                    "SELECT id, scope, owner_tenant_id FROM subscription_sources WHERE id = ?",
                    (source_id,),
                ).fetchone()
                if src is None:
                    raise ValueError(f"Subscription source not found: {source_id}")
                if src["scope"] == "tenant_private" and src["owner_tenant_id"] != tenant_id:
                    raise ValueError("Cannot assign another tenant's private source")
                override_raw = item.get("display_name_override")
                override = str(override_raw).strip() if override_raw is not None else None
                if override == "":
                    override = None
                normalized_filter = normalize_node_filter(item.get("node_filter"))
                has_filter = any(normalized_filter.values())
                filter_json = canonical_json(normalized_filter) if has_filter else None
                enabled = 1 if item.get("enabled", True) else 0
                validated.append((source_id, override, filter_json, enabled))

            if replace_private:
                self._conn.execute(
                    "DELETE FROM tenant_assignments WHERE tenant_id = ?", (tenant_id,)
                )
            else:
                self._conn.execute(
                    """
                    DELETE FROM tenant_assignments
                    WHERE tenant_id = ?
                      AND source_id IN (
                          SELECT id FROM subscription_sources WHERE scope = 'shared'
                      )
                    """,
                    (tenant_id,),
                )

            stamp = utc_now()
            for source_id, override, filter_json, enabled in validated:
                self._conn.execute(
                    """
                    INSERT INTO tenant_assignments (
                        tenant_id, source_id, display_name_override, node_filter_json,
                        enabled, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tenant_id, source_id) DO UPDATE SET
                        display_name_override = excluded.display_name_override,
                        node_filter_json = excluded.node_filter_json,
                        enabled = excluded.enabled
                    """,
                    (tenant_id, source_id, override, filter_json, enabled, stamp),
                )

            self._conn.execute("UPDATE tenants SET updated_at = ? WHERE id = ?", (stamp, tenant_id))
            self._conn.commit()
            return self.list_tenant_assignments(tenant_id)

    def _build_tenant_sealed_subscriptions(self, tenant_id: str) -> dict[str, Any]:
        assigned_rows = self._conn.execute(
            """
            SELECT ta.source_id, ta.display_name_override, ta.node_filter_json,
                   ta.enabled AS assign_enabled, ta.created_at AS assign_created,
                   s.name AS source_name, s.scope, s.owner_tenant_id,
                   s.resolved_nodes_enc, s.enabled AS source_enabled, s.updated_at AS source_updated
            FROM tenant_assignments ta
            JOIN subscription_sources s ON s.id = ta.source_id
            WHERE ta.tenant_id = ?
            ORDER BY ta.created_at ASC, ta.source_id ASC
            """,
            (tenant_id,),
        ).fetchall()

        items: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        latest_updated = ""

        for row in assigned_rows:
            seen_ids.add(row["source_id"])
            if not row["assign_enabled"] or not row["source_enabled"]:
                continue
            if row["scope"] == "tenant_private" and row["owner_tenant_id"] != tenant_id:
                continue
            all_nodes = self._decrypt_json(row["resolved_nodes_enc"]) or []
            node_filter = json.loads(row["node_filter_json"]) if row["node_filter_json"] else None
            filtered = filter_nodes(all_nodes, node_filter)
            if not filtered:
                continue
            display_name = (row["display_name_override"] or row["source_name"]).strip() or row[
                "source_name"
            ]
            items.append(
                {
                    "cloud_source_id": row["source_id"],
                    "name": display_name,
                    "kind": "sealed_nodes",
                    "nodes": filtered,
                    "auto_sync": True,
                    "enabled": True,
                }
            )
            latest_updated = max(latest_updated, str(row["source_updated"] or ""))

        private_rows = self._conn.execute(
            """
            SELECT id, name, resolved_nodes_enc, enabled, updated_at
            FROM subscription_sources
            WHERE scope = 'tenant_private' AND owner_tenant_id = ? AND enabled = 1
            ORDER BY created_at ASC, id ASC
            """,
            (tenant_id,),
        ).fetchall()
        for row in private_rows:
            if row["id"] in seen_ids:
                continue
            all_nodes = self._decrypt_json(row["resolved_nodes_enc"]) or []
            if not all_nodes:
                continue
            items.append(
                {
                    "cloud_source_id": row["id"],
                    "name": row["name"],
                    "kind": "sealed_nodes",
                    "nodes": all_nodes,
                    "auto_sync": True,
                    "enabled": True,
                }
            )
            latest_updated = max(latest_updated, str(row["updated_at"] or ""))

        tenant_row = self._conn.execute(
            "SELECT updated_at FROM tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
        if tenant_row is not None:
            latest_updated = max(latest_updated, str(tenant_row["updated_at"] or ""))
        if not latest_updated:
            latest_updated = utc_now()

        digest = canonical_sha256(items)
        return {
            "version": f"tenant-{digest[:12]}",
            "sha256": digest,
            "updated_at": latest_updated,
            "subscriptions": items,
        }

    def get_tenant_subscriptions(self, tenant_id: str = DEFAULT_TENANT_ID) -> dict[str, Any]:
        """Return the subscription payload scoped strictly to ``tenant_id``.

        For the built-in ``default`` tenant with no multi-tenant assignments, serves the
        legacy ``managed_subscriptions`` table for 100% single-tenant compatibility.
        """
        with self._lock:
            if tenant_id == DEFAULT_TENANT_ID:
                has_mt = self._conn.execute(
                    """
                    SELECT 1 FROM tenant_assignments WHERE tenant_id = ?
                    UNION ALL
                    SELECT 1 FROM subscription_sources
                    WHERE scope = 'tenant_private' AND owner_tenant_id = ?
                    LIMIT 1
                    """,
                    (DEFAULT_TENANT_ID, DEFAULT_TENANT_ID),
                ).fetchone()
                if has_mt is None:
                    return self.get_subscriptions()
            return self._build_tenant_sealed_subscriptions(tenant_id)

    # --- Profile Snapshots (Tenant-Scoped) ----------------------------------

    def list_profiles(self, tenant_id: str = DEFAULT_TENANT_ID) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT snapshot_json
                FROM cloud_profiles
                WHERE tenant_id = ?
                ORDER BY updated_at DESC, id ASC
                """,
                (tenant_id,),
            ).fetchall()
            return [json.loads(row["snapshot_json"]) for row in rows]

    def get_profile(
        self, profile_id: str, tenant_id: str = DEFAULT_TENANT_ID
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT snapshot_json FROM cloud_profiles WHERE tenant_id = ? AND id = ?",
                (tenant_id, profile_id),
            ).fetchone()
            return json.loads(row["snapshot_json"]) if row else None

    def upsert_profile(
        self, profile_id: str, snapshot: dict[str, Any], tenant_id: str = DEFAULT_TENANT_ID
    ) -> dict[str, Any]:
        stamp = utc_now()
        with self._lock:
            existing = self._conn.execute(
                "SELECT revision FROM cloud_profiles WHERE tenant_id = ? AND id = ?",
                (tenant_id, profile_id),
            ).fetchone()
            incoming_rev = int(snapshot.get("revision") or 0)
            if existing is not None:
                revision = max(int(existing["revision"]) + 1, incoming_rev)
            else:
                revision = max(1, incoming_rev)
            stored = {
                **snapshot,
                "id": profile_id,
                "revision": revision,
                "updated_at": stamp,
            }
            name = str(stored.get("name") or profile_id)
            self._conn.execute(
                """
                INSERT INTO cloud_profiles (tenant_id, id, name, revision, snapshot_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, id) DO UPDATE SET
                    name = excluded.name,
                    revision = excluded.revision,
                    snapshot_json = excluded.snapshot_json,
                    updated_at = excluded.updated_at
                """,
                (tenant_id, profile_id, name, revision, canonical_json(stored), stamp),
            )
            self._conn.commit()
            return stored

    def delete_profile(self, profile_id: str, tenant_id: str = DEFAULT_TENANT_ID) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM cloud_profiles WHERE tenant_id = ? AND id = ?",
                (tenant_id, profile_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    # --- Manifest & ETags (Tenant-Scoped) -----------------------------------

    def get_manifest(
        self,
        tenant_id: str = DEFAULT_TENANT_ID,
        *,
        active_devices_override: int | None = None,
    ) -> tuple[dict[str, Any], str]:
        with self._lock:
            if tenant_id == DEFAULT_TENANT_ID:
                tenant = self.ensure_default_tenant()
            else:
                tenant_opt = self.get_tenant_internal(tenant_id)
                if tenant_opt is None:
                    raise KeyError(f"Tenant not found: {tenant_id}")
                tenant = tenant_opt

            policy_row = self._conn.execute(
                "SELECT version, sha256, updated_at FROM isolation_policies WHERE id = 1"
            ).fetchone()
            assert policy_row is not None

            subs_payload = self.get_tenant_subscriptions(tenant_id)
            profile_stats = self._conn.execute(
                """
                SELECT COUNT(*) AS cnt, COALESCE(MAX(updated_at), '') AS max_updated
                FROM cloud_profiles
                WHERE tenant_id = ?
                """,
                (tenant_id,),
            ).fetchone()
            assert profile_stats is not None

            active_devices = (
                active_devices_override
                if active_devices_override is not None
                else self.count_active_devices(tenant_id)
            )
            now = utc_now()
            account = {
                "tenant_id": tenant["id"],
                "tenant_name": tenant["name"],
                "status": tenant["status"],
                "expires_at": tenant["expires_at"],
                "server_time": now,
                "max_devices": int(tenant["max_devices"]),
                "active_devices": int(active_devices),
                "offline_grace_hours": int(tenant["offline_grace_hours"]),
                "profiles_sync_enabled": bool(tenant["profiles_sync_enabled"]),
                "seal_salt": tenant["seal_salt"],
            }
            state = {
                "protocol_version": 1,
                "isolation_policy": {
                    "version": policy_row["version"],
                    "sha256": policy_row["sha256"],
                    "updated_at": policy_row["updated_at"],
                },
                "subscriptions": {
                    "version": subs_payload["version"],
                    "sha256": subs_payload["sha256"],
                    "updated_at": subs_payload["updated_at"],
                    "count": len(subs_payload["subscriptions"]),
                },
                "profiles_sync_enabled": bool(tenant["profiles_sync_enabled"]),
                "profiles_count": int(profile_stats["cnt"]),
            }
            etag_seed = {
                "tenant_id": tenant["id"],
                "policy": state["isolation_policy"],
                "subscriptions": state["subscriptions"],
                "profiles_count": state["profiles_count"],
                "profiles_max_updated": profile_stats["max_updated"],
                "account": {k: v for k, v in account.items() if k != "server_time"},
            }
            etag = f'"{canonical_sha256(etag_seed)[:32]}"'
            manifest = {**state, "server_time": now, "account": account}
            return manifest, etag

    # --- Audit Logs & Overview ----------------------------------------------

    def log_audit(
        self,
        actor: str,
        action: str,
        *,
        target_id: str | None = None,
        detail: dict[str, Any] | None = None,
        ip: str | None = None,
    ) -> None:
        detail_json = canonical_json(detail) if detail is not None else None
        stamp = utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO audit_logs (actor, action, target_id, detail_json, ip, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (actor, action, target_id, detail_json, ip, stamp),
            )
            self._conn.execute(
                """
                DELETE FROM audit_logs
                WHERE id NOT IN (
                    SELECT id FROM audit_logs ORDER BY id DESC LIMIT ?
                )
                """,
                (MAX_AUDIT_LOGS,),
            )
            self._conn.commit()

    def list_audit_logs(
        self,
        *,
        limit: int = 100,
        action: str | None = None,
        actor: str | None = None,
    ) -> list[dict[str, Any]]:
        clamped_limit = max(1, min(int(limit), 500))
        with self._lock:
            clauses: list[str] = []
            params: list[Any] = []
            if action and action.strip():
                clauses.append("action = ?")
                params.append(action.strip())
            if actor and actor.strip():
                clauses.append("actor LIKE ?")
                params.append(f"%{actor.strip()}%")
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            params.append(clamped_limit)
            rows = self._conn.execute(
                f"SELECT * FROM audit_logs{where} ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
            return [
                {
                    "id": int(row["id"]),
                    "actor": row["actor"],
                    "action": row["action"],
                    "target_id": row["target_id"],
                    "detail": json.loads(row["detail_json"]) if row["detail_json"] else None,
                    "ip": row["ip"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

    def get_overview(self) -> dict[str, Any]:
        with self._lock:
            tenants = self.list_tenants(include_default=False)
            active_tenants = sum(
                1 for t in tenants if t["effective_status"] in {"active", "expiring_soon"}
            )
            expiring_soon = sum(1 for t in tenants if t["effective_status"] == "expiring_soon")
            expired_tenants = sum(1 for t in tenants if t["effective_status"] == "expired")
            disabled_tenants = sum(1 for t in tenants if t["effective_status"] == "disabled")

            dev_row = self._conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM tenant_devices
                WHERE revoked = 0 AND tenant_id != ?
                """,
                (DEFAULT_TENANT_ID,),
            ).fetchone()
            active_devices = int(dev_row["cnt"] or 0) if dev_row else 0

            src_row = self._conn.execute(
                """
                SELECT
                    SUM(CASE WHEN scope = 'shared' THEN 1 ELSE 0 END) AS shared_cnt,
                    SUM(CASE WHEN scope = 'tenant_private' THEN 1 ELSE 0 END) AS private_cnt,
                    COALESCE(SUM(CASE WHEN enabled = 1 THEN node_count ELSE 0 END), 0) AS total_nodes
                FROM subscription_sources
                """
            ).fetchone()
            shared_sources = int(src_row["shared_cnt"] or 0) if src_row else 0
            private_sources = int(src_row["private_cnt"] or 0) if src_row else 0
            total_nodes = int(src_row["total_nodes"] or 0) if src_row else 0

            prof_row = self._conn.execute("SELECT COUNT(*) AS cnt FROM cloud_profiles").fetchone()
            total_profiles = int(prof_row["cnt"] or 0) if prof_row else 0

            policy = self.get_isolation_policy()
            return {
                "total_tenants": len(tenants),
                "active_tenants": active_tenants,
                "expiring_soon_tenants": expiring_soon,
                "expired_tenants": expired_tenants,
                "disabled_tenants": disabled_tenants,
                "active_devices": active_devices,
                "shared_sources": shared_sources,
                "private_sources": private_sources,
                "total_nodes": total_nodes,
                "total_profiles": total_profiles,
                "policy_version": policy["version"],
                "policy_updated_at": policy["updated_at"],
                "server_time": utc_now(),
            }
