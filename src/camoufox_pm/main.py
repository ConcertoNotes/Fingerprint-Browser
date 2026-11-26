"""棱镜浏览器 (Prism Browser) REST API (derived from Camoufox Profile Manager, MIT)."""

import asyncio
import mimetypes
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from camoufox_pm import __version__
from camoufox_pm.api.dependencies import (
    require_auth,
    set_profile_manager,
    set_storage_manager,
)
from camoufox_pm.api.errors import install_error_handlers
from camoufox_pm.api.middleware.logging import LoggingMiddleware
from camoufox_pm.api.models.system import ErrorResponse, HealthResponse
from camoufox_pm.api.routes import (
    cloud,
    cookies,
    groups,
    network,
    profiles,
    runtime,
    system,
)
from camoufox_pm.config import Settings, get_settings
from camoufox_pm.core.browser_install import browser_installer
from camoufox_pm.core.database import StorageManager
from camoufox_pm.core.profile_manager import ProfileManager

settings = get_settings()

CLOUD_HEARTBEAT_INTERVAL_SECONDS = 15 * 60


def _should_background_sync(status: object, *, is_startup: bool) -> bool:
    """Return whether startup/heartbeat sync should contact the cloud server."""
    configured = bool(getattr(status, "configured", False))
    if not configured:
        return False
    has_token = bool(getattr(status, "has_token", False))
    if get_settings().client_edition == "customer":
        return has_token
    auto_sync = bool(getattr(status, "auto_sync_on_startup", False))
    if is_startup:
        return auto_sync
    return has_token or auto_sync


async def _startup_cloud_sync(profile_manager: ProfileManager) -> None:
    """Silently sync cloud policy and subscriptions on startup if enabled."""
    try:
        from camoufox_pm.core.cloud import CloudDeliveryService

        service = CloudDeliveryService(profile_manager)
        status = service.get_status()
        if _should_background_sync(status, is_startup=True):
            await service.sync_all()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - startup must never fail when offline
        logger.warning(f"Startup cloud sync skipped: {exc}")


async def _cloud_sync_heartbeat_loop(
    profile_manager: ProfileManager,
    *,
    interval_seconds: float = CLOUD_HEARTBEAT_INTERVAL_SECONDS,
) -> None:
    """Run startup cloud sync followed by a periodic silent manifest/entitlement heartbeat."""
    await _startup_cloud_sync(profile_manager)
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            from camoufox_pm.core.cloud import CloudDeliveryService

            service = CloudDeliveryService(profile_manager)
            status = service.get_status()
            if _should_background_sync(status, is_startup=False):
                await service.sync_all()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - heartbeat must never stop on transient outage
            logger.warning(f"Periodic cloud heartbeat skipped: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize shared resources on startup and clean up on shutdown."""
    logger.info("Starting Prism Browser API...")
    current_settings = get_settings()
    storage_manager = StorageManager(current_settings.db_path)
    profile_manager: ProfileManager | None = None
    cloud_sync_task: asyncio.Task[None] | None = None
    try:
        await storage_manager.initialize()
        data_dir = str(Path(current_settings.db_path).parent)
        # Bootstrap bundled offline runtime (proxy engine + GeoIP synchronously,
        # paired isolation browser archive asynchronously in background).
        browser_installer.ensure_bundled_ready_async(Path(data_dir))
        profile_manager = ProfileManager(storage_manager, data_dir)
        await profile_manager.initialize()
        set_storage_manager(storage_manager)
        set_profile_manager(profile_manager)
        cloud_sync_task = asyncio.create_task(
            _cloud_sync_heartbeat_loop(profile_manager), name="cloud-sync-heartbeat"
        )
        logger.info("API ready")
        yield
    finally:
        logger.info("Shutting down API...")
        try:
            if cloud_sync_task is not None:
                cloud_sync_task.cancel()
                with suppress(asyncio.CancelledError):
                    await cloud_sync_task
            if profile_manager is not None:
                await profile_manager.browser_sessions.stop_heartbeat()
                try:
                    await profile_manager.browser_sessions.close_all()
                except Exception as exc:
                    logger.warning(f"Failed to close browsers on shutdown: {exc}")
                await profile_manager.network_probe.shutdown()
                await profile_manager.network.shutdown()
                await profile_manager.release_all_leases()
        finally:
            await storage_manager.close()


def _webui_dir(app_settings: Settings | None = None) -> Path | None:
    """Locate the bundled static web UI, if present."""
    effective = app_settings or get_settings()
    candidates = [
        effective.webui_dir,
        str(Path(__file__).parent / "webui"),
        "web/out",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return None


def create_app(app_settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application, hiding API docs in customer edition."""
    effective = app_settings or get_settings()
    is_customer = effective.client_edition == "customer"
    instance = FastAPI(
        title=f"{effective.brand_name} API",
        description=(
            "Local desktop antidetect isolation browser manager. "
            "Manage strictly isolated browser windows, world-map dedicated proxy bindings, "
            "fixed-optimization hardware fingerprints, and cloud-delivered policies over a clean REST API."
        ),
        version=__version__,
        docs_url=None if is_customer else "/docs",
        redoc_url=None if is_customer else "/redoc",
        openapi_url=None if is_customer else "/openapi.json",
        lifespan=lifespan,
        responses={"4XX": {"model": ErrorResponse}, "5XX": {"model": ErrorResponse}},
    )
    install_error_handlers(instance)

    instance.add_middleware(
        CORSMiddleware,
        allow_origins=effective.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    instance.add_middleware(LoggingMiddleware)

    protected = [Depends(require_auth)]
    for _prefix, _in_schema in (("/api/v1", True), ("/api", False)):
        for _router, _tag in (
            (profiles.router, "Profiles"),
            (network.router, "Network"),
            (cloud.router, "Cloud"),
            (groups.router, "Groups"),
            (cookies.router, "Cookies"),
            (runtime.router, "Runtime"),
            (system.router, "System"),
        ):
            instance.include_router(
                _router,
                prefix=_prefix,
                tags=[_tag],
                dependencies=protected,
                include_in_schema=_in_schema,
            )

    @instance.get(
        "/health",
        tags=["System"],
        response_model=HealthResponse,
        operation_id="health_check",
        summary="Report API and database health.",
    )
    async def health_check():
        """Report API and database health."""
        from camoufox_pm.api.dependencies import get_profile_manager

        try:
            profile_mgr = get_profile_manager()
            profiles_list = await profile_mgr.list_profiles()
            return HealthResponse(
                status="healthy",
                api_version=__version__,
                database="connected",
                profiles_count=None if get_settings().api_key else len(profiles_list),
            )
        except Exception:
            return JSONResponse(
                status_code=503,
                content=HealthResponse(
                    status="unhealthy",
                    api_version=__version__,
                    database="disconnected",
                    profiles_count=0,
                ).model_dump(),
            )

    webui = _webui_dir(effective)
    mimetypes.add_type("text/javascript", ".mjs")
    if webui is not None:
        instance.mount("/", StaticFiles(directory=str(webui), html=True), name="webui")
        logger.info(f"Serving web UI from {webui}")
    else:

        @instance.get("/", include_in_schema=False)
        async def root():
            """Redirect to API docs in standard mode, or return minimal status in customer mode."""
            if is_customer:
                return {"status": "ok", "service": effective.brand_name}
            return RedirectResponse(url="/docs")

    return instance


app = create_app(settings)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "camoufox_pm.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
        log_level="info",
    )
