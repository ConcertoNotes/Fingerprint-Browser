"""Cloud delivery & sync REST endpoints (``/api/v1/cloud/*``).

Token secrets are write-only and sealed at rest; responses carry ``Cache-Control: no-store``
and never echo cloud tokens or encrypted secret blobs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from camoufox_pm.api.dependencies import get_profile_manager
from camoufox_pm.api.errors import ApiError
from camoufox_pm.api.models.system import ApiResponse
from camoufox_pm.core.cloud import (
    CloudConfigUpdate,
    CloudDeliveryService,
    CloudSyncError,
)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/cloud", dependencies=[Depends(_no_store)])
Data = ApiResponse[dict[str, Any]]

_service_factory: Callable[[Any], CloudDeliveryService] | None = None


def set_cloud_service_factory(
    factory: Callable[[Any], CloudDeliveryService] | None,
) -> None:
    """Override :class:`CloudDeliveryService` construction (used in tests)."""
    global _service_factory
    _service_factory = factory


def _service() -> CloudDeliveryService:
    manager = get_profile_manager()
    if _service_factory is not None:
        return _service_factory(manager)
    return CloudDeliveryService(manager)


class CloudSyncRequest(BaseModel):
    """Request body for ``POST /api/v1/cloud/sync``."""

    scopes: list[str] | None = None
    force: bool = False
    include_cookies: bool = False


class CloudProfilePushRequest(BaseModel):
    """Request body for ``POST /api/v1/cloud/profiles/{id}/push``."""

    include_cookies: bool = False


class CloudActivateRequest(BaseModel):
    """Request body for ``POST /api/v1/cloud/activate``."""

    token: str = Field(..., min_length=1, max_length=512)


class CloudProfilesPullRequest(BaseModel):
    """Request body for ``POST /api/v1/cloud/profiles/pull``."""

    include_cookies: bool = False
    force: bool = False


@router.get("/status", response_model=Data, operation_id="cloud_status")
async def cloud_status() -> Data:
    """Return cloud connection settings (without secrets), policy version, and sync state."""
    try:
        data = _service().get_status().model_dump(mode="json")
    except CloudSyncError as exc:
        raise ApiError(exc.status_code, str(exc), exc.code) from None
    return Data(success=True, message="Cloud delivery status", data=data)


@router.put("/config", response_model=Data, operation_id="update_cloud_config")
async def update_cloud_config(request: CloudConfigUpdate) -> Data:
    """Update cloud server URL, write-only Bearer token, and automatic sync switches."""
    svc = _service()
    try:
        svc.update_config(request)
        data = svc.get_status().model_dump(mode="json")
    except CloudSyncError as exc:
        raise ApiError(exc.status_code, str(exc), exc.code) from None
    return Data(success=True, message="Cloud configuration saved", data=data)


@router.post("/activate", response_model=Data, operation_id="activate_cloud")
async def activate_cloud(request: CloudActivateRequest) -> Data:
    """Save activation token, immediately force sync, and return ``{status, report}``."""
    try:
        data = await _service().activate(request.token)
    except CloudSyncError as exc:
        raise ApiError(exc.status_code, str(exc), exc.code) from None
    return Data(success=True, message="Cloud license activated", data=data)


@router.post("/logout", response_model=Data, operation_id="logout_cloud")
async def logout_cloud() -> Data:
    """Clear cloud authorization token and revoke local cloud nodes (keeping local nodes)."""
    try:
        status = await _service().logout_cloud()
    except CloudSyncError as exc:
        raise ApiError(exc.status_code, str(exc), exc.code) from None
    return Data(
        success=True,
        message="Cloud license logged out",
        data=status.model_dump(mode="json"),
    )


@router.post("/sync", response_model=Data, operation_id="sync_cloud")
async def sync_cloud(request: CloudSyncRequest | None = None) -> Data:
    """Trigger synchronization for policy, subscriptions, and/or profiles."""
    body = request or CloudSyncRequest()
    try:
        report = await _service().sync_all(
            scopes=body.scopes,
            force=body.force,
            include_cookies=body.include_cookies,
        )
    except CloudSyncError as exc:
        raise ApiError(exc.status_code, str(exc), exc.code) from None
    return Data(
        success=True,
        message="Cloud sync completed" if report.ok else "Cloud sync completed with errors",
        data=report.model_dump(mode="json"),
    )


@router.get("/policy", response_model=Data, operation_id="get_cloud_policy")
async def get_cloud_policy() -> Data:
    """Inspect the active fixed-optimization isolation policy (cloud or builtin fallback)."""
    try:
        data = _service().get_active_policy_info()
    except CloudSyncError as exc:
        raise ApiError(exc.status_code, str(exc), exc.code) from None
    return Data(success=True, message="Active isolation policy", data=data)


@router.post(
    "/profiles/{profile_id}/push",
    response_model=Data,
    operation_id="push_cloud_profile",
)
async def push_cloud_profile(
    profile_id: str,
    request: CloudProfilePushRequest | None = None,
) -> Data:
    """Push one browser profile snapshot (with client-side encrypted secrets) to the cloud."""
    body = request or CloudProfilePushRequest()
    try:
        data = await _service().push_profile(
            profile_id,
            include_cookies=body.include_cookies,
        )
    except CloudSyncError as exc:
        raise ApiError(exc.status_code, str(exc), exc.code) from None
    return Data(success=True, message="Profile pushed to cloud", data=data)


@router.post(
    "/profiles/pull",
    response_model=Data,
    operation_id="pull_cloud_profiles",
)
async def pull_cloud_profiles(request: CloudProfilesPullRequest | None = None) -> Data:
    """Pull remote browser profiles, skipping running browsers and re-binding matching nodes."""
    body = request or CloudProfilesPullRequest()
    try:
        data = await _service().pull_profiles(
            include_cookies=body.include_cookies,
            force=body.force,
        )
    except CloudSyncError as exc:
        raise ApiError(exc.status_code, str(exc), exc.code) from None
    return Data(success=True, message="Cloud profiles pulled", data=data)
