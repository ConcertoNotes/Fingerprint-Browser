"""Visual Chinese admin panel router mounted at ``/admin``."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

ADMIN_HTML_PATH = Path(__file__).resolve().parent / "static" / "admin.html"

ADMIN_SECURITY_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    ),
}


def load_admin_html() -> str:
    """Read the self-contained Chinese admin HTML page from package resources."""
    return ADMIN_HTML_PATH.read_text(encoding="utf-8")


def create_admin_ui_router() -> APIRouter:
    """Create the FastAPI router serving the standalone ``/admin`` control panel."""
    router = APIRouter(include_in_schema=False)

    @router.get("/admin", response_class=HTMLResponse)
    @router.get("/admin/", response_class=HTMLResponse)
    async def admin_panel() -> HTMLResponse:
        return HTMLResponse(
            content=load_admin_html(),
            headers=ADMIN_SECURITY_HEADERS,
        )

    return router
