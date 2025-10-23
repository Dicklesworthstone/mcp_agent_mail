"""Application configuration loaded via python-decouple with typed helpers."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

from decouple import Config as DecoupleConfig, RepositoryEnv

_DOTENV_PATH: Final[Path] = Path(".env")
_decouple_config: Final[DecoupleConfig] = DecoupleConfig(RepositoryEnv(str(_DOTENV_PATH)))


@dataclass(slots=True, frozen=True)
class HttpSettings:
    """HTTP transport related settings."""

    host: str
    port: int
    path: str
    bearer_token: str | None


@dataclass(slots=True, frozen=True)
class DatabaseSettings:
    """Database connectivity settings."""

    url: str
    echo: bool


@dataclass(slots=True, frozen=True)
class StorageSettings:
    """Filesystem/Git storage configuration."""

    root: str
    git_author_name: str
    git_author_email: str
    inline_image_max_bytes: int
    convert_images: bool


@dataclass(slots=True, frozen=True)
class Settings:
    """Top-level application settings."""

    environment: str
    http: HttpSettings
    database: DatabaseSettings
    storage: StorageSettings


def _bool(value: str, *, default: bool) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "t", "yes", "y"}:
        return True
    if normalized in {"0", "false", "f", "no", "n"}:
        return False
    return default


def _int(value: str, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings."""
    environment = _decouple_config("APP_ENVIRONMENT", default="development")

    http_settings = HttpSettings(
        host=_decouple_config("HTTP_HOST", default="127.0.0.1"),
        port=_int(_decouple_config("HTTP_PORT", default="8765"), default=8765),
        path=_decouple_config("HTTP_PATH", default="/mcp/"),
        bearer_token=_decouple_config("HTTP_BEARER_TOKEN", default="") or None,
    )

    database_settings = DatabaseSettings(
        url=_decouple_config("DATABASE_URL", default="sqlite+aiosqlite:///./storage.sqlite3"),
        echo=_bool(_decouple_config("DATABASE_ECHO", default="false"), default=False),
    )

    storage_settings = StorageSettings(
        root=_decouple_config("STORAGE_ROOT", default="./storage"),
        git_author_name=_decouple_config("GIT_AUTHOR_NAME", default="mcp-agent"),
        git_author_email=_decouple_config("GIT_AUTHOR_EMAIL", default="mcp-agent@example.com"),
        inline_image_max_bytes=_int(_decouple_config("INLINE_IMAGE_MAX_BYTES", default=str(64 * 1024)), default=64 * 1024),
        convert_images=_bool(_decouple_config("CONVERT_IMAGES", default="true"), default=True),
    )

    return Settings(environment=environment, http=http_settings, database=database_settings, storage=storage_settings)
