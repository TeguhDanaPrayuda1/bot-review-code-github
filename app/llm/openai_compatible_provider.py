"""Provider for OpenAI-compatible APIs (chat completions).

Works with any gateway that exposes ``POST {base_url}/chat/completions`` with
``Authorization: Bearer <key>`` — e.g. OpenRouter-style aggregators or local
inference servers. The review prompt already demands JSON-only output and the
engine parses leniently, so no provider-side schema enforcement is required.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx

from app.llm.base import LLMError, LLMProvider
from app.logging_config import get_logger

logger = get_logger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class OpenAICompatibleProvider(LLMProvider):
    """Calls a ``/chat/completions`` endpoint with bearer-token auth."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        max_tokens: int,
        timeout: float,
        max_retries: int,
    ) -> None:
        if not base_url:
            raise ValueError(
                "LLM_BASE_URL is required for the openai_compatible provider "
                "(e.g. https://api.example.com/v1)"
            )
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._max_retries = max_retries

    async def complete(
        self,
        system: str,
        user: str,
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        # json_schema is intentionally unused: many gateways reject
        # response_format, and the caller parses JSON leniently anyway.
        payload = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self._base_url}/chat/completions"

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                last_error = exc
                await self._backoff(attempt, str(exc))
                continue

            if response.status_code in RETRYABLE_STATUS:
                last_error = LLMError(f"{url} -> {response.status_code}")
                retry_after = response.headers.get("Retry-After")
                await self._backoff(attempt, f"HTTP {response.status_code}", retry_after)
                continue
            if response.status_code >= 400:
                raise LLMError(
                    f"LLM API error {response.status_code}: {response.text[:500]}"
                )

            data = response.json()
            try:
                text = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise LLMError(f"Unexpected response shape: {str(data)[:500]}") from exc
            if not text or not text.strip():
                raise LLMError("Empty response from model")

            usage = data.get("usage") or {}
            logger.info(
                "llm_call_completed",
                model=self._model,
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
            )
            return text

        raise LLMError(
            f"LLM call failed after {self._max_retries + 1} attempts"
        ) from last_error

    @staticmethod
    async def _backoff(attempt: int, reason: str, retry_after: str | None = None) -> None:
        if retry_after and retry_after.isdigit():
            delay = min(float(retry_after), 60.0)
        else:
            delay = min(2.0 * (2**attempt) + random.uniform(0, 1), 60.0)
        logger.warning("llm_retry", attempt=attempt, delay=round(delay, 1), reason=reason)
        await asyncio.sleep(delay)
