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


class ManualNode(_Strict):
    name: str | None = Field(None, max_length=128)
    type: Literal["http", "https", "socks5"] = "http"
    server: str = Field(..., min_length=1, max_length=253)
    port: int = Field(..., ge=1, le=65535)
    username: str | None = Field(None, max_length=255)
    password: str | None = Field(None, max_length=255)


class ManualNodesRequest(_Strict):
    """Add fixed nodes to a manual group: one form entry, or pasted lines/links/YAML."""

    group_id: str | None = Field(None, max_length=64)
    group_name: str | None = Field(None, max_length=80)
    node: ManualNode | None = None
    text: str | None = Field(None, max_length=1024 * 1024)
    default_type: Literal["http", "https", "socks5"] = "http"

    @model_validator(mode="after")
    def _one_source(self) -> "ManualNodesRequest":
        if (self.node is None) == (self.text is None):
            raise ValueError("Provide either node or text")
        if self.group_id is None and not (self.group_name or "").strip():
            raise ValueError("Choose a group or name a new one")
        return self


class NodeUpdateRequest(_Strict):
    name: str | None = Field(None, min_length=1, max_length=128)
    server: str | None = Field(None, min_length=1, max_length=253)
    port: int | None = Field(None, ge=1, le=65535)
    username: str | None = Field(None, max_length=255)
    password: str | None = Field(None, max_length=255)


class NodeCheckRequest(_Strict):
    include_intel: bool = True
    refresh_intel: bool = False


class BatchCheckRequest(_Strict):
    node_ids: list[str] = Field(..., min_length=1, max_length=500)


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
