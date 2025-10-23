"""LiteLLM integration: router, caching, and cost tracking.

Centralizes LLM usage behind a minimal async helper. Providers + API keys
are configured via environment variables; configuration toggles come from
python-decouple in `config.py`.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Optional

import litellm
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

    def _on_success(kwargs: dict[str, Any], completion_response: Any, start_time: float, end_time: float) -> None:  # type: ignore[no-untyped-def]
        try:
            cost = float(kwargs.get("response_cost", 0.0) or 0.0)
            model = str(kwargs.get("model", ""))
            if cost > 0:
                # Prefer rich terminal output when enabled; fallback to structlog
                if settings.log_rich_enabled:
                    try:
                        from rich.console import Console  # type: ignore
                        from rich.panel import Panel  # type: ignore
                        from rich.text import Text  # type: ignore

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
        litellm.success_callback = [*_existing_callbacks(), _on_success]


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

        # Enable cache globally (memory or redis via env)
        if settings.llm.cache_enabled:
            from contextlib import suppress

            with suppress(Exception):
                # If Redis requested, set environment hints LiteLLM recognizes
                if settings.llm.cache_backend.lower() == "redis" and settings.llm.cache_redis_url:
                    os.environ.setdefault("LITELLM_CACHE", "redis")
                    os.environ.setdefault("REDIS_URL", settings.llm.cache_redis_url)
                    os.environ.setdefault("LITELLM_REDIS_URL", settings.llm.cache_redis_url)
                litellm.set_cache(True)  # type: ignore[attr-defined]

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
            return _router.completion(model=use_model, messages=messages, temperature=temp, max_tokens=mtoks, cache=True)
        return litellm.completion(model=use_model, messages=messages, temperature=temp, max_tokens=mtoks, cache=True)

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
    """Populate os.environ with provider API keys from .env via decouple if missing."""
    cfg = DecoupleConfig(RepositoryEnv(".env"))
    names = [
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GROQ_API_KEY",
        "XAI_API_KEY",
        "GOOGLE_API_KEY",
    ]
    for name in names:
        if os.environ.get(name):
            continue
        try:
            value = cfg(name, default="")
        except Exception:
            value = ""
        if value:
            os.environ[name] = value


