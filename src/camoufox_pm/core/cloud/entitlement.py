"""Cloud license entitlement guard, anonymous device identity, seal_salt cache, and revocation."""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

from camoufox_pm.core.crypto import decrypt, encrypt

from .protocol import CloudAccountEntitlement

DEVICE_ID_KEY = "device_id_enc"
ENTITLEMENT_KEY = "entitlement_enc"
SEAL_SALT_KEY = "seal_salt_enc"

CLOCK_ROLLBACK_TOLERANCE = timedelta(minutes=5)
EXPIRING_SOON_WINDOW = timedelta(days=7)

REVOCATION_ERROR_CODES = frozenset(
    {
        "tenant_expired",
        "tenant_disabled",
        "device_revoked",
        "invalid_token",
    }
)

_ERROR_CODE_TO_STATUS: dict[str, str] = {
    "tenant_expired": "expired",
    "expired": "expired",
    "tenant_disabled": "disabled",
    "disabled": "disabled",
    "device_revoked": "revoked",
    "revoked": "revoked",
    "invalid_token": "revoked",
    "unauthorized": "revoked",
    "device_limit_exceeded": "device_limit",
    "device_limit": "device_limit",
}

# Process-local cache of active cloud seal_salts keyed by database identity so
# isolated databases/tests in the same process never bleed salts across each other.
_MEMORY_SEAL_SALTS: dict[str, str] = {}


class CloudEntitlementError(RuntimeError):
    """Raised when access to a cloud-managed node is blocked by license/entitlement state."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 403,
        entitlement_status: str | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.entitlement_status = entitlement_status
        super().__init__(message)


@dataclass(frozen=True)
class EntitlementCheckResult:
    """Evaluated cloud entitlement state for enforcement and UI rendering."""

    allowed: bool
    status: str
    code: str | None = None
    message: str = ""
    tenant_id: str | None = None
    tenant_name: str | None = None
    expires_at: str | None = None
    days_remaining: int | None = None
    max_devices: int | None = None
    active_devices: int | None = None
    offline_grace_hours: int | None = None
    profiles_sync_enabled: bool = True
    legacy_mode: bool = False


def _utc_now(now_utc: datetime | None = None) -> datetime:
    if now_utc is None:
        return datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        return now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(timezone.utc)


def _parse_iso_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _db_cache_key(connection: sqlite3.Connection) -> str:
    try:
        rows = connection.execute("PRAGMA database_list").fetchall()
        for row in rows:
            name = row[1] if len(row) > 1 else None
            path = row[2] if len(row) > 2 else ""
            if name == "main" and path:
                return f"file:{path}"
    except sqlite3.Error:
        pass
    return f"mem:{id(connection)}"


def _get_setting(connection: sqlite3.Connection, key: str) -> str | None:
    try:
        row = connection.execute(
            "SELECT value FROM cloud_settings WHERE key = ?", (key,)
        ).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row is not None and row[0] is not None else None


def _set_setting(connection: sqlite3.Connection, key: str, value: str | None) -> None:
    stamp = _utc_now().isoformat()
    with connection:
        if value is None:
            connection.execute("DELETE FROM cloud_settings WHERE key = ?", (key,))
        else:
            connection.execute(
                "INSERT INTO cloud_settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, stamp),
            )


def _unwrap_stored_string(raw: str) -> str:
    try:
        decoded = json.loads(raw)
        if isinstance(decoded, str):
            return decoded
    except ValueError:
        pass
    return raw


def _unseal_setting(raw: str) -> str | None:
    stored = _unwrap_stored_string(raw)
    clear = decrypt(stored)
    if stored.startswith("enc:") and clear == stored:
        return None
    return clear


# --- Device ID ---------------------------------------------------------------


def get_or_create_device_id(connection: sqlite3.Connection) -> str:
    """Return this installation's stable anonymous UUID4 device_id, creating it if absent."""
    raw = _get_setting(connection, DEVICE_ID_KEY)
    if raw:
        clear = _unseal_setting(raw)
        if clear and clear.strip():
            return clear.strip()
    device_id = str(uuid.uuid4())
    _set_setting(connection, DEVICE_ID_KEY, json.dumps(encrypt(device_id)))
    return device_id


# --- Seal Salt Cache ---------------------------------------------------------


def set_active_seal_salt(
    connection: sqlite3.Connection | None,
    salt: str | None,
    *,
    persist: bool = True,
) -> None:
    """Cache the tenant's active ``seal_salt`` in memory (and optionally sealed in SQLite)."""
    clean = (salt or "").strip()
    if not clean:
        clear_active_seal_salt(connection)
        return
    if connection is None:
        _MEMORY_SEAL_SALTS["__default__"] = clean
        return
    key = _db_cache_key(connection)
    _MEMORY_SEAL_SALTS[key] = clean
    if persist:
        _set_setting(connection, SEAL_SALT_KEY, json.dumps(encrypt(clean)))


def clear_active_seal_salt(connection: sqlite3.Connection | None = None) -> None:
    """Wipe cached ``seal_salt`` from memory and SQLite immediately."""
    if connection is None:
        _MEMORY_SEAL_SALTS.clear()
        return
    key = _db_cache_key(connection)
    _MEMORY_SEAL_SALTS.pop(key, None)
    _set_setting(connection, SEAL_SALT_KEY, None)


def get_active_seal_salt(
    connection: sqlite3.Connection | None = None,
    *,
    now_utc: datetime | None = None,
) -> str | None:
    """Return the active ``seal_salt`` if entitlement is valid, else ``None``."""
    if connection is None:
        return _MEMORY_SEAL_SALTS.get("__default__")
    key = _db_cache_key(connection)
    # Refuse to expose a cached salt when entitlement is explicitly blocked.
    ent = evaluate_entitlement(connection, now_utc=now_utc)
    if not ent.allowed:
        _MEMORY_SEAL_SALTS.pop(key, None)
        return None
    cached = _MEMORY_SEAL_SALTS.get(key)
    if cached:
        return cached
    raw = _get_setting(connection, SEAL_SALT_KEY)
    if not raw:
        return None
    clear = _unseal_setting(raw)
    if not clear or not clear.strip():
        return None
    _MEMORY_SEAL_SALTS[key] = clear.strip()
    return clear.strip()


# --- Entitlement Persistence -------------------------------------------------


def save_entitlement(
    connection: sqlite3.Connection,
    account: CloudAccountEntitlement,
    *,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Seal and persist a cloud account entitlement returned by ``GET /v1/manifest``."""
    now_dt = _utc_now(now_utc)
    record: dict[str, Any] = {
        "tenant_id": account.tenant_id,
        "tenant_name": account.tenant_name,
        "status": (account.status or "active").strip().lower(),
        "expires_at": account.expires_at,
        "server_time": account.server_time or now_dt.isoformat(),
        "local_wall_at_sync": now_dt.isoformat(),
        "offline_grace_hours": account.offline_grace_hours,
        "max_devices": account.max_devices,
        "active_devices": account.active_devices,
        "profiles_sync_enabled": bool(account.profiles_sync_enabled),
    }
    sealed = encrypt(json.dumps(record, ensure_ascii=False))
    _set_setting(connection, ENTITLEMENT_KEY, json.dumps(sealed))
    if account.seal_salt:
        set_active_seal_salt(connection, account.seal_salt, persist=True)
    return record


def touch_entitlement_sync(
    connection: sqlite3.Connection,
    *,
    server_time: str | None = None,
    now_utc: datetime | None = None,
) -> None:
    """Refresh ``local_wall_at_sync`` after a successful 304 heartbeat check."""
    record = load_entitlement(connection)
    if record is None or record.get("_decrypt_failed"):
        return
    now_dt = _utc_now(now_utc)
    updated = dict(record)
    updated["local_wall_at_sync"] = now_dt.isoformat()
    if server_time:
        updated["server_time"] = server_time
    sealed = encrypt(json.dumps(updated, ensure_ascii=False))
    _set_setting(connection, ENTITLEMENT_KEY, json.dumps(sealed))


def record_entitlement_failure(
    connection: sqlite3.Connection,
    error_code: str,
    message: str = "",
    *,
    details: dict[str, Any] | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Persist a blocked/revoked entitlement status and clear active ``seal_salt``."""
    now_dt = _utc_now(now_utc)
    existing = load_entitlement(connection)
    base: dict[str, Any] = (
        dict(existing) if isinstance(existing, dict) and not existing.get("_decrypt_failed") else {}
    )
    mapped_status = _ERROR_CODE_TO_STATUS.get(error_code, "revoked")
    extra = details or {}
    if "expires_at" in extra and extra["expires_at"] is not None:
        base["expires_at"] = extra["expires_at"]
    if "max_devices" in extra and extra["max_devices"] is not None:
        base["max_devices"] = extra["max_devices"]
    if "active_devices" in extra and extra["active_devices"] is not None:
        base["active_devices"] = extra["active_devices"]
    if "tenant_id" in extra and extra["tenant_id"] is not None:
        base["tenant_id"] = extra["tenant_id"]
    if "tenant_name" in extra and extra["tenant_name"] is not None:
        base["tenant_name"] = extra["tenant_name"]
    base["status"] = mapped_status
    base["last_error_code"] = error_code
    base["last_error_message"] = message
    base["local_wall_at_sync"] = now_dt.isoformat()
    sealed = encrypt(json.dumps(base, ensure_ascii=False))
    _set_setting(connection, ENTITLEMENT_KEY, json.dumps(sealed))
    clear_active_seal_salt(connection)
    return base


def load_entitlement(connection: sqlite3.Connection) -> dict[str, Any] | None:
    """Read and unseal the stored entitlement record, or ``None`` if none exists."""
    raw = _get_setting(connection, ENTITLEMENT_KEY)
    if not raw:
        return None
    clear = _unseal_setting(raw)
    if clear is None:
        return {"status": "revoked", "_decrypt_failed": True}
    try:
        parsed = json.loads(clear)
    except ValueError:
        return {"status": "revoked", "_decrypt_failed": True}
    return parsed if isinstance(parsed, dict) else {"status": "revoked", "_decrypt_failed": True}


def clear_entitlement(connection: sqlite3.Connection) -> None:
    """Remove persisted entitlement and seal_salt (used on explicit logout)."""
    _set_setting(connection, ENTITLEMENT_KEY, None)
    clear_active_seal_salt(connection)


# --- Entitlement Evaluation & Guard -----------------------------------------


def _as_int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def evaluate_entitlement(
    connection: sqlite3.Connection,
    *,
    now_utc: datetime | None = None,
) -> EntitlementCheckResult:
    """Compute the effective entitlement status without raising."""
    now_dt = _utc_now(now_utc)
    record = load_entitlement(connection)
    if record is None:
        # Backwards compatible with legacy single-tenant deployments that emit no
        # account entitlement block in GET /v1/manifest.
        return EntitlementCheckResult(
            allowed=True,
            status="none",
            profiles_sync_enabled=True,
            legacy_mode=True,
        )

    if record.get("_decrypt_failed"):
        return EntitlementCheckResult(
            allowed=False,
            status="revoked",
            code="cloud_entitlement_revoked",
            message="本地授权凭证无法解密，请重新输入授权码激活（本地节点不受影响）。",
            profiles_sync_enabled=False,
        )

    tenant_id = str(record["tenant_id"]) if record.get("tenant_id") else None
    tenant_name = str(record["tenant_name"]) if record.get("tenant_name") else None
    raw_status = str(record.get("status") or "active").strip().lower()
    expires_at_str = str(record["expires_at"]) if record.get("expires_at") else None
    expires_dt = _parse_iso_utc(expires_at_str)
    server_time_dt = _parse_iso_utc(record.get("server_time"))
    local_wall_dt = _parse_iso_utc(record.get("local_wall_at_sync"))
    offline_grace_hours = _as_int_or_none(record.get("offline_grace_hours"))
    max_devices = _as_int_or_none(record.get("max_devices"))
    active_devices = _as_int_or_none(record.get("active_devices"))
    profiles_sync_enabled = bool(record.get("profiles_sync_enabled", False))

    effective_now = max(now_dt, server_time_dt) if server_time_dt is not None else now_dt
    days_remaining: int | None = None
    if expires_dt is not None:
        delta_seconds = (expires_dt - effective_now).total_seconds()
        days_remaining = 0 if delta_seconds <= 0 else math.ceil(delta_seconds / 86400.0)

    def _result(
        *,
        allowed: bool,
        status: str,
        code: str | None = None,
        message: str = "",
        override_days: int | None = None,
        use_override_days: bool = False,
    ) -> EntitlementCheckResult:
        return EntitlementCheckResult(
            allowed=allowed,
            status=status,
            code=code,
            message=message,
            tenant_id=tenant_id,
            tenant_name=tenant_name,
            expires_at=expires_at_str,
            days_remaining=override_days if use_override_days else days_remaining,
            max_devices=max_devices,
            active_devices=active_devices,
            offline_grace_hours=offline_grace_hours,
            profiles_sync_enabled=profiles_sync_enabled,
        )

    if raw_status == "expired":
        return _result(
            allowed=False,
            status="expired",
            code="cloud_entitlement_expired",
            message="您的专属节点授权已到期，请续期后重新同步（本地节点不受影响）。",
            override_days=0 if expires_dt is not None else days_remaining,
            use_override_days=True,
        )
    if raw_status == "disabled":
        return _result(
            allowed=False,
            status="disabled",
            code="cloud_entitlement_disabled",
            message="您的专属节点授权已被管理员停用（本地节点不受影响）。",
        )
    if raw_status == "revoked":
        return _result(
            allowed=False,
            status="revoked",
            code="cloud_entitlement_revoked",
            message="当前设备或授权码已被吊销，请重新激活授权码（本地节点不受影响）。",
        )
    if raw_status == "device_limit":
        return _result(
            allowed=False,
            status="device_limit",
            code="cloud_device_limit_exceeded",
            message="已超出最大并发设备数上限，请联系管理员解绑旧设备（本地节点不受影响）。",
        )

    # Expiry enforcement using max(local UTC, last server_time) to resist mild skew.
    if expires_dt is not None and effective_now > expires_dt:
        return _result(
            allowed=False,
            status="expired",
            code="cloud_entitlement_expired",
            message="您的专属节点授权已到期，请续期后重新同步（本地节点不受影响）。",
            override_days=0,
            use_override_days=True,
        )

    # Clock rollback detection: system clock moved backwards more than 5 minutes.
    if local_wall_dt is not None and now_dt < local_wall_dt - CLOCK_ROLLBACK_TOLERANCE:
        return _result(
            allowed=False,
            status="offline_grace_exceeded",
            code="clock_tampered_reverify_required",
            message="检测到系统时间异常回拨，请校准系统时间并联网重新校验授权（本地节点不受影响）。",
        )

    # Offline grace window enforcement.
    if (
        offline_grace_hours is not None
        and offline_grace_hours > 0
        and local_wall_dt is not None
        and now_dt - local_wall_dt > timedelta(hours=offline_grace_hours)
    ):
        return _result(
            allowed=False,
            status="offline_grace_exceeded",
            code="offline_grace_exceeded",
            message=(
                f"离线已超过 {offline_grace_hours} 小时宽限期，请联网同步授权状态后再使用云节点"
                "（本地节点不受影响）。"
            ),
        )

    ui_status = "active"
    if expires_dt is not None and (expires_dt - effective_now) <= EXPIRING_SOON_WINDOW:
        ui_status = "expiring_soon"

    return _result(allowed=True, status=ui_status)


def check_cloud_entitlement(
    connection: sqlite3.Connection,
    *,
    now_utc: datetime | None = None,
) -> EntitlementCheckResult:
    """Enforce cloud entitlement for a cloud-managed node; raise :class:`CloudEntitlementError` if denied."""
    result = evaluate_entitlement(connection, now_utc=now_utc)
    if not result.allowed:
        raise CloudEntitlementError(
            result.code or "cloud_entitlement_denied",
            result.message or "Cloud node access is denied by current license status.",
            status_code=403,
            entitlement_status=result.status,
        )
    return result


# --- Node Ownership Helpers & Revocation ------------------------------------


def is_cloud_node(connection: sqlite3.Connection, node_id: str) -> bool:
    """Return True iff ``node_id`` belongs to a ``managed_by='cloud'`` subscription."""
    row = connection.execute(
        "SELECT s.managed_by "
        "FROM network_nodes n "
        "JOIN network_subscriptions s ON s.id = n.subscription_id "
        "WHERE n.id = ?",
        (node_id,),
    ).fetchone()
    return bool(row is not None and row[0] == "cloud")


def any_cloud_nodes(connection: sqlite3.Connection, node_ids: Sequence[str]) -> bool:
    """Return True if any ID in ``node_ids`` belongs to a ``managed_by='cloud'`` subscription."""
    clean_ids = [nid for nid in node_ids if nid]
    if not clean_ids:
        return False
    marks = ",".join("?" * len(clean_ids))
    row = connection.execute(
        "SELECT 1 "
        "FROM network_nodes n "
        "JOIN network_subscriptions s ON s.id = n.subscription_id "
        f"WHERE n.id IN ({marks}) AND s.managed_by = 'cloud' "
        "LIMIT 1",
        clean_ids,
    ).fetchone()
    return row is not None


async def revoke_local_cloud_nodes(
    manager: Any,
    reason_status: str | None = None,
    message: str = "",
) -> dict[str, int]:
    """Stop running cloud-node sessions/runtimes and wipe all local cloud node credentials.

    Local subscriptions, local nodes, and browser sessions bound to local nodes
    (or unbound) are NEVER touched.
    """
    connection: sqlite3.Connection = manager.storage.db._connection

    # 1. Identify all profiles bound to or actively running a cloud-managed node.
    bound_rows = connection.execute(
        "SELECT b.profile_id "
        "FROM profile_network_bindings b "
        "JOIN network_nodes n ON n.id = b.node_id "
        "JOIN network_subscriptions s ON s.id = n.subscription_id "
        "WHERE s.managed_by = 'cloud'"
    ).fetchall()
    cloud_profile_ids: set[str] = {str(row[0]) for row in bound_rows}

    network_adapter = getattr(manager, "network", None)
    if network_adapter is not None:
        for pid, runtime in list(network_adapter.runtimes.items()):
            node_info = getattr(runtime, "node", None) or {}
            if node_info.get("subscription_managed_by") == "cloud":
                cloud_profile_ids.add(str(pid))

    sessions_closed = 0
    runtimes_closed = 0
    browser_sessions = getattr(manager, "browser_sessions", None)
    for profile_id in sorted(cloud_profile_ids):
        if browser_sessions is not None and browser_sessions.is_running(profile_id):
            try:
                if await browser_sessions.close_and_release(profile_id):
                    sessions_closed += 1
            except Exception as exc:  # noqa: BLE001 - revocation must continue wiping secrets
                logger.warning(f"Failed to close cloud browser session {profile_id}: {exc}")
        if network_adapter is not None:
            runtime = network_adapter.runtimes.get(profile_id)
            if runtime is not None:
                try:
                    await runtime.close()
                    runtimes_closed += 1
                except Exception as exc:  # noqa: BLE001 - revocation must continue
                    logger.warning(f"Failed to stop cloud network runtime {profile_id}: {exc}")

    # 2. Delete unbound cloud nodes and wipe credentials on bound cloud nodes.
    stamp = _utc_now().isoformat()
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        del_cursor = connection.execute(
            "DELETE FROM network_nodes "
            "WHERE subscription_id IN ("
            "  SELECT id FROM network_subscriptions WHERE managed_by = 'cloud'"
            ") "
            "AND id NOT IN (SELECT node_id FROM profile_network_bindings)"
        )
        nodes_deleted = del_cursor.rowcount if del_cursor.rowcount >= 0 else 0

        upd_cursor = connection.execute(
            "UPDATE network_nodes "
            "SET status = 'retired', "
            "    retired_at = COALESCE(retired_at, ?), "
            "    config_enc = 'revoked', "
            "    entry = '', "
            "    updated_at = ? "
            "WHERE subscription_id IN ("
            "  SELECT id FROM network_subscriptions WHERE managed_by = 'cloud'"
            ")",
            (stamp, stamp),
        )
        nodes_wiped = upd_cursor.rowcount if upd_cursor.rowcount >= 0 else 0

        connection.execute(
            "UPDATE network_subscriptions "
            "SET source_url_enc = NULL, source_host = NULL, source_file_name = NULL, "
            "    updated_at = ? "
            "WHERE managed_by = 'cloud'",
            (stamp,),
        )
        connection.execute("DELETE FROM cloud_cache WHERE kind = 'subscriptions'")
        connection.execute(
            "DELETE FROM cloud_settings WHERE key IN ('subscriptions_etag', 'manifest_etag')"
        )

    # 3. Clear cached salt and persist/clear entitlement status.
    clear_active_seal_salt(connection)
    if reason_status == "logout":
        clear_entitlement(connection)
    elif reason_status:
        record_entitlement_failure(connection, reason_status, message)

    return {
        "sessions_closed": sessions_closed,
        "runtimes_closed": runtimes_closed,
        "nodes_deleted": nodes_deleted,
        "nodes_wiped": nodes_wiped,
    }
