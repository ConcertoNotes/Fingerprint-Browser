"""System-level API models."""

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """Envelope for action endpoints that do not return a resource."""

    success: bool = Field(True, description="Whether the operation succeeded")
    message: str = Field("", description="Message")
    data: T | None = Field(None, description="Response payload")


class ErrorDetail(BaseModel):
    """The machine-readable part of every error response."""

    code: str = Field(..., description="Stable error code (snake_case)")
    message: str = Field(..., description="Human-readable description")
    details: list[Any] | None = Field(
        None, description="Structured specifics; set for validation errors"
    )


class ErrorResponse(BaseModel):
    """The one shape every non-2xx response uses."""

    error: ErrorDetail
    detail: str = Field(
        ...,
        description="Mirror of error.message for FastAPI-style clients",
        deprecated=True,
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error": {
                    "code": "not_found",
                    "message": "Profile with ID x1 not found",
                    "details": None,
                },
                "detail": "Profile with ID x1 not found",
            }
        }
    )


class HealthResponse(BaseModel):
    """Liveness report; unhealthy instances answer with it under a 503."""

    status: str = Field(..., description="healthy or unhealthy")
    api_version: str
    database: str = Field(..., description="connected or disconnected")
    profiles_count: int | None = Field(
        None, description="Profile count in open local mode; null when API key is configured"
    )


class SystemStatusResponse(BaseModel):
    """Live system and runtime status driving the desktop bottom status bar."""

    total_profiles: int = Field(..., description="Total profiles")
    active_profiles: int = Field(0, description="Active profiles")
    running_browsers: int = Field(..., description="Running browsers")
    total_groups: int = Field(..., description="Total groups")
    cpu_percent: float = Field(..., description="CPU utilization percentage")
    memory_used_mb: float = Field(..., description="Memory used in MB")
    memory_total_mb: float = Field(..., description="Total memory in MB")
    memory_percent: float = Field(..., description="Memory usage percentage")
    mihomo_available: bool = Field(..., description="Whether proxy routing engine is ready")
    mihomo_version: str | None = Field(
        None, description="Proxy routing engine version if available"
    )
    cloud_policy_source: str = Field(..., description="'cloud' or 'builtin'")
    cloud_policy_version: str = Field(..., description="Active isolation policy version")
    browser_installed: bool = Field(
        ..., description="Whether isolation browser kernel is installed"
    )
    uptime_seconds: int = Field(..., description="Server uptime in seconds")
    # Edition & cloud license entitlement fields
    client_edition: str = Field("standard", description="Client edition ('standard' or 'customer')")
    brand_name: str = Field("棱镜浏览器", description="Configured client brand name")
    require_activation: bool = Field(
        False, description="Whether customer edition requires activation before normal use"
    )
    entitlement_status: str = Field("none", description="Cloud license entitlement status")
    entitlement_tenant_name: str | None = Field(
        None, description="Licensed tenant name if activated"
    )
    entitlement_expires_at: str | None = Field(
        None, description="License expiration ISO timestamp if applicable"
    )
    entitlement_days_remaining: int | None = Field(
        None, description="Remaining license days if expiration is set"
    )
    # Legacy compatibility fields
    system_load: float = Field(0.0, description="System load average")
    memory_usage: float = Field(0.0, description="Memory usage in %")
    disk_usage: float = Field(0.0, description="Disk usage in %")


class SystemInfoData(BaseModel):
    """Payload of the system info endpoint."""

    name: str
    version: str
    description: str
    uptime_seconds: int


class SystemConfigData(BaseModel):
    """Effective configuration; secrets are reported only as set/unset."""

    version: str
    host: str
    port: int
    database_path: str
    api_key_set: bool
    encryption_enabled: bool
    cors_origins: list[str]
    camoufox_available: bool
    uptime_seconds: int
    host_os: str = "macos"


class DevicePreset(BaseModel):
    """A quality-filtered real device fingerprint preset."""

    id: str = Field(..., description="Preset id, '<os>:<index>' or 'cloud:<os>:<index>'")
    os: str
    source: str = "camoufox"
    screen: str | None = None
    hardware_concurrency: int | None = None
    gpu: str | None = None
    vendor: str | None = None
    user_agent: str | None = None
    fingerprint_hash: str | None = None
    in_use: bool = False


class PresetListData(BaseModel):
    """Payload of the preset catalogue endpoint."""

    presets: list[DevicePreset]
    total: int


class ProfileDiagnosticResponse(BaseModel):
    """Result of a profile storage diagnostic."""

    total_profiles_in_db: int
    total_directories_on_disk: int
    total_disk_size_mb: float
    orphaned_directories: int
    orphaned_size_mb: float
    missing_directories: int
    healthy_profiles: int
    issues_found: int


class ProfileCleanupResponse(BaseModel):
    """Result of a profile storage cleanup."""

    orphaned_removed: int
    directories_created: int
    freed_space_mb: float
    dry_run: bool
    message: str
