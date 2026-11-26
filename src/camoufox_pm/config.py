"""Application settings loaded from environment variables (prefix ``CPM_``)."""

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Values come from the environment or a ``.env`` file."""

    model_config = SettingsConfigDict(env_prefix="CPM_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]
    api_key: str | None = None
    desktop_token: str | None = None
    db_path: str = "data/profiles.db"
    secret_key: str | None = None
    webui_dir: str | None = None
    # Cloud delivery service configuration (optional; can also be managed via UI).
    cloud_url: str | None = None
    cloud_token: str | None = None
    cloud_allow_local: bool = False
    # Distribution edition & customer branding controls.
    client_edition: Literal["standard", "customer"] = "standard"
    brand_name: str = "棱镜浏览器"
    require_activation: bool = False
    # How long a profile lease survives without a heartbeat, in seconds. The
    # heartbeat renews every 30s while a browser is open.
    lease_ttl: int = 120

    @field_validator("client_edition", mode="before")
    @classmethod
    def _normalize_client_edition(cls, value: object) -> object:
        if isinstance(value, str):
            cleaned = value.strip().lower()
            return cleaned or "standard"
        return value

    @field_validator("lease_ttl")
    @classmethod
    def _lease_ttl_outlives_the_heartbeat(cls, value: int) -> int:
        if value < 60:
            raise ValueError(
                "CPM_LEASE_TTL must be at least 60 seconds (the heartbeat renews every 30s)"
            )
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance."""
    return Settings()


def desktop_data_dir() -> Path:
    """A stable, writable location; never relative to the app bundle or cwd."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Camoufox Profile Manager"
    if sys.platform == "win32":
        return (
            Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
            / "Camoufox Profile Manager"
        )
    return (
        Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
        / "camoufox-profile-manager"
    )


def bootstrap_desktop() -> None:
    """Configure packaged desktop defaults without changing CLI installations.

    Explicit environment/.env configuration wins. Keep the generated key beside
    the database across upgrades; never generate a replacement for a lost key
    on an existing database.
    """
    from cryptography.fernet import Fernet

    configured = Settings()
    if "db_path" not in configured.model_fields_set:
        os.environ["CPM_DB_PATH"] = str(desktop_data_dir() / "profiles.db")
    get_settings.cache_clear()
    settings = get_settings()
    data_dir = Path(settings.db_path).expanduser().resolve().parent
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not settings.secret_key:
        key_path = data_dir / "secret.key"
        if not key_path.exists() and not Path(settings.db_path).exists():
            try:
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(Fernet.generate_key())
        if key_path.exists():
            key = key_path.read_text().strip()
            Fernet(key.encode())  # Fail clearly instead of starting with a broken key.
            os.environ["CPM_SECRET_KEY"] = key
    get_settings.cache_clear()
