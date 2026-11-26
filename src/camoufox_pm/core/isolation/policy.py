"""Fixed-optimization isolation policy model, builtin baseline, and security validator.

Policies may be shipped built-in or delivered by a trusted cloud configuration
server into SQLite ``cloud_cache``. Every policy passes through
:func:`validate_policy` before taking effect so a misconfigured or compromised
remote payload can never weaken proxy isolation, inject extensions, or enable
telemetry/DoH/ECH/OCSP bypasses.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import sqlite3
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class PolicyValidationError(ValueError):
    """Raised when an isolation policy fails schema or security validation."""


class PresetFilters(BaseModel):
    """Minimum quality gates for selecting real device presets."""

    model_config = ConfigDict(extra="forbid")

    min_width: int = Field(default=1280, ge=800, le=7680)
    min_height: int = Field(default=720, ge=600, le=4320)
    min_cores: int = Field(default=4, ge=1, le=64)
    min_chrome_major: int = Field(default=125, ge=100, le=999)
    min_firefox_major: int = Field(default=128, ge=100, le=999)
    blocked_renderers: list[str] = Field(
        default_factory=lambda: [
            "SwiftShader",
            "llvmpipe",
            "Microsoft Basic Render Driver",
            "Software Adapter",
            "Apple Software Renderer",
            "VMware",
            "VirtualBox",
            "Parallels",
        ]
    )


# Explicit security allowlist of Firefox preferences a policy is allowed to set.
# Anything outside this set (e.g. xpinstall.*, extensions.*, proxy target/autoconfig
# overrides, or unsafe security.* toggles) is rejected outright.
ALLOWED_HARDENING_PREF_KEYS: frozenset[str] = frozenset(
    {
        "network.proxy.no_proxies_on",
        "network.proxy.socks_remote_dns",
        "network.proxy.failover_direct",
        "network.proxy.share_proxy_settings",
        "network.trr.mode",
        "network.trr.uri",
        "network.trr.bootstrapAddress",
        "network.trr.excluded-domains",
        "network.dns.disableIPv6",
        "network.dns.disablePrefetch",
        "network.dns.echconfig.enabled",
        "network.dns.http3_echconfig.enabled",
        "network.predictor.enabled",
        "network.predictor.enable-prefetch",
        "network.prefetch-next",
        "network.http.speculative-parallel-limit",
        "network.http.http3.enable",
        "network.captive-portal-service.enabled",
        "network.connectivity-service.enabled",
        "security.OCSP.enabled",
        "security.OCSP.require",
        "media.peerconnection.enabled",
        "media.peerconnection.ice.default_address_only",
        "media.peerconnection.ice.no_host",
        "media.peerconnection.ice.proxy_only",
        "datareporting.healthreport.uploadEnabled",
        "datareporting.policy.dataSubmissionEnabled",
        "toolkit.telemetry.enabled",
        "toolkit.telemetry.unified",
        "toolkit.telemetry.archive.enabled",
        "breakpad.reportURL",
        "browser.tabs.crashReporting.sendReport",
        "identity.fxaccounts.enabled",
        "browser.safebrowsing.malware.enabled",
        "browser.safebrowsing.phishing.enabled",
        "browser.safebrowsing.downloads.enabled",
        "dom.battery.enabled",
        "beacon.enabled",
        "geo.enabled",
        "privacy.resistFingerprinting.reduceTimerPrecision.microseconds",
        "privacy.trackingprotection.enabled",
    }
)

# Security-critical preferences whose values may NEVER be weakened by any policy.
LOCKED_PREF_VALUES: dict[str, bool | int | str] = {
    "network.proxy.no_proxies_on": "",
    "network.proxy.socks_remote_dns": True,
    "network.proxy.failover_direct": False,
    "network.proxy.share_proxy_settings": True,
    "network.trr.mode": 5,
    "network.trr.uri": "",
    "network.trr.bootstrapAddress": "",
    "network.trr.excluded-domains": "",
    "network.dns.disableIPv6": True,
    "network.dns.disablePrefetch": True,
    "network.dns.echconfig.enabled": False,
    "network.dns.http3_echconfig.enabled": False,
    "network.http.http3.enable": False,
    "security.OCSP.enabled": 0,
    "security.OCSP.require": False,
    "dom.battery.enabled": False,
    "media.peerconnection.ice.default_address_only": True,
    "media.peerconnection.ice.no_host": True,
    "media.peerconnection.ice.proxy_only": True,
}

_FORBIDDEN_TOP_KEYS = frozenset(
    {
        "addons",
        "extensions",
        "executable_path",
        "user_data_dir",
        "proxy",
        "env",
        "scripts",
        "firefox_binary",
    }
)
_LANG_TAG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}$")
_DOMAIN_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_UNSAFE_VALUE_TOKENS = ("file://", "javascript:", "data:", "..\\", "../")


def default_hardening_prefs() -> dict[str, bool | int | str]:
    """Return a fresh copy of the baseline anti-leak Firefox preferences."""
    return {
        "network.proxy.no_proxies_on": "",
        "network.proxy.socks_remote_dns": True,
        "network.proxy.failover_direct": False,
        "network.proxy.share_proxy_settings": True,
        "network.trr.mode": 5,
        "network.trr.uri": "",
        "network.trr.bootstrapAddress": "",
        "network.trr.excluded-domains": "",
        "network.dns.disableIPv6": True,
        "network.dns.disablePrefetch": True,
        "network.dns.echconfig.enabled": False,
        "network.dns.http3_echconfig.enabled": False,
        "network.predictor.enabled": False,
        "network.predictor.enable-prefetch": False,
        "network.prefetch-next": False,
        "network.http.speculative-parallel-limit": 0,
        "network.http.http3.enable": False,
        "network.captive-portal-service.enabled": False,
        "network.connectivity-service.enabled": False,
        "security.OCSP.enabled": 0,
        "security.OCSP.require": False,
        "dom.battery.enabled": False,
        "media.peerconnection.ice.default_address_only": True,
        "media.peerconnection.ice.no_host": True,
        "media.peerconnection.ice.proxy_only": True,
        "datareporting.healthreport.uploadEnabled": False,
        "datareporting.policy.dataSubmissionEnabled": False,
        "toolkit.telemetry.enabled": False,
        "toolkit.telemetry.unified": False,
        "toolkit.telemetry.archive.enabled": False,
        "breakpad.reportURL": "",
        "browser.tabs.crashReporting.sendReport": False,
        "identity.fxaccounts.enabled": False,
        "browser.safebrowsing.malware.enabled": False,
        "browser.safebrowsing.phishing.enabled": False,
        "browser.safebrowsing.downloads.enabled": False,
    }


def default_risk_probe_groups() -> dict[str, list[str]]:
    """Return the baseline risk & verification domain suite covered by the fail-closed tunnel."""
    return {
        "core": [
            "claude.ai",
            "anthropic.com",
            "claude.com",
            "claudeusercontent.com",
            "claude.app",
            "claudemcpcontent.com",
            "clau.de",
            "claudemcpclient.com",
            "claude.dev",
            "claude.site",
            "claudeacademy.com",
            "claude.new",
            "ant.dev",
            "anthropic.team",
            "antspace.dev",
            "chatbotclaude.com",
            "claudepages.dev",
            "claudestudio.com",
            "servd-anthropic-website.b-cdn.net",
            "anthropic.auth0.com",
            "anthropic-com.ghost.io",
            "anthropic.com.cdn.cloudflare.net",
        ],
        "antifraud_telemetry": [
            "intercom.io",
            "intercomcdn.com",
            "intercomassets.com",
            "intercom-messenger.com",
            "siftscience.com",
            "us5.datadoghq.com",
            "browser-intake-us5-datadoghq.com",
            "r.stripe.com",
            "us.sentry.io",
            "fcmtoken.googleapis.com",
            "statsigapi.net",
            "cdn.usefathom.com",
        ],
        "challenge_kyc": [
            "challenges.cloudflare.com",
            "hcaptcha.com",
            "withpersona.com",
            "yoti.com",
        ],
    }


def default_risk_probe_cidrs() -> list[str]:
    """Return the baseline IP CIDRs enforced through the zero-DIRECT fail-closed tunnel."""
    return [
        "34.8.0.0/16",
        "160.79.104.0/21",
        "153.61.208.0/20",
        "153.61.224.0/22",
    ]


def default_risk_probe_targets() -> list[dict[str, str]]:
    """Return the representative live-probe targets across all 3 risk categories."""
    return [
        {"domain": "claude.ai", "category": "core"},
        {"domain": "anthropic.auth0.com", "category": "core"},
        {"domain": "challenges.cloudflare.com", "category": "challenge_kyc"},
        {"domain": "hcaptcha.com", "category": "challenge_kyc"},
        {"domain": "r.stripe.com", "category": "antifraud_telemetry"},
        {"domain": "siftscience.com", "category": "antifraud_telemetry"},
        {"domain": "intercom.io", "category": "antifraud_telemetry"},
        {"domain": "withpersona.com", "category": "challenge_kyc"},
        {"domain": "us5.datadoghq.com", "category": "antifraud_telemetry"},
        {"domain": "us.sentry.io", "category": "antifraud_telemetry"},
    ]


class IsolationPolicy(BaseModel):
    """Fixed-optimization isolation policy governing all newly created and launched browsers."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    policy_version: str = Field(default="builtin-2026.11.26-r3", min_length=1, max_length=64)
    default_tier: Literal["strict", "standard"] = "strict"
    default_webrtc_policy: Literal["block", "spoof_exit"] = "block"
    default_canvas_policy: Literal["stable_hardware", "session_noise"] = "stable_hardware"
    default_locale_mode: Literal["fixed", "follow_exit"] = "fixed"
    default_languages: list[str] = Field(default_factory=lambda: ["en-US", "en"])
    default_timezone_mode: Literal["fixed", "follow_exit"] = "fixed"
    default_timezone: str = "America/Los_Angeles"
    install_webrtc_guard: bool = True
    hardening_prefs: dict[str, bool | int | str] = Field(default_factory=default_hardening_prefs)
    preset_filters: PresetFilters = Field(default_factory=PresetFilters)
    curated_presets: list[dict[str, Any]] = Field(default_factory=list)
    recommended_chrome_major: int | None = None
    recommended_camoufox_major: int | None = None
    risk_probe_groups: dict[str, list[str]] = Field(default_factory=default_risk_probe_groups)
    risk_probe_cidrs: list[str] = Field(default_factory=default_risk_probe_cidrs)
    risk_probe_targets: list[dict[str, str]] = Field(default_factory=default_risk_probe_targets)

    def total_enforced_domains(self) -> int:
        return len(
            {domain.lower() for group in self.risk_probe_groups.values() for domain in group}
        )


BUILTIN_POLICY = IsolationPolicy()


def _validate_curated_preset(entry: Any, index: int) -> None:
    if not isinstance(entry, dict):
        raise PolicyValidationError(f"curated_presets[{index}] must be an object")
    forbidden = _FORBIDDEN_TOP_KEYS & set(entry)
    if forbidden:
        raise PolicyValidationError(
            f"curated_presets[{index}] contains forbidden keys: {sorted(forbidden)}"
        )
    os_name = entry.get("os")
    if os_name not in {"windows", "macos", "linux"}:
        raise PolicyValidationError(f"curated_presets[{index}].os must be windows, macos, or linux")

    # Support both native Chromium flat presets and legacy nested navigator/screen/webgl presets.
    if "screen_width" in entry or "webgl_renderer" in entry:
        if not isinstance(entry.get("screen_width"), int) or not isinstance(
            entry.get("screen_height"), int
        ):
            raise PolicyValidationError(
                f"curated_presets[{index}] missing numeric screen_width/screen_height"
            )
        if not isinstance(entry.get("webgl_renderer"), str) or not entry["webgl_renderer"].strip():
            raise PolicyValidationError(f"curated_presets[{index}] missing webgl_renderer")
        if "user_agent" in entry and not isinstance(entry.get("user_agent"), str):
            raise PolicyValidationError(f"curated_presets[{index}].user_agent must be a string")
        return

    nav = entry.get("navigator")
    screen = entry.get("screen")
    webgl = entry.get("webgl")
    if not isinstance(nav, dict) or not isinstance(nav.get("userAgent"), str):
        raise PolicyValidationError(f"curated_presets[{index}] missing navigator.userAgent")
    if not isinstance(screen, dict) or not isinstance(screen.get("width"), int):
        raise PolicyValidationError(f"curated_presets[{index}] missing numeric screen.width")
    if not isinstance(screen.get("height"), int):
        raise PolicyValidationError(f"curated_presets[{index}] missing numeric screen.height")
    if not isinstance(webgl, dict) or not isinstance(webgl.get("unmaskedRenderer"), str):
        raise PolicyValidationError(f"curated_presets[{index}] missing webgl.unmaskedRenderer")


def _validate_domain_name(domain: Any, context: str) -> str:
    if not isinstance(domain, str):
        raise PolicyValidationError(f"{context} must be a hostname string")
    cleaned = domain.strip().lower()
    if not cleaned or len(cleaned) > 253 or not _DOMAIN_RE.fullmatch(cleaned):
        raise PolicyValidationError(f"Invalid domain in {context}: {domain!r}")
    return cleaned


def _validate_risk_suite(policy: IsolationPolicy) -> None:
    if len(policy.risk_probe_groups) > 20:
        raise PolicyValidationError("risk_probe_groups cannot exceed 20 categories")
    total_domains = 0
    for category, domains in policy.risk_probe_groups.items():
        if not isinstance(category, str) or not category.strip() or len(category) > 64:
            raise PolicyValidationError(f"Invalid risk_probe_groups category: {category!r}")
        for domain in domains:
            _validate_domain_name(domain, f"risk_probe_groups[{category!r}]")
            total_domains += 1
    if total_domains > 250:
        raise PolicyValidationError("risk_probe_groups cannot exceed 250 total domains")

    if len(policy.risk_probe_cidrs) > 64:
        raise PolicyValidationError("risk_probe_cidrs cannot exceed 64 entries")
    for cidr in policy.risk_probe_cidrs:
        if not isinstance(cidr, str):
            raise PolicyValidationError(f"Invalid CIDR entry: {cidr!r}")
        try:
            ipaddress.ip_network(cidr.strip(), strict=False)
        except ValueError as exc:
            raise PolicyValidationError(f"Invalid CIDR in risk_probe_cidrs: {cidr!r}") from exc

    if len(policy.risk_probe_targets) > 50:
        raise PolicyValidationError("risk_probe_targets cannot exceed 50 entries")
    for idx, target in enumerate(policy.risk_probe_targets):
        if not isinstance(target, dict):
            raise PolicyValidationError(f"risk_probe_targets[{idx}] must be an object")
        _validate_domain_name(target.get("domain"), f"risk_probe_targets[{idx}].domain")
        cat = target.get("category")
        if not isinstance(cat, str) or not cat.strip() or len(cat) > 64:
            raise PolicyValidationError(f"Invalid risk_probe_targets[{idx}].category: {cat!r}")


def validate_policy(raw: dict[str, Any] | IsolationPolicy) -> IsolationPolicy:
    """Validate an isolation policy against schema and strict security invariants."""
    if isinstance(raw, IsolationPolicy):
        payload: dict[str, Any] = raw.model_dump()
    elif isinstance(raw, dict):
        payload = raw
    else:
        raise PolicyValidationError("Isolation policy payload must be a JSON object")

    forbidden = _FORBIDDEN_TOP_KEYS & set(payload)
    if forbidden:
        raise PolicyValidationError(f"Policy contains forbidden keys: {sorted(forbidden)}")

    try:
        policy = IsolationPolicy.model_validate(payload)
    except ValidationError as exc:
        raise PolicyValidationError(f"Invalid isolation policy schema: {exc}") from exc

    if policy.schema_version != 1:
        raise PolicyValidationError(f"Unsupported policy schema_version: {policy.schema_version}")

    if not policy.default_languages or len(policy.default_languages) > 10:
        raise PolicyValidationError("default_languages must contain 1 to 10 language tags")
    for tag in policy.default_languages:
        if not isinstance(tag, str) or not _LANG_TAG.fullmatch(tag):
            raise PolicyValidationError(f"Invalid language tag in policy: {tag!r}")

    try:
        ZoneInfo(policy.default_timezone)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise PolicyValidationError(
            f"Invalid default_timezone in policy: {policy.default_timezone!r}"
        ) from exc

    # Security whitelist for Firefox preferences.
    for key, value in policy.hardening_prefs.items():
        if key not in ALLOWED_HARDENING_PREF_KEYS:
            raise PolicyValidationError(f"Disallowed Firefox preference in policy: {key!r}")
        if not isinstance(value, (bool, int, str)):
            raise PolicyValidationError(f"Unsupported value type for preference {key!r}")
        if isinstance(value, str):
            if len(value) > 256 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
                raise PolicyValidationError(f"Invalid string value for preference {key!r}")
            lower = value.lower()
            if any(token in lower for token in _UNSAFE_VALUE_TOKENS):
                raise PolicyValidationError(f"Unsafe URI or path token in preference {key!r}")
        if key in LOCKED_PREF_VALUES:
            expected = LOCKED_PREF_VALUES[key]
            if type(value) is not type(expected) or value != expected:
                raise PolicyValidationError(
                    f"Security-critical preference {key!r} cannot be weakened to {value!r}"
                )

    if len(policy.curated_presets) > 500:
        raise PolicyValidationError("curated_presets cannot exceed 500 entries")
    for idx, entry in enumerate(policy.curated_presets):
        _validate_curated_preset(entry, idx)

    _validate_risk_suite(policy)

    # Ensure mandatory hardening defaults are always present even if a cloud
    # policy only specified a subset of keys.
    merged_prefs = {**default_hardening_prefs(), **policy.hardening_prefs}
    return policy.model_copy(update={"hardening_prefs": merged_prefs})


def load_active_policy(connection: sqlite3.Connection | None) -> tuple[IsolationPolicy, str]:
    """Load the active isolation policy from ``cloud_cache``, falling back to ``BUILTIN_POLICY``.

    Returns ``(policy, source)`` where ``source`` is ``"cloud"`` or ``"builtin"``.
    """
    if connection is None:
        return BUILTIN_POLICY, "builtin"
    try:
        row = connection.execute(
            "SELECT version, sha256, payload FROM cloud_cache WHERE kind = 'isolation_policy'"
        ).fetchone()
    except sqlite3.Error:
        return BUILTIN_POLICY, "builtin"

    if row is None:
        return BUILTIN_POLICY, "builtin"

    payload_text = row["payload"] if isinstance(row, sqlite3.Row) else row[2]
    stored_sha256 = row["sha256"] if isinstance(row, sqlite3.Row) else row[1]

    if not isinstance(payload_text, str) or not payload_text.strip():
        return BUILTIN_POLICY, "builtin"

    if stored_sha256:
        actual_sha256 = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
        if actual_sha256 != str(stored_sha256).lower():
            logger.warning("Cached cloud isolation policy failed SHA-256 check; using builtin")
            return BUILTIN_POLICY, "builtin"

    try:
        parsed = json.loads(payload_text)
        if isinstance(parsed, dict) and "policy" in parsed and "hardening_prefs" not in parsed:
            parsed = parsed["policy"]
        policy = validate_policy(parsed)
        return policy, "cloud"
    except (ValueError, TypeError, PolicyValidationError) as exc:
        logger.warning(f"Cached cloud isolation policy is invalid ({exc}); using builtin")
        return BUILTIN_POLICY, "builtin"


def summarize_policy(policy: IsolationPolicy, source: str) -> dict[str, Any]:
    """Return a clean UI summary of the active isolation policy."""
    return {
        "source": source,
        "schema_version": policy.schema_version,
        "policy_version": policy.policy_version,
        "default_tier": policy.default_tier,
        "default_webrtc_policy": policy.default_webrtc_policy,
        "default_canvas_policy": policy.default_canvas_policy,
        "default_locale_mode": policy.default_locale_mode,
        "default_languages": list(policy.default_languages),
        "default_timezone_mode": policy.default_timezone_mode,
        "default_timezone": policy.default_timezone,
        "install_webrtc_guard": policy.install_webrtc_guard,
        "hardening_pref_count": len(policy.hardening_prefs),
        "hardening_prefs": dict(policy.hardening_prefs),
        "preset_filters": policy.preset_filters.model_dump(),
        "curated_preset_count": len(policy.curated_presets),
        "recommended_chrome_major": policy.recommended_chrome_major,
        "recommended_camoufox_major": policy.recommended_camoufox_major,
        "risk_probe_groups": {k: list(v) for k, v in policy.risk_probe_groups.items()},
        "risk_probe_cidrs": list(policy.risk_probe_cidrs),
        "risk_probe_targets": [dict(t) for t in policy.risk_probe_targets],
        "total_enforced_domains": policy.total_enforced_domains(),
        "total_enforced_cidrs": len(policy.risk_probe_cidrs),
    }
