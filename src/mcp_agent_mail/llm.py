"""LiteLLM integration: router, caching, and cost tracking.

Centralizes LLM usage behind a minimal async helper. Providers + API keys
are configured via environment variables; configuration toggles come from
python-decouple in `config.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import litellm
import structlog
from decouple import Config as DecoupleConfig, RepositoryEnv
from litellm.types.caching import LiteLLMCacheType

from .config import get_settings

_router: Optional[Any] = None
_init_lock = asyncio.Lock()
_logger = structlog.get_logger(__name__)


@dataclass(slots=True, frozen=True)
class LlmOutput:
    content: str
    model: str
    provider: str | None
    estimated_cost_usd: float | None = None


def _existing_callbacks() -> list[Any]:
    callbacks = getattr(litellm, "success_callback", []) or []
    return list(callbacks)


def _setup_callbacks() -> None:
    settings = get_settings()
    if not settings.llm.cost_logging_enabled:
        return

    def _on_success(kwargs: dict[str, Any], completion_response: Any, start_time: float, end_time: float) -> None:
        try:
            cost = float(kwargs.get("response_cost", 0.0) or 0.0)
            model = str(kwargs.get("model", ""))
            if cost > 0:
                # Prefer rich terminal output when enabled; fallback to structlog
                if settings.log_rich_enabled:
                    try:
                        import importlib as _imp
                        _rc = _imp.import_module("rich.console")
                        _rp = _imp.import_module("rich.panel")
                        _rt = _imp.import_module("rich.text")
                        Console = _rc.Console
                        Panel = _rp.Panel
                        Text = _rt.Text

                        body = Text.assemble(
                            ("model: ", "cyan"), (model, "white"), "\n",
                            ("cost: ", "cyan"), (f"${cost:.6f}", "bold green"),
                        )
                        Console().print(Panel(body, title="llm: cost", border_style="magenta"))
                    except Exception:
                        _logger.info("litellm.cost", model=model, cost_usd=cost)
                else:
                    _logger.info("litellm.cost", model=model, cost_usd=cost)
        except Exception:
            # Never let logging issues impact normal flow
            pass

    if _on_success not in _existing_callbacks():
        callbacks: list[Callable[..., Any]] = [*_existing_callbacks(), _on_success]
        # Attribute exists on modern LiteLLM; fall back safely if absent
        with contextlib.suppress(Exception):
            litellm.success_callback = callbacks


async def _ensure_initialized() -> None:
    global _router
    if _router is not None:
        return
    async with _init_lock:
        if _router is not None:
            return
        settings = get_settings()

        # Bridge provider keys from .env to environment for LiteLLM
        try:
            _bridge_provider_env()
        except Exception:
            _logger.debug("litellm.env.bridge_failed")

        # Enable cache globally (in-memory or Redis) using LiteLLM's API
        if settings.llm.cache_enabled:
            from contextlib import suppress

            with suppress(Exception):
                backend = (getattr(settings.llm, "cache_backend", "local") or "local").lower()
                if backend == "redis" and getattr(settings.llm, "cache_redis_url", ""):
                    parsed = urlparse(settings.llm.cache_redis_url)
                    host = parsed.hostname or "localhost"
                    port = str(parsed.port or "6379")
                    pwd = parsed.password or None
                    try:
                        # Fast DNS sanity check to avoid noisy connection errors on placeholders
                        socket.gethostbyname(host)
                        litellm.enable_cache(
                            type=LiteLLMCacheType.REDIS,
                            host=str(host),
                            port=str(port),
                            password=pwd,
                        )
                    except Exception:
                        _logger.info("litellm.cache.redis_unavailable_fallback_local", host=host, port=port)
                        litellm.enable_cache(type=LiteLLMCacheType.LOCAL)
                else:
                    litellm.enable_cache(type=LiteLLMCacheType.LOCAL)

        _setup_callbacks()

        # Skip Router initialization - we use direct litellm.completion() calls.
        # Router is designed for load balancing across multiple deployments with a model_list,
        # but we're just using single API keys, so direct completion is simpler and works fine.
        _router = None


def _choose_best_available_model(preferred: str) -> str:
    """Select a provider/model that is likely to be available based on configured API keys.

    If a concrete provider is not implied by the preferred name (e.g. 'gpt-5-mini'),
    pick a small, low-cost model for whatever provider keys are available.
    """
    env = os.environ

    # If the string already looks provider-qualified, leave it as-is
    if "/" in preferred or ":" in preferred:
        return preferred

    # Alias unsupported placeholder to sensible defaults by provider key presence
    if env.get("OPENAI_API_KEY"):
        return "gpt-5-mini"
    if env.get("GOOGLE_API_KEY"):
        return "gemini-1.5-flash"
    if env.get("ANTHROPIC_API_KEY"):
        return "claude-3-haiku-20240307"
    # Other providers as last resorts (strings accepted by LiteLLM)
    if env.get("GROQ_API_KEY"):
        return "groq/llama-3.1-70b-versatile"
    if env.get("DEEPSEEK_API_KEY"):
        return "deepseek/deepseek-chat"
    if env.get("XAI_API_KEY"):
        return "xai/grok-2-mini"
    if env.get("OPENROUTER_API_KEY"):
        # OpenRouter requires qualified model ids; choose a widely available one
        return "openrouter/openai/gpt-5-mini"
    return preferred


def _resolve_model_alias(name: str) -> str:
    """Map known placeholder or project-specific names to concrete provider models."""
    normalized = (name or "").strip().lower()
    if normalized in {"gpt-5-mini", "gpt5-mini", "gpt-5m"}:
        return _choose_best_available_model(normalized)
    return name

async def complete_system_user(system: str, user: str, *, model: Optional[str] = None, temperature: Optional[float] = None, max_tokens: Optional[int] = None) -> LlmOutput:
    """Chat completion helper returning content.

    Falls back to litellm.completion if Router isn't available.
    """
    global _router
    await _ensure_initialized()
    settings = get_settings()
    use_model = model or settings.llm.default_model
    use_model = _resolve_model_alias(use_model)
    temp = settings.llm.temperature if temperature is None else float(temperature)
    mtoks = settings.llm.max_tokens if max_tokens is None else int(max_tokens)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    def _call_router(router: Any):
        return router.completion(model=use_model, messages=messages, temperature=temp, max_tokens=mtoks)

    def _call_direct():
        return litellm.completion(model=use_model, messages=messages, temperature=temp, max_tokens=mtoks)

    resp: Any
    try:
        if _router is not None:
            current_router = _router
            resp = await asyncio.to_thread(lambda: _call_router(current_router))
        else:
            resp = await asyncio.to_thread(_call_direct)
    except Exception as err:
        # Fallback to direct completion if Router path fails (e.g., no deployments)
        _router = None
        _logger.debug("litellm.router.disabled_after_failure")
        try:
            resp = await asyncio.to_thread(_call_direct)
        except Exception:
            # As a last resort, try with a provider-backed small model if available
            alt_model = _choose_best_available_model(use_model)
            if alt_model != use_model:
                use_model = alt_model
                resp = await asyncio.to_thread(lambda: litellm.completion(model=use_model, messages=messages, temperature=temp, max_tokens=mtoks))
            else:
                raise err from None

    # Normalize content across potential shapes
    content: str
    try:
        msg = resp.choices[0].message
        content = str(msg.get("content", "")) if isinstance(msg, dict) else str(getattr(msg, "content", ""))
    except Exception:
        content = str(getattr(resp, "content", ""))

    provider = getattr(resp, "provider", None)
    model_used = getattr(resp, "model", use_model)
    return LlmOutput(content=content or "", model=str(model_used), provider=str(provider) if provider else None)


def _bridge_provider_env() -> None:
    """Populate os.environ with provider API keys from .env via decouple if missing.

    Also map common synonyms to LiteLLM's canonical env names, e.g. GEMINI_API_KEY -> GOOGLE_API_KEY,
    GROK_API_KEY -> XAI_API_KEY.
    """
    cfg = DecoupleConfig(RepositoryEnv(".env"))

    def _get_from_any(*keys: str) -> str:
        for k in keys:
            v = os.environ.get(k)
            if v:
                return v
        for k in keys:
            try:
                v = cfg(k, default="")
            except Exception:
                v = ""
            if v:
                return v
        return ""

    # Canonical targets with possible synonyms
    mappings: list[tuple[str, tuple[str, ...]]] = [
        ("OPENAI_API_KEY", ("OPENAI_API_KEY",)),
        ("ANTHROPIC_API_KEY", ("ANTHROPIC_API_KEY",)),
        ("GROQ_API_KEY", ("GROQ_API_KEY",)),
        ("XAI_API_KEY", ("XAI_API_KEY", "GROK_API_KEY")),
        ("GOOGLE_API_KEY", ("GOOGLE_API_KEY", "GEMINI_API_KEY")),
        ("OPENROUTER_API_KEY", ("OPENROUTER_API_KEY",)),
        ("DEEPSEEK_API_KEY", ("DEEPSEEK_API_KEY",)),
    ]

    for canonical, aliases in mappings:
        if not os.environ.get(canonical):
            val = _get_from_any(*aliases)
            if val:
                os.environ[canonical] = val


