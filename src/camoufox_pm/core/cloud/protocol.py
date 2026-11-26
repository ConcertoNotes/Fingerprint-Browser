"""Standard wire protocol models and canonical SHA-256 helpers for cloud delivery."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Literal

from pydantic import BaseModel, Field


def canonical_json(data: Any) -> str:
    """Serialize JSON deterministically for cross-platform SHA-256 verification."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_sha256(data: Any) -> str:
    """Compute the SHA-256 hex digest of ``canonical_json(data)``."""
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def verify_payload_sha256(data: Any, expected_sha256: str) -> bool:
    """Check ``data`` against ``expected_sha256`` using constant-time comparison."""
    expected = (expected_sha256 or "").strip().lower()
    if len(expected) != 64:
        return False
    actual = canonical_sha256(data)
    return hmac.compare_digest(actual, expected)


class CloudArtifactMeta(BaseModel):
    """Version and digest metadata for one artifact inside the cloud manifest."""

    version: str
    sha256: str
    updated_at: str | None = None
    count: int | None = None


class CloudAccountEntitlement(BaseModel):
    """Per-tenant license & device entitlement returned inside ``GET /v1/manifest``."""

    tenant_id: str
    tenant_name: str = ""
    status: str = "active"
    expires_at: str | None = None
    server_time: str | None = None
    max_devices: int = 0
    active_devices: int = 0
    offline_grace_hours: int = 24
    profiles_sync_enabled: bool = False
    seal_salt: str | None = None


class CloudManifest(BaseModel):
    """Response schema for ``GET /v1/manifest``."""

    protocol_version: int = 1
    server_time: str | None = None
    isolation_policy: CloudArtifactMeta
    subscriptions: CloudArtifactMeta
    profiles_sync_enabled: bool = True
    profiles_count: int = 0
    account: CloudAccountEntitlement | None = None


class CloudIsolationPolicyPayload(BaseModel):
    """Response schema for ``GET /v1/isolation-policy``."""

    version: str
    sha256: str
    updated_at: str | None = None
    policy: dict[str, Any]


class CloudSubscriptionItem(BaseModel):
    """One cloud-managed subscription entry inside ``GET /v1/subscriptions``."""

    cloud_source_id: str = Field(..., min_length=1, max_length=128)
    name: str = Field(..., min_length=1, max_length=128)
    kind: Literal["url", "content", "manual", "sealed_nodes"]
    url: str | None = None
    content: str | None = None
    manual_nodes: list[str] | None = None
    nodes: list[dict[str, Any]] | None = None
    auto_sync: bool = True
    enabled: bool = True


class CloudSubscriptionsPayload(BaseModel):
    """Response schema for ``GET /v1/subscriptions``."""

    version: str
    sha256: str
    updated_at: str | None = None
    subscriptions: list[CloudSubscriptionItem] = Field(default_factory=list)


class CloudProfileSnapshot(BaseModel):
    """Encrypted browser profile snapshot exchanged via ``/v1/profiles``."""

    id: str = Field(..., min_length=1, max_length=128)
    name: str = Field(..., min_length=1, max_length=255)
    group: str | None = None
    tags: list[str] = Field(default_factory=list)
    notes: str | None = None
    status: str = "active"
    isolation_config: dict[str, Any] | None = None
    fingerprint_hash: str | None = None
    bound_subscription_cloud_id: str | None = None
    bound_node_key: str | None = None
    encrypted_cookies_blob: str | None = None
    encrypted_metadata_blob: str | None = None
    revision: int = Field(default=0, ge=0)
    source_device: str | None = None
    updated_at: str | None = None


class CloudProfilesListPayload(BaseModel):
    """Response schema for ``GET /v1/profiles``."""

    profiles: list[CloudProfileSnapshot] = Field(default_factory=list)
    count: int = 0


class CloudScopeResult(BaseModel):
    """Base per-scope outcome inside a :class:`CloudSyncReport`."""

    scope: Literal["policy", "subscriptions", "profiles"]
    status: Literal["updated", "unchanged", "skipped", "error"]
    version: str | None = None
    message: str = ""
    error: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class CloudPolicySyncResult(CloudScopeResult):
    """Policy scope result with UI-friendly top-level summary fields."""

    scope: Literal["policy"] = "policy"
    updated: bool = False
    sha256: str | None = None


class CloudSubscriptionsSyncResult(CloudScopeResult):
    """Subscriptions scope result with reconciliation counters."""

    scope: Literal["subscriptions"] = "subscriptions"
    created: int = 0
    updated: int = 0
    retired: int = 0
    unchanged: int = 0
    total: int = 0


class CloudProfilesSyncResult(CloudScopeResult):
    """Profiles scope result with pull/rebind counters."""

    scope: Literal["profiles"] = "profiles"
    total_remote: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_running: int = 0
    skipped_running_ids: list[str] = Field(default_factory=list)
    rebound: int = 0
    cookies_restored: int = 0
    warnings: list[str] = Field(default_factory=list)


class CloudSyncReport(BaseModel):
    """Structured result returned by :meth:`CloudDeliveryService.sync_all`."""

    ok: bool
    synced_at: str
    scopes: list[str] = Field(default_factory=list)
    manifest_version: dict[str, str] = Field(default_factory=dict)
    policy: CloudPolicySyncResult | None = None
    subscriptions: CloudSubscriptionsSyncResult | None = None
    profiles: CloudProfilesSyncResult | None = None
    errors: list[str] = Field(default_factory=list)


class CloudConfigUpdate(BaseModel):
    """Input model for ``PUT /api/v1/cloud/config``."""

    base_url: str | None = None
    token: str | None = None
    clear_token: bool = False
    auto_sync_on_startup: bool | None = None
    sync_profiles_enabled: bool | None = None
    device_label: str | None = Field(default=None, max_length=64)
    allow_local: bool | None = None


class CloudConfigPublic(BaseModel):
    """Public cloud delivery settings — never includes token plaintext."""

    base_url: str | None = None
    has_token: bool = False
    auto_sync_on_startup: bool = True
    sync_profiles_enabled: bool = True
    device_label: str = ""
    allow_local: bool = False
    configured: bool = False


class CloudStatusData(BaseModel):
    """Payload for ``GET /api/v1/cloud/status`` and ``PUT /api/v1/cloud/config``."""

    configured: bool = False
    connected: bool = False
    base_url: str | None = None
    has_token: bool = False
    auto_sync_on_startup: bool = True
    sync_profiles_enabled: bool = True
    device_label: str | None = ""
    allow_local: bool = False
    policy_source: str = "builtin"
    policy_version: str = ""
    policy_sha256: str | None = None
    policy_fetched_at: str | None = None
    subscriptions_version: str | None = None
    subscriptions_fetched_at: str | None = None
    cloud_subscription_count: int = 0
    synced_profiles_count: int = 0
    last_synced_at: str | None = None
    last_sync_ok: bool | None = None
    last_error: str | None = None
    edition: str = "standard"
    brand_name: str = "棱镜浏览器"
    cloud_url_locked: bool = False
    require_activation: bool = False
    tenant_id: str | None = None
    tenant_name: str | None = None
    entitlement_status: str = "none"
    expires_at: str | None = None
    days_remaining: int | None = None
    max_devices: int | None = None
    active_devices: int | None = None
    offline_grace_hours: int | None = None
    profiles_sync_allowed: bool = True
    device_id: str | None = None
