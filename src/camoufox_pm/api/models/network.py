"""Request bodies for /api/v1/network. Responses use the ApiResponse envelope."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubscriptionCreateRequest(_Strict):
    name: str | None = Field(None, max_length=80)
    url: str | None = Field(None, max_length=4096, description="HTTPS Clash/Mihomo subscription")
    content: str | None = Field(
        None, max_length=10 * 1024 * 1024, description="Clash/Mihomo YAML text of a local file"
    )
    file_name: str | None = Field(None, max_length=128)

    @model_validator(mode="after")
    def _one_source(self) -> "SubscriptionCreateRequest":
        if (self.url is None) == (self.content is None):
            raise ValueError("Provide either url or content")
        return self


class SubscriptionUpdateRequest(_Strict):
    name: str = Field(..., min_length=1, max_length=80)
    row_version: int = Field(..., ge=0)


class SubscriptionSyncRequest(_Strict):
    content: str | None = Field(None, max_length=10 * 1024 * 1024)


class NodeCheckRequest(_Strict):
    include_intel: bool = True
    refresh_intel: bool = False


class BatchCheckRequest(_Strict):
    node_ids: list[str] = Field(..., min_length=1, max_length=200)


class BindingRequest(_Strict):
    node_id: str = Field(..., min_length=1, max_length=64)
    binding_row_version: int | None = Field(
        None, ge=0, description="Version of the current binding; null when none exists"
    )
    profile_row_version: int | None = Field(None, ge=0)


class CheckSiteRequest(_Strict):
    site: Literal["ippure", "ipip", "ipinfo", "scamalytics"]


class NetworkSettingsUpdate(_Strict):
    mihomo_path: str | None = Field(None, max_length=1024)
    probe_concurrency: int | None = Field(None, ge=1, le=32)
    probe_timeout_seconds: int | None = Field(None, ge=3, le=60)
    intel_cache_hours: int | None = Field(None, ge=1, le=720)
    intel_ipinfo_enabled: bool | None = None
    intel_ippure_enabled: bool | None = None
    intel_scamalytics_enabled: bool | None = None
    mask_exit_ips: bool | None = None
    intel_ipinfo_token: str | None = Field(None, max_length=512)
    intel_scamalytics_url: str | None = Field(None, max_length=512)
    intel_scamalytics_key: str | None = Field(None, max_length=512)
