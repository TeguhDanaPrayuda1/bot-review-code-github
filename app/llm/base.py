"""Provider-agnostic LLM abstraction.

To add a new provider (OpenAI, Gemini, local model, ...):
1. Subclass :class:`LLMProvider` and implement :meth:`complete`.
2. Register it in :func:`create_provider`.
3. Set ``LLM_PROVIDER=<name>`` in the environment.
"""

from __future__ import annotations

import abc
from typing import Any


class LLMError(Exception):
    """Base error for LLM calls (after provider-side retries were exhausted)."""


class LLMRefusalError(LLMError):
    """The model declined to answer (safety refusal)."""


class LLMProvider(abc.ABC):
    """Minimal interface every LLM backend must implement."""

    @abc.abstractmethod
    async def complete(
        self,
        system: str,
        user: str,
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        """Run one completion and return the text response.

        When ``json_schema`` is provided, providers that support structured
        output should constrain the response to that schema; others may ignore
        it (the prompt also asks for JSON, and the caller parses leniently).
        """


def create_provider(settings: Any) -> LLMProvider:
    """Factory: build the configured provider from application settings."""
    name = settings.llm_provider.lower()
    if name == "anthropic":
        from app.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(
            api_key=settings.anthropic_api_key or settings.llm_api_key,
            model=settings.llm_model,
            base_url=settings.llm_base_url or None,
            max_tokens=settings.llm_max_tokens,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
    if name == "openai_compatible":
        from app.llm.openai_compatible_provider import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            api_key=settings.llm_api_key or settings.anthropic_api_key,
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            max_tokens=settings.llm_max_tokens,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
    raise ValueError(f"Unknown LLM provider: {settings.llm_provider!r}")
