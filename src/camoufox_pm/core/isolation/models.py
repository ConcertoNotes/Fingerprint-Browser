"""Profile isolation data models, stable fingerprint hashing, and UI summaries."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from .chrome_presets import (
    DEFAULT_CHROME_MAJOR,
    browser_major,
    is_outdated,
    normalize_pinned_fingerprint,
    pin_cores,
    pin_gpu,
    pin_platform,
    pin_screen,
    pin_user_agent,
    pinned_os,
)
from .policy import BUILTIN_POLICY, IsolationPolicy


class ProfileIsolation(BaseModel):
    """Per-profile pinned hardware identity and strict isolation settings."""

    isolation_version: int = 1
    policy_version: str = BUILTIN_POLICY.policy_version
    tier: Literal["strict", "standard"] = "strict"
    os: Literal["macos", "windows", "linux"] = "macos"
    preset_id: str | None = None
    webrtc_policy: Literal["block", "spoof_exit"] = "block"
    canvas_policy: Literal["stable_hardware", "session_noise"] = "stable_hardware"
    locale_mode: Literal["fixed", "follow_exit"] = "fixed"
    languages: list[str] = Field(default_factory=lambda: ["en-US", "en"])
    timezone_mode: Literal["fixed", "follow_exit"] = "fixed"
    timezone: str | None = "America/Los_Angeles"
    install_webrtc_guard: bool = True
    window_size: tuple[int, int] = (1366, 860)
    pinned_fingerprint: dict[str, Any] = Field(default_factory=dict)
    fingerprint_hash: str = ""


def compute_fingerprint_hash(pin: dict[str, Any] | None) -> str:
    """Compute a deterministic short identity code ``FP-XXXXXXXX`` for a pinned fingerprint.

    Supports both native Chromium snake_case pins and legacy dotted-key pins.
    """
    if not pin:
        return ""
    w, h = pin_screen(pin)
    vendor, renderer = pin_gpu(pin)
    fonts = pin.get("fonts")
    normalized_fonts = sorted(str(item) for item in fonts) if isinstance(fonts, list) else []
    basis = {
        "userAgent": pin_user_agent(pin) or "",
        "platform": pin_platform(pin) or "",
        "screen_w": w,
        "screen_h": h,
        "cores": pin_cores(pin),
        "renderer": renderer or "",
        "vendor": vendor or "",
        "fonts": normalized_fonts,
    }
    encoded = json.dumps(basis, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:8].upper()
    return f"FP-{digest}"


def compute_hardware_signature(data: dict[str, Any] | None) -> str:
    """Hardware-only signature (excluding fonts/seeds) for device-pool deduplication.

    Accepts a Chrome flat preset, a pinned fingerprint dict, or a legacy nested preset dict.
    """
    if not data:
        return ""
    if "navigator" in data and isinstance(data["navigator"], dict):
        nav: dict[str, Any] = data["navigator"]
        raw_screen = data.get("screen")
        screen: dict[str, Any] = raw_screen if isinstance(raw_screen, dict) else {}
        raw_webgl = data.get("webgl")
        webgl: dict[str, Any] = raw_webgl if isinstance(raw_webgl, dict) else {}
        basis = {
            "platform": nav.get("platform") or "",
            "screen_w": screen.get("width"),
            "screen_h": screen.get("height"),
            "cores": nav.get("hardwareConcurrency"),
            "renderer": webgl.get("unmaskedRenderer") or "",
            "vendor": webgl.get("unmaskedVendor") or "",
        }
    else:
        w, h = pin_screen(data)
        vendor, renderer = pin_gpu(data)
        basis = {
            "platform": pin_platform(data) or "",
            "screen_w": w,
            "screen_h": h,
            "cores": pin_cores(data),
            "renderer": renderer or "",
            "vendor": vendor or "",
        }
    encoded = json.dumps(basis, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]


def compute_preset_hash(preset: dict[str, Any]) -> str:
    """Compute a deterministic ``FP-XXXXXXXX`` preview hash from a preset dict."""
    if "navigator" in preset and isinstance(preset["navigator"], dict):
        nav: dict[str, Any] = preset["navigator"]
        raw_screen = preset.get("screen")
        screen: dict[str, Any] = raw_screen if isinstance(raw_screen, dict) else {}
        raw_webgl = preset.get("webgl")
        webgl: dict[str, Any] = raw_webgl if isinstance(raw_webgl, dict) else {}
        pin_like = {
            "user_agent": nav.get("userAgent") or "",
            "platform": nav.get("platform") or "",
            "screen_width": screen.get("width"),
            "screen_height": screen.get("height"),
            "hardware_concurrency": nav.get("hardwareConcurrency"),
            "webgl_renderer": webgl.get("unmaskedRenderer") or "",
            "webgl_vendor": webgl.get("unmaskedVendor") or "",
            "fonts": preset.get("fonts") or [],
        }
        return compute_fingerprint_hash(pin_like)
    return compute_fingerprint_hash(preset)


def parse_isolation(value: ProfileIsolation | dict[str, Any] | None) -> ProfileIsolation | None:
    """Coerce a dict or ``ProfileIsolation`` into a validated ``ProfileIsolation``."""
    if value is None:
        return None
    if isinstance(value, ProfileIsolation):
        return value
    return ProfileIsolation.model_validate(value)


def summarize_isolation(
    iso: ProfileIsolation | dict[str, Any] | None,
    active_policy_version: str | None = None,
) -> dict[str, Any] | None:
    """Build a compact UI-friendly summary of a profile's isolation identity."""
    model = parse_isolation(iso)
    if model is None:
        return None

    pin = model.pinned_fingerprint or {}
    width, height = pin_screen(pin)
    vendor, renderer = pin_gpu(pin)
    fonts = pin.get("fonts")
    pinned_os_name = pinned_os(pin)
    fp_hash = model.fingerprint_hash or compute_fingerprint_hash(pin)

    return {
        "isolation_version": model.isolation_version,
        "policy_version": model.policy_version,
        "active_policy_version": active_policy_version,
        "policy_outdated": bool(
            active_policy_version and model.policy_version != active_policy_version
        ),
        "tier": model.tier,
        "os": model.os,
        "preset_id": model.preset_id,
        "webrtc_policy": model.webrtc_policy,
        "canvas_policy": model.canvas_policy,
        "locale_mode": model.locale_mode,
        "languages": list(model.languages),
        "timezone_mode": model.timezone_mode,
        "timezone": model.timezone,
        "install_webrtc_guard": model.install_webrtc_guard,
        "window_size": [model.window_size[0], model.window_size[1]],
        "fingerprint_hash": fp_hash,
        "pinned": bool(pin),
        "screen": f"{width}x{height}" if width and height else None,
        "gpu": renderer,
        "vendor": vendor,
        "hardware_concurrency": pin_cores(pin),
        "platform": pin_platform(pin),
        "user_agent": pin_user_agent(pin),
        "font_count": len(fonts) if isinstance(fonts, list) else None,
        "property_count": len(pin),
        "browser_major": browser_major(pin),
        "installed_major": DEFAULT_CHROME_MAJOR,
        "browser_outdated": is_outdated(pin, DEFAULT_CHROME_MAJOR),
        "pinned_os": pinned_os_name,
        "os_mismatch": bool(pinned_os_name and pinned_os_name != model.os),
    }


def isolation_from_legacy_row(
    browser_settings: dict[str, Any] | None,
    fingerprint: dict[str, Any] | None,
    policy: IsolationPolicy = BUILTIN_POLICY,
) -> ProfileIsolation:
    """Convert a pre-v8 profile record into ``ProfileIsolation`` without altering its hardware."""
    bs = browser_settings or {}
    raw_pin = dict(fingerprint) if isinstance(fingerprint, dict) else {}

    os_candidate = pinned_os(raw_pin) or str(bs.get("os") or "windows").lower()
    if os_candidate not in {"macos", "windows", "linux"}:
        os_candidate = "windows"

    pin = (
        normalize_pinned_fingerprint(
            raw_pin,
            os_hint=os_candidate,
            chrome_major=policy.recommended_chrome_major or DEFAULT_CHROME_MAJOR,
        )
        if raw_pin
        else {}
    )

    webrtc_raw = str(bs.get("webrtc_mode") or "none").lower()
    webrtc_policy: Literal["block", "spoof_exit"] = (
        "block" if webrtc_raw in {"none", "block"} else "spoof_exit"
    )
    canvas_policy: Literal["stable_hardware", "session_noise"] = (
        "stable_hardware" if bs.get("stable_canvas", True) else "session_noise"
    )
    languages = bs.get("languages")
    if not isinstance(languages, list) or not languages:
        languages = list(policy.default_languages)

    tz = bs.get("timezone")
    if isinstance(tz, str) and tz.strip():
        tz_mode: Literal["fixed", "follow_exit"] = "fixed"
        tz_val: str | None = tz.strip()
    else:
        tz_mode = policy.default_timezone_mode
        tz_val = policy.default_timezone if tz_mode == "fixed" else None

    w = bs.get("window_width") or 1366
    h = bs.get("window_height") or 860
    window_size = (
        int(w) if isinstance(w, int) and w >= 640 else 1366,
        int(h) if isinstance(h, int) and h >= 480 else 860,
    )

    return ProfileIsolation(
        isolation_version=1,
        policy_version=policy.policy_version,
        tier=policy.default_tier,
        os=os_candidate,  # type: ignore[arg-type]
        preset_id=None,
        webrtc_policy=webrtc_policy,
        canvas_policy=canvas_policy,
        locale_mode=policy.default_locale_mode,
        languages=[str(item) for item in languages],
        timezone_mode=tz_mode,
        timezone=tz_val,
        install_webrtc_guard=policy.install_webrtc_guard,
        window_size=window_size,
        pinned_fingerprint=pin,
        fingerprint_hash=compute_fingerprint_hash(pin) if pin else "",
    )
