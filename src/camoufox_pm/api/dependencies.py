"""API dependencies (dependency injection and optional API-key guard)."""

import hmac

from fastapi import Header, HTTPException

from camoufox_pm.api.errors import ApiError
from camoufox_pm.config import get_settings
from camoufox_pm.core.database import StorageManager
from camoufox_pm.core.profile_manager import ProfileManager

_storage_manager: StorageManager | None = None
_profile_manager: ProfileManager | None = None


def set_storage_manager(storage_manager: StorageManager) -> None:
    """Register the shared ``StorageManager`` instance."""
    global _storage_manager
    _storage_manager = storage_manager


def set_profile_manager(profile_manager: ProfileManager) -> None:
    """Register the shared ``ProfileManager`` instance."""
    global _profile_manager
    _profile_manager = profile_manager


def get_storage_manager() -> StorageManager:
    """Return the shared ``StorageManager`` instance."""
    if _storage_manager is None:
        raise HTTPException(status_code=500, detail="StorageManager is not initialized")
    return _storage_manager


def get_profile_manager() -> ProfileManager:
    """Return the shared ``ProfileManager`` instance."""
    if _profile_manager is None:
        raise HTTPException(status_code=500, detail="ProfileManager is not initialized")
    return _profile_manager


def _api_key_matches(x_api_key: str | None) -> bool:
    """Whether the presented key equals the configured one, in constant time."""
    configured = get_settings().api_key
    if not configured or x_api_key is None:
        return False
    return hmac.compare_digest(x_api_key, configured)


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Enforce the API key when ``CPM_API_KEY`` is set; a no-op otherwise."""
    if not get_settings().api_key:
        return
    if not _api_key_matches(x_api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


async def require_auth(x_api_key: str | None = Header(default=None)) -> None:
    """Optional ``CPM_API_KEY`` protection; loopback desktop usage is open by default."""
    await require_api_key(x_api_key)


async def require_desktop_token(
    x_prism_desktop_token: str | None = Header(default=None),
) -> None:
    """Protect credential-bearing session endpoints when ``CPM_DESKTOP_TOKEN`` is configured.

    Electron's main process passes a per-launch random hex token in
    ``X-Prism-Desktop-Token`` so neither renderer code nor guest views can read
    loopback proxy credentials.
    """
    configured = get_settings().desktop_token
    if not configured:
        return
    if x_prism_desktop_token is None or not hmac.compare_digest(x_prism_desktop_token, configured):
        raise ApiError(
            403,
            "Invalid or missing desktop session token.",
            "desktop_token_required",
        )
