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
    rate_limit_enabled: bool
    rate_limit_per_minute: int
    request_log_enabled: bool
    otel_enabled: bool
    otel_service_name: str
    otel_exporter_otlp_endpoint: str


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
    keep_original_images: bool


@dataclass(slots=True, frozen=True)
class CorsSettings:
    """CORS configuration for the HTTP app."""

    enabled: bool
    origins: list[str]
    allow_credentials: bool
    allow_methods: list[str]
    allow_headers: list[str]


@dataclass(slots=True, frozen=True)
class LlmSettings:
    """LiteLLM-related settings and defaults."""

    enabled: bool
    default_model: str
    temperature: float
    max_tokens: int
    cache_enabled: bool
    cache_backend: str  # "memory" | "redis"
    cache_redis_url: str
    cost_logging_enabled: bool


@dataclass(slots=True, frozen=True)
class Settings:
    """Top-level application settings."""

    environment: str
    http: HttpSettings
    database: DatabaseSettings
    storage: StorageSettings
    cors: CorsSettings
    llm: LlmSettings
    # Background maintenance toggles
    claims_cleanup_enabled: bool
    claims_cleanup_interval_seconds: int
    # Server-side enforcement
    claims_enforcement_enabled: bool
    # Ack TTL warnings
    ack_ttl_enabled: bool
    ack_ttl_seconds: int
    ack_ttl_scan_interval_seconds: int
    # Ack escalation
    ack_escalation_enabled: bool
    ack_escalation_mode: str  # "log" | "claim"
    ack_escalation_claim_ttl_seconds: int
    ack_escalation_claim_exclusive: bool
    ack_escalation_claim_holder_name: str
    # Logging
    log_rich_enabled: bool
    log_level: str
    log_include_trace: bool
    # Tools logging
    tools_log_enabled: bool


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
        rate_limit_enabled=_bool(_decouple_config("HTTP_RATE_LIMIT_ENABLED", default="false"), default=False),
        rate_limit_per_minute=_int(_decouple_config("HTTP_RATE_LIMIT_PER_MINUTE", default="60"), default=60),
        request_log_enabled=_bool(_decouple_config("HTTP_REQUEST_LOG_ENABLED", default="false"), default=False),
        otel_enabled=_bool(_decouple_config("HTTP_OTEL_ENABLED", default="false"), default=False),
        otel_service_name=_decouple_config("OTEL_SERVICE_NAME", default="mcp-agent-mail"),
        otel_exporter_otlp_endpoint=_decouple_config("OTEL_EXPORTER_OTLP_ENDPOINT", default=""),
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
        keep_original_images=_bool(_decouple_config("KEEP_ORIGINAL_IMAGES", default="false"), default=False),
    )

    def _csv(name: str, default: str) -> list[str]:
        raw = _decouple_config(name, default=default)
        items = [part.strip() for part in raw.split(",") if part.strip()]
        return items

    cors_settings = CorsSettings(
        enabled=_bool(_decouple_config("HTTP_CORS_ENABLED", default="false"), default=False),
        origins=_csv("HTTP_CORS_ORIGINS", default=""),
        allow_credentials=_bool(_decouple_config("HTTP_CORS_ALLOW_CREDENTIALS", default="false"), default=False),
        allow_methods=_csv("HTTP_CORS_ALLOW_METHODS", default="*"),
        allow_headers=_csv("HTTP_CORS_ALLOW_HEADERS", default="*"),
    )

    def _float(value: str, *, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    llm_settings = LlmSettings(
        enabled=_bool(_decouple_config("LLM_ENABLED", default="true"), default=True),
        default_model=_decouple_config("LLM_DEFAULT_MODEL", default="gpt-5-mini"),
        temperature=_float(_decouple_config("LLM_TEMPERATURE", default="0.2"), default=0.2),
        max_tokens=_int(_decouple_config("LLM_MAX_TOKENS", default="512"), default=512),
        cache_enabled=_bool(_decouple_config("LLM_CACHE_ENABLED", default="true"), default=True),
        cache_backend=_decouple_config("LLM_CACHE_BACKEND", default="memory"),
        cache_redis_url=_decouple_config("LLM_CACHE_REDIS_URL", default=""),
        cost_logging_enabled=_bool(_decouple_config("LLM_COST_LOGGING_ENABLED", default="true"), default=True),
    )

    return Settings(
        environment=environment,
        http=http_settings,
        database=database_settings,
        storage=storage_settings,
        cors=cors_settings,
        llm=llm_settings,
        claims_cleanup_enabled=_bool(_decouple_config("CLAIMS_CLEANUP_ENABLED", default="false"), default=False),
        claims_cleanup_interval_seconds=_int(_decouple_config("CLAIMS_CLEANUP_INTERVAL_SECONDS", default="60"), default=60),
        claims_enforcement_enabled=_bool(_decouple_config("CLAIMS_ENFORCEMENT_ENABLED", default="true"), default=True),
        ack_ttl_enabled=_bool(_decouple_config("ACK_TTL_ENABLED", default="false"), default=False),
        ack_ttl_seconds=_int(_decouple_config("ACK_TTL_SECONDS", default="1800"), default=1800),
        ack_ttl_scan_interval_seconds=_int(_decouple_config("ACK_TTL_SCAN_INTERVAL_SECONDS", default="60"), default=60),
        ack_escalation_enabled=_bool(_decouple_config("ACK_ESCALATION_ENABLED", default="false"), default=False),
        ack_escalation_mode=_decouple_config("ACK_ESCALATION_MODE", default="log"),
        ack_escalation_claim_ttl_seconds=_int(_decouple_config("ACK_ESCALATION_CLAIM_TTL_SECONDS", default="3600"), default=3600),
        ack_escalation_claim_exclusive=_bool(_decouple_config("ACK_ESCALATION_CLAIM_EXCLUSIVE", default="false"), default=False),
        ack_escalation_claim_holder_name=_decouple_config("ACK_ESCALATION_CLAIM_HOLDER_NAME", default=""),
        tools_log_enabled=_bool(_decouple_config("TOOLS_LOG_ENABLED", default="false"), default=False),
        log_rich_enabled=_bool(_decouple_config("LOG_RICH_ENABLED", default="true"), default=True),
        log_level=_decouple_config("LOG_LEVEL", default="INFO"),
        log_include_trace=_bool(_decouple_config("LOG_INCLUDE_TRACE", default="false"), default=False),
    )
