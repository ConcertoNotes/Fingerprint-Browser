"""First-run isolation browser kernel status and initialization endpoints."""

from fastapi import APIRouter

from camoufox_pm.api.models.system import ApiResponse
from camoufox_pm.core.browser_install import BrowserStatus, browser_installer

router = APIRouter()


@router.get(
    "/system/browser",
    response_model=ApiResponse[BrowserStatus],
    operation_id="get_browser_status",
    summary="Get isolation browser kernel installation status.",
)
async def browser_status():
    return ApiResponse(data=browser_installer.status())


@router.post(
    "/system/browser/install",
    response_model=ApiResponse[BrowserStatus],
    operation_id="install_browser",
    summary="Initialize or download the isolation browser kernel.",
)
async def install_browser():
    return ApiResponse(data=browser_installer.start())
