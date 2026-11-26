"""Data models for Prism Browser."""

import secrets
import string
import unicodedata
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

# Characters used for short IDs, excluding visually confusing ones (0, o, 1, l, i).
_ID_ALPHABET = "".join(c for c in (string.ascii_lowercase + string.digits) if c not in "0o1li")


def generate_short_id(length: int = 8) -> str:
    """Generate a short, readable random ID."""
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(length))


def generate_profile_id() -> str:
    """Generate a short ID for a profile."""
    return generate_short_id(8)


def generate_group_id() -> str:
    """Generate a short ID for a group."""
    return generate_short_id(8)


class ProfileStatus(str, Enum):
    """Profile status."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    BLOCKED = "blocked"
    MAINTENANCE = "maintenance"


class ProxyType(str, Enum):
    """Proxy protocol."""

    HTTP = "http"
    HTTPS = "https"
    SOCKS4 = "socks4"
    SOCKS5 = "socks5"

    @classmethod
    def _missing_(cls, value):
        return (
            {"socks4a": cls.SOCKS4, "socks5h": cls.SOCKS5}.get(value)
            if isinstance(value, str)
            else None
        )


class WebRTCMode(str, Enum):
    """WebRTC IP spoofing mode."""

    FORWARD = "forward"
    REPLACE = "replace"
    REAL = "real"
    NONE = "none"


class ProxyConfig(BaseModel):
    """Proxy configuration."""

    type: ProxyType
    server: str
    username: str | None = None
    password: str | None = None
    country: str | None = None

    def validate_authentication(self) -> None:
        from .socks_relay import validate_credentials

        if self.type == ProxyType.SOCKS4 and (self.username or self.password):
            raise ValueError(
                "SOCKS4 username/password authentication is unsupported. Use SOCKS5 instead."
            )
        if self.type == ProxyType.SOCKS5 and (self.username or self.password):
            validate_credentials(self.username, self.password)
        if self.password and not self.username:
            raise ValueError("A proxy password requires a username")

    def to_camoufox_format(self) -> dict[str, str]:
        """Convert to the proxy dict Camoufox/Playwright expects."""
        self.validate_authentication()
        config = {"server": f"{self.type.value}://{self.server}"}
        if self.username:
            config["username"] = self.username
        if self.password:
            config["password"] = self.password
        return config


class BrowserSettings(BaseModel):
    """Browser fingerprint and behaviour settings."""

    browser_ui: Literal["desktop", "camoufox"] = "desktop"

    os: str = "windows"
    screen: str = "1920x1080"
    user_agent: str | None = None
    languages: list[str] = Field(default_factory=lambda: ["en-US", "en"])
    timezone: str | None = None
    locale: str | None = None

    window_width: int | None = 1280
    window_height: int | None = 720

    geolocation: dict[str, float] | None = None

    webrtc_mode: WebRTCMode = WebRTCMode.REPLACE
    webrtc_public_ip: str | None = None
    webrtc_local_ips: list[str] | None = None

    hardware_concurrency: int | None = None
    device_memory: int | None = None
    max_touch_points: int = 0

    canvas_noise: bool = True
    webgl_noise: bool = True
    audio_noise: bool = True
    stable_canvas: bool = False

    fonts: list[str] | None = None

    def has_geography(self) -> bool:
        """Whether this profile states where it is instead of following its proxy."""
        return bool(self.timezone or self.geolocation)

    def clear_geography(self) -> bool:
        """Drop the stated location so the proxy's exit address supplies it again."""
        if not self.has_geography():
            return False
        self.timezone = None
        self.geolocation = None
        return True

    def to_camoufox_config(self) -> dict[str, Any]:
        """Build Camoufox ``config`` property overrides."""
        config: dict[str, Any] = {"disableTheming": self.browser_ui == "desktop"}
        if self.browser_ui == "desktop":
            config["showcursor"] = False
        if self.geolocation:
            config["geolocation:latitude"] = self.geolocation["lat"]
            config["geolocation:longitude"] = self.geolocation["lon"]
            if "accuracy" in self.geolocation:
                config["geolocation:accuracy"] = self.geolocation["accuracy"]
        if self.timezone:
            config["timezone"] = self.timezone
        if self.hardware_concurrency:
            config["navigator.hardwareConcurrency"] = self.hardware_concurrency
        if self.max_touch_points:
            config["navigator.maxTouchPoints"] = self.max_touch_points
        if self.webrtc_public_ip:
            config["webrtc:ipv4"] = self.webrtc_public_ip
        return config


Level = Literal["error", "warning", "info"]


class ProxyCheckFinding(BaseModel):
    """One finding from checking a profile's proxy."""

    level: Level
    field: str
    message: str


class ProxyCheckRecord(BaseModel):
    """Last proxy check answer stored on a profile."""

    checked_at: datetime
    reachable: bool
    error: str | None = None
    latency_ms: int | None = None
    ip: str | None = None
    country: str | None = None
    timezone: str | None = None
    findings: list[ProxyCheckFinding] = Field(default_factory=list)


def normalize_tags(values: Any) -> list[str]:
    """Bounded, case-insensitive labels; tags never affect browser identity."""
    if not isinstance(values, list) or len(values) > 100:
        raise ValueError("Tags must be a list with at most 100 inputs")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or any(
            unicodedata.category(char).startswith("C") for char in value
        ):
            raise ValueError("Tags must be strings without control characters")
        tag = " ".join(unicodedata.normalize("NFC", value).casefold().split())
        if not tag or len(tag) > 32 or "," in tag:
            raise ValueError("Each tag must contain 1 to 32 characters without commas")
        if tag not in result:
            result.append(tag)
    if len(result) > 20:
        raise ValueError("A profile can have at most 20 tags")
    return result


ProfileTags = Annotated[list[str], BeforeValidator(normalize_tags)]


class Profile(BaseModel):
    """Browser profile."""

    model_config = ConfigDict(use_enum_values=True)

    id: str = Field(default_factory=generate_profile_id)
    name: str
    group: str | None = None
    tags: ProfileTags = Field(default_factory=list)
    status: ProfileStatus = ProfileStatus.ACTIVE
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    last_used: datetime | None = None
    deleted_at: datetime | None = None

    browser_settings: BrowserSettings = Field(default_factory=BrowserSettings)
    proxy: ProxyConfig | None = None
    extensions: list[str] = Field(default_factory=list)
    storage_path: str | None = None
    notes: str | None = None

    fingerprint: dict[str, Any] | None = None
    isolation_config: dict[str, Any] | None = None
    fingerprint_hash: str | None = None
    ip_drift_guard: bool = True
    last_exit_ip: str | None = None
    last_exit_country: str | None = None
    last_exit_at: datetime | None = None
    cloud_revision: int = 0
    cloud_synced_at: datetime | None = None

    proxy_check: ProxyCheckRecord | None = None
    row_version: int = 0

    def get_storage_path(self, base_path: str = "data/profiles") -> str:
        """Return (and lazily assign) the on-disk path for this profile's data."""
        if not self.storage_path:
            self.storage_path = f"{base_path}/profile_{self.id}"
        return self.storage_path

    def to_camoufox_launch_options(self) -> dict[str, Any]:
        """Build legacy keyword arguments for ``AsyncCamoufox``."""
        bs = self.browser_settings
        options: dict[str, Any] = {
            "os": bs.os,
            "locale": ",".join(bs.languages) if bs.languages else "en-US",
            "config": bs.to_camoufox_config(),
            "user_data_dir": self.get_storage_path(),
            "persistent_context": True,
            "humanize": True,
            "i_know_what_im_doing": True,
            "geoip": not bool(bs.geolocation),
        }
        if bs.webrtc_mode == WebRTCMode.NONE:
            options["block_webrtc"] = True
        if bs.stable_canvas:
            options["firefox_user_prefs"] = {
                "privacy.baselineFingerprintingProtection": False,
            }
        if bs.window_width and bs.window_height:
            options["window"] = (bs.window_width, bs.window_height)
        if bs.fonts:
            options["fonts"] = bs.fonts
        if self.proxy:
            options["proxy"] = self.proxy.to_camoufox_format()
        return options


class ProfileGroup(BaseModel):
    """A group of profiles."""

    id: str = Field(default_factory=generate_group_id)
    name: str
    description: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    profile_count: int = 0


class UsageStats(BaseModel):
    """Profile usage record."""

    id: int | None = None
    profile_id: str
    action: str
    timestamp: datetime = Field(default_factory=datetime.now)
    duration: int | None = None
    success: bool = True
    details: dict[str, Any] | None = None


class ProxyTestResult(BaseModel):
    """Result of testing a proxy."""

    proxy_id: str
    success: bool
    response_time: int | None = None
    error_message: str | None = None
    ip_address: str | None = None
    country: str | None = None
    tested_at: datetime = Field(default_factory=datetime.now)


class SystemStatus(BaseModel):
    """System status snapshot."""

    total_profiles: int
    active_profiles: int = 0
    running_browsers: int
    total_groups: int
    cpu_percent: float = 0.0
    memory_used_mb: float = 0.0
    memory_total_mb: float = 0.0
    memory_percent: float = 0.0
    mihomo_available: bool = False
    mihomo_version: str | None = None
    cloud_policy_source: str = "builtin"
    cloud_policy_version: str = ""
    browser_installed: bool = False
    uptime_seconds: int = 0
