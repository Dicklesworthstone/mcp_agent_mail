"""LiteLLM integration: router, caching, and cost tracking.

Centralizes LLM usage behind a minimal async helper. Providers + API keys
are configured via environment variables; configuration toggles come from
python-decouple in `config.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import litellm
from litellm.types.caching import LiteLLMCacheType
import structlog
from decouple import Config as DecoupleConfig, RepositoryEnv

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
                    litellm.enable_cache(type=LiteLLMCacheType.REDIS, host=str(host), port=str(port), password=pwd)
                else:
                    litellm.enable_cache(type=LiteLLMCacheType.LOCAL)

        _setup_callbacks()

        # Router optional; direct completion works if Router is unavailable
        try:
            _router = litellm.Router()
        except Exception:
            _router = None


async def complete_system_user(system: str, user: str, *, model: Optional[str] = None, temperature: Optional[float] = None, max_tokens: Optional[int] = None) -> LlmOutput:
    """Chat completion helper returning content.

    Falls back to litellm.completion if Router isn't available.
    """
    await _ensure_initialized()
    settings = get_settings()
    use_model = model or settings.llm.default_model
    temp = settings.llm.temperature if temperature is None else float(temperature)
    mtoks = settings.llm.max_tokens if max_tokens is None else int(max_tokens)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    def _call():
        if _router is not None:
            return _router.completion(model=use_model, messages=messages, temperature=temp, max_tokens=mtoks)
        return litellm.completion(model=use_model, messages=messages, temperature=temp, max_tokens=mtoks)

    resp = await asyncio.to_thread(_call)

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


