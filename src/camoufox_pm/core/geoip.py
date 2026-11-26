"""Offline MaxMind GeoIP database lookup powered directly by ``maxminddb``.

Locates the local ``geoip.mmdb`` file under ``<data_dir>/geoip/geoip.mmdb``
(extracted on first launch from the bundled ``runtime/geoip/geoip.mmdb.zst``
archive), with optional environment override (``CPM_GEOIP_DB_PATH``) and
development fallbacks to standard user cache paths.
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import maxminddb
from loguru import logger


@dataclass(frozen=True)
class GeoRecord:
    """Offline geolocation lookup result for an IP address."""

    ip: str
    country: str | None = None
    city: str | None = None
    region: str | None = None
    timezone: str | None = None
    latitude: float | None = None
    longitude: float | None = None


_reader_lock = threading.Lock()
_cached_reader: maxminddb.Reader | None = None
_cached_key: tuple[Path, int, int] | None = None


def _default_data_dir() -> Path:
    from camoufox_pm.config import get_settings

    return Path(get_settings().db_path).expanduser().resolve().parent


def target_geoip_path(data_dir: Path | None = None) -> Path:
    """Return the canonical extraction target ``<data_dir>/geoip/geoip.mmdb``."""
    base = (data_dir or _default_data_dir()).expanduser().resolve()
    return base / "geoip" / "geoip.mmdb"


def _dev_fallback_paths() -> list[Path]:
    """Locate any pre-existing local mmdb cache in development environments."""
    candidates: list[Path] = []
    home = Path.home()
    roots = [
        home / "Library" / "Caches" / "prism-browser" / "geoip",
        home / ".cache" / "prism-browser" / "geoip",
        home / "Library" / "Caches" / "camoufox" / "geoip" / "mmdb",
        home / ".cache" / "camoufox" / "geoip" / "mmdb",
    ]
    if sys.platform == "win32":
        local_app = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
        roots.extend(
            [
                local_app / "prism-browser" / "geoip",
                local_app / "camoufox" / "camoufox" / "Cache" / "geoip" / "mmdb",
            ]
        )
    for root in roots:
        if not root.is_dir():
            continue
        direct = root / "geoip.mmdb"
        if direct.is_file():
            candidates.append(direct)
        for item in sorted(root.glob("*.mmdb")):
            if item.is_file() and item not in candidates:
                candidates.append(item)
    return candidates


def geoip_db_path(data_dir: Path | None = None) -> Path | None:
    """Return the first valid ``.mmdb`` database path on disk, or ``None``."""
    override = os.environ.get("CPM_GEOIP_DB_PATH")
    if override:
        explicit = Path(override).expanduser()
        if explicit.is_file() and explicit.stat().st_size > 1024:
            return explicit.resolve()

    primary = target_geoip_path(data_dir)
    if primary.is_file() and primary.stat().st_size > 1024:
        return primary.resolve()

    from . import bundled_runtime

    runtime_dir = bundled_runtime.bundled_runtime_dir()
    if runtime_dir is not None:
        raw_bundled = runtime_dir / "geoip" / "geoip.mmdb"
        if raw_bundled.is_file() and raw_bundled.stat().st_size > 1024:
            return raw_bundled.resolve()

    for candidate in _dev_fallback_paths():
        try:
            if candidate.is_file() and candidate.stat().st_size > 1024:
                return candidate.resolve()
        except OSError:
            continue
    return None


def geoip_ready(data_dir: Path | None = None) -> bool:
    """Return True when an offline GeoIP MMDB database is available on disk."""
    return geoip_db_path(data_dir) is not None


def close_reader() -> None:
    """Close any cached ``maxminddb.Reader`` handle (safe before file replacement)."""
    global _cached_reader, _cached_key
    with _reader_lock:
        if _cached_reader is not None:
            try:
                _cached_reader.close()
            except Exception:  # noqa: BLE001
                pass
            _cached_reader = None
            _cached_key = None


def _get_reader(path: Path) -> maxminddb.Reader:
    global _cached_reader, _cached_key
    stat_info = path.stat()
    key = (path, stat_info.st_mtime_ns, stat_info.st_size)
    with _reader_lock:
        if _cached_reader is not None and _cached_key == key:
            return _cached_reader
        if _cached_reader is not None:
            try:
                _cached_reader.close()
            except Exception:  # noqa: BLE001
                pass
        _cached_reader = maxminddb.open_database(str(path))
        _cached_key = key
        return _cached_reader


def _pick_name(node: Any) -> str | None:
    if isinstance(node, str) and node.strip():
        return node.strip()
    if isinstance(node, dict):
        names = node.get("names")
        if isinstance(names, dict):
            for lang in ("en", "en-US"):
                val = names.get(lang)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            for val in names.values():
                if isinstance(val, str) and val.strip():
                    return val.strip()
    return None


def _extract_record(ip: str, raw: dict[str, Any]) -> GeoRecord:
    country: str | None = None
    country_obj = raw.get("country") or raw.get("registered_country")
    if isinstance(country_obj, dict):
        code = country_obj.get("iso_code")
        if isinstance(code, str) and code.strip():
            country = code.strip().upper()
    elif isinstance(raw.get("country_code"), str) and raw["country_code"].strip():
        country = raw["country_code"].strip().upper()

    city = _pick_name(raw.get("city"))
    region: str | None = None
    subdivisions = raw.get("subdivisions")
    if isinstance(subdivisions, list) and subdivisions:
        region = _pick_name(subdivisions[0])
    elif isinstance(raw.get("region"), str):
        region = raw["region"].strip() or None

    raw_loc = raw.get("location")
    loc: dict[str, Any] = raw_loc if isinstance(raw_loc, dict) else {}
    tz_raw = loc.get("time_zone") or raw.get("timezone")
    tz = tz_raw.strip() if isinstance(tz_raw, str) and tz_raw.strip() else None

    lat_raw = loc.get("latitude") if "latitude" in loc else raw.get("latitude")
    lon_raw = loc.get("longitude") if "longitude" in loc else raw.get("longitude")
    lat = (
        float(lat_raw)
        if isinstance(lat_raw, (int, float)) and not isinstance(lat_raw, bool)
        else None
    )
    lon = (
        float(lon_raw)
        if isinstance(lon_raw, (int, float)) and not isinstance(lon_raw, bool)
        else None
    )

    return GeoRecord(
        ip=ip,
        country=country,
        city=city,
        region=region,
        timezone=tz,
        latitude=lat,
        longitude=lon,
    )


def lookup(ip: str, *, data_dir: Path | None = None) -> GeoRecord | None:
    """Resolve an IPv4 or IPv6 address against the local MaxMind database."""
    path = geoip_db_path(data_dir)
    if path is None:
        return None
    try:
        reader = _get_reader(path)
        raw = reader.get(ip)
    except (ValueError, OSError, maxminddb.InvalidDatabaseError) as exc:
        logger.debug(f"Offline GeoIP lookup failed for {ip}: {exc}")
        return None
    if not isinstance(raw, dict):
        return None
    return _extract_record(ip, raw)
