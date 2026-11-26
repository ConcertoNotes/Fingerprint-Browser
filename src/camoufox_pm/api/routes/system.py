"""API routes for system status, storage diagnostics, and preset catalog."""

import time
from pathlib import Path

import psutil
from fastapi import APIRouter, HTTPException
from loguru import logger

from camoufox_pm import __version__
from camoufox_pm.api.dependencies import get_profile_manager
from camoufox_pm.api.models.system import (
    ApiResponse,
    DevicePreset,
    PresetListData,
    ProfileCleanupResponse,
    ProfileDiagnosticResponse,
    SystemConfigData,
    SystemInfoData,
    SystemStatusResponse,
)
from camoufox_pm.config import get_settings
from camoufox_pm.core.browser_install import installed_browser
from camoufox_pm.core.cleanup import ProfileCleanupManager
from camoufox_pm.core.cloud.entitlement import evaluate_entitlement
from camoufox_pm.core.isolation import host_os
from camoufox_pm.core.mihomo_manager import MihomoUnavailable

router = APIRouter()

startup_time = time.time()


def _cleanup_manager() -> ProfileCleanupManager:
    db_path = Path(get_settings().db_path)
    return ProfileCleanupManager(str(db_path.parent), str(db_path))


@router.get(
    "/system/status",
    response_model=SystemStatusResponse,
    operation_id="get_system_status",
    summary="Get the system status.",
    description="Real-time overview of profiles, running browsers, system resources, proxy routing engine, and active cloud policy.",
)
async def get_system_status():
    """Return a real-time overview of the system and runtime state."""
    try:
        profile_manager = get_profile_manager()
        profiles = await profile_manager.list_profiles()
        active_profiles = len([p for p in profiles if p.status == "active"])
        active_browsers = await profile_manager.get_active_browsers()
        groups = await profile_manager.list_groups()

        vmem = psutil.virtual_memory()
        cpu_pct = float(psutil.cpu_percent(interval=None))
        mem_used_mb = round(vmem.used / (1024 * 1024), 1)
        mem_total_mb = round(vmem.total / (1024 * 1024), 1)
        mem_pct = float(vmem.percent)
        load_avg = float(psutil.getloadavg()[0]) if hasattr(psutil, "getloadavg") else 0.0
        disk_pct = float(psutil.disk_usage("/").percent)

        mihomo_available = False
        mihomo_version: str | None = None
        try:
            binary = profile_manager.network.binary()
            mihomo_available = True
            mihomo_version = binary.version
        except MihomoUnavailable:
            pass

        policy, policy_source = profile_manager.active_policy()
        browser_ready, _ = installed_browser()
        current_settings = get_settings()
        ent = evaluate_entitlement(profile_manager.storage.db._connection)

        return SystemStatusResponse(
            total_profiles=len(profiles),
            active_profiles=active_profiles,
            running_browsers=len(active_browsers),
            total_groups=len(groups),
            cpu_percent=cpu_pct,
            memory_used_mb=mem_used_mb,
            memory_total_mb=mem_total_mb,
            memory_percent=mem_pct,
            mihomo_available=mihomo_available,
            mihomo_version=mihomo_version,
            cloud_policy_source=policy_source,
            cloud_policy_version=policy.policy_version,
            browser_installed=browser_ready,
            uptime_seconds=int(time.time() - startup_time),
            client_edition=current_settings.client_edition,
            brand_name=current_settings.brand_name,
            require_activation=bool(current_settings.require_activation),
            entitlement_status=ent.status,
            entitlement_tenant_name=ent.tenant_name,
            entitlement_expires_at=ent.expires_at,
            entitlement_days_remaining=ent.days_remaining,
            system_load=load_avg,
            memory_usage=mem_pct,
            disk_usage=disk_pct,
        )
    except Exception as exc:
        logger.error(f"Failed to get system status: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/system/profiles/diagnostic",
    response_model=ProfileDiagnosticResponse,
    operation_id="diagnose_profiles",
    summary="Diagnose profile storage.",
)
async def diagnostic_profiles():
    """Diagnose profile storage health."""
    try:
        cleanup_manager = _cleanup_manager()
        await cleanup_manager.initialize()
        try:
            result = await cleanup_manager.full_diagnostic()
            return ProfileDiagnosticResponse(
                total_profiles_in_db=result["total_profiles_in_db"],
                total_directories_on_disk=result["total_directories_on_disk"],
                total_disk_size_mb=result["total_disk_size_mb"],
                orphaned_directories=result["orphaned_directories"],
                orphaned_size_mb=result["orphaned_size_mb"],
                missing_directories=result["missing_directories"],
                healthy_profiles=result["healthy_profiles"],
                issues_found=result["issues_found"],
            )
        finally:
            await cleanup_manager.close()
    except Exception as exc:
        logger.error(f"Failed to diagnose profiles: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/system/profiles/cleanup",
    response_model=ProfileCleanupResponse,
    operation_id="cleanup_orphaned_profiles",
    summary="Clean up orphaned profiles.",
)
async def cleanup_orphaned_profiles(dry_run: bool = False):
    """Clean up orphaned profile directories."""
    try:
        cleanup_manager = _cleanup_manager()
        await cleanup_manager.initialize()
        try:
            if dry_run:
                orphaned = await cleanup_manager.find_orphaned_profile_directories()
                missing = await cleanup_manager.find_missing_profile_directories()
                return ProfileCleanupResponse(
                    orphaned_removed=len(orphaned),
                    directories_created=len(missing),
                    freed_space_mb=sum(item["size_mb"] for item in orphaned),
                    dry_run=True,
                    message=f"Dry run: would remove {len(orphaned)} directories, create {len(missing)}",
                )
            results = await cleanup_manager.auto_cleanup(dry_run=False)
            freed_space = results["orphaned_removed"] * 30
            return ProfileCleanupResponse(
                orphaned_removed=results["orphaned_removed"],
                directories_created=results["directories_created"],
                freed_space_mb=freed_space,
                dry_run=False,
                message=(
                    f"Cleanup complete: removed {results['orphaned_removed']}, "
                    f"created {results['directories_created']}"
                ),
            )
        finally:
            await cleanup_manager.close()
    except Exception as exc:
        logger.error(f"Failed to clean up profiles: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/system/restart",
    response_model=ApiResponse[None],
    operation_id="restart_system",
    summary="Prepare for a restart.",
)
async def restart_system():
    """Close all browsers in preparation for a restart."""
    try:
        await get_profile_manager().close_all_browsers()
        return ApiResponse(
            success=True, message="Browsers closed; restart the process to continue", data=None
        )
    except Exception as exc:
        logger.error(f"Failed to restart system: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/fingerprints/presets",
    response_model=ApiResponse[PresetListData],
    operation_id="list_fingerprint_presets",
    summary="List real device presets.",
)
async def list_fingerprint_presets(os: str | None = None):
    """List quality-filtered real device fingerprint presets."""
    presets = await get_profile_manager().list_fingerprint_presets(os)
    return ApiResponse(
        success=True,
        message=f"{len(presets)} presets available",
        data=PresetListData(
            presets=[DevicePreset(**preset) for preset in presets], total=len(presets)
        ),
    )


@router.get(
    "/system/config",
    response_model=ApiResponse[SystemConfigData],
    operation_id="get_system_config",
    summary="Report the effective configuration.",
)
async def get_system_config():
    """Return effective configuration without exposing secrets."""
    settings = get_settings()
    return ApiResponse(
        success=True,
        message="Effective configuration",
        data=SystemConfigData(
            version=__version__,
            host=settings.host,
            port=settings.port,
            database_path=str(Path(settings.db_path).resolve()),
            api_key_set=bool(settings.api_key),
            encryption_enabled=bool(settings.secret_key),
            cors_origins=settings.cors_origins,
            camoufox_available=installed_browser()[0],
            uptime_seconds=int(time.time() - startup_time),
            host_os=host_os(),
        ),
    )


@router.get(
    "/system/info",
    response_model=ApiResponse[SystemInfoData],
    operation_id="get_system_info",
    summary="Get basic API information.",
)
async def get_system_info():
    """Return basic information about the API."""
    return ApiResponse(
        success=True,
        message="棱镜浏览器 API",
        data=SystemInfoData(
            name="camoufox-profile-manager",
            version=__version__,
            description="Prism Browser API",
            uptime_seconds=int(time.time() - startup_time),
        ),
    )
