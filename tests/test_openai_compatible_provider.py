"""Tests for the OpenAI-compatible provider against a mocked endpoint."""

import httpx
import pytest

from app.llm.base import LLMError
from app.llm.openai_compatible_provider import OpenAICompatibleProvider


def _provider(**overrides) -> OpenAICompatibleProvider:
    params = dict(
        api_key="test-key",
        model="test-model",
        base_url="https://gw.example/v1",
        max_tokens=100,
        timeout=5.0,
        max_retries=0,
    )
    params.update(overrides)
    return OpenAICompatibleProvider(**params)


def _mock_transport(handler):
    return httpx.MockTransport(handler)


@pytest.fixture
def patch_client(monkeypatch):
    """Route the provider's httpx.AsyncClient through a mock transport."""

    def _apply(handler):
        real_init = httpx.AsyncClient.__init__

        def fake_init(self, *args, **kwargs):
            kwargs["transport"] = _mock_transport(handler)
            real_init(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", fake_init)

    return _apply


@pytest.mark.asyncio
async def test_success_parses_content_and_sends_auth(patch_client):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"summary": "ok", "findings": []}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    patch_client(handler)
    text = await _provider().complete("system", "user")
    assert text == '{"summary": "ok", "findings": []}'
    assert seen["url"] == "https://gw.example/v1/chat/completions"
    assert seen["auth"] == "Bearer test-key"


@pytest.mark.asyncio
async def test_client_error_raises_without_retry(patch_client):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": "bad key"})

    patch_client(handler)
    with pytest.raises(LLMError, match="401"):
        await _provider(max_retries=3).complete("s", "u")
    assert calls["n"] == 1  # 4xx (non-429) must not be retried


@pytest.mark.asyncio
async def test_retryable_error_then_success(patch_client, monkeypatch):
    # Make backoff instant for the test.
    import app.llm.openai_compatible_provider as mod

    async def no_sleep(*a, **k):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "hasil"}}]}
        )

    patch_client(handler)
    text = await _provider(max_retries=2).complete("s", "u")
    assert text == "hasil"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_empty_content_raises(patch_client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]})

    patch_client(handler)
    with pytest.raises(LLMError, match="Empty response"):
        await _provider().complete("s", "u")


def test_missing_base_url_rejected():
    with pytest.raises(ValueError, match="LLM_BASE_URL"):
        _provider(base_url="")
