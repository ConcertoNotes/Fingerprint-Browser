"""Browser profile manager powered by the rewritten isolation engine."""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from camoufox_pm.config import get_settings

from . import profile_archive, proxy_check
from .browser_session import BrowserSessionManager
from .database import StaleWriteError, StorageManager
from .isolation import (
    DEFAULT_CHROME_MAJOR,
    IsolationPolicy,
    ProfileIsolation,
    allocate_isolation,
    browser_major,
    compile_session_payload,
    compute_fingerprint_hash,
    compute_hardware_signature,
    ensure_profile_runtime_dirs,
    isolation_from_legacy_row,
    list_filtered_presets,
    load_active_policy,
    normalize_pinned_fingerprint,
    parse_isolation,
    pinned_os,
    refresh_chrome_version,
    summarize_isolation,
)
from .isolation import audit_profile as run_isolation_audit
from .isolation.chrome_presets import pin_cores, pin_screen
from .leases import ProfileLocked, make_lease_holder
from .models import (
    BrowserSettings,
    Profile,
    ProfileGroup,
    ProfileStatus,
    ProxyConfig,
    UsageStats,
    WebRTCMode,
    generate_profile_id,
    normalize_tags,
)
from .network_adapter import NetworkAdapter
from .network_binding import NodeUnavailable
from .network_catalog import NetworkCatalog
from .network_catalog import now as network_now
from .network_probe import NetworkProbe
from .process_lock import profile_lock
from .profile_operations import profile_data_path, profile_operation, run_file_operation


class ExitIpDriftError(RuntimeError):
    """Raised when a bound profile's live exit IP or country changed since its last launch."""

    def __init__(
        self,
        *,
        previous_ip: str | None,
        previous_country: str | None,
        current_ip: str | None,
        current_country: str | None,
        country_changed: bool,
    ):
        self.previous_ip = previous_ip
        self.previous_country = previous_country
        self.current_ip = current_ip
        self.current_country = current_country
        self.country_changed = country_changed
        prev_label = f"{previous_ip or '?'} ({previous_country or '??'})"
        curr_label = f"{current_ip or '?'} ({current_country or '??'})"
        super().__init__(
            f"Exit IP changed from {prev_label} to {curr_label}. "
            "Confirm launch to proceed or switch back to the original node."
        )

    def to_detail(self) -> dict[str, Any]:
        return {
            "previous_ip": self.previous_ip,
            "previous_country": self.previous_country,
            "current_ip": self.current_ip,
            "current_country": self.current_country,
            "country_changed": self.country_changed,
        }


def _sync_browser_settings_from_isolation(
    bs: BrowserSettings,
    iso: ProfileIsolation,
    *,
    explicit_bs_keys: set[str] | None = None,
) -> BrowserSettings:
    """Keep legacy ``BrowserSettings`` mirror fields coherent with ``ProfileIsolation``."""
    explicit = explicit_bs_keys or set()
    pin = iso.pinned_fingerprint or {}
    bs.os = iso.os
    if "languages" not in explicit:
        bs.languages = list(iso.languages)
    if "timezone" not in explicit:
        bs.timezone = iso.timezone if iso.timezone_mode == "fixed" else None
    if "webrtc_mode" not in explicit:
        bs.webrtc_mode = WebRTCMode.NONE if iso.webrtc_policy == "block" else WebRTCMode.REPLACE
    if "stable_canvas" not in explicit:
        bs.stable_canvas = iso.canvas_policy == "stable_hardware"
    if "window_width" not in explicit:
        bs.window_width = iso.window_size[0]
    if "window_height" not in explicit:
        bs.window_height = iso.window_size[1]
    width, height = pin_screen(pin)
    if "screen" not in explicit and width and height:
        bs.screen = f"{width}x{height}"
    cores = pin_cores(pin)
    if "hardware_concurrency" not in explicit and isinstance(cores, int) and cores > 0:
        bs.hardware_concurrency = cores
    return bs


class ProfileManager:
    """Manage browser profiles: CRUD, isolation pinning, auditing, cloning, groups, and launches."""

    def __init__(self, storage_manager: StorageManager, data_dir: str = "data"):
        self.storage = storage_manager
        self.data_dir = Path(data_dir)
        self.profiles_dir = self.data_dir / "profiles"

        self.lease_holder = make_lease_holder()
        self.browser_sessions = BrowserSessionManager(storage_manager, self.lease_holder)
        self.network = NetworkAdapter(self)
        self.network_probe = NetworkProbe(self)

        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Initialized ProfileManager with data directory: {self.data_dir}")

    async def initialize(self):
        """Initialize the profile manager and its database."""
        await self.storage.initialize()
        self.browser_sessions.start_heartbeat()
        self.network.reap()
        logger.info("ProfileManager initialized")

    def active_policy(self) -> tuple[IsolationPolicy, str]:
        """Return ``(policy, source)`` for the currently active fixed-optimization policy."""
        return load_active_policy(self.storage.db._connection)

    async def _existing_isolation_sets(
        self, exclude_profile_id: str | None = None
    ) -> tuple[set[str], set[str], set[str]]:
        """Collect fingerprint hashes, preset IDs, and hardware signatures across local profiles."""
        hashes: set[str] = set()
        preset_ids: set[str] = set()
        signatures: set[str] = set()
        for existing in await self.storage.list_profiles():
            if exclude_profile_id and existing.id == exclude_profile_id:
                continue
            if existing.fingerprint_hash:
                hashes.add(existing.fingerprint_hash)
            iso = parse_isolation(existing.isolation_config)
            if iso is not None:
                if iso.fingerprint_hash:
                    hashes.add(iso.fingerprint_hash)
                if iso.preset_id:
                    preset_ids.add(iso.preset_id)
                if iso.pinned_fingerprint:
                    sig = compute_hardware_signature(iso.pinned_fingerprint)
                    if sig:
                        signatures.add(sig)
            elif existing.fingerprint:
                fp_hash = compute_fingerprint_hash(existing.fingerprint)
                if fp_hash:
                    hashes.add(fp_hash)
                sig = compute_hardware_signature(existing.fingerprint)
                if sig:
                    signatures.add(sig)
        return hashes, preset_ids, signatures

    async def list_fingerprint_presets(self, os_name: str | None = None) -> list[dict[str, Any]]:
        """Return quality-filtered real device presets annotated with ``in_use``."""
        policy, _ = self.active_policy()
        hashes, preset_ids, signatures = await self._existing_isolation_sets()
        return list_filtered_presets(
            os_name,
            policy,
            used_hashes=hashes,
            used_preset_ids=preset_ids,
            used_signatures=signatures,
        )

    async def preview_isolation(
        self,
        *,
        os_choice: str | None = None,
        tier: Literal["strict", "standard"] | None = None,
        preset_id: str | None = None,
        exclude_preset_ids: set[str] | None = None,
        webrtc_policy: Literal["block", "spoof_exit"] | None = None,
        canvas_policy: Literal["stable_hardware", "session_noise"] | None = None,
        locale_mode: Literal["fixed", "follow_exit"] | None = None,
        languages: list[str] | None = None,
        timezone_mode: Literal["fixed", "follow_exit"] | None = None,
        timezone: str | None = None,
        install_webrtc_guard: bool | None = None,
    ) -> dict[str, Any]:
        """Preview an auto-allocated deduplicated real-device isolation config."""
        policy, source = self.active_policy()
        hashes, preset_ids, signatures = await self._existing_isolation_sets()
        iso = allocate_isolation(
            os_choice=os_choice,
            tier=tier,
            existing_hashes=hashes,
            policy=policy,
            preset_id=preset_id,
            existing_preset_ids=preset_ids,
            existing_signatures=signatures,
            exclude_preset_ids=exclude_preset_ids,
            webrtc_policy=webrtc_policy,
            canvas_policy=canvas_policy,
            locale_mode=locale_mode,
            languages=languages,
            timezone_mode=timezone_mode,
            timezone=timezone,
            install_webrtc_guard=install_webrtc_guard,
        )
        filtered = list_filtered_presets(
            iso.os,
            policy,
            used_hashes=hashes,
            used_preset_ids=preset_ids,
            used_signatures=signatures,
        )
        unused_count = sum(1 for item in filtered if not item["in_use"])
        iso_public = iso.model_dump(mode="json")
        # Keep the preview payload lightweight by omitting giant font/voice lists.
        pin = iso_public.get("pinned_fingerprint")
        if isinstance(pin, dict):
            iso_public["pinned_fingerprint"] = {
                k: v for k, v in pin.items() if k not in {"fonts", "voices"}
            }
        return {
            "isolation": iso_public,
            "summary": summarize_isolation(iso, policy.policy_version),
            "policy_source": source,
            "policy_version": policy.policy_version,
            "available_unused_presets": unused_count,
            "total_presets": len(filtered),
        }

    async def create_profile(
        self,
        name: str,
        group: str | None = None,
        browser_settings: dict[str, Any] | BrowserSettings | None = None,
        proxy_config: dict[str, Any] | None = None,
        generate_fingerprint: bool = True,
        notes: str | None = None,
        fingerprint_preset: str | None = None,
        status: ProfileStatus | str | None = None,
        tags: list[str] | None = None,
        node_id: str | None = None,
        isolation: dict[str, Any] | ProfileIsolation | None = None,
        ip_drift_guard: bool = True,
    ) -> Profile:
        """Create a profile, immediately pin a deduplicated hardware fingerprint, and optionally bind a node."""
        logger.info(f"Creating new profile: {name}")

        # Validate node_id before writing anything so creation + binding is atomic.
        if node_id is not None:
            node = NetworkCatalog(self).get_node(node_id)
            if node["status"] != "active":
                raise NodeUnavailable("This node is retired or invalid and cannot be bound")

        profile = Profile(
            name=name,
            group=group,
            notes=notes,
            tags=tags or [],
            ip_drift_guard=bool(ip_drift_guard),
        )
        if status:
            profile.status = ProfileStatus(status)

        if proxy_config:
            profile.proxy = ProxyConfig(**proxy_config)
            profile.proxy.validate_authentication()

        policy, _ = self.active_policy()
        iso_input: dict[str, Any] = {}
        if isinstance(isolation, ProfileIsolation):
            iso_input = isolation.model_dump(exclude_none=True)
        elif isinstance(isolation, dict):
            iso_input = dict(isolation)

        bs_dict: dict[str, Any] = {}
        if isinstance(browser_settings, BrowserSettings):
            bs_dict = browser_settings.model_dump()
            profile.browser_settings = browser_settings.model_copy(deep=True)
        elif isinstance(browser_settings, dict):
            bs_dict = dict(browser_settings)
            merged_bs = profile.browser_settings.model_dump()
            merged_bs.update(bs_dict)
            profile.browser_settings = BrowserSettings(**merged_bs)

        explicit_bs_keys = set(bs_dict)
        chosen_os = iso_input.get("os") or bs_dict.get("os")
        chosen_preset = iso_input.get("preset_id") or fingerprint_preset
        chosen_tier = iso_input.get("tier")
        chosen_webrtc = iso_input.get("webrtc_policy")
        if chosen_webrtc is None and "webrtc_mode" in bs_dict:
            raw_w = str(bs_dict["webrtc_mode"]).lower()
            chosen_webrtc = "block" if raw_w in {"none", "block"} else "spoof_exit"
        chosen_canvas = iso_input.get("canvas_policy")
        if chosen_canvas is None and "stable_canvas" in bs_dict:
            chosen_canvas = "stable_hardware" if bs_dict["stable_canvas"] else "session_noise"
        chosen_locale_mode = iso_input.get("locale_mode")
        chosen_languages = iso_input.get("languages") or bs_dict.get("languages")

        chosen_tz_mode = iso_input.get("timezone_mode")
        chosen_tz = iso_input.get("timezone")
        if "timezone" in bs_dict and chosen_tz is None and chosen_tz_mode is None:
            if bs_dict["timezone"]:
                chosen_tz_mode = "fixed"
                chosen_tz = bs_dict["timezone"]
            else:
                chosen_tz_mode = "follow_exit"
                chosen_tz = None

        chosen_guard = iso_input.get("install_webrtc_guard")
        chosen_win: tuple[int, int] | None = None
        if iso_input.get("window_size") and len(iso_input["window_size"]) == 2:
            chosen_win = (int(iso_input["window_size"][0]), int(iso_input["window_size"][1]))
        elif "window_width" in bs_dict and "window_height" in bs_dict:
            if bs_dict["window_width"] and bs_dict["window_height"]:
                chosen_win = (int(bs_dict["window_width"]), int(bs_dict["window_height"]))

        if generate_fingerprint or isolation is not None or chosen_preset is not None:
            hashes, preset_ids, signatures = await self._existing_isolation_sets()
            allocated = allocate_isolation(
                os_choice=chosen_os,
                tier=chosen_tier,
                existing_hashes=hashes,
                policy=policy,
                preset_id=chosen_preset,
                existing_preset_ids=preset_ids,
                existing_signatures=signatures,
                webrtc_policy=chosen_webrtc,
                canvas_policy=chosen_canvas,
                locale_mode=chosen_locale_mode,
                languages=chosen_languages,
                timezone_mode=chosen_tz_mode,
                timezone=chosen_tz,
                install_webrtc_guard=chosen_guard,
                window_size=chosen_win,
            )
            # Apply explicit hardware_concurrency override from browser_settings if provided.
            if (
                "hardware_concurrency" in explicit_bs_keys
                and isinstance(profile.browser_settings.hardware_concurrency, int)
                and profile.browser_settings.hardware_concurrency > 0
            ):
                allocated.pinned_fingerprint["hardware_concurrency"] = (
                    profile.browser_settings.hardware_concurrency
                )
                allocated.fingerprint_hash = compute_fingerprint_hash(allocated.pinned_fingerprint)

            profile.isolation_config = allocated.model_dump(mode="json")
            profile.fingerprint = dict(allocated.pinned_fingerprint)
            profile.fingerprint_hash = allocated.fingerprint_hash
            _sync_browser_settings_from_isolation(
                profile.browser_settings, allocated, explicit_bs_keys=explicit_bs_keys
            )

        profile_dir = Path(profile.get_storage_path(str(self.profiles_dir)))
        ensure_profile_runtime_dirs(profile_dir)

        try:
            await self.storage.save_profile(profile)
            if node_id is not None:
                conn = self.storage.db._connection
                with conn:
                    conn.execute("BEGIN IMMEDIATE")
                    status_row = conn.execute(
                        "SELECT status FROM network_nodes WHERE id = ?", (node_id,)
                    ).fetchone()
                    if status_row is None or status_row[0] != "active":
                        raise NodeUnavailable("This node is retired or invalid and cannot be bound")
                    conn.execute(
                        "INSERT INTO profile_network_bindings (profile_id, node_id, row_version, bound_at) "
                        "VALUES (?, ?, 0, ?)",
                        (profile.id, node_id, network_now()),
                    )
        except BaseException:
            # Roll back profile row and directory on failure so creation + binding stays atomic.
            await self.storage.delete_profile(profile.id)
            shutil.rmtree(profile_dir, ignore_errors=True)
            raise

        await self.storage.log_usage(
            UsageStats(
                profile_id=profile.id,
                action="create_profile",
                details={"name": name, "group": group, "node_id": node_id},
            )
        )
        logger.info(f"Profile '{name}' created with ID: {profile.id} ({profile.fingerprint_hash})")
        return profile

    async def get_profile(self, profile_id: str) -> Profile | None:
        """Get a profile by ID."""
        profile = await self.storage.get_profile(profile_id)
        if profile:
            if profile.storage_path and not Path(profile.storage_path).is_absolute():
                try:
                    profile.storage_path = str(profile_data_path(self, profile))
                except ValueError:
                    pass
            logger.debug(f"Loaded profile: {profile.name} ({profile_id})")
        else:
            logger.warning(f"Profile with ID {profile_id} not found")
        return profile

    async def update_profile(
        self,
        profile_id: str,
        updates: dict[str, Any],
        expected_row_version: int | None = None,
    ) -> Profile | None:
        """Update a profile, guarded by optimistic concurrency and a local profile lock."""
        logger.info(f"Updating profile {profile_id}")

        async with profile_lock(self, profile_id):
            profile = await self.get_profile(profile_id)
            if not profile:
                return None
            guard = profile.row_version if expected_row_version is None else expected_row_version
            if guard != profile.row_version:
                raise StaleWriteError(profile_id, expected_row_version)

            proxy_before = profile.proxy
            policy, _ = self.active_policy()

            for key, value in updates.items():
                if key == "proxy_config":
                    key = "proxy"

                if key == "isolation" and isinstance(value, dict):
                    current_iso = parse_isolation(profile.isolation_config) or (
                        isolation_from_legacy_row(
                            profile.browser_settings.model_dump(), profile.fingerprint, policy
                        )
                    )
                    merged_iso = current_iso.model_dump()
                    for field_key in (
                        "tier",
                        "os",
                        "webrtc_policy",
                        "canvas_policy",
                        "locale_mode",
                        "languages",
                        "timezone_mode",
                        "timezone",
                        "install_webrtc_guard",
                        "window_size",
                    ):
                        if field_key in value:
                            merged_iso[field_key] = value[field_key]
                    updated_iso = ProfileIsolation.model_validate(merged_iso)
                    profile.isolation_config = updated_iso.model_dump(mode="json")
                    profile.fingerprint_hash = (
                        updated_iso.fingerprint_hash or profile.fingerprint_hash
                    )
                    _sync_browser_settings_from_isolation(profile.browser_settings, updated_iso)
                    continue

                if hasattr(profile, key):
                    if key == "tags":
                        value = normalize_tags(value if value is not None else [])
                    elif key == "status" and isinstance(value, str):
                        value = ProfileStatus(value)
                    elif key == "proxy" and value is not None:
                        if isinstance(value, dict):
                            value = ProxyConfig(**value)
                        value.validate_authentication()
                    elif key == "proxy" and value is None:
                        value = None
                    elif key == "ip_drift_guard":
                        value = bool(value)
                    elif key == "browser_settings" and value is not None:
                        if isinstance(value, dict):
                            if "webrtc_mode" in value and isinstance(value["webrtc_mode"], str):
                                try:
                                    value["webrtc_mode"] = WebRTCMode(value["webrtc_mode"])
                                except ValueError:
                                    value["webrtc_mode"] = WebRTCMode.REPLACE
                            value = BrowserSettings(**value)
                    setattr(profile, key, value)

            # Keep isolation_config synchronized if browser_settings changed directly.
            if "browser_settings" in updates and "isolation" not in updates:
                synced_iso = parse_isolation(profile.isolation_config)
                if synced_iso is not None:
                    bs = profile.browser_settings
                    if bs.os in {"macos", "windows", "linux"}:
                        synced_iso.os = bs.os  # type: ignore[assignment]
                    if bs.languages:
                        synced_iso.languages = list(bs.languages)
                    if bs.timezone:
                        synced_iso.timezone_mode = "fixed"
                        synced_iso.timezone = bs.timezone
                    elif "timezone" in (updates.get("browser_settings") or {}):
                        synced_iso.timezone_mode = "follow_exit"
                        synced_iso.timezone = None
                    synced_iso.webrtc_policy = (
                        "block" if bs.webrtc_mode == WebRTCMode.NONE else "spoof_exit"
                    )
                    synced_iso.canvas_policy = (
                        "stable_hardware" if bs.stable_canvas else "session_noise"
                    )
                    if bs.window_width and bs.window_height:
                        synced_iso.window_size = (bs.window_width, bs.window_height)
                    profile.isolation_config = synced_iso.model_dump(mode="json")

            if "proxy_config" in updates or "proxy" in updates:
                if profile.proxy != proxy_before:
                    profile.proxy_check = None

            if "browser_settings" in updates and profile.fingerprint:
                p_os = pinned_os(profile.fingerprint)
                if p_os and p_os != profile.browser_settings.os:
                    logger.warning(
                        f"Profile {profile_id} is now set to {profile.browser_settings.os} "
                        f"while its pinned machine is {p_os}"
                    )

            profile.updated_at = datetime.now()
            await self.storage.update_profile(profile, expected_row_version=guard)

            await self.storage.log_usage(
                UsageStats(
                    profile_id=profile_id,
                    action="update_profile",
                    details={"updated_fields": list(updates.keys())},
                )
            )
            logger.info(f"Profile {profile_id} updated")
            return profile

    async def delete_profile(self, profile_id: str, remove_data: bool = True) -> bool:
        """Permanently delete a closed profile and its on-disk data (no Trash)."""
        if not await self.get_profile(profile_id):
            return False
        async with profile_operation(self, profile_id) as profile:
            if remove_data:
                try:
                    path = profile_data_path(self, profile)
                    if path.exists():
                        await run_file_operation(shutil.rmtree, path)
                except ValueError:
                    pass
            return await self.storage.delete_profile(profile_id)

    async def list_profiles(
        self,
        group: str | None = None,
        status: ProfileStatus | None = None,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Profile]:
        """List profiles with optional filtering."""
        combined_filters: dict[str, Any] = {}
        if group:
            combined_filters["group"] = group
        if status:
            combined_filters["status"] = status
        if filters:
            combined_filters.update(filters)
        profiles = await self.storage.list_profiles(combined_filters, limit, offset)
        logger.debug(f"Loaded {len(profiles)} profiles")
        return profiles

    async def bulk_update_tags(
        self, profiles: list[dict], add: list[str], remove: list[str]
    ) -> dict:
        add, remove = normalize_tags(add), normalize_tags(remove)
        result: dict[str, list] = {"updated": [], "skipped": []}
        seen: set[str] = set()
        for target in profiles:
            profile_id = target["id"]
            if profile_id in seen:
                continue
            seen.add(profile_id)
            async with profile_lock(self, profile_id):
                profile = await self.get_profile(profile_id)
                if profile is None:
                    result["skipped"].append({"id": profile_id, "reason": "Profile not found"})
                    continue
                try:
                    tags = normalize_tags([tag for tag in profile.tags if tag not in remove] + add)
                    updated = await self.storage.db.set_profile_tags(
                        profile_id, tags, target["row_version"]
                    )
                    result["updated"].append(updated)
                except StaleWriteError:
                    result["skipped"].append(
                        {"id": profile_id, "reason": "Profile changed; reload before editing tags"}
                    )
                except ValueError as exc:
                    result["skipped"].append({"id": profile_id, "reason": str(exc)})
        return result

    async def clone_profile(
        self, source_id: str, new_name: str, regenerate_fingerprint: bool = True
    ) -> Profile | None:
        if not await self.get_profile(source_id):
            return None
        async with profile_operation(self, source_id):
            return await self._clone_profile_unlocked(source_id, new_name, regenerate_fingerprint)

    async def _clone_profile_unlocked(
        self, source_id: str, new_name: str, regenerate_fingerprint: bool = True
    ) -> Profile | None:
        logger.info(f"Cloning profile {source_id} -> {new_name}")
        source_profile = await self.get_profile(source_id)
        if not source_profile:
            return None

        profile_data = source_profile.model_dump()
        profile_data.pop("id")
        profile_data["name"] = new_name
        profile_data["created_at"] = datetime.now()
        profile_data["updated_at"] = datetime.now()
        profile_data["last_used"] = None
        profile_data["storage_path"] = None
        profile_data["proxy_check"] = None
        profile_data["last_exit_ip"] = None
        profile_data["last_exit_country"] = None
        profile_data["last_exit_at"] = None
        profile_data["cloud_revision"] = 0
        profile_data["cloud_synced_at"] = None
        profile_data["row_version"] = 0

        new_profile = Profile(**profile_data)

        if regenerate_fingerprint:
            policy, _ = self.active_policy()
            hashes, preset_ids, signatures = await self._existing_isolation_sets()
            source_iso = parse_isolation(source_profile.isolation_config)
            allocated = allocate_isolation(
                os_choice=source_iso.os if source_iso else source_profile.browser_settings.os,
                tier=source_iso.tier if source_iso else policy.default_tier,
                existing_hashes=hashes,
                policy=policy,
                existing_preset_ids=preset_ids,
                existing_signatures=signatures,
                webrtc_policy=source_iso.webrtc_policy if source_iso else None,
                canvas_policy=source_iso.canvas_policy if source_iso else None,
                locale_mode=source_iso.locale_mode if source_iso else None,
                languages=source_iso.languages if source_iso else None,
                timezone_mode=source_iso.timezone_mode if source_iso else None,
                timezone=source_iso.timezone if source_iso else None,
                install_webrtc_guard=source_iso.install_webrtc_guard if source_iso else None,
                window_size=source_iso.window_size if source_iso else None,
            )
            new_profile.isolation_config = allocated.model_dump(mode="json")
            new_profile.fingerprint = dict(allocated.pinned_fingerprint)
            new_profile.fingerprint_hash = allocated.fingerprint_hash
            _sync_browser_settings_from_isolation(new_profile.browser_settings, allocated)

        profile_dir = Path(new_profile.get_storage_path(str(self.profiles_dir)))
        ensure_profile_runtime_dirs(profile_dir)

        source_path = profile_data_path(self, source_profile)
        if source_path.exists():
            try:

                def copy_data() -> None:
                    if any(path.is_symlink() for path in source_path.rglob("*")):
                        raise ValueError("Cannot clone profile data containing symbolic links")
                    shutil.copytree(source_path, profile_dir, dirs_exist_ok=True)

                await run_file_operation(copy_data)
            except BaseException:
                shutil.rmtree(profile_dir, ignore_errors=True)
                raise

        await self.storage.save_profile(new_profile)
        await self.storage.log_usage(
            UsageStats(
                profile_id=new_profile.id,
                action="clone_profile",
                details={"source_id": source_id, "new_name": new_name},
            )
        )
        logger.info(f"Profile cloned: {new_profile.id}")
        return new_profile

    async def rotate_profile_fingerprint(
        self,
        profile_id: str,
        *,
        os_choice: str | None = None,
        preset_id: str | None = None,
    ) -> Profile | None:
        """Allocate and immediately pin a fresh deduplicated hardware fingerprint."""
        logger.info(f"Rotating fingerprint for profile {profile_id}")

        async with profile_lock(self, profile_id):
            profile = await self.get_profile(profile_id)
            if not profile:
                return None

            policy, _ = self.active_policy()
            current_iso = parse_isolation(profile.isolation_config)
            hashes, preset_ids, signatures = await self._existing_isolation_sets()
            if profile.fingerprint_hash:
                hashes.add(profile.fingerprint_hash)
            exclude_ids = (
                {current_iso.preset_id} if (current_iso and current_iso.preset_id) else set()
            )

            allocated = allocate_isolation(
                os_choice=os_choice
                or (current_iso.os if current_iso else profile.browser_settings.os),
                tier=current_iso.tier if current_iso else policy.default_tier,
                existing_hashes=hashes,
                policy=policy,
                preset_id=preset_id,
                existing_preset_ids=preset_ids,
                existing_signatures=signatures,
                exclude_preset_ids=exclude_ids,
                webrtc_policy=current_iso.webrtc_policy if current_iso else None,
                canvas_policy=current_iso.canvas_policy if current_iso else None,
                locale_mode=current_iso.locale_mode if current_iso else None,
                languages=current_iso.languages if current_iso else None,
                timezone_mode=current_iso.timezone_mode if current_iso else None,
                timezone=current_iso.timezone if current_iso else None,
                install_webrtc_guard=current_iso.install_webrtc_guard if current_iso else None,
                window_size=current_iso.window_size if current_iso else None,
            )
            profile.isolation_config = allocated.model_dump(mode="json")
            profile.fingerprint = dict(allocated.pinned_fingerprint)
            profile.fingerprint_hash = allocated.fingerprint_hash
            _sync_browser_settings_from_isolation(profile.browser_settings, allocated)
            profile.updated_at = datetime.now()

            await self.storage.update_profile(profile, expected_row_version=profile.row_version)
            await self.storage.log_usage(
                UsageStats(
                    profile_id=profile_id,
                    action="rotate_fingerprint",
                    details={"fingerprint_hash": allocated.fingerprint_hash},
                )
            )
            logger.info(
                f"Fingerprint for profile {profile_id} rotated to {allocated.fingerprint_hash}"
            )
            return profile

    async def reoptimize_profile(self, profile_id: str) -> Profile | None:
        """Apply the latest active isolation policy & hardening without replacing hardware identity."""
        async with profile_lock(self, profile_id):
            profile = await self.get_profile(profile_id)
            if not profile:
                return None

            policy, source = self.active_policy()
            current_iso = parse_isolation(profile.isolation_config)
            if current_iso is None:
                current_iso = isolation_from_legacy_row(
                    profile.browser_settings.model_dump(), profile.fingerprint, policy
                )

            # If no hardware pin exists yet (legacy profile), pin one now; otherwise preserve it!
            if not current_iso.pinned_fingerprint:
                hashes, preset_ids, signatures = await self._existing_isolation_sets(
                    exclude_profile_id=profile_id
                )
                allocated = allocate_isolation(
                    os_choice=current_iso.os,
                    tier=current_iso.tier,
                    existing_hashes=hashes,
                    policy=policy,
                    existing_preset_ids=preset_ids,
                    existing_signatures=signatures,
                    webrtc_policy=current_iso.webrtc_policy,
                    canvas_policy=current_iso.canvas_policy,
                    locale_mode=current_iso.locale_mode,
                    languages=current_iso.languages,
                    timezone_mode=current_iso.timezone_mode,
                    timezone=current_iso.timezone,
                    install_webrtc_guard=policy.install_webrtc_guard,
                    window_size=current_iso.window_size,
                )
                current_iso = allocated
            else:
                pin = normalize_pinned_fingerprint(
                    dict(current_iso.pinned_fingerprint),
                    os_hint=current_iso.os,
                    chrome_major=policy.recommended_chrome_major or DEFAULT_CHROME_MAJOR,
                )
                current_iso.pinned_fingerprint = pin
                current_iso.fingerprint_hash = compute_fingerprint_hash(pin)
                current_iso.policy_version = policy.policy_version
                current_iso.install_webrtc_guard = policy.install_webrtc_guard

            profile.isolation_config = current_iso.model_dump(mode="json")
            profile.fingerprint = dict(current_iso.pinned_fingerprint)
            profile.fingerprint_hash = current_iso.fingerprint_hash
            _sync_browser_settings_from_isolation(profile.browser_settings, current_iso)
            profile.updated_at = datetime.now()

            # Tighten private 0700 storage directory permissions.
            try:
                pdir = profile_data_path(self, profile)
                ensure_profile_runtime_dirs(pdir)
            except (ValueError, OSError) as exc:
                logger.warning(f"Could not refresh storage hardening for {profile_id}: {exc}")

            await self.storage.update_profile(profile, expected_row_version=profile.row_version)
            await self.storage.log_usage(
                UsageStats(
                    profile_id=profile_id,
                    action="reoptimize_isolation",
                    details={"policy_version": policy.policy_version, "source": source},
                )
            )
            return profile

    async def audit_profile(self, profile_id: str) -> dict[str, Any]:
        """Run the comprehensive Isolation Audit on a profile."""
        return await run_isolation_audit(self, profile_id)

    async def get_profile_stats(self, profile_id: str) -> dict[str, Any]:
        """Get usage statistics for a profile."""
        profile = await self.get_profile(profile_id)
        if not profile:
            return {}
        stats = await self.storage.get_profile_usage_stats(profile_id)
        return {
            "profile_id": profile_id,
            "name": profile.name,
            "status": profile.status,
            "created_at": profile.created_at,
            "last_used": profile.last_used,
            "total_sessions": len([s for s in stats if s.action == "launch_browser"]),
            "total_duration_minutes": sum(s.duration or 0 for s in stats) // 60,
            "last_session": profile.last_used,
            "success_rate": len([s for s in stats if s.success]) / len(stats) if stats else 0,
            "actions": [
                {
                    "action": s.action,
                    "timestamp": s.timestamp.isoformat(),
                    "success": s.success,
                }
                for s in stats
            ],
        }

    async def bulk_update_profiles(self, profile_ids: list[str], updates: dict[str, Any]) -> int:
        logger.info(f"Bulk-updating {len(profile_ids)} profiles")
        updated_count = 0
        for profile_id in profile_ids:
            try:
                result = await self.update_profile(profile_id, updates)
                if result:
                    updated_count += 1
            except Exception as exc:
                logger.error(f"Failed to update profile {profile_id}: {exc}")
        return updated_count

    # --- Profile group operations ---

    async def create_group(self, name: str, description: str | None = None) -> dict[str, Any]:
        group = ProfileGroup(name=name, description=description)
        await self.storage.save_profile_group(group)
        return {
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "created_at": group.created_at,
        }

    async def get_group(self, group_id: str) -> dict[str, Any] | None:
        groups = await self.storage.list_profile_groups()
        group = next((g for g in groups if g.id == group_id), None)
        if not group:
            return None
        profiles = await self.list_profiles(filters={"group": group_id})
        return {
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "profile_count": len(profiles),
            "created_at": group.created_at,
        }

    async def list_groups(self) -> list[dict[str, Any]]:
        groups = await self.storage.list_profile_groups()
        result = []
        for group in groups:
            profiles = await self.list_profiles(filters={"group": group.id})
            result.append(
                {
                    "id": group.id,
                    "name": group.name,
                    "description": group.description,
                    "profile_count": len(profiles),
                    "created_at": group.created_at,
                }
            )
        return result

    async def update_group(self, group_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        groups = await self.storage.list_profile_groups()
        group = next((g for g in groups if g.id == group_id), None)
        if not group:
            return None
        for key, value in updates.items():
            if hasattr(group, key):
                setattr(group, key, value)
        await self.storage.save_profile_group(group)
        profiles = await self.list_profiles(filters={"group": group_id})
        return {
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "profile_count": len(profiles),
            "created_at": group.created_at,
        }

    async def delete_group(self, group_id: str) -> bool:
        groups = await self.storage.list_profile_groups()
        group = next((g for g in groups if g.id == group_id), None)
        if not group:
            return False
        profiles = await self.list_profiles(filters={"group": group_id})
        for profile in profiles:
            await self.update_profile(profile.id, {"group": None})
        return await self.storage.delete_profile_group(group_id)

    async def refresh_browser_version(self, profile_id: str) -> Profile | None:
        """Advance only the Chrome version/User-Agent of a pinned profile, keeping all hardware."""
        async with profile_lock(self, profile_id):
            profile = await self.get_profile(profile_id)
            if not profile:
                return None
            if not profile.fingerprint:
                raise ValueError("This profile has no pinned machine yet; launch it once first.")

            policy, _ = self.active_policy()
            target_major = policy.recommended_chrome_major or DEFAULT_CHROME_MAJOR
            before = browser_major(profile.fingerprint)
            p_os = pinned_os(profile.fingerprint) or profile.browser_settings.os
            profile.fingerprint = refresh_chrome_version(
                profile.fingerprint,
                target_major=target_major,
                os_hint=p_os,
            )
            after = browser_major(profile.fingerprint)
            new_hash = compute_fingerprint_hash(profile.fingerprint)
            profile.fingerprint_hash = new_hash

            iso = parse_isolation(profile.isolation_config)
            if iso is not None:
                iso.pinned_fingerprint = dict(profile.fingerprint)
                iso.fingerprint_hash = new_hash
                profile.isolation_config = iso.model_dump(mode="json")

            profile.updated_at = datetime.now()
            await self.storage.update_profile(profile, expected_row_version=profile.row_version)
            await self.storage.log_usage(
                UsageStats(
                    profile_id=profile_id,
                    action="refresh_browser_version",
                    details={"from": before, "to": after},
                )
            )
            return profile

    async def reconcile_os(self, profile_id: str, keep_machine: bool) -> Profile | None:
        """Put a profile's OS setting and pinned machine back in agreement."""
        async with profile_lock(self, profile_id):
            profile = await self.get_profile(profile_id)
            if not profile:
                return None
            if not profile.fingerprint:
                raise ValueError("This profile has no pinned machine yet, so nothing can disagree.")

            p_os = pinned_os(profile.fingerprint)
            if not p_os:
                raise ValueError(
                    "Cannot tell which operating system this profile's machine describes."
                )
            if p_os == profile.browser_settings.os:
                raise ValueError(f"This profile is already set to {p_os}.")

            was = profile.browser_settings.os
            policy, _ = self.active_policy()
            iso = parse_isolation(profile.isolation_config) or isolation_from_legacy_row(
                profile.browser_settings.model_dump(), profile.fingerprint, policy
            )

            if keep_machine:
                profile.browser_settings.os = p_os
                iso.os = p_os
                profile.isolation_config = iso.model_dump(mode="json")
            else:
                hashes, preset_ids, signatures = await self._existing_isolation_sets(
                    exclude_profile_id=profile_id
                )
                allocated = allocate_isolation(
                    os_choice=profile.browser_settings.os,
                    tier=iso.tier,
                    existing_hashes=hashes,
                    policy=policy,
                    existing_preset_ids=preset_ids,
                    existing_signatures=signatures,
                )
                iso = allocated
                profile.fingerprint = dict(allocated.pinned_fingerprint)
                profile.fingerprint_hash = allocated.fingerprint_hash
                profile.isolation_config = allocated.model_dump(mode="json")
                width, height = pin_screen(profile.fingerprint)
                if width and height:
                    profile.browser_settings.screen = f"{width}x{height}"

            profile.updated_at = datetime.now()
            await self.storage.update_profile(profile, expected_row_version=profile.row_version)
            await self.storage.log_usage(
                UsageStats(
                    profile_id=profile_id,
                    action="reconcile_os",
                    details={
                        "kept": "machine" if keep_machine else "setting",
                        "from": was,
                        "to": profile.browser_settings.os,
                    },
                )
            )
            return profile

    async def clear_geography(self, profile_ids: list[str]) -> dict[str, list[str]]:
        """Drop stored timezone and coordinates so the proxy supplies both."""
        cleared: list[str] = []
        unchanged: list[str] = []
        not_found: list[str] = []

        for profile_id in profile_ids:
            async with profile_lock(self, profile_id):
                profile = await self.get_profile(profile_id)
                if not profile:
                    not_found.append(profile_id)
                    continue
                if not profile.browser_settings.clear_geography():
                    unchanged.append(profile_id)
                    continue
                iso = parse_isolation(profile.isolation_config)
                if iso is not None:
                    iso.timezone_mode = "follow_exit"
                    iso.timezone = None
                    profile.isolation_config = iso.model_dump(mode="json")
                profile.updated_at = datetime.now()
                await self.storage.update_profile(profile, expected_row_version=profile.row_version)
                await self.storage.log_usage(
                    UsageStats(profile_id=profile_id, action="clear_geography", details={})
                )
                cleared.append(profile_id)

        return {"cleared": cleared, "unchanged": unchanged, "not_found": not_found}

    # --- Portability ---

    async def export_profile(self, profile_id: str, destination: Path) -> Path:
        async with profile_operation(self, profile_id) as profile:
            await run_file_operation(
                profile_archive.export_profile,
                profile,
                profile_data_path(self, profile),
                destination,
            )
        await self.storage.log_usage(
            UsageStats(profile_id=profile_id, action="export_profile", details={})
        )
        return destination

    async def import_profile(self, source: Path, name: str | None = None) -> Profile:
        record = profile_archive.read_archive(source)
        profile = Profile(**{**record, "name": name or record.get("name") or "Imported profile"})
        profile.id = generate_profile_id()
        profile.storage_path = None
        profile.deleted_at = None
        profile.row_version = 0
        profile.group = None
        profile.proxy_check = None
        profile.last_exit_ip = None
        profile.last_exit_country = None
        profile.last_exit_at = None
        profile.cloud_revision = 0
        profile.cloud_synced_at = None

        if profile.isolation_config is None and profile.fingerprint:
            policy, _ = self.active_policy()
            iso = isolation_from_legacy_row(
                profile.browser_settings.model_dump(), profile.fingerprint, policy
            )
            profile.isolation_config = iso.model_dump(mode="json")
            profile.fingerprint_hash = iso.fingerprint_hash

        data_dir = Path(profile.get_storage_path(str(self.profiles_dir)))
        data_dir.mkdir(parents=True, exist_ok=False)
        try:
            await run_file_operation(profile_archive.extract_data, source, data_dir)
            await self.storage.save_profile(profile)
        except BaseException:
            shutil.rmtree(data_dir, ignore_errors=True)
            raise

        await self.storage.log_usage(
            UsageStats(
                profile_id=profile.id, action="import_profile", details={"name": profile.name}
            )
        )
        return profile

    # --- Embedded Electron session control ---

    async def prepare_session(
        self,
        profile_id: str,
        *,
        allow_ip_drift: bool = False,
        include_proxy_secrets: bool = True,
    ) -> dict[str, Any]:
        """Prepare a profile's isolated proxy runtime, verify exit IP, enforce drift guard, and return CDP descriptor."""
        async with profile_lock(self, profile_id):
            return await self._prepare_session(
                profile_id,
                allow_ip_drift=allow_ip_drift,
                include_proxy_secrets=include_proxy_secrets,
            )

    async def _prepare_session(
        self,
        profile_id: str,
        *,
        allow_ip_drift: bool = False,
        include_proxy_secrets: bool = True,
    ) -> dict[str, Any]:
        acquired = await self.storage.acquire_lease(
            profile_id, self.lease_holder, get_settings().lease_ttl
        )
        if not acquired:
            lease = await self.storage.get_lease(profile_id)
            if lease is None or not await self.get_profile(profile_id):
                raise ValueError(f"Profile with ID {profile_id} not found")
            raise ProfileLocked(profile_id, lease[0])

        network = None
        current_ip: str | None = None
        current_country: str | None = None
        current_city: str | None = None
        try:
            profile = await self.get_profile(profile_id)
            if not profile:
                raise ValueError(f"Profile with ID {profile_id} not found")

            policy, _ = self.active_policy()

            # Idempotent return if this sidecar already holds the running session.
            if self.browser_sessions.is_running(profile_id):
                existing_session = self.browser_sessions.active_sessions[profile_id]
                active_net = existing_session.network or self.network.runtimes.get(profile_id)
                iso = parse_isolation(profile.isolation_config) or isolation_from_legacy_row(
                    profile.browser_settings.model_dump(), profile.fingerprint, policy
                )
                return compile_session_payload(
                    profile,
                    iso,
                    policy,
                    network=active_net,
                    exit_ip=active_net.exit_ip if active_net else profile.last_exit_ip,
                    exit_country=(
                        getattr(active_net, "exit_country", None)
                        if active_net
                        else profile.last_exit_country
                    ),
                    exit_city=getattr(active_net, "exit_city", None) if active_net else None,
                    include_proxy_secrets=include_proxy_secrets,
                )

            network = await self.network.prepare(profile_id)
            if network is not None:
                await self.network.verify_exit(network, proxy_check.LAUNCH_TIMEOUT * 2)
                current_ip = network.exit_ip
                if current_ip:
                    node_check = network.node.get("check") or {}
                    try:
                        loc = await asyncio.to_thread(proxy_check.locate, current_ip)
                        current_country = loc.country
                        current_city = loc.city
                    except proxy_check.LocationUnavailable:
                        if node_check.get("exit_ip") == current_ip:
                            current_country = node_check.get("country_iso")
                            current_city = node_check.get("city")
                    if current_city is None and node_check.get("exit_ip") == current_ip:
                        current_city = node_check.get("city")
                    network.exit_country = current_country
                    network.exit_city = current_city

                    if profile.ip_drift_guard and not allow_ip_drift:
                        ip_changed = bool(
                            profile.last_exit_ip and profile.last_exit_ip != current_ip
                        )
                        country_changed = bool(
                            profile.last_exit_country
                            and current_country
                            and profile.last_exit_country.upper() != current_country.upper()
                        )
                        if ip_changed or country_changed:
                            raise ExitIpDriftError(
                                previous_ip=profile.last_exit_ip,
                                previous_country=profile.last_exit_country,
                                current_ip=current_ip,
                                current_country=current_country,
                                country_changed=country_changed,
                            )

            profile_dir = profile_data_path(self, profile)
            profile.storage_path = str(profile_dir)
            ensure_profile_runtime_dirs(profile_dir)

            parsed_iso = parse_isolation(profile.isolation_config)
            if parsed_iso is None:
                iso = isolation_from_legacy_row(
                    profile.browser_settings.model_dump(), profile.fingerprint, policy
                )
            else:
                iso = parsed_iso

            target_major = policy.recommended_chrome_major or DEFAULT_CHROME_MAJOR
            if profile.fingerprint:
                pin = normalize_pinned_fingerprint(
                    dict(profile.fingerprint), os_hint=iso.os, chrome_major=target_major
                )
                iso.pinned_fingerprint = pin
                iso.fingerprint_hash = compute_fingerprint_hash(pin)
                profile.fingerprint = pin
            elif not iso.pinned_fingerprint:
                hashes, preset_ids, signatures = await self._existing_isolation_sets(
                    exclude_profile_id=profile_id
                )
                allocated = allocate_isolation(
                    os_choice=iso.os,
                    tier=iso.tier,
                    existing_hashes=hashes,
                    policy=policy,
                    existing_preset_ids=preset_ids,
                    existing_signatures=signatures,
                    webrtc_policy=iso.webrtc_policy,
                    canvas_policy=iso.canvas_policy,
                    locale_mode=iso.locale_mode,
                    languages=iso.languages,
                    timezone_mode=iso.timezone_mode,
                    timezone=iso.timezone,
                    install_webrtc_guard=iso.install_webrtc_guard,
                    window_size=iso.window_size,
                )
                iso = allocated
                profile.fingerprint = dict(allocated.pinned_fingerprint)

            profile.isolation_config = iso.model_dump(mode="json")
            profile.fingerprint_hash = iso.fingerprint_hash or profile.fingerprint_hash

            payload = compile_session_payload(
                profile,
                iso,
                policy,
                network=network,
                exit_ip=current_ip,
                exit_country=current_country,
                exit_city=current_city,
                include_proxy_secrets=include_proxy_secrets,
            )

            profile.last_used = datetime.now()
            profile.updated_at = profile.last_used
            await self.storage.set_launch_pin(
                profile_id,
                profile.fingerprint,
                profile.last_used,
                isolation_config=profile.isolation_config,
                fingerprint_hash=profile.fingerprint_hash,
            )

            await self.storage.log_usage(
                UsageStats(
                    profile_id=profile_id,
                    action="launch_browser",
                    details={"engine": "embedded-chromium"},
                )
            )

            self.browser_sessions.start_heartbeat()
            await self.browser_sessions.launch(
                profile_id,
                payload,
                on_exit=self._on_browser_exit,
                network=network,
            )

            if network is not None and network.exit_ip:
                await self.storage.set_last_exit(
                    profile_id,
                    network.exit_ip,
                    current_country,
                    datetime.now(),
                )
            return payload
        except (Exception, asyncio.CancelledError):
            if network is not None:
                owner = self.browser_sessions.active_sessions.get(profile_id)
                if owner is None or owner.network is not network:
                    try:
                        await network.close()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"Failed to stop the network for {profile_id}: {exc}")
            if not self.browser_sessions.is_live(profile_id):
                await self._release_lease_quietly(profile_id)
            raise

    async def release_session(self, profile_id: str) -> dict[str, Any]:
        """Stop the profile's dedicated proxy runtime, untrack session, and release lease."""
        return await self.close_browser(profile_id)

    async def launch_browser(
        self,
        profile_id: str,
        headless: bool = False,
        window_size: str | None = None,
        allow_ip_drift: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Prepare a profile session and return a credential-safe launch summary."""
        already = self.browser_sessions.is_running(profile_id)
        payload = await self.prepare_session(
            profile_id,
            allow_ip_drift=allow_ip_drift,
            include_proxy_secrets=False,
        )
        session = self.browser_sessions.active_sessions.get(profile_id)
        if already:
            return {
                "status": "already_running",
                "profile_id": profile_id,
                "message": "Browser is already running for this profile",
                "process_id": session.process_id if session else None,
                "camoufox_options": payload,
            }
        return {
            "status": "launched",
            "profile_id": profile_id,
            "message": "Browser launched successfully",
            "process_id": session.process_id if session else None,
            "camoufox_options": payload,
        }

    def _cleanup_runtime_tmp(self, profile_id: str) -> None:
        tmp_dir = self.profiles_dir / f"profile_{profile_id}" / ".runtime" / "tmp"
        if tmp_dir.is_dir() and not tmp_dir.is_symlink():
            shutil.rmtree(tmp_dir, ignore_errors=True)

    async def _on_browser_exit(self, profile_id: str) -> None:
        """Record usage and clean per-profile temp files when a browser exits."""
        self._cleanup_runtime_tmp(profile_id)
        try:
            await self.storage.log_usage(
                UsageStats(profile_id=profile_id, action="close_browser", details={"forced": False})
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to log browser exit for {profile_id}: {exc}")

    async def close_browser(self, profile_id: str) -> dict[str, Any]:
        """Close the browser running for a profile, clean temp dir, and hand its lease back."""
        closed = await self.browser_sessions.close_and_release(profile_id)
        if not closed:
            return {
                "status": "not_running",
                "profile_id": profile_id,
                "message": "Browser is not running for this profile",
            }
        self._cleanup_runtime_tmp(profile_id)
        await self.storage.log_usage(
            UsageStats(profile_id=profile_id, action="close_browser", details={"forced": True})
        )
        return {
            "status": "closed",
            "profile_id": profile_id,
            "message": "Browser closed successfully",
        }

    async def get_active_browsers(self) -> list[dict[str, Any]]:
        return self.browser_sessions.list_active()

    async def close_all_browsers(self) -> dict[str, Any]:
        active_ids = list(self.browser_sessions.active_sessions)
        count = await self.browser_sessions.close_all()
        for pid in active_ids:
            self._cleanup_runtime_tmp(pid)
        return {
            "status": "completed",
            "closed_count": count,
            "errors": [],
            "message": f"Closed {count} browsers",
        }

    async def _release_lease_quietly(self, profile_id: str) -> None:
        try:
            await self.storage.release_lease(profile_id, self.lease_holder)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to release the lease on {profile_id}: {exc}")

    async def release_all_leases(self) -> None:
        try:
            holders = await self.storage.get_lease_holders()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not enumerate leases to release on shutdown: {exc}")
            return
        for entry in holders:
            if entry.get("locked_by") != self.lease_holder:
                continue
            if self.browser_sessions.is_live(entry["id"]):
                continue
            await self._release_lease_quietly(entry["id"])
