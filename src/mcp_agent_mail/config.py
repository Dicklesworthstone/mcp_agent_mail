"""Application configuration loaded via python-decouple with typed helpers."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final, Protocol, cast

from decouple import (
    Config as DecoupleConfig,
    RepositoryEmpty,
    RepositoryEnv,
)

_DOTENV_PATH: Final[Path] = Path(".env")


def _build_decouple_config() -> DecoupleConfig:
    # Gracefully handle missing .env (e.g., in CI/tests) by falling back to an empty repository.
    try:
        return DecoupleConfig(RepositoryEnv(str(_DOTENV_PATH)))
    except FileNotFoundError:
        # Fall back to an empty repository (reads only os.environ; all .env lookups use defaults)
        return DecoupleConfig(RepositoryEmpty())


_decouple_config: Final[DecoupleConfig] = _build_decouple_config()


@dataclass(slots=True, frozen=True)
class HttpSettings:
    """HTTP transport related settings."""

    host: str
    port: int
    path: str
    bearer_token: str | None
    # Basic per-IP limiter (legacy/simple)
    rate_limit_enabled: bool
    rate_limit_per_minute: int
    # Robust token-bucket limiter
    rate_limit_backend: str  # "memory" | "redis"
    rate_limit_tools_per_minute: int
    rate_limit_resources_per_minute: int
    rate_limit_redis_url: str
    # Optional bursts to control spikiness
    rate_limit_tools_burst: int
    rate_limit_resources_burst: int
    request_log_enabled: bool
    otel_enabled: bool
    otel_service_name: str
    otel_exporter_otlp_endpoint: str
    # JWT / RBAC
    jwt_enabled: bool
    jwt_algorithms: list[str]
    jwt_secret: str | None
    jwt_jwks_url: str | None
    jwt_audience: str | None
    jwt_issuer: str | None
    jwt_role_claim: str
    rbac_enabled: bool
    rbac_reader_roles: list[str]
    rbac_writer_roles: list[str]
    rbac_default_role: str
    rbac_readonly_tools: list[str]
    # Dev convenience
    allow_localhost_unauthenticated: bool


@dataclass(slots=True, frozen=True)
class DatabaseSettings:
    """Database connectivity settings."""

    url: str
    echo: bool
    pool_size: int | None
    max_overflow: int | None
    pool_timeout: int | None


@dataclass(slots=True, frozen=True)
class StorageSettings:
    """Filesystem/Git storage configuration."""

    root: str
    git_author_name: str
    git_author_email: str
    inline_image_max_bytes: int
    convert_images: bool
    keep_original_images: bool
    allow_absolute_attachment_paths: bool


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
class ToolFilterSettings:
    """Tool filtering configuration for context reduction.

    When enabled, only a subset of tools are exposed to the MCP client,
    reducing context overhead by up to ~70% for minimal workflows.

    Profiles:
        - "full": All tools exposed (default behavior)
        - "core": Essential tools only (identity, messaging, file_reservations)
        - "minimal": Bare minimum (identity, messaging basics)
        - "messaging": Messaging-focused subset
        - "custom": Use explicit include/exclude lists

    Example .env:
        TOOLS_FILTER_ENABLED=true
        TOOLS_FILTER_PROFILE=core

    Or for custom filtering:
        TOOLS_FILTER_ENABLED=true
        TOOLS_FILTER_PROFILE=custom
        TOOLS_FILTER_MODE=include
        TOOLS_FILTER_CLUSTERS=messaging,identity
        TOOLS_FILTER_TOOLS=send_message,fetch_inbox
    """

    enabled: bool
    profile: str  # "full" | "core" | "minimal" | "messaging" | "custom"
    mode: str  # "include" | "exclude"
    clusters: list[str]  # Cluster names to include/exclude
    tools: list[str]  # Specific tool names to include/exclude


@dataclass(slots=True, frozen=True)
class NotificationSettings:
    """Push notification configuration for local deployments.

    When enabled, touching a signal file notifies agents of new messages.
    Agents can watch these files using inotify/FSEvents/kqueue for instant
    notification without polling.

    Signal file location: {signals_dir}/{project_slug}/{agent_name}.signal
    Signal files contain JSON metadata for the most recent notification.

    Example .env:
        NOTIFICATIONS_ENABLED=true
        NOTIFICATIONS_SIGNALS_DIR=~/.mcp_agent_mail/signals
    """

    enabled: bool
    signals_dir: str  # Directory for signal files
    include_metadata: bool  # Include message metadata in signal file
    debounce_ms: int  # Debounce multiple signals within this window


@dataclass(slots=True, frozen=True)
class Settings:
    """Top-level application settings."""

    environment: str
    # Global gate for worktree-friendly behavior (opt-in; default False)
    worktrees_enabled: bool
    # Identity preferences (phase 1: read-only; behavior remains 'dir' unless features enabled)
    project_identity_mode: str  # "dir" | "git-remote" | "git-common-dir" | "git-toplevel"
    project_identity_remote: str  # e.g., "origin"
    http: HttpSettings
    database: DatabaseSettings
    storage: StorageSettings
    cors: CorsSettings
    llm: LlmSettings
    tool_filter: ToolFilterSettings
    notifications: NotificationSettings
    # Background maintenance toggles
    file_reservations_cleanup_enabled: bool
    file_reservations_cleanup_interval_seconds: int
    file_reservation_inactivity_seconds: int
    file_reservation_activity_grace_seconds: int
    # Server-side enforcement
    file_reservations_enforcement_enabled: bool
    # Ack TTL warnings
    ack_ttl_enabled: bool
    ack_ttl_seconds: int
    ack_ttl_scan_interval_seconds: int
    # Ack escalation
    ack_escalation_enabled: bool
    ack_escalation_mode: str  # "log" | "file_reservation"
    ack_escalation_claim_ttl_seconds: int
    ack_escalation_claim_exclusive: bool
    ack_escalation_claim_holder_name: str
    # Contacts/links
    contact_enforcement_enabled: bool
    contact_auto_ttl_seconds: int
    contact_auto_retry_enabled: bool
    # Logging
    log_rich_enabled: bool
    log_level: str
    log_include_trace: bool
    log_json_enabled: bool
    # Output formatting
    output_format_default: str
    toon_default_format: str
    toon_stats_enabled: bool
    toon_bin: str
    # Tools logging
    tools_log_enabled: bool
    # Query/latency instrumentation
    instrumentation_enabled: bool
    instrumentation_slow_query_ms: int
    # Tool metrics emission
    tool_metrics_emit_enabled: bool
    tool_metrics_emit_interval_seconds: int
    # Retention/quota reporting (non-destructive)
    retention_report_enabled: bool
    retention_report_interval_seconds: int
    retention_max_age_days: int
    quota_enabled: bool
    quota_attachments_limit_bytes: int
    quota_inbox_limit_count: int
    # Retention/project listing filters
    retention_ignore_project_patterns: list[str]
    # Agent identity naming policy
    # Values: "strict" | "coerce" | "always_auto"
    # - strict: reject invalid provided names (current hard-fail behavior)
    # - coerce: ignore invalid provided names and auto-generate a valid one (default)
    # - always_auto: ignore any provided name and always auto-generate
    agent_name_enforcement_mode: str
    # Messaging ergonomics
    # When true, attempt to register missing local recipients during send_message
    messaging_auto_register_recipients: bool
    # When true, attempt a contact handshake automatically if delivery is blocked
    messaging_auto_handshake_on_block: bool
    # Window-based agent identity
    # UUID read from MCP_AGENT_MAIL_WINDOW_ID env var (empty string = not set)
    window_identity_uuid: str
    # Days of inactivity before a window identity expires (default 30)
    window_identity_ttl_days: int


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


def _int_optional(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings."""
    environment = _decouple_config("APP_ENVIRONMENT", default="development")

    def _csv(name: str, default: str) -> list[str]:
        raw = _decouple_config(name, default=default)
        items = [part.strip() for part in raw.split(",") if part.strip()]
        return items

    http_settings = HttpSettings(
        host=_decouple_config("HTTP_HOST", default="127.0.0.1"),
        port=_int(_decouple_config("HTTP_PORT", default="8765"), default=8765),
        path=_decouple_config("HTTP_PATH", default="/api/"),
        bearer_token=_decouple_config("HTTP_BEARER_TOKEN", default="") or None,
        rate_limit_enabled=_bool(_decouple_config("HTTP_RATE_LIMIT_ENABLED", default="false"), default=False),
        rate_limit_per_minute=_int(_decouple_config("HTTP_RATE_LIMIT_PER_MINUTE", default="60"), default=60),
        rate_limit_backend=_decouple_config("HTTP_RATE_LIMIT_BACKEND", default="memory").lower(),
        rate_limit_tools_per_minute=_int(_decouple_config("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", default="60"), default=60),
        rate_limit_resources_per_minute=_int(_decouple_config("HTTP_RATE_LIMIT_RESOURCES_PER_MINUTE", default="120"), default=120),
        rate_limit_redis_url=_decouple_config("HTTP_RATE_LIMIT_REDIS_URL", default=""),
        rate_limit_tools_burst=_int(_decouple_config("HTTP_RATE_LIMIT_TOOLS_BURST", default="0"), default=0),
        rate_limit_resources_burst=_int(_decouple_config("HTTP_RATE_LIMIT_RESOURCES_BURST", default="0"), default=0),
        request_log_enabled=_bool(_decouple_config("HTTP_REQUEST_LOG_ENABLED", default="false"), default=False),
        otel_enabled=_bool(_decouple_config("HTTP_OTEL_ENABLED", default="false"), default=False),
        otel_service_name=_decouple_config("OTEL_SERVICE_NAME", default="mcp-agent-mail"),
        otel_exporter_otlp_endpoint=_decouple_config("OTEL_EXPORTER_OTLP_ENDPOINT", default=""),
        jwt_enabled=_bool(_decouple_config("HTTP_JWT_ENABLED", default="false"), default=False),
        jwt_algorithms=_csv("HTTP_JWT_ALGORITHMS", default="HS256"),
        jwt_secret=_decouple_config("HTTP_JWT_SECRET", default="") or None,
        jwt_jwks_url=_decouple_config("HTTP_JWT_JWKS_URL", default="") or None,
        jwt_audience=_decouple_config("HTTP_JWT_AUDIENCE", default="") or None,
        jwt_issuer=_decouple_config("HTTP_JWT_ISSUER", default="") or None,
        jwt_role_claim=_decouple_config("HTTP_JWT_ROLE_CLAIM", default="role") or "role",
        rbac_enabled=_bool(_decouple_config("HTTP_RBAC_ENABLED", default="true"), default=True),
        rbac_reader_roles=_csv("HTTP_RBAC_READER_ROLES", default="reader,read,ro"),
        rbac_writer_roles=_csv("HTTP_RBAC_WRITER_ROLES", default="writer,write,tools,rw"),
        rbac_default_role=_decouple_config("HTTP_RBAC_DEFAULT_ROLE", default="reader"),
        rbac_readonly_tools=_csv(
            "HTTP_RBAC_READONLY_TOOLS",
            default="health_check,fetch_inbox,whois,search_messages,summarize_thread",
        ),
        allow_localhost_unauthenticated=_bool(_decouple_config("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", default="true"), default=True),
    )

    database_settings = DatabaseSettings(
        url=_decouple_config("DATABASE_URL", default="sqlite+aiosqlite:///./storage.sqlite3"),
        echo=_bool(_decouple_config("DATABASE_ECHO", default="false"), default=False),
        pool_size=_int_optional(_decouple_config("DATABASE_POOL_SIZE", default="")),
        max_overflow=_int_optional(_decouple_config("DATABASE_MAX_OVERFLOW", default="")),
        pool_timeout=_int_optional(_decouple_config("DATABASE_POOL_TIMEOUT", default="")),
    )

    allow_abs_default = "true" if environment.lower() == "development" else "false"
    storage_settings = StorageSettings(
        # Default to a global, user-scoped archive directory outside the source tree
        root=_decouple_config("STORAGE_ROOT", default="~/.mcp_agent_mail_git_mailbox_repo"),
        git_author_name=_decouple_config("GIT_AUTHOR_NAME", default="mcp-agent"),
        git_author_email=_decouple_config("GIT_AUTHOR_EMAIL", default="mcp-agent@example.com"),
        inline_image_max_bytes=_int(_decouple_config("INLINE_IMAGE_MAX_BYTES", default=str(64 * 1024)), default=64 * 1024),
        convert_images=_bool(_decouple_config("CONVERT_IMAGES", default="true"), default=True),
        keep_original_images=_bool(_decouple_config("KEEP_ORIGINAL_IMAGES", default="false"), default=False),
        allow_absolute_attachment_paths=_bool(
            _decouple_config("ALLOW_ABSOLUTE_ATTACHMENT_PATHS", default=allow_abs_default),
            default=allow_abs_default == "true",
        ),
    )

    cors_default = "true" if environment.lower() == "development" else "false"
    cors_settings = CorsSettings(
        enabled=_bool(_decouple_config("HTTP_CORS_ENABLED", default=cors_default), default=cors_default == "true"),
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
        default_model=_decouple_config("LLM_DEFAULT_MODEL", default="gpt-4o-mini"),
        temperature=_float(_decouple_config("LLM_TEMPERATURE", default="0.2"), default=0.2),
        max_tokens=_int(_decouple_config("LLM_MAX_TOKENS", default="512"), default=512),
        cache_enabled=_bool(_decouple_config("LLM_CACHE_ENABLED", default="true"), default=True),
        cache_backend=_decouple_config("LLM_CACHE_BACKEND", default="memory"),
        cache_redis_url=_decouple_config("LLM_CACHE_REDIS_URL", default=""),
        cost_logging_enabled=_bool(_decouple_config("LLM_COST_LOGGING_ENABLED", default="true"), default=True),
    )

    def _tool_filter_profile(value: str) -> str:
        v = (value or "").strip().lower()
        if v in {"full", "core", "minimal", "messaging", "custom"}:
            return v
        return "full"

    def _tool_filter_mode(value: str) -> str:
        v = (value or "").strip().lower()
        if v in {"include", "exclude"}:
            return v
        return "include"

    tool_filter_settings = ToolFilterSettings(
        enabled=_bool(_decouple_config("TOOLS_FILTER_ENABLED", default="false"), default=False),
        profile=_tool_filter_profile(_decouple_config("TOOLS_FILTER_PROFILE", default="full")),
        mode=_tool_filter_mode(_decouple_config("TOOLS_FILTER_MODE", default="include")),
        clusters=_csv("TOOLS_FILTER_CLUSTERS", default=""),
        tools=_csv("TOOLS_FILTER_TOOLS", default=""),
    )

    notification_settings = NotificationSettings(
        enabled=_bool(_decouple_config("NOTIFICATIONS_ENABLED", default="false"), default=False),
        signals_dir=_decouple_config("NOTIFICATIONS_SIGNALS_DIR", default="~/.mcp_agent_mail/signals"),
        include_metadata=_bool(_decouple_config("NOTIFICATIONS_INCLUDE_METADATA", default="true"), default=True),
        debounce_ms=_int(_decouple_config("NOTIFICATIONS_DEBOUNCE_MS", default="100"), default=100),
    )

    def _agent_name_mode(value: str) -> str:
        v = (value or "").strip().lower()
        if v in {"strict", "coerce", "always_auto"}:
            return v
        return "coerce"

    return Settings(
        environment=environment,
        # Gate: allow either legacy WORKTREES_ENABLED or new GIT_IDENTITY_ENABLED to enable features
        worktrees_enabled=(
            _bool(_decouple_config("WORKTREES_ENABLED", default="false"), default=False)
            or _bool(_decouple_config("GIT_IDENTITY_ENABLED", default="false"), default=False)
        ),
        project_identity_mode=_decouple_config("PROJECT_IDENTITY_MODE", default="dir").strip().lower(),
        project_identity_remote=_decouple_config("PROJECT_IDENTITY_REMOTE", default="origin").strip(),
        http=http_settings,
        database=database_settings,
        storage=storage_settings,
        cors=cors_settings,
        llm=llm_settings,
        tool_filter=tool_filter_settings,
        notifications=notification_settings,
        file_reservations_cleanup_enabled=_bool(_decouple_config("FILE_RESERVATIONS_CLEANUP_ENABLED", default="false"), default=False),
        file_reservations_cleanup_interval_seconds=_int(_decouple_config("FILE_RESERVATIONS_CLEANUP_INTERVAL_SECONDS", default="60"), default=60),
        file_reservation_inactivity_seconds=_int(_decouple_config("FILE_RESERVATION_INACTIVITY_SECONDS", default="1800"), default=1800),
        file_reservation_activity_grace_seconds=_int(_decouple_config("FILE_RESERVATION_ACTIVITY_GRACE_SECONDS", default="900"), default=900),
        file_reservations_enforcement_enabled=_bool(_decouple_config("FILE_RESERVATIONS_ENFORCEMENT_ENABLED", default="true"), default=True),
        ack_ttl_enabled=_bool(_decouple_config("ACK_TTL_ENABLED", default="false"), default=False),
        ack_ttl_seconds=_int(_decouple_config("ACK_TTL_SECONDS", default="1800"), default=1800),
        ack_ttl_scan_interval_seconds=_int(_decouple_config("ACK_TTL_SCAN_INTERVAL_SECONDS", default="60"), default=60),
        ack_escalation_enabled=_bool(_decouple_config("ACK_ESCALATION_ENABLED", default="false"), default=False),
        ack_escalation_mode=_decouple_config("ACK_ESCALATION_MODE", default="log"),
        ack_escalation_claim_ttl_seconds=_int(_decouple_config("ACK_ESCALATION_CLAIM_TTL_SECONDS", default="3600"), default=3600),
        ack_escalation_claim_exclusive=_bool(_decouple_config("ACK_ESCALATION_CLAIM_EXCLUSIVE", default="false"), default=False),
        ack_escalation_claim_holder_name=_decouple_config("ACK_ESCALATION_CLAIM_HOLDER_NAME", default=""),
        tools_log_enabled=_bool(_decouple_config("TOOLS_LOG_ENABLED", default="true"), default=True),
        instrumentation_enabled=_bool(_decouple_config("INSTRUMENTATION_ENABLED", default="false"), default=False),
        instrumentation_slow_query_ms=_int(_decouple_config("INSTRUMENTATION_SLOW_QUERY_MS", default="250"), default=250),
        log_rich_enabled=_bool(_decouple_config("LOG_RICH_ENABLED", default="true"), default=True),
        log_level=_decouple_config("LOG_LEVEL", default="INFO"),
        log_include_trace=_bool(_decouple_config("LOG_INCLUDE_TRACE", default="false"), default=False),
        contact_enforcement_enabled=_bool(_decouple_config("CONTACT_ENFORCEMENT_ENABLED", default="true"), default=True),
        contact_auto_ttl_seconds=_int(_decouple_config("CONTACT_AUTO_TTL_SECONDS", default="86400"), default=86400),
        contact_auto_retry_enabled=_bool(_decouple_config("CONTACT_AUTO_RETRY_ENABLED", default="true"), default=True),
        log_json_enabled=_bool(_decouple_config("LOG_JSON_ENABLED", default="false"), default=False),
        output_format_default=_decouple_config("MCP_AGENT_MAIL_OUTPUT_FORMAT", default="").strip().lower(),
        toon_default_format=_decouple_config("TOON_DEFAULT_FORMAT", default="").strip().lower(),
        toon_stats_enabled=_bool(_decouple_config("TOON_STATS", default="false"), default=False),
        toon_bin=(
            _decouple_config("TOON_TRU_BIN", default="").strip()
            or _decouple_config("TOON_BIN", default="").strip()
            or "tru"
        ),
        tool_metrics_emit_enabled=_bool(_decouple_config("TOOL_METRICS_EMIT_ENABLED", default="false"), default=False),
        tool_metrics_emit_interval_seconds=_int(_decouple_config("TOOL_METRICS_EMIT_INTERVAL_SECONDS", default="60"), default=60),
        retention_report_enabled=_bool(_decouple_config("RETENTION_REPORT_ENABLED", default="false"), default=False),
        retention_report_interval_seconds=_int(_decouple_config("RETENTION_REPORT_INTERVAL_SECONDS", default="3600"), default=3600),
        retention_max_age_days=_int(_decouple_config("RETENTION_MAX_AGE_DAYS", default="180"), default=180),
        quota_enabled=_bool(_decouple_config("QUOTA_ENABLED", default="false"), default=False),
        quota_attachments_limit_bytes=_int(_decouple_config("QUOTA_ATTACHMENTS_LIMIT_BYTES", default="0"), default=0),
        quota_inbox_limit_count=_int(_decouple_config("QUOTA_INBOX_LIMIT_COUNT", default="0"), default=0),
        retention_ignore_project_patterns=_csv(
            "RETENTION_IGNORE_PROJECT_PATTERNS",
            default="demo,test*,testproj*,testproject,backendproj*,frontendproj*",
        ),
        agent_name_enforcement_mode=_agent_name_mode(_decouple_config("AGENT_NAME_ENFORCEMENT_MODE", default="coerce")),
        messaging_auto_register_recipients=_bool(_decouple_config("MESSAGING_AUTO_REGISTER_RECIPIENTS", default="true"), default=True),
        messaging_auto_handshake_on_block=_bool(_decouple_config("MESSAGING_AUTO_HANDSHAKE_ON_BLOCK", default="true"), default=True),
        window_identity_uuid=_decouple_config("MCP_AGENT_MAIL_WINDOW_ID", default="").strip(),
        window_identity_ttl_days=_int(_decouple_config("MCP_AGENT_MAIL_WINDOW_TTL_DAYS", default="30"), default=30),
    )


class _CacheClearable(Protocol):
    def cache_clear(self) -> None: ...


def clear_settings_cache() -> None:
    """Clear the lru_cache for get_settings in a type-checker-friendly way."""
    cache_clear = getattr(cast(_CacheClearable, get_settings), "cache_clear", None)
    if callable(cache_clear):
        cache_clear()
