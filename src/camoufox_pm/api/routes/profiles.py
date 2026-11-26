"""API routes for managing browser profiles and isolation controls."""

from __future__ import annotations

import re
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from loguru import logger
from pydantic import ValidationError
from starlette.background import BackgroundTask

from camoufox_pm.api.dependencies import get_profile_manager, require_desktop_token
from camoufox_pm.api.errors import ApiError, validation_message
from camoufox_pm.api.models.profiles import (
    ActiveBrowsersResponse,
    BrowserCloseResponse,
    BrowsersCloseAllResponse,
    BulkTagsRequest,
    BulkTagsResponse,
    ClearGeographyRequest,
    ClearGeographyResponse,
    IsolationAuditResponse,
    ProfileCloneRequest,
    ProfileCreateRequest,
    ProfileLaunchRequest,
    ProfileLaunchResponse,
    ProfileListResponse,
    ProfileResponse,
    ProfileStatsResponse,
    ProfileUpdateRequest,
    ProxyCheckRequest,
    ProxyCheckResponse,
    ReconcileOsRequest,
    SessionPrepareRequest,
)
from camoufox_pm.api.models.system import ApiResponse, DevicePreset, PresetListData
from camoufox_pm.core import proxy_check
from camoufox_pm.core.database import StaleWriteError
from camoufox_pm.core.isolated_terminal import TerminalLaunchError, open_isolated_terminal
from camoufox_pm.core.leases import ProfileLocked
from camoufox_pm.core.mihomo_manager import MihomoUnavailable
from camoufox_pm.core.models import BrowserSettings, ProfileStatus, ProxyConfig, normalize_tags
from camoufox_pm.core.network_adapter import NetworkLaunchError
from camoufox_pm.core.network_binding import NetworkBindings, NodeUnavailable
from camoufox_pm.core.network_catalog import NetworkNotFound, NetworkSecretsUnavailable
from camoufox_pm.core.profile_manager import ExitIpDriftError

router = APIRouter()

# Maps deprecated flattened update fields onto their browser_settings keys.
_FLATTENED_BROWSER_FIELDS = {
    "browser_os": "os",
    "browser_screen": "screen",
    "browser_user_agent": "user_agent",
    "browser_languages": "languages",
    "browser_timezone": "timezone",
    "browser_locale": "locale",
    "browser_webrtc_mode": "webrtc_mode",
    "browser_canvas_noise": "canvas_noise",
    "browser_stable_canvas": "stable_canvas",
    "browser_webgl_noise": "webgl_noise",
    "browser_audio_noise": "audio_noise",
    "browser_hardware_concurrency": "hardware_concurrency",
    "browser_device_memory": "device_memory",
    "browser_max_touch_points": "max_touch_points",
    "browser_window_width": "window_width",
    "browser_window_height": "window_height",
}


def _policy_version() -> str:
    return get_profile_manager().active_policy()[0].policy_version


@router.get(
    "/profiles",
    response_model=ProfileListResponse,
    operation_id="list_profiles",
    summary="List profiles.",
    description="List all profiles with filtering and pagination.",
)
async def list_profiles(
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(50, ge=1, le=200, description="Profiles per page"),
    status: ProfileStatus | None = Query(None, description="Filter by status"),
    group: str | None = Query(None, description="Filter by group"),
    search: str | None = Query(None, max_length=255, description="Search by name or ID"),
    tag: str | None = Query(None, max_length=32),
    tags: list[str] | None = Query(None, description="All selected tags must match"),
):
    """List profiles with filtering."""
    try:
        profile_manager = get_profile_manager()
        filters: dict[str, Any] = {}
        if status:
            filters["status"] = status
        if group:
            filters["group"] = group
        if search:
            filters["search"] = search
        filters["tags"] = normalize_tags([*(tags or []), *([tag] if tag is not None else [])])

        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        total = await profile_manager.storage.count_profiles(filters)
        paginated_profiles = await profile_manager.list_profiles(
            filters=filters, limit=per_page, offset=start_idx
        )
        policy_ver = _policy_version()

        return ProfileListResponse(
            profiles=[
                ProfileResponse.from_profile(profile, active_policy_version=policy_ver)
                for profile in paginated_profiles
            ],
            total=total,
            page=page,
            per_page=per_page,
            has_next=end_idx < total,
            has_prev=page > 1,
        )
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    except Exception as e:
        logger.error(f"Failed to list profiles: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/profiles",
    response_model=ProfileResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_profile",
    summary="Create a profile.",
    description="Create a new isolated profile with an immediately pinned deduplicated hardware fingerprint and optional atomic node binding.",
)
async def create_profile(request: ProfileCreateRequest):
    """Create a new profile."""
    try:
        profile_manager = get_profile_manager()
        iso_dict = request.isolation.model_dump(exclude_none=True) if request.isolation else None
        profile = await profile_manager.create_profile(
            name=request.name,
            group=request.group,
            browser_settings=request.browser_settings,
            proxy_config=request.proxy_config,
            generate_fingerprint=request.generate_fingerprint,
            notes=request.notes,
            fingerprint_preset=request.fingerprint_preset,
            tags=request.tags,
            node_id=request.node_id,
            isolation=iso_dict,
            ip_drift_guard=request.ip_drift_guard,
        )
        return ProfileResponse.from_profile(profile, active_policy_version=_policy_version())
    except NetworkNotFound as e:
        raise ApiError(404, str(e), "node_not_found") from e
    except NodeUnavailable as e:
        raise ApiError(422, str(e), "node_unavailable") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Failed to create profile: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get(
    "/profiles/isolation/preview",
    response_model=ApiResponse[dict[str, Any]],
    operation_id="preview_profile_isolation",
    summary="Preview an auto-allocated deduplicated real device fingerprint.",
)
async def preview_profile_isolation(
    os: Literal["macos", "windows", "linux"] | None = Query(None),
    tier: Literal["strict", "standard"] | None = Query(None),
    preset_id: str | None = Query(None),
    exclude_preset_id: str | None = Query(None),
    webrtc_policy: Literal["block", "spoof_exit"] | None = Query(None),
    canvas_policy: Literal["stable_hardware", "session_noise"] | None = Query(None),
    locale_mode: Literal["fixed", "follow_exit"] | None = Query(None),
    timezone_mode: Literal["fixed", "follow_exit"] | None = Query(None),
    timezone: str | None = Query(None),
    install_webrtc_guard: bool | None = Query(None),
):
    """Preview an auto-allocated deduplicated device preset prior to profile creation."""
    try:
        preview = await get_profile_manager().preview_isolation(
            os_choice=os,
            tier=tier,
            preset_id=preset_id,
            exclude_preset_ids={exclude_preset_id} if exclude_preset_id else None,
            webrtc_policy=webrtc_policy,
            canvas_policy=canvas_policy,
            locale_mode=locale_mode,
            timezone_mode=timezone_mode,
            timezone=timezone,
            install_webrtc_guard=install_webrtc_guard,
        )
        return ApiResponse(success=True, message="Isolation preview ready", data=preview)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/profiles/fingerprint-presets",
    response_model=ApiResponse[PresetListData],
    operation_id="list_profile_fingerprint_presets",
    summary="List quality-filtered real device presets annotated with local usage.",
)
async def list_profile_fingerprint_presets(
    os: Literal["macos", "windows", "linux"] | None = Query(None),
):
    """Return filtered real device presets with ``in_use`` flags."""
    presets = await get_profile_manager().list_fingerprint_presets(os)
    return ApiResponse(
        success=True,
        message=f"{len(presets)} filtered presets available",
        data=PresetListData(
            presets=[DevicePreset(**preset) for preset in presets],
            total=len(presets),
        ),
    )


@router.post(
    "/profiles/tags",
    response_model=ApiResponse[BulkTagsResponse],
    operation_id="bulk_update_profile_tags",
    summary="Bulk add or remove tags on profiles.",
)
async def bulk_tags(request: BulkTagsRequest):
    result = await get_profile_manager().bulk_update_tags(
        [target.model_dump() for target in request.profiles], request.add, request.remove
    )
    policy_ver = _policy_version()
    return ApiResponse(
        success=True,
        message="Tag edit completed",
        data=BulkTagsResponse(
            updated=[
                ProfileResponse.from_profile(profile, active_policy_version=policy_ver)
                for profile in result["updated"]
            ],
            skipped=result["skipped"],
        ),
    )


@router.post(
    "/profiles/clear-geography",
    response_model=ClearGeographyResponse,
    operation_id="clear_profiles_geography",
    summary="Clear stored timezone and coordinates.",
)
async def clear_profiles_geography(request: ClearGeographyRequest):
    """Make the named profiles derive their location from their proxy again."""
    try:
        result = await get_profile_manager().clear_geography(request.profile_ids)
        return ClearGeographyResponse(**result)
    except StaleWriteError:
        raise
    except Exception as exc:
        logger.error(f"Failed to clear geography: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/profiles/import",
    response_model=ProfileResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="import_profile",
    summary="Import a profile.",
    description="Create a profile from an archive produced by the export endpoint.",
)
async def import_profile(
    file: UploadFile = File(...),
    name: str | None = None,
    form_name: str | None = Form(default=None, alias="name"),
):
    """Restore a profile from an uploaded archive."""
    name = name or form_name
    profile_manager = get_profile_manager()
    handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    source = Path(handle.name)
    try:
        shutil.copyfileobj(file.file, handle)
        handle.close()
        profile = await profile_manager.import_profile(source, name=name)
        return ProfileResponse.from_profile(profile, active_policy_version=_policy_version())
    except (ValueError, zipfile.BadZipFile) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(f"Failed to import profile: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        handle.close()
        source.unlink(missing_ok=True)


@router.get(
    "/profiles/{profile_id}",
    response_model=ProfileResponse,
    operation_id="get_profile",
    summary="Get a profile.",
    description="Get detailed information about a profile.",
)
async def get_profile(profile_id: str):
    """Get a profile by ID."""
    try:
        profile = await get_profile_manager().get_profile(profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail=f"Profile with ID {profile_id} not found")
        return ProfileResponse.from_profile(profile, active_policy_version=_policy_version())
    except HTTPException:
        raise
    except StaleWriteError:
        raise
    except Exception as e:
        logger.error(f"Failed to get profile {profile_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.put(
    "/profiles/{profile_id}",
    response_model=ProfileResponse,
    operation_id="update_profile",
    summary="Update a profile.",
)
async def update_profile(profile_id: str, request: ProfileUpdateRequest):
    """Update a profile."""
    try:
        profile_manager = get_profile_manager()
        sent = request.model_dump(exclude_unset=True)

        updates: dict[str, Any] = {}
        if sent.get("name") is not None:
            updates["name"] = sent["name"]
        if "group" in sent:
            updates["group"] = sent["group"]
        if sent.get("status") is not None:
            updates["status"] = sent["status"]
        if "proxy_config" in sent:
            updates["proxy_config"] = sent["proxy_config"]
        if "notes" in sent:
            updates["notes"] = sent["notes"]
        if "tags" in sent:
            updates["tags"] = sent["tags"] or []
        if sent.get("ip_drift_guard") is not None:
            updates["ip_drift_guard"] = sent["ip_drift_guard"]
        if sent.get("isolation") is not None:
            updates["isolation"] = {
                k: v for k, v in sent["isolation"].items() if v is not None or k == "timezone"
            }

        expected_row_version = sent.get("row_version")

        browser_updates: dict[str, Any] = {}
        if sent.get("browser_settings") is not None:
            browser_updates.update(sent["browser_settings"])
        for field, key in _FLATTENED_BROWSER_FIELDS.items():
            if sent.get(field) is not None:
                browser_updates[key] = sent[field]

        if browser_updates:
            current_profile = await profile_manager.get_profile(profile_id)
            if current_profile and current_profile.browser_settings:
                merged = current_profile.browser_settings.model_dump()
                merged.update(browser_updates)
                updates["browser_settings"] = merged
            else:
                updates["browser_settings"] = browser_updates

        updated_profile = await profile_manager.update_profile(
            profile_id, updates, expected_row_version=expected_row_version
        )
        if not updated_profile:
            raise HTTPException(status_code=404, detail=f"Profile with ID {profile_id} not found")
        return ProfileResponse.from_profile(
            updated_profile, active_policy_version=_policy_version()
        )
    except HTTPException:
        raise
    except StaleWriteError:
        raise
    except ValidationError as e:
        raise ApiError(422, validation_message(e.errors()), details=e.errors()) from e
    except Exception as e:
        logger.error(f"Failed to update profile {profile_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.delete(
    "/profiles/{profile_id}",
    response_model=ApiResponse[None],
    operation_id="delete_profile",
    summary="Delete a profile.",
    description="Permanently delete a closed profile and its browser data.",
)
async def delete_profile(profile_id: str):
    """Permanently delete a profile (no Trash)."""
    try:
        success = await get_profile_manager().delete_profile(profile_id)
        if not success:
            raise HTTPException(status_code=404, detail=f"Profile with ID {profile_id} not found")
        return ApiResponse(success=True, message=f"Profile {profile_id} deleted", data=None)
    except HTTPException:
        raise
    except ProfileLocked:
        raise
    except StaleWriteError:
        raise
    except Exception as e:
        logger.error(f"Failed to delete profile {profile_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/profiles/{profile_id}/audit",
    response_model=ApiResponse[IsolationAuditResponse],
    operation_id="audit_profile_isolation",
    summary="Run an isolation health audit on a profile.",
)
async def audit_profile(profile_id: str):
    """Run all isolation, anti-leak, and uniqueness checks on a profile."""
    try:
        report = await get_profile_manager().audit_profile(profile_id)
        return ApiResponse(
            success=True,
            message="Isolation audit complete",
            data=IsolationAuditResponse(**report),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(f"Failed to audit profile {profile_id}: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/profiles/{profile_id}/reoptimize",
    response_model=ProfileResponse,
    operation_id="reoptimize_profile_isolation",
    summary="Apply the latest isolation policy to a profile without changing its hardware.",
)
async def reoptimize_profile(profile_id: str):
    """Upgrade a profile's isolation policy & anti-leak prefs in place."""
    try:
        profile = await get_profile_manager().reoptimize_profile(profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail=f"Profile with ID {profile_id} not found")
        return ProfileResponse.from_profile(profile, active_policy_version=_policy_version())
    except HTTPException:
        raise
    except StaleWriteError:
        raise
    except ProfileLocked:
        raise
    except Exception as exc:
        logger.error(f"Failed to reoptimize profile {profile_id}: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/profiles/{profile_id}/session/prepare",
    response_model=ApiResponse[dict[str, Any]],
    dependencies=[Depends(require_desktop_token)],
    operation_id="prepare_profile_session",
    summary="Prepare an isolated embedded browser session, dedicated proxy listener, and CDP fingerprint payload.",
)
async def prepare_profile_session(
    profile_id: str,
    request: SessionPrepareRequest | None = Body(default=None),
):
    """Prepare the profile's proxy tunnel and return its CDP isolation descriptor to Electron."""
    options = request or SessionPrepareRequest()
    try:
        payload = await get_profile_manager().prepare_session(
            profile_id,
            allow_ip_drift=options.allow_ip_drift,
            include_proxy_secrets=True,
        )
        return ApiResponse(success=True, message="Profile session ready", data=payload)
    except ExitIpDriftError as e:
        raise ApiError(409, str(e), "exit_ip_drift", details=[e.to_detail()]) from e
    except NetworkLaunchError as e:
        raise ApiError(e.status, str(e), e.code) from e
    except ProfileLocked as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Failed to prepare session for profile {profile_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/profiles/{profile_id}/session/release",
    response_model=ApiResponse[dict[str, Any]],
    dependencies=[Depends(require_desktop_token)],
    operation_id="release_profile_session",
    summary="Release an active embedded browser session, its dedicated proxy runtime, and its lease.",
)
async def release_profile_session(profile_id: str):
    """Stop the profile's proxy runtime and release its SQLite lease."""
    try:
        result = await get_profile_manager().release_session(profile_id)
        return ApiResponse(success=True, message=result["message"], data=result)
    except Exception as e:
        logger.error(f"Failed to release session for profile {profile_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/profiles/{profile_id}/launch",
    response_model=ProfileLaunchResponse,
    operation_id="launch_profile",
    summary="Launch a browser.",
    description="Launch an isolated browser window with the profile's pinned configuration.",
)
async def launch_profile(
    profile_id: str,
    request: ProfileLaunchRequest | None = Body(default=None),
):
    """Launch a browser with a profile."""
    options = request or ProfileLaunchRequest()
    try:
        browser_session = await get_profile_manager().launch_browser(
            profile_id,
            headless=options.headless,
            window_size=options.window_size,
            allow_ip_drift=options.allow_ip_drift,
            **(options.additional_options or {}),
        )
        return ProfileLaunchResponse(
            profile_id=profile_id,
            browser_session_id=str(uuid.uuid4()),
            status=browser_session.get("status", "launched"),
            message=browser_session.get("message", "Browser launched successfully"),
            process_id=browser_session.get("process_id"),
            camoufox_options={
                "process_id": browser_session.get("process_id"),
                "status": browser_session.get("status"),
                "options": browser_session.get("camoufox_options", {}),
            },
        )
    except ExitIpDriftError as e:
        raise ApiError(409, str(e), "exit_ip_drift", details=[e.to_detail()]) from e
    except NetworkLaunchError as e:
        raise ApiError(e.status, str(e), e.code) from e
    except ProfileLocked as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Failed to launch browser for profile {profile_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/profiles/{profile_id}/terminal",
    response_model=ApiResponse[dict[str, Any]],
    operation_id="open_profile_terminal",
    summary="Open an isolated native terminal routed through a running profile's proxy tunnel.",
)
async def open_profile_terminal(profile_id: str):
    """Spawn a same-tunnel isolated OS terminal for a running bound profile."""
    try:
        result = await open_isolated_terminal(get_profile_manager(), profile_id)
        return ApiResponse(success=True, message="Isolated terminal launched", data=result)
    except TerminalLaunchError as exc:
        raise ApiError(exc.status, str(exc), exc.code) from None


@router.post(
    "/profiles/{profile_id}/probe-risk-domains",
    response_model=ApiResponse[dict[str, Any]],
    operation_id="probe_profile_risk_domains",
    summary="Probe representative risk and verification domains through a profile's bound proxy tunnel.",
)
async def probe_profile_risk_domains(profile_id: str):
    """Run live reachability checks for risk & KYC domains via the profile's bound node."""
    manager = get_profile_manager()
    profile = await manager.get_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"Profile with ID {profile_id} not found")
    binding = NetworkBindings(manager).get(profile_id)
    if binding is None:
        raise ApiError(
            409,
            "Bind a proxy node to this profile before probing risk domains.",
            "no_bound_node",
        )
    try:
        report = await manager.network_probe.probe_risk_domains(
            binding["node_id"], profile_id=profile_id
        )
    except NetworkNotFound as exc:
        raise ApiError(404, str(exc)) from None
    except NetworkSecretsUnavailable as exc:
        raise ApiError(409, str(exc), "secrets_unavailable") from None
    except MihomoUnavailable as exc:
        raise ApiError(503, str(exc), exc.code) from None
    except NetworkLaunchError as exc:
        raise ApiError(exc.status, str(exc), exc.code) from None
    return ApiResponse(success=True, message="Risk domain probe completed", data=report)


@router.post(
    "/profiles/{profile_id}/clone",
    response_model=ProfileResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="clone_profile",
    summary="Clone a profile.",
    description="Create a copy of a profile with a new deduplicated fingerprint.",
)
async def clone_profile(profile_id: str, request: ProfileCloneRequest):
    """Clone a profile."""
    try:
        cloned_profile = await get_profile_manager().clone_profile(
            profile_id, request.new_name, regenerate_fingerprint=request.regenerate_fingerprint
        )
        if not cloned_profile:
            raise HTTPException(status_code=404, detail=f"Profile with ID {profile_id} not found")
        return ProfileResponse.from_profile(cloned_profile, active_policy_version=_policy_version())
    except HTTPException:
        raise
    except ProfileLocked:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Failed to clone profile {profile_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/profiles/{profile_id}/check-proxy",
    response_model=ProxyCheckResponse,
    operation_id="check_profile_proxy",
    summary="Check a profile's proxy.",
)
async def check_profile_proxy(profile_id: str):
    """Check a saved profile's proxy against its settings."""
    profile = await get_profile_manager().get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail=f"Profile with ID {profile_id} not found")
    result = await proxy_check.check(profile.proxy, profile.browser_settings)
    stored = proxy_check.record(result)
    manager = get_profile_manager()
    current = await manager.get_profile(profile_id)
    if current and current.proxy == profile.proxy:
        await manager.storage.set_proxy_check(profile_id, stored)
        return ProxyCheckResponse.from_result(result, checked_at=stored.checked_at)
    return ProxyCheckResponse.from_result(result)


@router.post(
    "/proxy/check",
    response_model=ProxyCheckResponse,
    operation_id="check_proxy",
    summary="Check an unsaved proxy.",
)
async def check_proxy(request: ProxyCheckRequest):
    """Check an unsaved proxy against unsaved settings."""
    try:
        proxy = ProxyConfig(**request.proxy_config) if request.proxy_config else None
        settings = BrowserSettings(**(request.browser_settings or {}))
    except ValidationError as e:
        raise ApiError(422, validation_message(e.errors()), details=e.errors()) from e
    result = await proxy_check.check(proxy, settings)
    return ProxyCheckResponse.from_result(result)


@router.get(
    "/profiles/{profile_id}/stats",
    response_model=ProfileStatsResponse,
    operation_id="get_profile_stats",
    summary="Get profile statistics.",
)
async def get_profile_stats(profile_id: str):
    """Get usage statistics for a profile."""
    try:
        profile_manager = get_profile_manager()
        profile = await profile_manager.get_profile(profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail=f"Profile with ID {profile_id} not found")
        stats = await profile_manager.get_profile_stats(profile_id)
        return ProfileStatsResponse(
            profile_id=profile_id,
            total_sessions=stats.get("total_sessions", 0),
            total_duration_minutes=stats.get("total_duration_minutes", 0),
            last_session=stats.get("last_session"),
            success_rate=stats.get("success_rate", 0.0),
            actions=stats.get("actions", []),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get statistics for profile {profile_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/profiles/{profile_id}/refresh-browser",
    response_model=ProfileResponse,
    operation_id="refresh_browser_version",
    summary="Refresh the pinned browser version.",
)
async def refresh_browser_version(profile_id: str):
    """Update only the browser-version part of a profile's pinned fingerprint."""
    try:
        profile = await get_profile_manager().refresh_browser_version(profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail=f"Profile with ID {profile_id} not found")
        return ProfileResponse.from_profile(profile, active_policy_version=_policy_version())
    except HTTPException:
        raise
    except StaleWriteError:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(f"Failed to refresh browser version for {profile_id}: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/profiles/{profile_id}/reconcile-os",
    response_model=ProfileResponse,
    operation_id="reconcile_profile_os",
    summary="Reconcile the OS setting with the pinned machine.",
)
async def reconcile_profile_os(profile_id: str, request: ReconcileOsRequest):
    """Bring a profile's OS setting and its pinned machine back into agreement."""
    try:
        profile = await get_profile_manager().reconcile_os(profile_id, request.keep_machine)
        if not profile:
            raise HTTPException(status_code=404, detail=f"Profile with ID {profile_id} not found")
        return ProfileResponse.from_profile(profile, active_policy_version=_policy_version())
    except HTTPException:
        raise
    except StaleWriteError:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(f"Failed to reconcile OS for {profile_id}: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/profiles/{profile_id}/export",
    operation_id="export_profile",
    summary="Export a profile.",
    response_class=FileResponse,
    responses={200: {"content": {"application/zip": {}}, "description": "The profile archive"}},
)
async def export_profile(profile_id: str):
    """Stream a profile archive."""
    profile_manager = get_profile_manager()
    profile = await profile_manager.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail=f"Profile with ID {profile_id} not found")

    handle = tempfile.NamedTemporaryFile(suffix=".camoufox.zip", delete=False)
    handle.close()
    destination = Path(handle.name)

    try:
        await profile_manager.export_profile(profile_id, destination)
    except (ValueError, ProfileLocked) as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        destination.unlink(missing_ok=True)
        logger.error(f"Failed to export profile {profile_id}: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", profile.name).strip("-") or profile_id
    return FileResponse(
        destination,
        media_type="application/zip",
        filename=f"{safe_name}.camoufox.zip",
        headers={"Cache-Control": "no-store"},
        background=BackgroundTask(destination.unlink, missing_ok=True),
    )


@router.post(
    "/profiles/{profile_id}/reset-fingerprint",
    response_model=ProfileResponse,
    operation_id="reset_profile_fingerprint",
    summary="Reset the fingerprint.",
    description="Allocate and immediately pin a fresh deduplicated real-device fingerprint.",
)
async def reset_profile_fingerprint(profile_id: str):
    """Reset and regenerate a profile's fingerprint."""
    try:
        updated_profile = await get_profile_manager().rotate_profile_fingerprint(profile_id)
        if not updated_profile:
            raise HTTPException(status_code=404, detail=f"Profile with ID {profile_id} not found")
        return ProfileResponse.from_profile(
            updated_profile, active_policy_version=_policy_version()
        )
    except HTTPException:
        raise
    except StaleWriteError:
        raise
    except Exception as e:
        logger.error(f"Failed to reset fingerprint for profile {profile_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/profiles/{profile_id}/close",
    response_model=BrowserCloseResponse,
    operation_id="close_profile_browser",
    summary="Close a profile's browser.",
)
async def close_profile_browser(profile_id: str):
    """Close the browser for a profile."""
    try:
        return await get_profile_manager().close_browser(profile_id)
    except Exception as e:
        logger.error(f"Failed to close browser for profile {profile_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get(
    "/browsers/active",
    response_model=ActiveBrowsersResponse,
    operation_id="get_active_browsers",
    summary="List active browsers.",
)
async def get_active_browsers():
    """List active browsers."""
    try:
        active_browsers = await get_profile_manager().get_active_browsers()
        return {"active_browsers": active_browsers, "count": len(active_browsers)}
    except Exception as e:
        logger.error(f"Failed to list active browsers: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/browsers/close-all",
    response_model=BrowsersCloseAllResponse,
    operation_id="close_all_browsers",
    summary="Close all browsers.",
)
async def close_all_browsers():
    """Close all active browsers."""
    try:
        return await get_profile_manager().close_all_browsers()
    except Exception as e:
        logger.error(f"Failed to close all browsers: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e
