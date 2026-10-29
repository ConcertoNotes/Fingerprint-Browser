"""Instance-wide network settings. Provider keys are sealed and write-only."""

from __future__ import annotations

import json
from typing import Any

from .network_catalog import now, seal, unseal

DEFAULTS: dict[str, Any] = {
    "mihomo_path": None,
    "probe_concurrency": 16,
    "probe_timeout_seconds": 10,
    "intel_cache_hours": 24,
    "intel_ipinfo_enabled": True,
    "intel_ippure_enabled": True,
    "intel_scamalytics_enabled": False,
    "mask_exit_ips": False,
}
SECRETS = ("intel_ipinfo_token", "intel_scamalytics_url", "intel_scamalytics_key")
LIMITS = {
    "probe_concurrency": (1, 32),
    "probe_timeout_seconds": (3, 60),
    "intel_cache_hours": (1, 720),
}


class NetworkSettings:
    def __init__(self, connection: Any):
        self.connection = connection

    def _raw(self) -> dict[str, str]:
        return {
            row[0]: row[1]
            for row in self.connection.execute("SELECT key, value FROM network_settings")
        }

    def values(self) -> dict[str, Any]:
        raw = self._raw()
        result = dict(DEFAULTS)
        for key in DEFAULTS:
            if key in raw:
                result[key] = json.loads(raw[key])
        return result

    def secret(self, key: str) -> str | None:
        value = self._raw().get(key)
        return unseal(json.loads(value)) if value else None

    def public(self) -> dict[str, Any]:
        raw = self._raw()
        result = self.values()
        for key in SECRETS:
            result[f"has_{key}"] = key in raw
        return result

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        rows: list[tuple[str, str | None]] = []
        for key, value in changes.items():
            if key in SECRETS:
                if value in (None, ""):
                    rows.append((key, None))
                else:
                    text = str(value).strip()
                    if key == "intel_scamalytics_url" and not text.startswith("https://"):
                        raise ValueError("The Scamalytics API URL must use HTTPS")
                    if len(text) > 512:
                        raise ValueError("Value is too long")
                    rows.append((key, json.dumps(seal(text))))
                continue
            if key not in DEFAULTS:
                raise ValueError(f"Unknown setting {key}")
            if key in LIMITS:
                low, high = LIMITS[key]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or not low <= value <= high
                ):
                    raise ValueError(f"{key} must be between {low} and {high}")
            elif key == "mihomo_path":
                if value is not None and (not isinstance(value, str) or len(value) > 1024):
                    raise ValueError("Invalid Mihomo path")
                value = value.strip() or None if isinstance(value, str) else None
            elif not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false")
            rows.append((key, json.dumps(value)))
        stamp = now()
        with self.connection:
            for key, value in rows:
                if value is None:
                    self.connection.execute("DELETE FROM network_settings WHERE key = ?", (key,))
                else:
                    self.connection.execute(
                        "INSERT INTO network_settings (key, value, updated_at) VALUES (?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                        "updated_at=excluded.updated_at",
                        (key, value, stamp),
                    )
        return self.public()
