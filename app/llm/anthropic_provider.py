"""Anthropic Claude implementation of the LLM provider interface."""

from __future__ import annotations

from typing import Any

import anthropic

from app.llm.base import LLMError, LLMProvider, LLMRefusalError
from app.logging_config import get_logger

logger = get_logger(__name__)


class AnthropicProvider(LLMProvider):
    """Calls the Anthropic Messages API.

    The SDK already retries 429/5xx/connection errors with exponential backoff
    (``max_retries``); on top of that the review engine treats a failed chunk as
    non-fatal, so an LLM outage never breaks webhook handling.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        max_tokens: int,
        timeout: float,
        max_retries: int,
    ) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key or None,  # None -> resolve from environment
            timeout=timeout,
            max_retries=max_retries,
        )
        self._model = model
        self._max_tokens = max_tokens

    async def complete(
        self,
        system: str,
        user: str,
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {}
        if json_schema is not None:
            kwargs["output_config"] = {"format": {"type": "json_schema", "schema": json_schema}}

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                **kwargs,
            )
        except anthropic.RateLimitError as exc:
            raise LLMError(f"Rate limited after retries: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Connection error calling Anthropic: {exc}") from exc

        if response.stop_reason == "refusal":
            raise LLMRefusalError("Model refused to process this diff")
        if response.stop_reason == "max_tokens":
            logger.warning("llm_output_truncated", model=self._model)

        text = "".join(block.text for block in response.content if block.type == "text")
        if not text.strip():
            raise LLMError("Empty response from model")

        logger.info(
            "llm_call_completed",
            model=self._model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return text
