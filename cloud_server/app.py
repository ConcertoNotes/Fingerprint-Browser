"""Standalone multi-tenant FastAPI cloud configuration server (`prism-cloud-server`).

Serves:
- ``GET  /health``                                  — Unauthenticated healthcheck
- ``GET  /admin``                                   — Visual Chinese admin web console
- ``GET  /v1/manifest``                             — Per-tenant manifest + account entitlement + ETag
- ``GET  /v1/isolation-policy``                     — Fixed-optimization isolation policy
- ``GET  /v1/subscriptions``                        — Per-tenant sealed_nodes subscriptions (no upstream URLs)
- ``GET/PUT/DELETE /v1/profiles[/{profile_id}]``    — Tenant-isolated encrypted browser profile snapshots
- ``POST /v1/admin/login|logout`` & ``GET /session``— Admin session cookie auth (HttpOnly, SameSite=Strict)
- ``GET  /v1/admin/overview``                       — Dashboard statistics
- ``CRUD /v1/admin/tenants`` & ``rotate-token``     — Multi-tenant account & expiry management
- ``GET/DELETE /v1/admin/tenants/{id}/devices``     — Device inventory & revocation
- ``CRUD /v1/admin/sources`` & ``refresh``          — Server-side upstream subscription pool & resolver
- ``GET/PUT /v1/admin/tenants/{id}/assignments``    — Shared pool assignment with keyword/key filtering
- ``POST /v1/admin/tenants/{id}/private-nodes``     — Per-tenant private static IP management
- ``GET  /v1/admin/audit-logs``                     — Admin & client interception audit log
- ``GET/PUT /v1/admin/isolation-policy``            — Global isolation policy management
- ``PUT  /v1/admin/subscriptions``                  — Legacy single-tenant subscription update
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import threading
import time
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from .admin_ui import create_admin_ui_router
from .resolver import (
    FetcherCallable,
    ResolveError,
    normalize_node_filter,
    resolve_source,
    summarize_nodes,
)
from .store import DEFAULT_TENANT_ID, CloudStore, is_expired

ADMIN_COOKIE_NAME = "prism_admin_session"
ADMIN_SESSION_TTL_SECONDS = 12 * 3600
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 300.0
LOGIN_RATE_LIMIT_MAX_FAILURES = 10

_UPSTREAM_NETWORK_ERROR_CODES = frozenset(
    {
        "connection_failed",
        "timeout",
        "http_status",
        "dns_failed",
        "tls_failed",
        "too_many_redirects",
    }
)


class _CloudAuthError(Exception):
    """Structured 401/403/429 JSON error aligned with Prism client protocol."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra or {}
        super().__init__(message)


@dataclass(frozen=True)
class TenantContext:
    """Authenticated tenant state resolved per client request."""

    tenant: dict[str, Any]
    active_devices: int
    device_id: str | None = None

    @property
    def tenant_id(self) -> str:
        return str(self.tenant["id"])


class _LoginRateLimiter:
    """Thread-safe in-memory sliding-window rate limiter for failed admin logins."""

    def __init__(
        self,
        *,
        max_failures: int = LOGIN_RATE_LIMIT_MAX_FAILURES,
        window_seconds: float = LOGIN_RATE_LIMIT_WINDOW_SECONDS,
    ) -> None:
        self._max_failures = max_failures
        self._window = window_seconds
        self._lock = threading.Lock()
        self._attempts: dict[str, list[float]] = {}

    def _prune(self, key: str, now: float) -> list[float]:
        cutoff = now - self._window
        recent = [ts for ts in self._attempts.get(key, []) if ts >= cutoff]
        if recent:
            self._attempts[key] = recent
        else:
            self._attempts.pop(key, None)
        return recent

    def is_blocked(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            return len(self._prune(key, now)) >= self._max_failures

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            recent = self._prune(key, now)
            recent.append(now)
            self._attempts[key] = recent

    def clear(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)


# --- Pydantic Schemas -------------------------------------------------------


class CloudSubscriptionInput(BaseModel):
    """One cloud-managed subscription entry pushed via legacy ``PUT /v1/admin/subscriptions``."""

    cloud_source_id: str = Field(..., min_length=1, max_length=128)
    name: str = Field(..., min_length=1, max_length=128)
    kind: Literal["url", "content", "manual"]
    url: str | None = None
    content: str | None = None
    manual_nodes: list[str] | None = None
    auto_sync: bool = True
    enabled: bool = True

    @model_validator(mode="after")
    def _validate_kind_payload(self) -> CloudSubscriptionInput:
        if self.kind == "url" and not (self.url and self.url.strip()):
            raise ValueError("kind='url' requires a non-empty url")
        if self.kind == "content" and not (self.content and self.content.strip()):
            raise ValueError("kind='content' requires non-empty content")
        if self.kind == "manual" and not self.manual_nodes:
            raise ValueError("kind='manual' requires a non-empty manual_nodes list")
        return self


class AdminIsolationPolicyUpdate(BaseModel):
    """Request body for ``PUT /v1/admin/isolation-policy``."""

    version: str | None = Field(default=None, max_length=128)
    policy: dict[str, Any]


class AdminSubscriptionsUpdate(BaseModel):
    """Request body for legacy ``PUT /v1/admin/subscriptions``."""

    version: str | None = Field(default=None, max_length=128)
    subscriptions: list[CloudSubscriptionInput] = Field(default_factory=list)


class CloudProfileUpsert(BaseModel):
    """Request body for ``PUT /v1/profiles/{profile_id}``."""

    id: str | None = None
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


class AdminLoginRequest(BaseModel):
    """Request body for ``POST /v1/admin/login``."""

    token: str = Field(default="")


class TenantCreateRequest(BaseModel):
    """Request body for ``POST /v1/admin/tenants``."""

    name: str = Field(..., min_length=1, max_length=128)
    notes: str = Field(default="", max_length=1000)
    expires_at: str | None = None
    max_devices: int = Field(default=3, ge=0, le=1000)
    offline_grace_hours: int = Field(default=24, ge=0, le=8760)
    profiles_sync_enabled: bool = False
    status: Literal["active", "disabled"] = "active"


class TenantUpdateRequest(BaseModel):
    """Request body for ``PATCH /v1/admin/tenants/{id}``."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    notes: str | None = Field(default=None, max_length=1000)
    status: Literal["active", "disabled"] | None = None
    expires_at: str | None = None
    extend_days: int | None = Field(default=None, ge=1, le=3650)
    max_devices: int | None = Field(default=None, ge=0, le=1000)
    offline_grace_hours: int | None = Field(default=None, ge=0, le=8760)
    profiles_sync_enabled: bool | None = None


class SourceCreateRequest(BaseModel):
    """Request body for ``POST /v1/admin/sources``."""

    name: str = Field(..., min_length=1, max_length=128)
    scope: Literal["shared", "tenant_private"] = "shared"
    owner_tenant_id: str | None = None
    kind: Literal["url", "content", "manual"]
    url: str | None = None
    content: str | None = None
    manual_nodes: list[str] | None = None
    manual_text: str | None = None
    default_type: Literal["http", "https", "socks5"] = "http"
    enabled: bool = True
    resolve_now: bool = True

    def build_payload(self) -> dict[str, Any]:
        if self.kind == "url":
            clean = (self.url or "").strip()
            if not clean:
                raise ValueError("kind='url' requires a non-empty url")
            return {"url": clean}
        if self.kind == "content":
            clean = (self.content or "").strip()
            if not clean:
                raise ValueError("kind='content' requires non-empty content")
            return {"content": self.content or ""}
        text = (self.manual_text or "").strip()
        if not text and self.manual_nodes:
            text = "\n".join(str(line) for line in self.manual_nodes if str(line).strip())
        if not text:
            raise ValueError("kind='manual' requires non-empty manual_text or manual_nodes")
        return {"text": text, "default_type": self.default_type}


class SourceUpdateRequest(BaseModel):
    """Request body for ``PATCH /v1/admin/sources/{id}``."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool | None = None
    kind: Literal["url", "content", "manual"] | None = None
    url: str | None = None
    content: str | None = None
    manual_nodes: list[str] | None = None
    manual_text: str | None = None
    default_type: Literal["http", "https", "socks5"] | None = None
    resolve_now: bool = True


class TenantAssignmentItem(BaseModel):
    """One shared/private source assignment with optional display override & node filter."""

    source_id: str = Field(..., min_length=1, max_length=128)
    display_name_override: str | None = Field(default=None, max_length=128)
    node_filter: dict[str, Any] | None = None
    enabled: bool = True


class TenantAssignmentsUpdate(BaseModel):
    """Request body for ``PUT /v1/admin/tenants/{id}/assignments``."""

    assignments: list[TenantAssignmentItem] = Field(default_factory=list)
    replace_private: bool = False


class PrivateNodesRequest(BaseModel):
    """Request body for ``POST /v1/admin/tenants/{id}/private-nodes``."""

    name: str = Field(default="专属静态节点", min_length=1, max_length=128)
    nodes_text: str | None = None
    manual_nodes: list[str] | None = None
    default_type: Literal["http", "https", "socks5"] = "http"
    source_id: str | None = None
    enabled: bool = True

    def extract_text(self) -> str:
        text = (self.nodes_text or "").strip()
        if not text and self.manual_nodes:
            text = "\n".join(str(line) for line in self.manual_nodes if str(line).strip())
        if not text:
            raise ValueError("请粘贴至少一条代理节点或静态 IP")
        return text


# --- Helpers ----------------------------------------------------------------


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _etag_matches(if_none_match: str | None, current_etag: str) -> bool:
    if not if_none_match:
        return False
    target = current_etag.strip().removeprefix("W/").strip()
    for candidate in if_none_match.split(","):
        cleaned = candidate.strip()
        if cleaned == "*" or cleaned.removeprefix("W/").strip() == target:
            return True
    return False


def _env_first(*names: str, default: str = "") -> str:
    """Return the first non-empty environment variable value among ``names``."""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


_TRUSTED_PROXY_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def _is_trusted_proxy_peer(host: str | None) -> bool:
    """Return True when the direct TCP peer is loopback or RFC1918/ULA (e.g. Caddy/Docker)."""
    if not host:
        return False
    cleaned = host.strip().strip("[]").split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(cleaned)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _TRUSTED_PROXY_NETWORKS)


def _client_ip(request: Request) -> str:
    peer = str(request.client.host).strip() if (request.client and request.client.host) else ""
    if _is_trusted_proxy_peer(peer):
        real_ip = request.headers.get("x-real-ip", "").strip()
        if real_ip:
            return real_ip[:64]
        forwarded = request.headers.get("x-forwarded-for", "").strip()
        if forwarded:
            parts = [part.strip() for part in forwarded.split(",") if part.strip()]
            if parts:
                return parts[-1][:64]
    if peer:
        return peer[:64]
    return "unknown"


def _is_https_request(request: Request) -> bool:
    if request.url.scheme.lower() == "https":
        return True
    peer = str(request.client.host).strip() if (request.client and request.client.host) else ""
    if not _is_trusted_proxy_peer(peer):
        return False
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return proto == "https"


def _sign_admin_session(hmac_key: bytes, admin_token: str) -> str:
    token_hint = hashlib.sha256(admin_token.encode("utf-8")).hexdigest()[:16]
    payload = {
        "sub": "admin",
        "exp": int(time.time()) + ADMIN_SESSION_TTL_SECONDS,
        "th": token_hint,
    }
    raw = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii")
    sig = hmac.new(hmac_key, raw.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def _verify_admin_session(cookie_value: str | None, hmac_key: bytes, admin_token: str) -> bool:
    if not cookie_value or "." not in cookie_value:
        return False
    raw, _, sig = cookie_value.rpartition(".")
    if not raw or not sig:
        return False
    expected_sig = hmac.new(hmac_key, raw.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return False
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(payload, dict) or payload.get("sub") != "admin":
        return False
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < int(time.time()):
        return False
    expected_hint = hashlib.sha256(admin_token.encode("utf-8")).hexdigest()[:16]
    return hmac.compare_digest(str(payload.get("th") or ""), expected_hint)


# --- App Factory ------------------------------------------------------------


def create_app(
    *,
    db_path: str | Path | None = None,
    token: str | None = None,
    admin_token: str | None = None,
    secret_key: str | None = None,
    seed_policy_path: str | Path | None = None,
    subscription_fetcher: FetcherCallable | None = None,
) -> FastAPI:
    """Create a configured FastAPI application for the standalone cloud server."""
    resolved_db = (
        db_path
        if db_path is not None
        else _env_first("PRISM_CLOUD_DB", "LULU_CLOUD_DB", default="data/cloud_server.db")
    )
    resolved_token = (
        token if token is not None else _env_first("PRISM_CLOUD_TOKEN", "LULU_CLOUD_TOKEN")
    ).strip() or None
    resolved_admin_token = (
        admin_token
        if admin_token is not None
        else _env_first("PRISM_CLOUD_ADMIN_TOKEN", "LULU_CLOUD_ADMIN_TOKEN")
    ).strip() or None
    resolved_secret_key = (
        secret_key
        if secret_key is not None
        else _env_first("PRISM_CLOUD_SECRET_KEY", "LULU_CLOUD_SECRET_KEY")
    ).strip() or None

    store = CloudStore(
        resolved_db,
        seed_policy_path=seed_policy_path,
        secret_key=resolved_secret_key,
    )
    session_hmac_key = store.derive_hmac_key(b"prism-cloud-admin-session-v1")
    login_limiter = _LoginRateLimiter()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            store.close()

    app = FastAPI(
        title="Prism Cloud Config Server",
        version="2.0.0",
        description="Multi-tenant cloud delivery and sync server for Prism Browser (棱镜浏览器).",
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.subscription_fetcher = subscription_fetcher

    @app.exception_handler(_CloudAuthError)
    async def _handle_cloud_auth_error(_request: Request, exc: _CloudAuthError) -> JSONResponse:
        body: dict[str, Any] = {
            "code": exc.code,
            "message": exc.message,
            "detail": exc.message,
            **exc.extra,
        }
        return JSONResponse(
            status_code=exc.status_code,
            content=body,
            headers={"Cache-Control": "no-store"},
        )

    def _expected_admin_token() -> str | None:
        return resolved_admin_token or resolved_token

    def _is_admin_authenticated(request: Request, authorization: str | None) -> bool:
        expected = _expected_admin_token()
        if expected is None:
            return True
        presented = _extract_bearer(authorization)
        if presented is not None and hmac.compare_digest(presented, expected):
            return True
        cookie_val = request.cookies.get(ADMIN_COOKIE_NAME)
        return _verify_admin_session(cookie_val, session_hmac_key, expected)

    def require_admin_auth(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> None:
        if not _is_admin_authenticated(request, authorization):
            raise _CloudAuthError(401, "invalid_admin_token", "需要管理员认证，请先登录。")

    def require_tenant_auth(
        request: Request,
        authorization: str | None = Header(default=None),
        x_prism_device_id: str | None = Header(default=None, alias="X-Prism-Device-Id"),
        x_prism_device_label: str | None = Header(default=None, alias="X-Prism-Device-Label"),
        x_prism_client_version: str | None = Header(default=None, alias="X-Prism-Client-Version"),
    ) -> TenantContext:
        ip = _client_ip(request)
        presented = _extract_bearer(authorization)
        tenant: dict[str, Any] | None = None

        if presented is not None:
            tenant = store.get_tenant_by_token(presented)
            if tenant is None and resolved_token is not None:
                if hmac.compare_digest(presented, resolved_token):
                    tenant = store.ensure_default_tenant()
                elif resolved_admin_token is not None and hmac.compare_digest(
                    presented, resolved_admin_token
                ):
                    tenant = store.ensure_default_tenant()
            if tenant is None:
                store.log_audit(
                    "anonymous",
                    "tenant_auth_failed",
                    detail={"reason": "invalid_token", "path": request.url.path},
                    ip=ip,
                )
                raise _CloudAuthError(401, "invalid_token", "授权码无效或已失效，请检查后重试。")
        else:
            if (
                resolved_token is None
                and resolved_admin_token is None
                and not store.has_managed_tenants()
            ):
                tenant = store.ensure_default_tenant()
            else:
                raise _CloudAuthError(401, "invalid_token", "缺少有效的授权令牌。")

        tenant_id = str(tenant["id"])
        if tenant["status"] != "active":
            store.log_audit(
                f"tenant:{tenant_id}",
                "tenant_access_denied",
                target_id=tenant_id,
                detail={"reason": "tenant_disabled", "path": request.url.path},
                ip=ip,
            )
            raise _CloudAuthError(403, "tenant_disabled", "当前授权已被管理员停用，请联系管理员。")

        if is_expired(tenant["expires_at"]):
            store.log_audit(
                f"tenant:{tenant_id}",
                "tenant_access_denied",
                target_id=tenant_id,
                detail={
                    "reason": "tenant_expired",
                    "expires_at": tenant["expires_at"],
                    "path": request.url.path,
                },
                ip=ip,
            )
            raise _CloudAuthError(
                403,
                "tenant_expired",
                "当前授权已到期，请联系管理员续期。",
                extra={"expires_at": tenant["expires_at"]},
            )

        err_code, dev_info = store.verify_and_record_device(
            tenant,
            device_id=x_prism_device_id,
            device_label=x_prism_device_label,
            client_version=x_prism_client_version,
        )
        if err_code == "device_revoked":
            store.log_audit(
                f"tenant:{tenant_id}",
                "device_access_denied",
                target_id=tenant_id,
                detail={"reason": "device_revoked", "device_id": x_prism_device_id},
                ip=ip,
            )
            raise _CloudAuthError(
                403,
                "device_revoked",
                "当前设备已被管理员移除授权，请重新激活或联系管理员。",
            )
        if err_code == "device_limit_exceeded":
            store.log_audit(
                f"tenant:{tenant_id}",
                "device_access_denied",
                target_id=tenant_id,
                detail={
                    "reason": "device_limit_exceeded",
                    "device_id": x_prism_device_id,
                    **dev_info,
                },
                ip=ip,
            )
            raise _CloudAuthError(
                403,
                "device_limit_exceeded",
                f"已达到设备数上限（{dev_info['active_devices']}/{dev_info['max_devices']}），"
                "请联系管理员清理旧设备后再试。",
                extra={
                    "max_devices": dev_info["max_devices"],
                    "active_devices": dev_info["active_devices"],
                },
            )

        store.touch_tenant_seen(tenant_id)
        return TenantContext(
            tenant=tenant,
            active_devices=dev_info["active_devices"],
            device_id=(x_prism_device_id or "").strip() or None,
        )

    def _require_profiles_sync(ctx: TenantContext) -> None:
        if not ctx.tenant["profiles_sync_enabled"]:
            raise _CloudAuthError(
                403,
                "profiles_sync_disabled",
                "当前授权未开启浏览器配置云同步功能。",
            )

    def _get_fetcher() -> FetcherCallable | None:
        return getattr(app.state, "subscription_fetcher", None)

    # --- Visual Admin UI ----------------------------------------------------

    app.include_router(create_admin_ui_router())

    # --- Health & Client Protocol Routes ------------------------------------

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "service": "prism-cloud-server", "protocol_version": 1}

    @app.get("/v1/manifest")
    async def get_manifest(
        ctx: TenantContext = Depends(require_tenant_auth),
        if_none_match: str | None = Header(default=None),
    ) -> Response:
        manifest, etag = store.get_manifest(
            ctx.tenant_id,
            active_devices_override=ctx.active_devices,
        )
        if _etag_matches(if_none_match, etag):
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
        return JSONResponse(
            content=manifest,
            headers={"ETag": etag, "Cache-Control": "no-cache"},
        )

    @app.get("/v1/isolation-policy")
    async def get_isolation_policy(
        _ctx: TenantContext = Depends(require_tenant_auth),
        if_none_match: str | None = Header(default=None),
    ) -> Response:
        payload = store.get_isolation_policy()
        etag = f'"{payload["sha256"][:32]}"'
        if _etag_matches(if_none_match, etag):
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
        return JSONResponse(
            content=payload,
            headers={"ETag": etag, "Cache-Control": "no-cache"},
        )

    @app.get("/v1/subscriptions")
    async def get_subscriptions(
        ctx: TenantContext = Depends(require_tenant_auth),
        if_none_match: str | None = Header(default=None),
    ) -> Response:
        payload = store.get_tenant_subscriptions(ctx.tenant_id)
        etag = f'"{payload["sha256"][:32]}"'
        if _etag_matches(if_none_match, etag):
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
        return JSONResponse(
            content=payload,
            headers={"ETag": etag, "Cache-Control": "no-cache"},
        )

    @app.get("/v1/profiles")
    async def list_profiles(ctx: TenantContext = Depends(require_tenant_auth)) -> dict[str, Any]:
        _require_profiles_sync(ctx)
        profiles = store.list_profiles(tenant_id=ctx.tenant_id)
        return {"profiles": profiles, "count": len(profiles)}

    @app.get("/v1/profiles/{profile_id}")
    async def get_profile(
        profile_id: str,
        ctx: TenantContext = Depends(require_tenant_auth),
    ) -> dict[str, Any]:
        _require_profiles_sync(ctx)
        snapshot = store.get_profile(profile_id, tenant_id=ctx.tenant_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Cloud profile snapshot not found")
        return snapshot

    @app.put("/v1/profiles/{profile_id}")
    async def put_profile(
        profile_id: str,
        body: CloudProfileUpsert,
        ctx: TenantContext = Depends(require_tenant_auth),
    ) -> dict[str, Any]:
        _require_profiles_sync(ctx)
        clean_id = profile_id.strip()
        if not clean_id or len(clean_id) > 128 or "/" in clean_id or "\\" in clean_id:
            raise HTTPException(status_code=422, detail="Invalid profile_id")
        return store.upsert_profile(clean_id, body.model_dump(), tenant_id=ctx.tenant_id)

    @app.delete("/v1/profiles/{profile_id}")
    async def delete_profile(
        profile_id: str,
        ctx: TenantContext = Depends(require_tenant_auth),
    ) -> dict[str, Any]:
        _require_profiles_sync(ctx)
        deleted = store.delete_profile(profile_id, tenant_id=ctx.tenant_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Cloud profile snapshot not found")
        return {"deleted": True, "id": profile_id}

    # --- Admin Session Auth Routes ------------------------------------------

    @app.get("/v1/admin/session")
    async def get_admin_session(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        expected = _expected_admin_token()
        return JSONResponse(
            content={
                "authenticated": _is_admin_authenticated(request, authorization),
                "auth_required": expected is not None,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/v1/admin/login")
    async def admin_login(body: AdminLoginRequest, request: Request) -> JSONResponse:
        ip = _client_ip(request)
        expected = _expected_admin_token()
        if expected is None:
            return JSONResponse(
                content={"authenticated": True, "auth_required": False},
                headers={"Cache-Control": "no-store"},
            )
        if login_limiter.is_blocked(ip):
            store.log_audit("admin", "admin_login_rate_limited", ip=ip)
            raise _CloudAuthError(
                429,
                "rate_limited",
                "管理员登录失败次数过多，请 5 分钟后再试。",
            )
        presented = body.token.strip()
        if not presented or not hmac.compare_digest(presented, expected):
            login_limiter.record_failure(ip)
            store.log_audit("admin", "admin_login_failed", ip=ip)
            raise _CloudAuthError(401, "invalid_admin_token", "管理员令牌不正确。")

        login_limiter.clear(ip)
        cookie_value = _sign_admin_session(session_hmac_key, expected)
        store.log_audit("admin", "admin_login", ip=ip)
        response = JSONResponse(
            content={"authenticated": True, "auth_required": True},
            headers={"Cache-Control": "no-store"},
        )
        response.set_cookie(
            key=ADMIN_COOKIE_NAME,
            value=cookie_value,
            max_age=ADMIN_SESSION_TTL_SECONDS,
            httponly=True,
            samesite="strict",
            secure=_is_https_request(request),
            path="/",
        )
        return response

    @app.post("/v1/admin/logout")
    async def admin_logout(request: Request) -> JSONResponse:
        store.log_audit("admin", "admin_logout", ip=_client_ip(request))
        response = JSONResponse(
            content={"ok": True, "authenticated": False},
            headers={"Cache-Control": "no-store"},
        )
        response.delete_cookie(
            key=ADMIN_COOKIE_NAME,
            path="/",
            samesite="strict",
            httponly=True,
            secure=_is_https_request(request),
        )
        return response

    # --- Admin Overview & Policy / Legacy Subscriptions ---------------------

    @app.get("/v1/admin/overview", dependencies=[Depends(require_admin_auth)])
    async def get_admin_overview() -> dict[str, Any]:
        return store.get_overview()

    @app.get("/v1/admin/isolation-policy", dependencies=[Depends(require_admin_auth)])
    async def get_admin_isolation_policy() -> dict[str, Any]:
        return store.get_isolation_policy()

    @app.put("/v1/admin/isolation-policy", dependencies=[Depends(require_admin_auth)])
    async def put_admin_isolation_policy(
        body: AdminIsolationPolicyUpdate, request: Request
    ) -> dict[str, Any]:
        try:
            updated = store.set_isolation_policy(body.policy, version=body.version)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        store.log_audit(
            "admin",
            "isolation_policy_updated",
            target_id=updated["version"],
            detail={"sha256": updated["sha256"]},
            ip=_client_ip(request),
        )
        return updated

    @app.put("/v1/admin/subscriptions", dependencies=[Depends(require_admin_auth)])
    async def put_admin_subscriptions(
        body: AdminSubscriptionsUpdate, request: Request
    ) -> dict[str, Any]:
        try:
            items = [item.model_dump(exclude_none=True) for item in body.subscriptions]
            updated = store.set_subscriptions(items, version=body.version)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        store.log_audit(
            "admin",
            "legacy_subscriptions_updated",
            target_id=updated["version"],
            detail={"count": len(items)},
            ip=_client_ip(request),
        )
        return updated

    # --- Admin Tenants CRUD -------------------------------------------------

    @app.get("/v1/admin/tenants", dependencies=[Depends(require_admin_auth)])
    async def list_admin_tenants(
        search: str | None = Query(default=None),
        status: str | None = Query(default=None),
    ) -> dict[str, Any]:
        tenants = store.list_tenants(include_default=False, search=search, status_filter=status)
        return {"tenants": tenants, "count": len(tenants)}

    @app.post("/v1/admin/tenants", status_code=201, dependencies=[Depends(require_admin_auth)])
    async def create_admin_tenant(body: TenantCreateRequest, request: Request) -> JSONResponse:
        try:
            tenant, raw_token = store.create_tenant(
                name=body.name,
                notes=body.notes,
                expires_at=body.expires_at,
                max_devices=body.max_devices,
                offline_grace_hours=body.offline_grace_hours,
                profiles_sync_enabled=body.profiles_sync_enabled,
                status=body.status,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        store.log_audit(
            "admin",
            "tenant_created",
            target_id=tenant["id"],
            detail={
                "name": tenant["name"],
                "expires_at": tenant["expires_at"],
                "max_devices": tenant["max_devices"],
            },
            ip=_client_ip(request),
        )
        return JSONResponse(
            status_code=201,
            content={"tenant": tenant, "token": raw_token},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/v1/admin/tenants/{tenant_id}", dependencies=[Depends(require_admin_auth)])
    async def get_admin_tenant(tenant_id: str) -> dict[str, Any]:
        tenant = store.get_tenant(tenant_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail="Tenant not found")
        return {
            **tenant,
            "devices": store.list_tenant_devices(tenant_id),
            "assignments": store.list_tenant_assignments(tenant_id),
        }

    @app.patch("/v1/admin/tenants/{tenant_id}", dependencies=[Depends(require_admin_auth)])
    async def patch_admin_tenant(
        tenant_id: str,
        body: TenantUpdateRequest,
        request: Request,
    ) -> dict[str, Any]:
        fields = body.model_fields_set
        kwargs: dict[str, Any] = {}
        if "name" in fields and body.name is not None:
            kwargs["name"] = body.name
        if "notes" in fields and body.notes is not None:
            kwargs["notes"] = body.notes
        if "status" in fields and body.status is not None:
            kwargs["status"] = body.status
        if "extend_days" in fields and body.extend_days is not None:
            kwargs["extend_days"] = body.extend_days
        elif "expires_at" in fields:
            kwargs["expires_at"] = body.expires_at
        if "max_devices" in fields and body.max_devices is not None:
            kwargs["max_devices"] = body.max_devices
        if "offline_grace_hours" in fields and body.offline_grace_hours is not None:
            kwargs["offline_grace_hours"] = body.offline_grace_hours
        if "profiles_sync_enabled" in fields and body.profiles_sync_enabled is not None:
            kwargs["profiles_sync_enabled"] = body.profiles_sync_enabled

        try:
            updated = store.update_tenant(tenant_id, **kwargs)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        if updated is None:
            raise HTTPException(status_code=404, detail="Tenant not found")
        store.log_audit(
            "admin",
            "tenant_updated",
            target_id=tenant_id,
            detail=dict(kwargs),
            ip=_client_ip(request),
        )
        return updated

    @app.delete("/v1/admin/tenants/{tenant_id}", dependencies=[Depends(require_admin_auth)])
    async def delete_admin_tenant(tenant_id: str, request: Request) -> dict[str, Any]:
        if tenant_id == DEFAULT_TENANT_ID:
            raise HTTPException(status_code=422, detail="Built-in default tenant cannot be deleted")
        deleted = store.delete_tenant(tenant_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Tenant not found")
        store.log_audit("admin", "tenant_deleted", target_id=tenant_id, ip=_client_ip(request))
        return {"deleted": True, "id": tenant_id}

    @app.post(
        "/v1/admin/tenants/{tenant_id}/rotate-token",
        dependencies=[Depends(require_admin_auth)],
    )
    async def rotate_admin_tenant_token(tenant_id: str, request: Request) -> JSONResponse:
        try:
            rotated = store.rotate_tenant_token(tenant_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        if rotated is None:
            raise HTTPException(status_code=404, detail="Tenant not found")
        tenant, raw_token = rotated
        store.log_audit(
            "admin",
            "tenant_token_rotated",
            target_id=tenant_id,
            detail={"token_prefix": tenant["token_prefix"]},
            ip=_client_ip(request),
        )
        return JSONResponse(
            content={"tenant": tenant, "token": raw_token},
            headers={"Cache-Control": "no-store"},
        )

    # --- Admin Tenant Devices -----------------------------------------------

    @app.get("/v1/admin/tenants/{tenant_id}/devices", dependencies=[Depends(require_admin_auth)])
    async def list_admin_tenant_devices(tenant_id: str) -> dict[str, Any]:
        tenant = store.get_tenant(tenant_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail="Tenant not found")
        devices = store.list_tenant_devices(tenant_id)
        return {
            "tenant_id": tenant_id,
            "max_devices": tenant["max_devices"],
            "active_devices": store.count_active_devices(tenant_id),
            "devices": devices,
        }

    @app.delete(
        "/v1/admin/tenants/{tenant_id}/devices/{device_id}",
        dependencies=[Depends(require_admin_auth)],
    )
    async def revoke_admin_tenant_device(
        tenant_id: str,
        device_id: str,
        request: Request,
        purge: bool = Query(default=False),
    ) -> dict[str, Any]:
        tenant = store.get_tenant(tenant_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail="Tenant not found")
        changed = store.revoke_tenant_device(tenant_id, device_id, purge=purge)
        if not changed:
            raise HTTPException(status_code=404, detail="Device not found")
        store.log_audit(
            "admin",
            "device_purged" if purge else "device_revoked",
            target_id=tenant_id,
            detail={"device_id": device_id, "purge": purge},
            ip=_client_ip(request),
        )
        return {
            "revoked": True,
            "purged": purge,
            "tenant_id": tenant_id,
            "device_id": device_id,
            "active_devices": store.count_active_devices(tenant_id),
        }

    # --- Admin Subscription Sources & Server-Side Resolver ------------------

    @app.get("/v1/admin/sources", dependencies=[Depends(require_admin_auth)])
    async def list_admin_sources(
        scope: str | None = Query(default=None),
        owner_tenant_id: str | None = Query(default=None),
        include_nodes: bool = Query(default=False),
    ) -> dict[str, Any]:
        sources = store.list_sources(
            scope=scope,
            owner_tenant_id=owner_tenant_id,
            include_nodes_summary=include_nodes,
        )
        return {"sources": sources, "count": len(sources)}

    @app.post("/v1/admin/sources", status_code=201, dependencies=[Depends(require_admin_auth)])
    async def create_admin_source(body: SourceCreateRequest, request: Request) -> dict[str, Any]:
        if body.scope == "tenant_private":
            if not body.owner_tenant_id or store.get_tenant(body.owner_tenant_id) is None:
                raise HTTPException(
                    status_code=422, detail="owner_tenant_id must name an existing tenant"
                )
        try:
            payload = body.build_payload()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

        if body.kind == "url":
            from camoufox_pm.core.network_subscription import (
                SubscriptionError,
                validate_subscription_url,
            )

            try:
                payload["url"] = validate_subscription_url(payload["url"])
            except SubscriptionError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from None

        resolved_nodes: list[dict[str, Any]] | None = None
        last_error: str | None = None
        warnings: list[dict[str, Any]] = []

        if body.resolve_now or body.kind in {"content", "manual"}:
            try:
                resolved = await resolve_source(
                    body.kind,
                    payload,
                    fetcher=_get_fetcher(),
                    default_manual_type=body.default_type,
                )
                resolved_nodes = resolved.nodes
                warnings = resolved.warnings
            except ResolveError as exc:
                if body.kind in {"content", "manual"}:
                    raise HTTPException(status_code=422, detail=exc.message) from None
                # For URL sources, preserve the source record with last_error so admins can retry.
                last_error = f"{exc.code}: {exc.message}"

        try:
            source = store.create_source(
                name=body.name,
                kind=body.kind,
                payload=payload,
                scope=body.scope,
                owner_tenant_id=body.owner_tenant_id,
                enabled=body.enabled,
                resolved_nodes=resolved_nodes,
                last_error=last_error,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

        store.log_audit(
            "admin",
            "source_created",
            target_id=source["id"],
            detail={
                "name": source["name"],
                "scope": source["scope"],
                "kind": source["kind"],
                "node_count": source["node_count"],
                "last_error": last_error,
            },
            ip=_client_ip(request),
        )
        return {**source, "warnings": warnings}

    @app.get("/v1/admin/sources/{source_id}", dependencies=[Depends(require_admin_auth)])
    async def get_admin_source(source_id: str) -> dict[str, Any]:
        source = store.get_source(source_id, include_nodes_summary=True)
        if source is None:
            raise HTTPException(status_code=404, detail="Subscription source not found")
        return source

    @app.patch("/v1/admin/sources/{source_id}", dependencies=[Depends(require_admin_auth)])
    async def patch_admin_source(
        source_id: str,
        body: SourceUpdateRequest,
        request: Request,
    ) -> dict[str, Any]:
        existing = store.get_source_secret_payload(source_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Subscription source not found")
        meta, current_payload = existing
        target_kind = body.kind or str(meta["kind"])

        fields = body.model_fields_set
        payload_changed = bool(
            fields.intersection(
                {"kind", "url", "content", "manual_nodes", "manual_text", "default_type"}
            )
        )
        new_payload = dict(current_payload)
        if payload_changed:
            if target_kind == "url":
                if body.url is None or not body.url.strip():
                    raise HTTPException(
                        status_code=422, detail="kind='url' requires a non-empty url"
                    )
                from camoufox_pm.core.network_subscription import (
                    SubscriptionError,
                    validate_subscription_url,
                )

                try:
                    new_payload = {"url": validate_subscription_url(body.url.strip())}
                except SubscriptionError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from None
            elif target_kind == "content":
                if body.content is None or not body.content.strip():
                    raise HTTPException(
                        status_code=422, detail="kind='content' requires non-empty content"
                    )
                new_payload = {"content": body.content}
            else:
                text = (body.manual_text or "").strip()
                if not text and body.manual_nodes:
                    text = "\n".join(str(line) for line in body.manual_nodes if str(line).strip())
                if not text:
                    raise HTTPException(
                        status_code=422,
                        detail="kind='manual' requires non-empty manual_text or manual_nodes",
                    )
                new_payload = {
                    "text": text,
                    "default_type": body.default_type
                    or current_payload.get("default_type")
                    or "http",
                }

        update_kwargs: dict[str, Any] = {}
        if "name" in fields and body.name is not None:
            update_kwargs["name"] = body.name
        if "enabled" in fields and body.enabled is not None:
            update_kwargs["enabled"] = body.enabled
        if payload_changed:
            update_kwargs["kind"] = target_kind
            update_kwargs["payload"] = new_payload
            if body.resolve_now or target_kind in {"content", "manual"}:
                try:
                    resolved = await resolve_source(
                        target_kind,
                        new_payload,
                        fetcher=_get_fetcher(),
                        default_manual_type=str(new_payload.get("default_type") or "http"),
                    )
                    update_kwargs["resolved_nodes"] = resolved.nodes
                    update_kwargs["last_error"] = None
                except ResolveError as exc:
                    if target_kind in {"content", "manual"}:
                        raise HTTPException(status_code=422, detail=exc.message) from None
                    update_kwargs["last_error"] = f"{exc.code}: {exc.message}"

        try:
            updated = store.update_source(source_id, **update_kwargs)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        assert updated is not None
        store.log_audit(
            "admin",
            "source_updated",
            target_id=source_id,
            detail={"name": updated["name"], "enabled": updated["enabled"]},
            ip=_client_ip(request),
        )
        return updated

    @app.delete("/v1/admin/sources/{source_id}", dependencies=[Depends(require_admin_auth)])
    async def delete_admin_source(source_id: str, request: Request) -> dict[str, Any]:
        deleted = store.delete_source(source_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Subscription source not found")
        store.log_audit("admin", "source_deleted", target_id=source_id, ip=_client_ip(request))
        return {"deleted": True, "id": source_id}

    @app.post("/v1/admin/sources/{source_id}/refresh", dependencies=[Depends(require_admin_auth)])
    async def refresh_admin_source(source_id: str, request: Request) -> dict[str, Any]:
        secret_pair = store.get_source_secret_payload(source_id)
        if secret_pair is None:
            raise HTTPException(status_code=404, detail="Subscription source not found")
        meta, payload = secret_pair
        try:
            resolved = await resolve_source(
                str(meta["kind"]),
                payload,
                fetcher=_get_fetcher(),
                default_manual_type=str(payload.get("default_type") or "http"),
            )
        except ResolveError as exc:
            store.set_source_resolution(
                source_id,
                resolved_nodes=None,
                last_error=f"{exc.code}: {exc.message}",
            )
            store.log_audit(
                "admin",
                "source_refresh_failed",
                target_id=source_id,
                detail={"code": exc.code, "message": exc.message},
                ip=_client_ip(request),
            )
            status_code = 502 if exc.code in _UPSTREAM_NETWORK_ERROR_CODES else 422
            raise _CloudAuthError(status_code, exc.code, exc.message) from None

        updated = store.set_source_resolution(
            source_id,
            resolved_nodes=resolved.nodes,
            last_error=None,
        )
        assert updated is not None
        store.log_audit(
            "admin",
            "source_refreshed",
            target_id=source_id,
            detail={"node_count": len(resolved.nodes), "warnings": len(resolved.warnings)},
            ip=_client_ip(request),
        )
        return {
            "source": updated,
            "node_count": len(resolved.nodes),
            "warnings": resolved.warnings,
            "nodes_preview": summarize_nodes(resolved.nodes),
        }

    # --- Admin Tenant Assignments & Private Static Nodes --------------------

    @app.get(
        "/v1/admin/tenants/{tenant_id}/assignments",
        dependencies=[Depends(require_admin_auth)],
    )
    async def get_admin_tenant_assignments(tenant_id: str) -> dict[str, Any]:
        if store.get_tenant(tenant_id) is None:
            raise HTTPException(status_code=404, detail="Tenant not found")
        return {
            "tenant_id": tenant_id,
            "assignments": store.list_tenant_assignments(tenant_id),
        }

    @app.put(
        "/v1/admin/tenants/{tenant_id}/assignments",
        dependencies=[Depends(require_admin_auth)],
    )
    async def put_admin_tenant_assignments(
        tenant_id: str,
        body: TenantAssignmentsUpdate,
        request: Request,
    ) -> dict[str, Any]:
        try:
            payload = []
            for item in body.assignments:
                normalize_node_filter(item.node_filter)
                payload.append(item.model_dump())
            updated = store.set_tenant_assignments(
                tenant_id,
                payload,
                replace_private=body.replace_private,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Tenant not found") from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        store.log_audit(
            "admin",
            "tenant_assignments_updated",
            target_id=tenant_id,
            detail={"count": len(payload)},
            ip=_client_ip(request),
        )
        return {"tenant_id": tenant_id, "assignments": updated}

    @app.post(
        "/v1/admin/tenants/{tenant_id}/private-nodes",
        status_code=201,
        dependencies=[Depends(require_admin_auth)],
    )
    async def post_admin_tenant_private_nodes(
        tenant_id: str,
        body: PrivateNodesRequest,
        request: Request,
    ) -> dict[str, Any]:
        if store.get_tenant(tenant_id) is None:
            raise HTTPException(status_code=404, detail="Tenant not found")
        try:
            raw_text = body.extract_text()
            payload = {"text": raw_text, "default_type": body.default_type}
            resolved = await resolve_source(
                "manual",
                payload,
                default_manual_type=body.default_type,
            )
        except (ValueError, ResolveError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

        if body.source_id:
            secret_pair = store.get_source_secret_payload(body.source_id)
            if secret_pair is None:
                raise HTTPException(status_code=404, detail="Private source not found")
            meta, _ = secret_pair
            if meta["scope"] != "tenant_private" or meta["owner_tenant_id"] != tenant_id:
                raise HTTPException(status_code=403, detail="Source does not belong to this tenant")
            source = store.update_source(
                body.source_id,
                name=body.name,
                enabled=body.enabled,
                kind="manual",
                payload=payload,
                resolved_nodes=resolved.nodes,
                last_error=None,
            )
            assert source is not None
        else:
            source = store.create_source(
                name=body.name,
                kind="manual",
                payload=payload,
                scope="tenant_private",
                owner_tenant_id=tenant_id,
                enabled=body.enabled,
                resolved_nodes=resolved.nodes,
                last_error=None,
            )

        store.log_audit(
            "admin",
            "tenant_private_nodes_saved",
            target_id=tenant_id,
            detail={"source_id": source["id"], "node_count": len(resolved.nodes)},
            ip=_client_ip(request),
        )
        return {
            "source": source,
            "node_count": len(resolved.nodes),
            "warnings": resolved.warnings,
            "nodes_preview": summarize_nodes(resolved.nodes),
        }

    # --- Admin Audit Logs ---------------------------------------------------

    @app.get("/v1/admin/audit-logs", dependencies=[Depends(require_admin_auth)])
    async def list_admin_audit_logs(
        limit: int = Query(default=100, ge=1, le=500),
        action: str | None = Query(default=None),
        actor: str | None = Query(default=None),
    ) -> dict[str, Any]:
        logs = store.list_audit_logs(limit=limit, action=action, actor=actor)
        return {"logs": logs, "count": len(logs)}

    return app


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entrypoint for ``prism-cloud-server`` (alias ``lulu-cloud-server``)."""
    parser = argparse.ArgumentParser(
        prog="prism-cloud-server",
        description="Run the standalone Prism Cloud Config Server (棱镜浏览器云配置服务端).",
    )
    parser.add_argument(
        "--host",
        default=_env_first("PRISM_CLOUD_HOST", "LULU_CLOUD_HOST", default="127.0.0.1"),
        help="Bind address (default: 127.0.0.1 or PRISM_CLOUD_HOST)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(_env_first("PRISM_CLOUD_PORT", "LULU_CLOUD_PORT", default="8787")),
        help="Listen port (default: 8787 or PRISM_CLOUD_PORT)",
    )
    parser.add_argument(
        "--db",
        default=_env_first("PRISM_CLOUD_DB", "LULU_CLOUD_DB", default="data/cloud_server.db"),
        help="SQLite database path (default: data/cloud_server.db or PRISM_CLOUD_DB)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Legacy single-tenant client Bearer token (defaults to PRISM_CLOUD_TOKEN)",
    )
    parser.add_argument(
        "--admin-token",
        default=None,
        help="Admin Bearer/login token (defaults to PRISM_CLOUD_ADMIN_TOKEN)",
    )
    parser.add_argument(
        "--secret-key",
        default=None,
        help="Fernet secret key for encrypting upstream sources (defaults to PRISM_CLOUD_SECRET_KEY)",
    )
    parser.add_argument(
        "--seed-policy",
        default=None,
        help="Optional path to initial isolation_policy.json seed file",
    )
    args = parser.parse_args(argv)

    import uvicorn

    app = create_app(
        db_path=args.db,
        token=args.token,
        admin_token=args.admin_token,
        secret_key=args.secret_key,
        seed_policy_path=args.seed_policy,
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
