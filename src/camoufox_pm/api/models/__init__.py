"""API models for 噜噜指纹浏览器."""

from .groups import (
    GroupCreateRequest,
    GroupListResponse,
    GroupResponse,
    GroupUpdateRequest,
)
from .profiles import (
    ProfileCloneRequest,
    ProfileCreateRequest,
    ProfileLaunchRequest,
    ProfileLaunchResponse,
    ProfileListResponse,
    ProfileResponse,
    ProfileStatsResponse,
    ProfileUpdateRequest,
)
from .system import (
    ApiResponse,
    ErrorResponse,
    SystemStatusResponse,
)

__all__ = [
    "ProfileCreateRequest",
    "ProfileUpdateRequest",
    "ProfileResponse",
    "ProfileListResponse",
    "ProfileStatsResponse",
    "ProfileCloneRequest",
    "ProfileLaunchRequest",
    "ProfileLaunchResponse",
    "GroupCreateRequest",
    "GroupUpdateRequest",
    "GroupResponse",
    "GroupListResponse",
    "SystemStatusResponse",
    "ApiResponse",
    "ErrorResponse",
]
