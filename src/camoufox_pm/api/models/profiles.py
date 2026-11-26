"""API models for profile endpoints."""

from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from camoufox_pm.core.models import Profile, ProfileStatus, ProfileTags, ProxyCheckRecord
from camoufox_pm.core.proxy_check import Level, ProxyCheckResult


class IsolationInput(BaseModel):
    """Optional isolation overrides when creating or updating a profile."""

    os: Literal["macos", "windows", "linux"] | None = None
    tier: Literal["strict", "standard"] | None = None
    preset_id: str | None = None
    webrtc_policy: Literal["block", "spoof_exit"] | None = None
    canvas_policy: Literal["stable_hardware", "session_noise"] | None = None
    locale_mode: Literal["fixed", "follow_exit"] | None = None
    languages: list[str] | None = None
    timezone_mode: Literal["fixed", "follow_exit"] | None = None
    timezone: str | None = None
    install_webrtc_guard: bool | None = None
    window_size: tuple[int, int] | None = None


class ProfileCreateRequest(BaseModel):
    """Request body for creating a profile."""

    name: str = Field(..., min_length=1, max_length=255, description="Profile name")
    group: str | None = Field(None, description="Profile group ID")
    browser_settings: dict[str, Any] | None = Field(
        None, description="Legacy browser settings (os, screen, languages, etc.)"
    )
    proxy_config: dict[str, Any] | None = Field(None, description="Optional manual proxy config")
    node_id: str | None = Field(
        None, description="Optional network node ID to bind atomically upon creation"
    )
    isolation: IsolationInput | None = Field(
        None, description="Fixed-optimization isolation preferences"
    )
    notes: str | None = Field(None, max_length=1000, description="Notes")
    tags: ProfileTags = Field(default_factory=list)
    generate_fingerprint: bool = Field(True, description="Generate and pin a hardware fingerprint")
    fingerprint_preset: str | None = Field(
        None,
        description="Pin the profile to a specific real device preset ID.",
    )
    ip_drift_guard: bool = Field(
        True,
        description="Block launch when the bound node's exit IP or country changes.",
    )


class ProfileUpdateRequest(BaseModel):
    """Request body for updating a profile."""

    name: str | None = Field(None, min_length=1, max_length=255)
    group: str | None = Field(None)
    status: ProfileStatus | None = Field(None)
    browser_settings: dict[str, Any] | None = Field(None)
    isolation: IsolationInput | None = Field(None)
    proxy_config: dict[str, Any] | None = Field(None)
    ip_drift_guard: bool | None = Field(None, description="Enable or disable exit IP drift guard.")
    notes: str | None = Field(None, max_length=1000)
    tags: ProfileTags | None = None
    row_version: int | None = Field(
        None,
        description="Optimistic concurrency counter returned when the profile was last read.",
    )

    # Deprecated flattened browser fields kept for backward API compatibility.
    browser_os: str | None = Field(None, deprecated=True)
    browser_screen: str | None = Field(None, deprecated=True)
    browser_user_agent: str | None = Field(None, deprecated=True)
    browser_languages: list[str] | None = Field(None, deprecated=True)
    browser_timezone: str | None = Field(None, deprecated=True)
    browser_locale: str | None = Field(None, deprecated=True)
    browser_webrtc_mode: str | None = Field(None, deprecated=True)
    browser_canvas_noise: bool | None = Field(None, deprecated=True)
    browser_stable_canvas: bool | None = Field(None, deprecated=True)
    browser_webgl_noise: bool | None = Field(None, deprecated=True)
    browser_audio_noise: bool | None = Field(None, deprecated=True)
    browser_hardware_concurrency: int | None = Field(None, ge=1, le=32, deprecated=True)
    browser_device_memory: int | None = Field(None, ge=1, le=128, deprecated=True)
    browser_max_touch_points: int | None = Field(None, ge=0, le=10, deprecated=True)
    browser_window_width: int | None = Field(None, ge=800, le=3840, deprecated=True)
    browser_window_height: int | None = Field(None, ge=600, le=2160, deprecated=True)


class ProfileResponse(BaseModel):
    """Profile data returned by the API."""

    id: str
    name: str
    tags: list[str] = Field(default_factory=list)
    group: str | None
    status: ProfileStatus
    browser_settings: dict[str, Any]
    proxy_config: dict[str, Any] | None
    storage_path: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime
    last_used: datetime | None
    fingerprint: dict[str, Any] | None = None
    isolation: dict[str, Any] | None = None
    isolation_summary: dict[str, Any] | None = None
    fingerprint_hash: str | None = None
    ip_drift_guard: bool = True
    last_exit_ip: str | None = None
    last_exit_country: str | None = None
    last_exit_at: datetime | None = None
    cloud_revision: int = 0
    cloud_synced_at: datetime | None = None
    proxy_check: ProxyCheckRecord | None = None
    row_version: int = 0

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_profile(
        cls, profile: "Profile", *, active_policy_version: str | None = None
    ) -> "ProfileResponse":
        """Build the API view of a profile."""
        from camoufox_pm.core.isolation import (
            parse_isolation,
            summarize_fingerprint,
            summarize_isolation,
        )

        proxy_config = profile.proxy.model_dump() if profile.proxy else None
        iso_model = parse_isolation(profile.isolation_config)
        iso_public: dict[str, Any] | None = None
        if iso_model is not None:
            iso_public = iso_model.model_dump(mode="json")
            pin = iso_public.get("pinned_fingerprint")
            if isinstance(pin, dict):
                iso_public["pinned_fingerprint"] = {
                    k: v for k, v in pin.items() if k not in {"fonts", "voices"}
                }
        iso_summary = summarize_isolation(iso_model, active_policy_version)
        fp_hash = (
            profile.fingerprint_hash or (iso_model.fingerprint_hash if iso_model else None) or None
        )
        return cls(
            id=profile.id,
            name=profile.name,
            group=profile.group,
            tags=profile.tags,
            status=profile.status,
            browser_settings=(
                profile.browser_settings.model_dump() if profile.browser_settings else {}
            ),
            proxy_config=proxy_config,
            storage_path=profile.storage_path,
            notes=profile.notes,
            created_at=profile.created_at,
            updated_at=profile.updated_at,
            last_used=profile.last_used,
            fingerprint=summarize_fingerprint(
                profile.fingerprint,
                profile.browser_settings.os if profile.browser_settings else None,
            ),
            isolation=iso_public,
            isolation_summary=iso_summary,
            fingerprint_hash=fp_hash,
            ip_drift_guard=profile.ip_drift_guard,
            last_exit_ip=profile.last_exit_ip,
            last_exit_country=profile.last_exit_country,
            last_exit_at=profile.last_exit_at,
            cloud_revision=profile.cloud_revision,
            cloud_synced_at=profile.cloud_synced_at,
            proxy_check=profile.proxy_check,
            row_version=profile.row_version,
        )


class ProfileListResponse(BaseModel):
    """Paginated list of profiles."""

    profiles: list[ProfileResponse]
    total: int
    page: int
    per_page: int
    has_next: bool
    has_prev: bool


class ProfileStatsResponse(BaseModel):
    """Usage statistics for a profile."""

    profile_id: str
    total_sessions: int
    total_duration_minutes: int
    last_session: datetime | None
    success_rate: float
    actions: list[dict[str, Any]]


class ProfileCloneRequest(BaseModel):
    """Request body for cloning a profile."""

    new_name: str = Field(..., min_length=1, max_length=255)
    regenerate_fingerprint: bool = Field(
        True, description="Allocate a new deduplicated fingerprint"
    )


class SessionPrepareRequest(BaseModel):
    """Request body for preparing an Electron embedded browser session."""

    allow_ip_drift: bool = Field(
        default=False,
        description="Proceed even if the bound node's exit IP or country changed since last launch.",
    )


class ProfileLaunchRequest(BaseModel):
    """Request body for launching a browser with a profile."""

    headless: bool = Field(default=False, description="Launch in headless mode")
    window_size: str | None = Field(default=None, description="Window size (1920x1080)")
    allow_ip_drift: bool = Field(
        default=False,
        description="Proceed with launch even if the bound node's exit IP or country changed.",
    )
    additional_options: dict[str, Any] | None = Field(
        default=None, description="Additional browser launch options"
    )


class ProfileLaunchResponse(BaseModel):
    """Response returned when a browser is launched."""

    profile_id: str
    browser_session_id: str = Field(..., deprecated=True)
    status: str = Field(..., description="launched or already_running")
    message: str
    process_id: int | None = None
    camoufox_options: dict[str, Any]


class BrowserCloseResponse(BaseModel):
    """Response returned when a browser is asked to close."""

    status: str
    profile_id: str
    message: str


class ActiveBrowserInfo(BaseModel):
    """One running browser."""

    profile_id: str
    process_id: int | None
    started_at: datetime


class ActiveBrowsersResponse(BaseModel):
    """The browsers currently running."""

    active_browsers: list[ActiveBrowserInfo]
    count: int


class BrowsersCloseAllResponse(BaseModel):
    """Response returned when every browser is asked to close."""

    status: str
    closed_count: int
    errors: list[str]
    message: str


class ProxyCheckRequest(BaseModel):
    """Request body for checking an unsaved proxy."""

    proxy_config: dict[str, Any] | None = None
    browser_settings: dict[str, Any] | None = None


class ReconcileOsRequest(BaseModel):
    """Which of the two things a profile says about its OS should win."""

    keep_machine: bool


class ClearGeographyRequest(BaseModel):
    """Profiles whose stored timezone and coordinates should follow the proxy again."""

    profile_ids: list[Annotated[str, StringConstraints(min_length=1)]] = Field(..., min_length=1)


class ClearGeographyResponse(BaseModel):
    """What the request did to each profile it named."""

    cleared: list[str]
    unchanged: list[str]
    not_found: list[str]


class ProxyLocationResponse(BaseModel):
    """Where a proxy actually comes out."""

    ip: str
    country: str | None = None
    city: str | None = None
    timezone: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class ProxyFindingResponse(BaseModel):
    """One finding from a proxy check."""

    level: Level
    field: str
    message: str


class ProxyCheckResponse(BaseModel):
    """Result of reaching the internet through a proxy and comparing it."""

    reachable: bool
    error: str | None = None
    latency_ms: int | None = None
    location: ProxyLocationResponse | None = None
    findings: list[ProxyFindingResponse] = Field(default_factory=list)
    checked_at: datetime | None = None

    @classmethod
    def from_result(
        cls, result: ProxyCheckResult, checked_at: datetime | None = None
    ) -> "ProxyCheckResponse":
        return cls(
            checked_at=checked_at,
            reachable=result.reachable,
            error=result.error,
            latency_ms=result.latency_ms,
            location=(
                ProxyLocationResponse(**asdict(result.location)) if result.location else None
            ),
            findings=[ProxyFindingResponse(**asdict(f)) for f in result.findings],
        )


class TagTarget(BaseModel):
    id: str = Field(min_length=1, max_length=255)
    row_version: int = Field(ge=0)


class BulkTagsRequest(BaseModel):
    profiles: list[TagTarget] = Field(min_length=1, max_length=1000)
    add: ProfileTags = Field(default_factory=list)
    remove: ProfileTags = Field(default_factory=list)


class TagSkipped(BaseModel):
    id: str
    reason: str


class BulkTagsResponse(BaseModel):
    updated: list[ProfileResponse]
    skipped: list[TagSkipped]


class IsolationAuditCheckResponse(BaseModel):
    """One item in an Isolation Audit report."""

    key: str
    title: str
    status: Literal["pass", "warn", "fail"]
    summary: str
    detail: str
    action: str | None = None


class IsolationAuditResponse(BaseModel):
    """Scored Isolation Audit report for one browser profile."""

    profile_id: str
    score: int
    status: Literal["healthy", "warning", "critical"]
    checked_at: str
    checks: list[IsolationAuditCheckResponse]
