"""Isolation allocator, curated Chrome preset filter, and Electron session compiler."""

from __future__ import annotations

import random
import re
import sys
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from .. import geoip
from ..fs_permissions import create_private_directory, protect_path, reject_reparse_paths
from .chrome_presets import (
    DEFAULT_CHROME_MAJOR,
    build_chrome_user_agent,
    build_pinned_fingerprint,
    get_chrome_preset,
    list_chrome_presets,
    normalize_pinned_fingerprint,
    preset_from_curated,
)
from .models import (
    ProfileIsolation,
    compute_fingerprint_hash,
    compute_hardware_signature,
    compute_preset_hash,
)
from .policy import BUILTIN_POLICY, IsolationPolicy

_COUNTRY_LANGUAGES: dict[str, list[str]] = {
    "US": ["en-US", "en"],
    "GB": ["en-GB", "en"],
    "CA": ["en-CA", "en"],
    "AU": ["en-AU", "en"],
    "SG": ["en-SG", "en"],
    "JP": ["ja-JP", "ja", "en-US"],
    "KR": ["ko-KR", "ko", "en-US"],
    "DE": ["de-DE", "de", "en"],
    "FR": ["fr-FR", "fr", "en"],
    "ES": ["es-ES", "es", "en"],
    "IT": ["it-IT", "it", "en"],
    "NL": ["nl-NL", "nl", "en"],
    "BR": ["pt-BR", "pt", "en"],
    "TW": ["zh-TW", "zh", "en"],
    "HK": ["zh-HK", "zh", "en"],
    "CN": ["zh-CN", "zh", "en"],
}

_CHROME_UA_RE = re.compile(r"Chrome/(\d+)")


def host_os() -> Literal["macos", "windows", "linux"]:
    """This computer's OS in profile terms: windows, macos, or linux."""
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


def _target_chrome_major(policy: IsolationPolicy) -> int:
    return policy.recommended_chrome_major or DEFAULT_CHROME_MAJOR


def _preset_passes_filters(preset: dict[str, Any], policy: IsolationPolicy) -> bool:
    filters = policy.preset_filters
    width = preset.get("screen_width")
    height = preset.get("screen_height")
    if not isinstance(width, int) or not isinstance(height, int):
        return False
    if width < filters.min_width or height < filters.min_height:
        return False

    cores = preset.get("hardware_concurrency")
    if isinstance(cores, int) and cores < filters.min_cores:
        return False

    renderer = str(preset.get("webgl_renderer") or "").lower()
    if any(blocked.lower() in renderer for blocked in filters.blocked_renderers):
        return False

    ua = str(preset.get("user_agent") or "")
    match = _CHROME_UA_RE.search(ua)
    if match and int(match.group(1)) < filters.min_chrome_major:
        return False

    return True


def _raw_preset_entries(
    os_name: str | None, policy: IsolationPolicy
) -> list[tuple[str, str, str, dict[str, Any]]]:
    """Return ``(preset_id, os_name, source, canonical_preset)`` tuples across cloud + built-in pools."""
    entries: list[tuple[str, str, str, dict[str, Any]]] = []
    wanted = {os_name.lower()} if os_name else {"macos", "windows", "linux"}
    major = _target_chrome_major(policy)

    for idx, raw_curated in enumerate(policy.curated_presets):
        c_os = str(raw_curated.get("os") or "").lower()
        if c_os not in wanted:
            continue
        pid = str(raw_curated.get("id") or f"cloud:{c_os}:{idx}")
        normalized = preset_from_curated(raw_curated, pid)
        if not normalized.get("user_agent"):
            normalized["user_agent"] = build_chrome_user_agent(c_os, major)
        entries.append((pid, c_os, "cloud", normalized))

    for name in ("macos", "windows", "linux"):
        if name not in wanted:
            continue
        for preset in list_chrome_presets(name):
            preset["user_agent"] = build_chrome_user_agent(name, major)
            preset["chrome_major"] = major
            entries.append((preset["id"], name, "builtin", preset))

    return entries


def _lookup_raw_preset(
    preset_id: str, policy: IsolationPolicy
) -> tuple[str, dict[str, Any]] | None:
    """Resolve a preset ID against curated cloud presets first, then built-in Chrome presets."""
    major = _target_chrome_major(policy)
    for idx, raw_curated in enumerate(policy.curated_presets):
        c_os = str(raw_curated.get("os") or "").lower()
        cid = str(raw_curated.get("id") or f"cloud:{c_os}:{idx}")
        if cid == preset_id and c_os in {"macos", "windows", "linux"}:
            normalized = preset_from_curated(raw_curated, cid)
            normalized["user_agent"] = build_chrome_user_agent(c_os, major)
            return c_os, normalized

    found = get_chrome_preset(preset_id)
    if found is not None:
        os_name = str(found["os"])
        found["user_agent"] = build_chrome_user_agent(os_name, major)
        found["chrome_major"] = major
        return os_name, found
    return None


def list_filtered_presets(
    os_name: str | None = None,
    policy: IsolationPolicy = BUILTIN_POLICY,
    used_hashes: set[str] | None = None,
    *,
    used_preset_ids: set[str] | None = None,
    used_signatures: set[str] | None = None,
) -> list[dict[str, Any]]:
    """List quality-filtered real desktop Chrome device presets annotated with ``in_use``."""
    hashes = used_hashes or set()
    preset_ids = used_preset_ids or set()
    signatures = used_signatures or set()

    results: list[dict[str, Any]] = []
    for preset_id, p_os, source, preset in _raw_preset_entries(os_name, policy):
        if not _preset_passes_filters(preset, policy):
            continue
        fp_hash = compute_preset_hash(preset)
        hw_sig = compute_hardware_signature(preset)
        in_use = (
            preset_id in preset_ids
            or (bool(fp_hash) and fp_hash in hashes)
            or (bool(hw_sig) and hw_sig in signatures)
        )
        results.append(
            {
                "id": preset_id,
                "os": p_os,
                "source": source,
                "screen": f"{preset['screen_width']}x{preset['screen_height']}",
                "hardware_concurrency": preset["hardware_concurrency"],
                "gpu": preset["webgl_renderer"],
                "vendor": preset["webgl_vendor"],
                "user_agent": preset["user_agent"],
                "fingerprint_hash": fp_hash,
                "hardware_signature": hw_sig,
                "in_use": in_use,
            }
        )
    return results


def _default_window_for_pin(pin: dict[str, Any]) -> tuple[int, int]:
    width = pin.get("avail_width") or pin.get("screen_width")
    height = pin.get("avail_height") or pin.get("screen_height")
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        win_w = min(1366, max(1024, width - 40)) if width >= 1064 else width
        win_h = min(860, max(680, height - 80)) if height >= 760 else height
        return (win_w, win_h)
    return (1366, 860)


def allocate_isolation(
    os_choice: str | None = None,
    tier: Literal["strict", "standard"] | None = None,
    existing_hashes: set[str] | None = None,
    policy: IsolationPolicy = BUILTIN_POLICY,
    preset_id: str | None = None,
    *,
    existing_preset_ids: set[str] | None = None,
    existing_signatures: set[str] | None = None,
    exclude_preset_ids: set[str] | None = None,
    webrtc_policy: Literal["block", "spoof_exit"] | None = None,
    canvas_policy: Literal["stable_hardware", "session_noise"] | None = None,
    locale_mode: Literal["fixed", "follow_exit"] | None = None,
    languages: list[str] | None = None,
    timezone_mode: Literal["fixed", "follow_exit"] | None = None,
    timezone: str | None = None,
    install_webrtc_guard: bool | None = None,
    window_size: tuple[int, int] | None = None,
) -> ProfileIsolation:
    """Allocate and immediately pin a deduplicated real-device Chromium ``ProfileIsolation``."""
    used_hashes = set(existing_hashes or ())
    used_presets = set(existing_preset_ids or ())
    used_sigs = set(existing_signatures or ())
    excluded = set(exclude_preset_ids or ())

    chosen_os = (os_choice or host_os()).lower()
    if chosen_os not in {"macos", "windows", "linux"}:
        raise ValueError(f"Unsupported operating system: {os_choice!r}")

    chosen_tier: Literal["strict", "standard"] = tier or policy.default_tier
    chosen_webrtc: Literal["block", "spoof_exit"] = webrtc_policy or policy.default_webrtc_policy
    chosen_canvas: Literal["stable_hardware", "session_noise"] = (
        canvas_policy or policy.default_canvas_policy
    )
    chosen_locale_mode: Literal["fixed", "follow_exit"] = locale_mode or policy.default_locale_mode
    chosen_languages = list(languages) if languages else list(policy.default_languages)
    chosen_tz_mode: Literal["fixed", "follow_exit"] = timezone_mode or policy.default_timezone_mode
    if timezone is not None:
        chosen_tz: str | None = timezone
    elif chosen_tz_mode == "fixed":
        chosen_tz = policy.default_timezone
    else:
        chosen_tz = None
    chosen_guard = (
        policy.install_webrtc_guard if install_webrtc_guard is None else bool(install_webrtc_guard)
    )

    major = _target_chrome_major(policy)
    selected_preset_id: str | None = None
    pinned: dict[str, Any] = {}
    fp_hash = ""

    if preset_id is not None:
        looked_up = _lookup_raw_preset(preset_id, policy)
        if looked_up is None:
            raise ValueError(f"Unknown fingerprint preset: {preset_id}")
        resolved_os, raw_preset = looked_up
        chosen_os = resolved_os
        selected_preset_id = preset_id
        pinned = build_pinned_fingerprint(raw_preset, chrome_major=major)
        fp_hash = compute_fingerprint_hash(pinned)
    else:
        candidates = list_filtered_presets(
            chosen_os,
            policy,
            used_hashes=used_hashes,
            used_preset_ids=used_presets,
            used_signatures=used_sigs,
        )
        if not candidates:
            for pid, p_os, source, raw in _raw_preset_entries(chosen_os, policy):
                candidates.append(
                    {
                        "id": pid,
                        "os": p_os,
                        "source": source,
                        "fingerprint_hash": compute_preset_hash(raw),
                        "hardware_signature": compute_hardware_signature(raw),
                        "in_use": pid in used_presets,
                    }
                )

        unused = [c for c in candidates if not c["in_use"] and c["id"] not in excluded]
        fallback = [c for c in candidates if c["id"] not in excluded] or candidates

        curated_unused = [c for c in unused if c.get("source") == "cloud"]
        builtin_unused = [c for c in unused if c.get("source") != "cloud"]
        random.shuffle(curated_unused)
        random.shuffle(builtin_unused)
        ordered = curated_unused + builtin_unused
        if not ordered:
            shuffled_fb = list(fallback)
            random.shuffle(shuffled_fb)
            ordered = shuffled_fb

        for candidate in ordered[:32]:
            looked_up = _lookup_raw_preset(candidate["id"], policy)
            if looked_up is None:
                continue
            _, raw_preset = looked_up
            attempt_pin = build_pinned_fingerprint(raw_preset, chrome_major=major)
            attempt_hash = compute_fingerprint_hash(attempt_pin)
            selected_preset_id = candidate["id"]
            pinned = attempt_pin
            fp_hash = attempt_hash
            if attempt_hash not in used_hashes:
                break

    final_window = window_size or _default_window_for_pin(pinned)

    return ProfileIsolation(
        isolation_version=1,
        policy_version=policy.policy_version,
        tier=chosen_tier,
        os=chosen_os,  # type: ignore[arg-type]
        preset_id=selected_preset_id,
        webrtc_policy=chosen_webrtc,
        canvas_policy=chosen_canvas,
        locale_mode=chosen_locale_mode,
        languages=chosen_languages,
        timezone_mode=chosen_tz_mode,
        timezone=chosen_tz,
        install_webrtc_guard=chosen_guard,
        window_size=final_window,
        pinned_fingerprint=pinned,
        fingerprint_hash=fp_hash,
    )


def ensure_profile_runtime_dirs(profile_dir: Path) -> dict[str, Path]:
    """Ensure private ``0700`` profile directory and isolated ``.runtime`` subdirectories."""
    profile_dir = profile_dir.resolve()
    reject_reparse_paths(profile_dir)
    if not profile_dir.exists():
        create_private_directory(profile_dir)
    else:
        protect_path(profile_dir)

    runtime_dir = profile_dir / ".runtime"
    tmp_dir = runtime_dir / "tmp"
    cache_dir = runtime_dir / "cache"
    config_dir = runtime_dir / "config"
    for directory in (runtime_dir, tmp_dir, cache_dir, config_dir):
        if not directory.exists():
            create_private_directory(directory)
        else:
            protect_path(directory)

    return {
        "runtime": runtime_dir,
        "tmp": tmp_dir,
        "cache": cache_dir,
        "config": config_dir,
    }


def _languages_for_exit_country(country_iso: str | None, fallback: list[str]) -> list[str]:
    if not country_iso:
        return list(fallback)
    return list(_COUNTRY_LANGUAGES.get(country_iso.upper(), fallback))


def _proxy_payload(profile: Any, network: Any) -> dict[str, Any] | None:
    if network is not None and getattr(network, "listener", None) is not None:
        listener = network.listener
        port = int(listener.port)
        return {
            "server": f"http://127.0.0.1:{port}",
            "host": "127.0.0.1",
            "port": port,
            "username": listener.username,
            "password": listener.password,
        }
    manual = getattr(profile, "proxy", None)
    if manual is not None:
        parsed = urlsplit(f"//{manual.server}")
        host = parsed.hostname or manual.server
        port = parsed.port or 0
        scheme = manual.type.value if hasattr(manual.type, "value") else str(manual.type)
        return {
            "server": f"{scheme}://{manual.server}",
            "host": host,
            "port": port,
            "username": manual.username,
            "password": manual.password,
        }
    return None


def compile_session_payload(
    profile: Any,
    isolation: ProfileIsolation,
    policy: IsolationPolicy,
    network: Any = None,
    *,
    exit_ip: str | None = None,
    exit_country: str | None = None,
    exit_city: str | None = None,
    include_proxy_secrets: bool = True,
) -> dict[str, Any]:
    """Build the Electron ``WebContentsView`` + CDP isolation session descriptor."""
    major = _target_chrome_major(policy)
    raw_pin = dict(isolation.pinned_fingerprint or getattr(profile, "fingerprint", None) or {})
    pinned = normalize_pinned_fingerprint(raw_pin, os_hint=isolation.os, chrome_major=major)

    # Respect explicit browser_settings.hardware_concurrency override if set.
    bs = getattr(profile, "browser_settings", None)
    if (
        bs is not None
        and isinstance(bs.hardware_concurrency, int)
        and bs.hardware_concurrency > 0
        and bs.hardware_concurrency != pinned.get("hardware_concurrency")
    ):
        pinned["hardware_concurrency"] = bs.hardware_concurrency

    # Timezone: fixed Los Angeles by default; follow exit GeoIP when dynamic.
    if isolation.timezone_mode == "fixed":
        resolved_tz = isolation.timezone or policy.default_timezone
    else:
        geo_tz: str | None = None
        if exit_ip:
            record = geoip.lookup(exit_ip)
            if record and record.timezone:
                geo_tz = record.timezone
        resolved_tz = geo_tz or policy.default_timezone

    # Languages: fixed en-US by default; follow exit country when dynamic.
    if isolation.locale_mode == "follow_exit":
        resolved_languages = _languages_for_exit_country(
            exit_country, isolation.languages or policy.default_languages
        )
    else:
        resolved_languages = list(isolation.languages or policy.default_languages)

    proxy = _proxy_payload(profile, network)
    if proxy is not None and not include_proxy_secrets:
        proxy = {
            "server": proxy["server"],
            "host": proxy["host"],
            "port": proxy["port"],
        }

    fp_hash = isolation.fingerprint_hash or compute_fingerprint_hash(pinned)
    return {
        "profile_id": profile.id,
        "status": "ready",
        "proxy": proxy,
        "exit_ip": exit_ip,
        "exit_country": exit_country,
        "exit_city": exit_city,
        "isolation": {
            "os": isolation.os,
            "tier": isolation.tier,
            "timezone": resolved_tz,
            "languages": resolved_languages,
            "webrtc_policy": isolation.webrtc_policy,
            "canvas_policy": isolation.canvas_policy,
            "fingerprint_hash": fp_hash,
            "policy_version": isolation.policy_version,
            "pinned_fingerprint": pinned,
        },
    }
