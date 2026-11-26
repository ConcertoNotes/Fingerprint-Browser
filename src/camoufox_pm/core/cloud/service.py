"""Cloud delivery & sync orchestration service (policies, subscriptions, profiles)."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from loguru import logger

from camoufox_pm import __version__
from camoufox_pm.config import get_settings
from camoufox_pm.core.crypto import decrypt, encrypt, encrypt_with_salt
from camoufox_pm.core.leases import lease_expired
from camoufox_pm.core.models import (
    BrowserSettings,
    Profile,
    ProfileStatus,
    WebRTCMode,
    normalize_tags,
)
from camoufox_pm.core.network_catalog import (
    NetworkCatalog,
    NetworkSecretsUnavailable,
    new_id,
    seal,
)
from camoufox_pm.core.network_subscription import (
    ParsedSubscription,
    SubscriptionError,
    _collect,
    fetch_subscription,
    validate_subscription_url,
)
from camoufox_pm.core.network_uri import parse_manual_text

from .client import CloudClient, CloudClientError, CloudResponse, validate_cloud_base_url
from .entitlement import (
    REVOCATION_ERROR_CODES,
    evaluate_entitlement,
    get_active_seal_salt,
    get_or_create_device_id,
    record_entitlement_failure,
    revoke_local_cloud_nodes,
    save_entitlement,
    touch_entitlement_sync,
)
from .protocol import (
    CloudConfigPublic,
    CloudConfigUpdate,
    CloudManifest,
    CloudPolicySyncResult,
    CloudProfileSnapshot,
    CloudProfilesSyncResult,
    CloudStatusData,
    CloudSubscriptionItem,
    CloudSubscriptionsSyncResult,
    CloudSyncReport,
    canonical_json,
    verify_payload_sha256,
)

CookieExporter = Callable[[str], Awaitable[str]]
CookieImporter = Callable[[str, str], Awaitable[Any]]

_VALID_SCOPES = ("policy", "subscriptions", "profiles")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CloudSyncError(RuntimeError):
    """Structured error raised by :class:`CloudDeliveryService`."""

    def __init__(self, code: str, message: str, *, status_code: int = 422) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(message)


def _env_allow_local() -> bool:
    settings = get_settings()
    if bool(getattr(settings, "cloud_allow_local", False)):
        return True
    return os.environ.get("CPM_CLOUD_ALLOW_LOCAL", "").strip().lower() in {"1", "true", "yes"}


def _load_isolation_policy_module():
    from camoufox_pm.core.isolation import policy as isolation_policy

    return isolation_policy


def _isolation_to_browser_settings(isolation_config: dict[str, Any] | None) -> BrowserSettings:
    """Derive compatible ``BrowserSettings`` from an ``isolation_config`` dict."""
    if not isinstance(isolation_config, dict):
        return BrowserSettings()
    os_name = str(isolation_config.get("os") or "windows")
    if os_name not in {"windows", "macos", "linux"}:
        os_name = "windows"
    languages = isolation_config.get("languages")
    if not isinstance(languages, list) or not languages:
        languages = ["en-US", "en"]
    window_size = isolation_config.get("window_size")
    win_w, win_h = 1366, 860
    if isinstance(window_size, (list, tuple)) and len(window_size) == 2:
        try:
            win_w, win_h = int(window_size[0]), int(window_size[1])
        except (TypeError, ValueError):
            win_w, win_h = 1366, 860
    webrtc_policy = isolation_config.get("webrtc_policy", "block")
    webrtc_mode = WebRTCMode.NONE if webrtc_policy == "block" else WebRTCMode.REPLACE
    canvas_policy = isolation_config.get("canvas_policy", "stable_hardware")
    timezone_mode = isolation_config.get("timezone_mode", "fixed")
    tz = isolation_config.get("timezone") if timezone_mode == "fixed" else None
    pinned = isolation_config.get("pinned_fingerprint")
    screen = "1920x1080"
    if isinstance(pinned, dict):
        w = pinned.get("screen_width") or pinned.get("screen.width")
        h = pinned.get("screen_height") or pinned.get("screen.height")
        if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
            screen = f"{w}x{h}"
    return BrowserSettings(
        os=os_name,
        screen=screen,
        languages=[str(lang) for lang in languages],
        timezone=str(tz) if tz else None,
        window_width=win_w,
        window_height=win_h,
        webrtc_mode=webrtc_mode,
        stable_canvas=(canvas_policy == "stable_hardware"),
    )


class CloudDeliveryService:
    """Manages cloud settings, policy/subscription/profile sync, and local SQLite cache."""

    def __init__(
        self,
        manager: Any,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        subscription_fetcher: Any | None = None,
        cookie_exporter: CookieExporter | None = None,
        cookie_importer: CookieImporter | None = None,
    ) -> None:
        self.manager = manager
        self.connection: sqlite3.Connection = manager.storage.db._connection
        self._transport = transport
        self._subscription_fetcher = subscription_fetcher or fetch_subscription
        self._cookie_exporter = cookie_exporter
        self._cookie_importer = cookie_importer

    # --- Settings helpers ---------------------------------------------------

    def _raw_settings(self) -> dict[str, str]:
        rows = self.connection.execute("SELECT key, value FROM cloud_settings").fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    def _set_setting(self, key: str, value: str | None) -> None:
        stamp = _utc_now()
        with self.connection:
            if value is None:
                self.connection.execute("DELETE FROM cloud_settings WHERE key = ?", (key,))
            else:
                self.connection.execute(
                    "INSERT INTO cloud_settings (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (key, value, stamp),
                )

    def _set_many_settings(self, updates: dict[str, str | None]) -> None:
        stamp = _utc_now()
        with self.connection:
            for key, value in updates.items():
                if value is None:
                    self.connection.execute("DELETE FROM cloud_settings WHERE key = ?", (key,))
                else:
                    self.connection.execute(
                        "INSERT INTO cloud_settings (key, value, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                        (key, value, stamp),
                    )

    def _is_customer_edition(self) -> bool:
        return get_settings().client_edition == "customer"

    def _effective_base_url(self, raw: dict[str, str]) -> str | None:
        env_url = getattr(get_settings(), "cloud_url", None)
        cleaned_env = (
            env_url.strip().rstrip("/") if isinstance(env_url, str) and env_url.strip() else None
        )
        if self._is_customer_edition():
            return cleaned_env
        if "base_url" in raw:
            try:
                val = json.loads(raw["base_url"])
                if isinstance(val, str) and val.strip():
                    return val.strip().rstrip("/")
            except ValueError:
                if raw["base_url"].strip():
                    return raw["base_url"].strip().rstrip("/")
        return cleaned_env

    def _effective_token(self, raw: dict[str, str]) -> str | None:
        if "token_enc" in raw and raw["token_enc"]:
            stored = raw["token_enc"]
            try:
                stored_val = json.loads(stored)
                if isinstance(stored_val, str):
                    stored = stored_val
            except ValueError:
                pass
            clear = decrypt(stored)
            if stored.startswith("enc:") and clear == stored:
                raise CloudSyncError(
                    "token_decrypt_failed",
                    "Stored cloud token could not be decrypted. Restore CPM_SECRET_KEY or re-enter the token.",
                    status_code=409,
                )
            return clear.strip() or None
        env_token = getattr(get_settings(), "cloud_token", None)
        return env_token.strip() if isinstance(env_token, str) and env_token.strip() else None

    def _has_token(self, raw: dict[str, str]) -> bool:
        if raw.get("token_enc"):
            return True
        env_token = getattr(get_settings(), "cloud_token", None)
        return bool(isinstance(env_token, str) and env_token.strip())

    def _bool_setting(self, raw: dict[str, str], key: str, default: bool) -> bool:
        if key not in raw:
            return default
        try:
            val = json.loads(raw[key])
            return bool(val)
        except ValueError:
            return raw[key].strip().lower() in {"1", "true", "yes"}

    def _str_setting(self, raw: dict[str, str], key: str, default: str = "") -> str:
        if key not in raw:
            return default
        try:
            val = json.loads(raw[key])
            return str(val) if val is not None else default
        except ValueError:
            return raw[key]

    def _effective_allow_local(self, raw: dict[str, str]) -> bool:
        if self._is_customer_edition():
            return _env_allow_local()
        return self._bool_setting(raw, "allow_local", False) or _env_allow_local()

    def get_config(self) -> CloudConfigPublic:
        raw = self._raw_settings()
        base_url = self._effective_base_url(raw)
        is_customer = self._is_customer_edition()
        default_label = socket.gethostname()[:64]
        return CloudConfigPublic(
            base_url=None if is_customer else base_url,
            has_token=self._has_token(raw),
            auto_sync_on_startup=self._bool_setting(raw, "auto_sync_on_startup", True),
            sync_profiles_enabled=self._bool_setting(raw, "sync_profiles_enabled", True),
            device_label=self._str_setting(raw, "device_label", default_label),
            allow_local=self._effective_allow_local(raw),
            configured=bool(base_url),
        )

    def update_config(self, update: CloudConfigUpdate) -> CloudConfigPublic:
        raw = self._raw_settings()
        fields = update.model_fields_set
        if self._is_customer_edition() and ("base_url" in fields or "allow_local" in fields):
            raise CloudSyncError(
                "cloud_url_locked",
                "Cloud server URL and local endpoint settings are locked in customer edition.",
                status_code=403,
            )
        allow_local = (
            bool(update.allow_local)
            if "allow_local" in fields and update.allow_local is not None
            else self._effective_allow_local(raw)
        )
        updates: dict[str, str | None] = {}

        if "allow_local" in fields and update.allow_local is not None:
            updates["allow_local"] = json.dumps(bool(update.allow_local))

        if "base_url" in fields:
            if update.base_url is None or not update.base_url.strip():
                updates["base_url"] = None
                updates["manifest_etag"] = None
                updates["policy_etag"] = None
                updates["subscriptions_etag"] = None
            else:
                try:
                    normalized = validate_cloud_base_url(
                        update.base_url,
                        allow_local=allow_local or _env_allow_local(),
                    )
                except CloudClientError as exc:
                    raise CloudSyncError(exc.code, str(exc), status_code=422) from None
                prev_url = self._effective_base_url(raw)
                updates["base_url"] = json.dumps(normalized)
                if normalized != prev_url:
                    updates["manifest_etag"] = None
                    updates["policy_etag"] = None
                    updates["subscriptions_etag"] = None

        if update.clear_token:
            updates["token_enc"] = None
            updates["manifest_etag"] = None
            updates["policy_etag"] = None
            updates["subscriptions_etag"] = None
        elif "token" in fields and update.token is not None:
            cleaned_token = update.token.strip()
            if not cleaned_token:
                updates["token_enc"] = None
            else:
                updates["token_enc"] = json.dumps(encrypt(cleaned_token))
            updates["manifest_etag"] = None
            updates["policy_etag"] = None
            updates["subscriptions_etag"] = None

        if "auto_sync_on_startup" in fields and update.auto_sync_on_startup is not None:
            updates["auto_sync_on_startup"] = json.dumps(bool(update.auto_sync_on_startup))

        if "sync_profiles_enabled" in fields and update.sync_profiles_enabled is not None:
            updates["sync_profiles_enabled"] = json.dumps(bool(update.sync_profiles_enabled))

        if "device_label" in fields:
            if update.device_label is None:
                updates["device_label"] = None
            else:
                updates["device_label"] = json.dumps(update.device_label.strip()[:64])

        if updates:
            self._set_many_settings(updates)
        return self.get_config()

    def _build_client(self, *, token_override: str | None = None) -> CloudClient:
        raw = self._raw_settings()
        base_url = self._effective_base_url(raw)
        if not base_url:
            raise CloudSyncError(
                "not_configured",
                "Cloud server URL is not configured.",
                status_code=400,
            )
        token = token_override if token_override is not None else self._effective_token(raw)
        allow_local = self._effective_allow_local(raw)
        device_id = get_or_create_device_id(self.connection)
        default_label = socket.gethostname()[:64]
        device_label = self._str_setting(raw, "device_label", default_label) or default_label
        try:
            return CloudClient(
                base_url,
                token=token,
                device_id=device_id,
                device_label=device_label,
                client_version=__version__,
                allow_local=allow_local,
                transport=self._transport,
            )
        except CloudClientError as exc:
            raise CloudSyncError(exc.code, str(exc), status_code=422) from None

    async def _handle_client_error(self, exc: CloudClientError) -> None:
        """Persist entitlement status and revoke local cloud nodes on server-side rejection."""
        if exc.code in REVOCATION_ERROR_CODES:
            record_entitlement_failure(
                self.connection,
                exc.code,
                str(exc),
                details=exc.details,
            )
            await revoke_local_cloud_nodes(self.manager)
        elif exc.code == "device_limit_exceeded":
            record_entitlement_failure(
                self.connection,
                exc.code,
                str(exc),
                details=exc.details,
            )

    # --- Status & Active Policy ---------------------------------------------

    def get_active_policy_info(self) -> dict[str, Any]:
        policy_mod = _load_isolation_policy_module()
        active_policy, source = policy_mod.load_active_policy(self.connection)
        cache_row = self.connection.execute(
            "SELECT version, sha256, fetched_at FROM cloud_cache WHERE kind = 'isolation_policy'"
        ).fetchone()
        policy_dict = active_policy.model_dump(mode="json")
        risk_groups = policy_dict.get("risk_probe_groups") or {}
        risk_domains_count = (
            sum(len(items) for items in risk_groups.values() if isinstance(items, list))
            if isinstance(risk_groups, dict)
            else 0
        )
        risk_cidrs = policy_dict.get("risk_probe_cidrs") or []
        risk_targets = policy_dict.get("risk_probe_targets") or []
        return {
            "source": source,
            "version": active_policy.policy_version,
            "sha256": cache_row["sha256"] if cache_row and source == "cloud" else None,
            "fetched_at": cache_row["fetched_at"] if cache_row and source == "cloud" else None,
            "policy": policy_dict,
            "summary": {
                "default_tier": policy_dict.get("default_tier", "strict"),
                "default_webrtc_policy": policy_dict.get("default_webrtc_policy", "block"),
                "default_canvas_policy": policy_dict.get(
                    "default_canvas_policy", "stable_hardware"
                ),
                "default_locale_mode": policy_dict.get("default_locale_mode", "fixed"),
                "default_languages": policy_dict.get("default_languages", ["en-US", "en"]),
                "default_timezone_mode": policy_dict.get("default_timezone_mode", "fixed"),
                "default_timezone": policy_dict.get("default_timezone", "America/Los_Angeles"),
                "install_webrtc_guard": bool(policy_dict.get("install_webrtc_guard", True)),
                "hardening_prefs_count": len(policy_dict.get("hardening_prefs") or {}),
                "curated_presets_count": len(policy_dict.get("curated_presets") or []),
                "risk_domains_count": risk_domains_count,
                "risk_cidrs_count": len(risk_cidrs) if isinstance(risk_cidrs, list) else 0,
                "risk_probe_targets_count": len(risk_targets)
                if isinstance(risk_targets, list)
                else 0,
            },
        }

    def get_status(self) -> CloudStatusData:
        raw = self._raw_settings()
        cfg = self.get_config()
        settings = get_settings()
        is_customer = settings.client_edition == "customer"
        ent = evaluate_entitlement(self.connection)
        device_id = get_or_create_device_id(self.connection)
        policy_info = self.get_active_policy_info()
        subs_cache = self.connection.execute(
            "SELECT version, fetched_at FROM cloud_cache WHERE kind = 'subscriptions'"
        ).fetchone()
        cloud_sub_count = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM network_subscriptions "
                "WHERE managed_by = 'cloud' AND deleted_at IS NULL"
            ).fetchone()[0]
        )
        synced_profiles_count = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM profiles WHERE cloud_revision > 0 AND deleted_at IS NULL"
            ).fetchone()[0]
        )
        last_synced_at = self._str_setting(raw, "last_sync_at", "") or None
        last_sync_ok: bool | None = None
        if "last_sync_ok" in raw:
            last_sync_ok = self._bool_setting(raw, "last_sync_ok", False)
        last_error = self._str_setting(raw, "last_error", "") or None
        profiles_sync_allowed = (
            (False if is_customer else True) if ent.legacy_mode else bool(ent.profiles_sync_enabled)
        )
        return CloudStatusData(
            configured=cfg.configured,
            connected=bool(cfg.configured and last_sync_ok is True),
            base_url=None if is_customer else cfg.base_url,
            has_token=cfg.has_token,
            auto_sync_on_startup=cfg.auto_sync_on_startup,
            sync_profiles_enabled=cfg.sync_profiles_enabled,
            device_label=cfg.device_label or None,
            allow_local=cfg.allow_local,
            policy_source=str(policy_info["source"]),
            policy_version=str(policy_info["version"]),
            policy_sha256=policy_info["sha256"],
            policy_fetched_at=policy_info["fetched_at"],
            subscriptions_version=subs_cache["version"] if subs_cache else None,
            subscriptions_fetched_at=subs_cache["fetched_at"] if subs_cache else None,
            cloud_subscription_count=cloud_sub_count,
            synced_profiles_count=synced_profiles_count,
            last_synced_at=last_synced_at,
            last_sync_ok=last_sync_ok,
            last_error=last_error,
            edition=settings.client_edition,
            brand_name=settings.brand_name,
            cloud_url_locked=is_customer,
            require_activation=bool(settings.require_activation),
            tenant_id=ent.tenant_id,
            tenant_name=ent.tenant_name,
            entitlement_status=ent.status,
            expires_at=ent.expires_at,
            days_remaining=ent.days_remaining,
            max_devices=ent.max_devices,
            active_devices=ent.active_devices,
            offline_grace_hours=ent.offline_grace_hours,
            profiles_sync_allowed=profiles_sync_allowed,
            device_id=device_id,
        )

    # --- Policy Sync --------------------------------------------------------

    async def sync_policy(
        self, *, client: CloudClient | None = None, force: bool = False
    ) -> CloudPolicySyncResult:
        """Fetch, verify SHA-256 & security whitelist, and cache the cloud isolation policy."""
        cloud = client or self._build_client()
        raw_settings = self._raw_settings()
        etag = None if force else (self._str_setting(raw_settings, "policy_etag", "") or None)
        cache_row = self.connection.execute(
            "SELECT version, sha256 FROM cloud_cache WHERE kind = 'isolation_policy'"
        ).fetchone()
        if cache_row is None:
            etag = None

        try:
            response = await cloud.get_isolation_policy(etag=etag)
        except CloudClientError as exc:
            await self._handle_client_error(exc)
            raise CloudSyncError(
                exc.code,
                str(exc),
                status_code=exc.status_code or 502,
            ) from None

        if response.not_modified and cache_row is not None:
            return CloudPolicySyncResult(
                status="unchanged",
                updated=False,
                version=str(cache_row["version"]),
                sha256=str(cache_row["sha256"]),
                message="Isolation policy is already up to date.",
                details={"sha256": cache_row["sha256"]},
            )

        assert response.data is not None and response.raw is not None
        payload = response.data
        raw_policy_obj = response.raw.get("policy")
        if not isinstance(raw_policy_obj, dict) or not verify_payload_sha256(
            raw_policy_obj, payload.sha256
        ):
            raise CloudSyncError(
                "sha256_mismatch",
                "Cloud isolation policy failed SHA-256 integrity verification.",
                status_code=502,
            )

        policy_mod = _load_isolation_policy_module()
        try:
            validated = policy_mod.validate_policy(raw_policy_obj)
        except Exception as exc:
            raise CloudSyncError(
                "invalid_policy",
                f"Cloud isolation policy rejected by local security whitelist: {exc}",
                status_code=422,
            ) from None

        version = payload.version or validated.policy_version
        stored_json = canonical_json(raw_policy_obj)
        stamp = _utc_now()
        with self.connection:
            self.connection.execute(
                "INSERT INTO cloud_cache (kind, version, sha256, payload, fetched_at) "
                "VALUES ('isolation_policy', ?, ?, ?, ?) "
                "ON CONFLICT(kind) DO UPDATE SET version=excluded.version, sha256=excluded.sha256, "
                "payload=excluded.payload, fetched_at=excluded.fetched_at",
                (version, payload.sha256.strip().lower(), stored_json, stamp),
            )
        if response.etag:
            self._set_setting("policy_etag", json.dumps(response.etag))

        changed = cache_row is None or cache_row["sha256"] != payload.sha256
        return CloudPolicySyncResult(
            status="updated" if changed else "unchanged",
            updated=changed,
            version=version,
            sha256=payload.sha256,
            message=(
                f"Updated isolation policy to {version}."
                if changed
                else f"Isolation policy {version} verified."
            ),
            details={"sha256": payload.sha256, "fetched_at": stamp},
        )

    # --- Subscription Sync --------------------------------------------------

    async def sync_subscriptions(
        self, *, client: CloudClient | None = None, force: bool = False
    ) -> CloudSubscriptionsSyncResult:
        """Sync cloud-managed subscription sources without touching local subscriptions or bindings."""
        cloud = client or self._build_client()
        raw_settings = self._raw_settings()
        etag = (
            None if force else (self._str_setting(raw_settings, "subscriptions_etag", "") or None)
        )
        cache_row = self.connection.execute(
            "SELECT version, sha256 FROM cloud_cache WHERE kind = 'subscriptions'"
        ).fetchone()
        if cache_row is None:
            etag = None

        try:
            response = await cloud.get_subscriptions(etag=etag)
        except CloudClientError as exc:
            await self._handle_client_error(exc)
            raise CloudSyncError(
                exc.code,
                str(exc),
                status_code=exc.status_code or 502,
            ) from None

        if response.not_modified and cache_row is not None:
            total_active = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM network_subscriptions "
                    "WHERE managed_by = 'cloud' AND deleted_at IS NULL"
                ).fetchone()[0]
            )
            return CloudSubscriptionsSyncResult(
                status="unchanged",
                version=str(cache_row["version"]),
                unchanged=total_active,
                total=total_active,
                message="Cloud subscriptions are already up to date.",
                details={"sha256": cache_row["sha256"]},
            )

        assert response.data is not None and response.raw is not None
        payload = response.data
        raw_subs_list = response.raw.get("subscriptions")
        if not isinstance(raw_subs_list, list) or not verify_payload_sha256(
            raw_subs_list, payload.sha256
        ):
            raise CloudSyncError(
                "sha256_mismatch",
                "Cloud subscriptions payload failed SHA-256 integrity verification.",
                status_code=502,
            )

        catalog = NetworkCatalog(self.manager)
        existing_rows = self.connection.execute(
            "SELECT * FROM network_subscriptions WHERE managed_by = 'cloud' AND deleted_at IS NULL"
        ).fetchall()
        existing_by_source: dict[str, sqlite3.Row] = {
            str(row["cloud_source_id"]): row for row in existing_rows if row["cloud_source_id"]
        }

        seen_sources: set[str] = set()
        created = 0
        updated = 0
        unchanged = 0
        retired = 0
        item_errors: list[dict[str, str]] = []

        for item in payload.subscriptions:
            if not item.enabled:
                continue
            seen_sources.add(item.cloud_source_id)
            existing = existing_by_source.get(item.cloud_source_id)
            try:
                outcome = await self._reconcile_subscription_item(catalog, item, existing)
                if outcome == "created":
                    created += 1
                elif outcome == "updated":
                    updated += 1
                else:
                    unchanged += 1
            except (SubscriptionError, NetworkSecretsUnavailable, ValueError) as exc:
                logger.warning(
                    f"Failed to reconcile cloud subscription '{item.cloud_source_id}': {exc}"
                )
                item_errors.append({"cloud_source_id": item.cloud_source_id, "error": str(exc)})

        # Retire/remove cloud subscriptions removed on the server without clobbering
        # local subscriptions or active profile bindings.
        for source_id, row in existing_by_source.items():
            if source_id in seen_sources:
                continue
            catalog.delete_subscription(str(row["id"]), retire_bound=True, _allow_cloud=True)
            retired += 1

        sanitized_meta = [
            {
                "cloud_source_id": item.cloud_source_id,
                "name": item.name,
                "kind": item.kind,
                "auto_sync": item.auto_sync,
                "enabled": item.enabled,
            }
            for item in payload.subscriptions
        ]
        stamp = _utc_now()
        with self.connection:
            self.connection.execute(
                "INSERT INTO cloud_cache (kind, version, sha256, payload, fetched_at) "
                "VALUES ('subscriptions', ?, ?, ?, ?) "
                "ON CONFLICT(kind) DO UPDATE SET version=excluded.version, sha256=excluded.sha256, "
                "payload=excluded.payload, fetched_at=excluded.fetched_at",
                (payload.version, payload.sha256, canonical_json(sanitized_meta), stamp),
            )
        if response.etag and not item_errors:
            self._set_setting("subscriptions_etag", json.dumps(response.etag))

        total = len(seen_sources)
        err_text = "; ".join(f"{e['cloud_source_id']}: {e['error']}" for e in item_errors) or None
        status: Literal["updated", "unchanged", "error"]
        if item_errors and created == 0 and updated == 0:
            status = "error"
        elif created > 0 or updated > 0 or retired > 0:
            status = "updated"
        else:
            status = "unchanged"
        return CloudSubscriptionsSyncResult(
            status=status,
            version=payload.version,
            created=created,
            updated=updated,
            retired=retired,
            unchanged=unchanged,
            total=total,
            error=err_text,
            message=(
                f"Synced cloud subscriptions ({created} created, {updated} updated, "
                f"{unchanged} unchanged, {retired} retired)."
                if not item_errors
                else f"Synced cloud subscriptions with {len(item_errors)} error(s)."
            ),
            details={
                "created": created,
                "updated": updated,
                "unchanged": unchanged,
                "retired": retired,
                "total": total,
                "errors": item_errors,
                "sha256": payload.sha256,
            },
        )

    def _cloud_node_seal_fn(self) -> Callable[[str], str] | None:
        """Return an ``enc2:`` sealer bound to the active ``seal_salt``, or ``None`` for legacy ``enc:``."""
        salt = get_active_seal_salt(self.connection)
        if not salt:
            return None

        def _seal_v2(plaintext: str) -> str:
            if not get_settings().secret_key:
                raise NetworkSecretsUnavailable(
                    "An encryption key is required to store subscriptions and nodes. "
                    "Restore secret.key or configure CPM_SECRET_KEY."
                )
            try:
                return encrypt_with_salt(plaintext, salt)
            except ValueError as exc:
                raise NetworkSecretsUnavailable(str(exc)) from None

        return _seal_v2

    async def _reconcile_subscription_item(
        self,
        catalog: NetworkCatalog,
        item: CloudSubscriptionItem,
        existing: sqlite3.Row | None,
    ) -> Literal["created", "updated", "unchanged"]:
        """Create or update one cloud-managed subscription."""
        stamp = _utc_now()
        cloud_seal_fn = self._cloud_node_seal_fn()

        if item.kind == "sealed_nodes":
            if item.nodes is None:
                raise SubscriptionError(
                    "invalid_source", "Cloud sealed_nodes subscription has no nodes list."
                )
            seal("")  # fail early if CPM_SECRET_KEY is not configured
            if len(item.nodes) == 0:
                parsed = ParsedSubscription(nodes=[])
            else:
                parsed = _collect(
                    [(index, raw_node, None) for index, raw_node in enumerate(item.nodes, start=1)]
                )
            display_name = " ".join(item.name.split())[:80] or "Cloud Nodes"
            if existing is None:
                sub_id = new_id("s")
                with self.connection:
                    self.connection.execute("BEGIN IMMEDIATE")
                    self.connection.execute(
                        "INSERT INTO network_subscriptions (id, name, source_kind, source_url_enc, "
                        "source_host, source_file_name, status, managed_by, cloud_source_id, "
                        "created_at, updated_at) "
                        "VALUES (?, ?, 'file', NULL, NULL, NULL, 'idle', 'cloud', ?, ?, ?)",
                        (sub_id, display_name, item.cloud_source_id, stamp, stamp),
                    )
                    catalog._apply(sub_id, parsed, mask_entry=True, seal_fn=cloud_seal_fn)
                return "created"
            sub_id = str(existing["id"])
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                self.connection.execute(
                    "UPDATE network_subscriptions "
                    "SET name = ?, source_kind = 'file', source_url_enc = NULL, source_host = NULL, "
                    "source_file_name = NULL, managed_by = 'cloud', cloud_source_id = ?, "
                    "updated_at = ? WHERE id = ?",
                    (display_name, item.cloud_source_id, stamp, sub_id),
                )
                rep = catalog._apply(sub_id, parsed, mask_entry=True, seal_fn=cloud_seal_fn)
            if (
                rep["created"] == 0
                and rep["updated"] == 0
                and rep["retired"] == 0
                and existing["name"] == display_name
            ):
                return "unchanged"
            return "updated"

        if item.kind == "url":
            if not item.url:
                raise SubscriptionError("invalid_source", "Cloud URL subscription has no URL.")
            validated_url = validate_subscription_url(item.url)
            if existing is None:
                res = await catalog.create_subscription(
                    item.name,
                    url=validated_url,
                    fetch=self._subscription_fetcher,
                    _mask_entry=True,
                    _seal_fn=cloud_seal_fn,
                )
                sub_id = res["subscription"]["id"]
                with self.connection:
                    self.connection.execute(
                        "UPDATE network_subscriptions "
                        "SET managed_by = 'cloud', cloud_source_id = ?, updated_at = ? "
                        "WHERE id = ?",
                        (item.cloud_source_id, stamp, sub_id),
                    )
                return "created"
            sub_id = str(existing["id"])
            with self.connection:
                self.connection.execute(
                    "UPDATE network_subscriptions "
                    "SET name = ?, source_kind = 'url', source_url_enc = ?, source_host = ?, "
                    "source_file_name = NULL, managed_by = 'cloud', cloud_source_id = ?, "
                    "updated_at = ? WHERE id = ?",
                    (
                        item.name[:80],
                        seal(validated_url),
                        urlsplit(validated_url).hostname,
                        item.cloud_source_id,
                        stamp,
                        sub_id,
                    ),
                )
            if item.auto_sync:
                sync_res = await catalog.sync_subscription(
                    sub_id,
                    fetch=self._subscription_fetcher,
                    _allow_cloud=True,
                    _mask_entry=True,
                    _seal_fn=cloud_seal_fn,
                )
                rep = sync_res["report"]
                if (
                    rep["created"] == 0
                    and rep["updated"] == 0
                    and rep["retired"] == 0
                    and existing["name"] == item.name[:80]
                ):
                    return "unchanged"
                return "updated"
            return "unchanged" if existing["name"] == item.name[:80] else "updated"

        if item.kind == "content":
            if not item.content:
                raise SubscriptionError(
                    "invalid_source", "Cloud content subscription has no content."
                )
            content_bytes = item.content.encode("utf-8")
            file_label = f"cloud:{item.cloud_source_id}.yaml"
            if existing is None:
                res = await catalog.create_subscription(
                    item.name,
                    content=content_bytes,
                    file_name=file_label,
                    _mask_entry=True,
                    _seal_fn=cloud_seal_fn,
                )
                sub_id = res["subscription"]["id"]
                with self.connection:
                    self.connection.execute(
                        "UPDATE network_subscriptions "
                        "SET managed_by = 'cloud', cloud_source_id = ?, updated_at = ? "
                        "WHERE id = ?",
                        (item.cloud_source_id, stamp, sub_id),
                    )
                return "created"
            sub_id = str(existing["id"])
            with self.connection:
                self.connection.execute(
                    "UPDATE network_subscriptions "
                    "SET name = ?, source_kind = 'file', source_url_enc = NULL, source_host = NULL, "
                    "source_file_name = ?, managed_by = 'cloud', cloud_source_id = ?, "
                    "updated_at = ? WHERE id = ?",
                    (item.name[:80], file_label[:128], item.cloud_source_id, stamp, sub_id),
                )
            sync_res = await catalog.sync_subscription(
                sub_id,
                content=content_bytes,
                _allow_cloud=True,
                _mask_entry=True,
                _seal_fn=cloud_seal_fn,
            )
            rep = sync_res["report"]
            if (
                rep["created"] == 0
                and rep["updated"] == 0
                and rep["retired"] == 0
                and existing["name"] == item.name[:80]
            ):
                return "unchanged"
            return "updated"

        # item.kind == "manual"
        lines = item.manual_nodes or []
        if not lines:
            raise SubscriptionError("empty", "Cloud manual node group has no nodes.")
        seal("")  # fail early if CPM_SECRET_KEY is not configured
        candidates = parse_manual_text("\n".join(lines))
        parsed = _collect(candidates)
        if existing is None:
            group = catalog.create_manual_group(item.name)
            sub_id = str(group["id"])
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                self.connection.execute(
                    "UPDATE network_subscriptions "
                    "SET managed_by = 'cloud', cloud_source_id = ?, updated_at = ? "
                    "WHERE id = ?",
                    (item.cloud_source_id, stamp, sub_id),
                )
                catalog._apply(sub_id, parsed, mask_entry=True, seal_fn=cloud_seal_fn)
            return "created"

        sub_id = str(existing["id"])
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "UPDATE network_subscriptions "
                "SET name = ?, source_kind = 'manual', source_url_enc = NULL, source_host = NULL, "
                "managed_by = 'cloud', cloud_source_id = ?, updated_at = ? "
                "WHERE id = ?",
                (item.name[:80], item.cloud_source_id, stamp, sub_id),
            )
            rep = catalog._apply(sub_id, parsed, mask_entry=True, seal_fn=cloud_seal_fn)
        if (
            rep["created"] == 0
            and rep["updated"] == 0
            and rep["retired"] == 0
            and existing["name"] == item.name[:80]
        ):
            return "unchanged"
        return "updated"

    # --- Profile Push / Pull ------------------------------------------------

    def _is_profile_active_or_leased(self, profile_id: str) -> bool:
        if self.manager.browser_sessions.is_running(profile_id):
            return True
        row = self.connection.execute(
            "SELECT locked_by, lock_expires FROM profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        if row is None or not row["locked_by"]:
            return False
        return not lease_expired(row["lock_expires"])

    async def _export_profile_cookies_json(self, profile: Profile) -> str:
        if self._cookie_exporter is not None:
            return await self._cookie_exporter(profile.id)
        try:
            from camoufox_pm.core import cookie_transfer

            transfer_cls = getattr(cookie_transfer, "CookieTransfer", None)
            if transfer_cls is None:
                raise CloudSyncError(
                    "cookie_export_unavailable",
                    "Cookie export must be supplied by the Prism Desktop session bridge.",
                    status_code=422,
                )
            transfer = transfer_cls(self.manager)
            return str(await transfer.export_cookies(profile.id, "json", profile.row_version))
        except CloudSyncError:
            raise
        except Exception as exc:
            raise CloudSyncError(
                "cookie_export_failed",
                f"Could not export cookies for cloud upload: {exc}",
                status_code=422,
            ) from None

    async def _import_profile_cookies_json(self, profile: Profile, cookies_json: str) -> None:
        if self._cookie_importer is not None:
            await self._cookie_importer(profile.id, cookies_json)
            return
        from camoufox_pm.core import cookie_transfer

        transfer_cls = getattr(cookie_transfer, "CookieTransfer", None)
        if transfer_cls is None:
            raise CloudSyncError(
                "cookie_import_unavailable",
                "Cookie restore requires the Prism Desktop session bridge.",
                status_code=422,
            )
        transfer = transfer_cls(self.manager)
        await transfer.import_cookies(
            profile.id,
            cookies_json,
            "json",
            "merge",
            profile.row_version,
            False,
        )

    async def push_profile(
        self,
        profile_id: str,
        *,
        include_cookies: bool = False,
        client: CloudClient | None = None,
    ) -> dict[str, Any]:
        """Push a browser profile snapshot (with client-side encrypted secrets) to the cloud."""
        profile = await self.manager.get_profile(profile_id)
        if profile is None:
            raise CloudSyncError(
                "profile_not_found", f"Profile {profile_id} not found.", status_code=404
            )
        row = self.connection.execute(
            "SELECT isolation_config, fingerprint_hash, cloud_revision "
            "FROM profiles WHERE id = ? AND deleted_at IS NULL",
            (profile_id,),
        ).fetchone()
        if row is None:
            raise CloudSyncError(
                "profile_not_found", f"Profile {profile_id} not found.", status_code=404
            )

        isolation_config: dict[str, Any] | None = None
        if row["isolation_config"]:
            isolation_config = json.loads(row["isolation_config"])
        elif profile.isolation_config is not None:
            isolation_config = dict(profile.isolation_config)

        fingerprint_hash = row["fingerprint_hash"]
        if not fingerprint_hash and isinstance(isolation_config, dict):
            fingerprint_hash = isolation_config.get("fingerprint_hash") or None
        current_rev = int(row["cloud_revision"] or 0)

        binding_row = self.connection.execute(
            "SELECT n.node_key, s.cloud_source_id "
            "FROM profile_network_bindings b "
            "JOIN network_nodes n ON n.id = b.node_id "
            "JOIN network_subscriptions s ON s.id = n.subscription_id "
            "WHERE b.profile_id = ?",
            (profile_id,),
        ).fetchone()
        bound_cloud_sub_id = (
            str(binding_row["cloud_source_id"])
            if binding_row and binding_row["cloud_source_id"]
            else None
        )
        bound_node_key = str(binding_row["node_key"]) if binding_row else None

        secret_key = get_settings().secret_key
        encrypted_cookies_blob: str | None = None
        if include_cookies:
            if not secret_key:
                raise CloudSyncError(
                    "encryption_key_required",
                    "Configure CPM_SECRET_KEY (or secret.key) before syncing cookies to the cloud.",
                    status_code=409,
                )
            if self._is_profile_active_or_leased(profile_id):
                raise CloudSyncError(
                    "profile_running",
                    "Stop the browser before pushing its cookies to the cloud.",
                    status_code=409,
                )
            cookies_json = await self._export_profile_cookies_json(profile)
            sealed_cookies = encrypt(cookies_json)
            if not sealed_cookies.startswith("enc:"):
                raise CloudSyncError(
                    "encryption_key_required",
                    "Failed to encrypt cookies client-side; refusing plaintext upload.",
                    status_code=409,
                )
            encrypted_cookies_blob = sealed_cookies

        # Client-side encrypt notes whenever CPM_SECRET_KEY is configured so private
        # credentials or account memos are never stored in plaintext on the cloud server.
        wire_notes = profile.notes
        encrypted_metadata_blob: str | None = None
        if profile.notes and secret_key:
            sealed_meta = encrypt(json.dumps({"notes": profile.notes}, ensure_ascii=False))
            if sealed_meta.startswith("enc:"):
                encrypted_metadata_blob = sealed_meta
                wire_notes = None

        cfg = self.get_config()
        status_value = (
            profile.status.value if hasattr(profile.status, "value") else str(profile.status)
        )
        snapshot = CloudProfileSnapshot(
            id=profile.id,
            name=profile.name,
            group=profile.group,
            tags=list(profile.tags),
            notes=wire_notes,
            status=status_value,
            isolation_config=isolation_config,
            fingerprint_hash=fingerprint_hash,
            bound_subscription_cloud_id=bound_cloud_sub_id,
            bound_node_key=bound_node_key,
            encrypted_cookies_blob=encrypted_cookies_blob,
            encrypted_metadata_blob=encrypted_metadata_blob,
            revision=current_rev + 1,
            source_device=cfg.device_label or None,
        )

        cloud = client or self._build_client()
        try:
            saved = await cloud.put_profile(profile_id, snapshot)
        except CloudClientError as exc:
            await self._handle_client_error(exc)
            raise CloudSyncError(
                exc.code,
                str(exc),
                status_code=exc.status_code or 502,
            ) from None

        synced_at = saved.updated_at or _utc_now()
        with self.connection:
            self.connection.execute(
                "UPDATE profiles SET cloud_revision = ?, cloud_synced_at = ? WHERE id = ?",
                (saved.revision, synced_at, profile_id),
            )
        return {
            "profile_id": profile_id,
            "revision": saved.revision,
            "synced_at": synced_at,
            "included_cookies": bool(encrypted_cookies_blob),
            "bound_subscription_cloud_id": saved.bound_subscription_cloud_id,
            "bound_node_key": saved.bound_node_key,
        }

    async def pull_profiles(
        self,
        *,
        include_cookies: bool = False,
        force: bool = False,
        client: CloudClient | None = None,
    ) -> dict[str, Any]:
        """Pull profile snapshots from cloud, skipping running browsers and re-binding local nodes."""
        cloud = client or self._build_client()
        try:
            payload = await cloud.list_profiles()
        except CloudClientError as exc:
            await self._handle_client_error(exc)
            raise CloudSyncError(
                exc.code,
                str(exc),
                status_code=exc.status_code or 502,
            ) from None

        created = 0
        updated = 0
        unchanged = 0
        rebound = 0
        cookies_restored = 0
        skipped_running_ids: list[str] = []
        warnings: list[str] = []
        stamp = _utc_now()

        for snapshot in payload.profiles:
            if self._is_profile_active_or_leased(snapshot.id):
                skipped_running_ids.append(snapshot.id)
                warnings.append(
                    f"Skipped profile '{snapshot.name}' ({snapshot.id}): browser is currently running."
                )
                continue

            resolved_notes = self._resolve_snapshot_notes(snapshot, warnings)
            iso_dict, fp_hash, pinned_fp = self._normalize_snapshot_isolation(snapshot, warnings)
            browser_settings = _isolation_to_browser_settings(iso_dict)

            existing = self.connection.execute(
                "SELECT id, cloud_revision, row_version, notes FROM profiles "
                "WHERE id = ? AND deleted_at IS NULL",
                (snapshot.id,),
            ).fetchone()

            if (
                existing is not None
                and not force
                and int(existing["cloud_revision"] or 0) >= snapshot.revision
            ):
                unchanged += 1
                if self._try_rebind_profile(snapshot, warnings):
                    rebound += 1
                continue

            if resolved_notes is None and existing is not None and snapshot.encrypted_metadata_blob:
                # Decryption failed earlier; preserve existing local notes.
                resolved_notes = existing["notes"]

            try:
                status_enum = ProfileStatus(snapshot.status)
            except ValueError:
                status_enum = ProfileStatus.ACTIVE

            try:
                clean_tags = normalize_tags(list(snapshot.tags))
            except ValueError:
                clean_tags = []

            profile_dir = Path(self.manager.profiles_dir) / f"profile_{snapshot.id}"
            profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(profile_dir, 0o700)
            except OSError:
                pass

            if existing is None:
                profile = Profile(
                    id=snapshot.id,
                    name=snapshot.name,
                    group=snapshot.group,
                    tags=clean_tags,
                    status=status_enum,
                    browser_settings=browser_settings,
                    storage_path=str(profile_dir),
                    notes=resolved_notes,
                    fingerprint=pinned_fp,
                    isolation_config=iso_dict,
                    fingerprint_hash=fp_hash,
                    cloud_revision=snapshot.revision,
                    cloud_synced_at=datetime.fromisoformat(stamp),
                )
                await self.manager.storage.save_profile(profile)
                with self.connection:
                    self.connection.execute(
                        "UPDATE profiles SET isolation_config = ?, fingerprint_hash = ?, "
                        "cloud_revision = ?, cloud_synced_at = ? WHERE id = ?",
                        (
                            canonical_json(iso_dict) if iso_dict is not None else None,
                            fp_hash,
                            snapshot.revision,
                            stamp,
                            snapshot.id,
                        ),
                    )
                created += 1
            else:
                with self.connection:
                    self.connection.execute(
                        "UPDATE profiles SET name = ?, group_id = ?, tags = ?, status = ?, "
                        "browser_settings = ?, notes = ?, fingerprint = ?, isolation_config = ?, "
                        "fingerprint_hash = ?, cloud_revision = ?, cloud_synced_at = ?, "
                        "updated_at = ?, row_version = row_version + 1 "
                        "WHERE id = ? AND deleted_at IS NULL",
                        (
                            snapshot.name,
                            snapshot.group,
                            json.dumps(clean_tags, ensure_ascii=False),
                            status_enum.value,
                            json.dumps(browser_settings.model_dump()),
                            resolved_notes,
                            json.dumps(pinned_fp) if pinned_fp is not None else None,
                            canonical_json(iso_dict) if iso_dict is not None else None,
                            fp_hash,
                            snapshot.revision,
                            stamp,
                            stamp,
                            snapshot.id,
                        ),
                    )
                updated += 1

            if self._try_rebind_profile(snapshot, warnings):
                rebound += 1

            if include_cookies and snapshot.encrypted_cookies_blob:
                restored = await self._restore_snapshot_cookies(snapshot, warnings)
                if restored:
                    cookies_restored += 1

        return {
            "total_remote": len(payload.profiles),
            "created": created,
            "updated": updated,
            "unchanged": unchanged,
            "rebound": rebound,
            "cookies_restored": cookies_restored,
            "skipped_running": len(skipped_running_ids),
            "skipped_running_ids": skipped_running_ids,
            "warnings": warnings,
            "errors": [],
        }

    def _resolve_snapshot_notes(
        self, snapshot: CloudProfileSnapshot, warnings: list[str]
    ) -> str | None:
        if not snapshot.encrypted_metadata_blob:
            return snapshot.notes
        if not get_settings().secret_key:
            warnings.append(
                f"Profile '{snapshot.name}' ({snapshot.id}) has encrypted notes; set CPM_SECRET_KEY to decrypt."
            )
            return None
        decrypted = decrypt(snapshot.encrypted_metadata_blob)
        if decrypted == snapshot.encrypted_metadata_blob or decrypted.startswith("enc:"):
            warnings.append(
                f"Could not decrypt notes for profile '{snapshot.name}' ({snapshot.id}); check CPM_SECRET_KEY."
            )
            return None
        try:
            data = json.loads(decrypted)
            if isinstance(data, dict) and "notes" in data:
                return str(data["notes"]) if data["notes"] is not None else None
        except ValueError:
            warnings.append(f"Corrupted encrypted metadata for profile '{snapshot.id}'.")
        return None

    def _normalize_snapshot_isolation(
        self, snapshot: CloudProfileSnapshot, warnings: list[str]
    ) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None]:
        iso_dict = snapshot.isolation_config
        fp_hash = snapshot.fingerprint_hash
        pinned_fp: dict[str, Any] | None = None
        if isinstance(iso_dict, dict):
            try:
                from camoufox_pm.core.isolation.models import ProfileIsolation

                validated_iso = ProfileIsolation.model_validate(iso_dict)
                iso_dict = validated_iso.model_dump(mode="json")
                fp_hash = validated_iso.fingerprint_hash or fp_hash
                if validated_iso.pinned_fingerprint:
                    pinned_fp = dict(validated_iso.pinned_fingerprint)
            except Exception as exc:
                warnings.append(
                    f"Isolation config for profile '{snapshot.id}' kept as raw snapshot ({exc})."
                )
                raw_pin = iso_dict.get("pinned_fingerprint")
                if isinstance(raw_pin, dict):
                    pinned_fp = raw_pin
                if not fp_hash and isinstance(iso_dict.get("fingerprint_hash"), str):
                    fp_hash = iso_dict["fingerprint_hash"] or None
        return iso_dict, fp_hash, pinned_fp

    def _try_rebind_profile(self, snapshot: CloudProfileSnapshot, warnings: list[str]) -> bool:
        if not snapshot.bound_subscription_cloud_id or not snapshot.bound_node_key:
            return False
        node_row = self.connection.execute(
            "SELECT n.id FROM network_nodes n "
            "JOIN network_subscriptions s ON s.id = n.subscription_id "
            "WHERE s.cloud_source_id = ? AND s.deleted_at IS NULL "
            "AND n.node_key = ? AND n.status = 'active' "
            "LIMIT 1",
            (snapshot.bound_subscription_cloud_id, snapshot.bound_node_key),
        ).fetchone()
        if node_row is None:
            warnings.append(
                f"Profile '{snapshot.name}' ({snapshot.id}): bound node "
                f"({snapshot.bound_subscription_cloud_id}/{snapshot.bound_node_key[:8]}) "
                "was not found locally; sync subscriptions first."
            )
            return False
        node_id = str(node_row["id"])
        stamp = _utc_now()
        with self.connection:
            existing_binding = self.connection.execute(
                "SELECT node_id FROM profile_network_bindings WHERE profile_id = ?",
                (snapshot.id,),
            ).fetchone()
            if existing_binding is not None and str(existing_binding["node_id"]) == node_id:
                return False
            if existing_binding is None:
                self.connection.execute(
                    "INSERT INTO profile_network_bindings (profile_id, node_id, row_version, bound_at) "
                    "VALUES (?, ?, 0, ?)",
                    (snapshot.id, node_id, stamp),
                )
            else:
                self.connection.execute(
                    "UPDATE profile_network_bindings "
                    "SET node_id = ?, row_version = row_version + 1, bound_at = ? "
                    "WHERE profile_id = ?",
                    (node_id, stamp, snapshot.id),
                )
        return True

    async def _restore_snapshot_cookies(
        self, snapshot: CloudProfileSnapshot, warnings: list[str]
    ) -> bool:
        blob = snapshot.encrypted_cookies_blob
        if not blob:
            return False
        if not get_settings().secret_key:
            warnings.append(
                f"Skipped cookie restore for '{snapshot.id}': CPM_SECRET_KEY is not configured."
            )
            return False
        clear = decrypt(blob)
        if clear == blob or clear.startswith("enc:"):
            warnings.append(
                f"Skipped cookie restore for '{snapshot.id}': failed to decrypt cookie blob."
            )
            return False
        profile = await self.manager.get_profile(snapshot.id)
        if profile is None:
            return False
        try:
            await self._import_profile_cookies_json(profile, clear)
        except Exception as exc:
            warnings.append(f"Failed to import cookies for '{snapshot.id}': {exc}")
            return False
        return True

    # --- Full Sync ----------------------------------------------------------

    async def sync_all(
        self,
        scopes: Sequence[str] | None = None,
        *,
        force: bool = False,
        include_cookies: bool = False,
        _prefetched_manifest: CloudResponse[CloudManifest] | None = None,
    ) -> CloudSyncReport:
        """Synchronize requested scopes against the configured cloud server."""
        cfg = self.get_config()
        if scopes is None:
            resolved_scopes = ["policy", "subscriptions"]
            if cfg.sync_profiles_enabled:
                resolved_scopes.append("profiles")
        else:
            normalized: list[str] = []
            for raw_scope in scopes:
                s = raw_scope.strip().lower()
                if s == "all":
                    for item in _VALID_SCOPES:
                        if item not in normalized:
                            normalized.append(item)
                elif s in _VALID_SCOPES:
                    if s not in normalized:
                        normalized.append(s)
                else:
                    raise CloudSyncError(
                        "invalid_scope",
                        f"Unknown cloud sync scope '{raw_scope}'. Valid scopes: policy, subscriptions, profiles, all.",
                        status_code=422,
                    )
            resolved_scopes = normalized or ["policy", "subscriptions"]

        synced_at = _utc_now()
        errors: list[str] = []
        manifest_version: dict[str, str] = {}
        policy_res: CloudPolicySyncResult | None = None
        subs_res: CloudSubscriptionsSyncResult | None = None
        profiles_res: CloudProfilesSyncResult | None = None

        client = self._build_client()
        raw_settings = self._raw_settings()
        manifest_etag = (
            None if force else (self._str_setting(raw_settings, "manifest_etag", "") or None)
        )

        try:
            manifest_resp = (
                _prefetched_manifest
                if _prefetched_manifest is not None
                else await client.get_manifest(etag=manifest_etag)
            )
            if manifest_resp.etag:
                self._set_setting("manifest_etag", json.dumps(manifest_resp.etag))
            if manifest_resp.not_modified:
                touch_entitlement_sync(self.connection)
                if scopes is None:
                    ent = evaluate_entitlement(self.connection)
                    if not ent.profiles_sync_enabled or (
                        self._is_customer_edition() and ent.legacy_mode
                    ):
                        resolved_scopes = [s for s in resolved_scopes if s != "profiles"]
            if manifest_resp.data is not None:
                manifest_version = {
                    "isolation_policy": manifest_resp.data.isolation_policy.version,
                    "subscriptions": manifest_resp.data.subscriptions.version,
                }
                if manifest_resp.data.account is not None:
                    save_entitlement(self.connection, manifest_resp.data.account)
                    if manifest_resp.data.account.status.strip().lower() in {
                        "expired",
                        "disabled",
                        "revoked",
                    }:
                        await revoke_local_cloud_nodes(self.manager)
                # Respect server-side per-tenant profiles_sync_enabled when caller asked for defaults.
                if scopes is None and not manifest_resp.data.profiles_sync_enabled:
                    resolved_scopes = [s for s in resolved_scopes if s != "profiles"]
                elif (
                    scopes is None
                    and manifest_resp.data.account is not None
                    and not manifest_resp.data.account.profiles_sync_enabled
                ):
                    resolved_scopes = [s for s in resolved_scopes if s != "profiles"]
        except CloudClientError as exc:
            await self._handle_client_error(exc)
            err_msg = str(exc)
            errors.append(err_msg)
            self._set_many_settings(
                {
                    "last_sync_at": json.dumps(synced_at),
                    "last_sync_ok": json.dumps(False),
                    "last_error": json.dumps(err_msg),
                }
            )
            raise CloudSyncError(
                exc.code,
                err_msg,
                status_code=exc.status_code or 502,
            ) from None

        if "policy" in resolved_scopes:
            try:
                policy_res = await self.sync_policy(client=client, force=force)
            except CloudSyncError as exc:
                errors.append(f"policy: {exc}")
                policy_res = CloudPolicySyncResult(
                    status="error",
                    updated=False,
                    message=str(exc),
                    error=str(exc),
                    details={"code": exc.code},
                )

        if "subscriptions" in resolved_scopes:
            try:
                subs_res = await self.sync_subscriptions(client=client, force=force)
                if subs_res.status == "error":
                    errors.append(f"subscriptions: {subs_res.error or subs_res.message}")
            except CloudSyncError as exc:
                errors.append(f"subscriptions: {exc}")
                subs_res = CloudSubscriptionsSyncResult(
                    status="error",
                    message=str(exc),
                    error=str(exc),
                    details={"code": exc.code},
                )

        if "profiles" in resolved_scopes:
            try:
                pull_data = await self.pull_profiles(
                    include_cookies=include_cookies,
                    force=force,
                    client=client,
                )
                changed = (
                    pull_data["created"] > 0 or pull_data["updated"] > 0 or pull_data["rebound"] > 0
                )
                profiles_res = CloudProfilesSyncResult(
                    status="updated" if changed else "unchanged",
                    total_remote=pull_data["total_remote"],
                    created=pull_data["created"],
                    updated=pull_data["updated"],
                    unchanged=pull_data["unchanged"],
                    skipped_running=pull_data["skipped_running"],
                    skipped_running_ids=pull_data["skipped_running_ids"],
                    rebound=pull_data["rebound"],
                    cookies_restored=pull_data["cookies_restored"],
                    warnings=pull_data["warnings"],
                    message=(
                        f"Pulled {pull_data['total_remote']} remote profile(s): "
                        f"{pull_data['created']} created, {pull_data['updated']} updated, "
                        f"{pull_data['rebound']} rebound."
                    ),
                    details=pull_data,
                )
            except CloudSyncError as exc:
                errors.append(f"profiles: {exc}")
                profiles_res = CloudProfilesSyncResult(
                    status="error",
                    message=str(exc),
                    error=str(exc),
                    details={"code": exc.code},
                )

        ok = len(errors) == 0
        self._set_many_settings(
            {
                "last_sync_at": json.dumps(synced_at),
                "last_sync_ok": json.dumps(ok),
                "last_error": None if ok else json.dumps("; ".join(errors)),
            }
        )
        return CloudSyncReport(
            ok=ok,
            synced_at=synced_at,
            scopes=resolved_scopes,
            manifest_version=manifest_version,
            policy=policy_res,
            subscriptions=subs_res,
            profiles=profiles_res,
            errors=errors,
        )

    async def activate(self, token: str) -> dict[str, Any]:
        """Validate a candidate token before saving it, force sync, and return ``{status, report}``."""
        cleaned = (token or "").strip()
        if not cleaned:
            raise CloudSyncError(
                "invalid_token",
                "请输入有效的授权码。",
                status_code=422,
            )
        # Validate the candidate token against GET /v1/manifest BEFORE mutating
        # stored token_enc, entitlement state, or local cloud nodes so a typo on
        # either a fresh install or an already-active license never corrupts state.
        probe_client = self._build_client(token_override=cleaned)
        try:
            manifest_resp = await probe_client.get_manifest()
        except CloudClientError as exc:
            raise CloudSyncError(
                exc.code,
                str(exc),
                status_code=exc.status_code or 502,
            ) from None

        if (
            manifest_resp.data is not None
            and manifest_resp.data.account is not None
            and manifest_resp.data.account.status.strip().lower()
            in {"expired", "disabled", "revoked"}
        ):
            bad_status = manifest_resp.data.account.status.strip().lower()
            raise CloudSyncError(
                f"tenant_{bad_status}",
                "该授权码当前不可用，请联系管理员确认授权状态。",
                status_code=403,
            )

        self._set_many_settings(
            {
                "token_enc": json.dumps(encrypt(cleaned)),
                "manifest_etag": None,
                "policy_etag": None,
                "subscriptions_etag": None,
            }
        )
        report = await self.sync_all(force=True, _prefetched_manifest=manifest_resp)
        return {
            "status": self.get_status().model_dump(mode="json"),
            "report": report.model_dump(mode="json"),
        }

    async def logout_cloud(self) -> CloudStatusData:
        """Clear saved cloud token and revoke all local cloud nodes (keeping local nodes)."""
        self._set_many_settings(
            {
                "token_enc": None,
                "manifest_etag": None,
                "policy_etag": None,
                "subscriptions_etag": None,
                "last_sync_ok": None,
                "last_error": None,
            }
        )
        await revoke_local_cloud_nodes(self.manager, reason_status="logout")
        return self.get_status()

    async def maybe_sync_on_startup(self) -> CloudSyncReport | None:
        """Silently run an initial sync if cloud delivery is configured and enabled."""
        cfg = self.get_config()
        if not cfg.configured:
            return None
        if self._is_customer_edition():
            if not cfg.has_token:
                return None
        elif not cfg.auto_sync_on_startup:
            return None
        try:
            return await self.sync_all()
        except CloudSyncError as exc:
            logger.warning(f"Startup cloud sync failed ({exc.code}): {exc}")
            return None
