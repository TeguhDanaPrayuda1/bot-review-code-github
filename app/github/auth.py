"""GitHub App authentication: App JWT + cached installation tokens."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx
import jwt

from app.logging_config import get_logger

logger = get_logger(__name__)


def build_app_jwt(app_id: str, private_key_pem: str) -> str:
    """Create a short-lived RS256 JWT identifying the GitHub App."""
    now = int(time.time())
    payload = {
        "iat": now - 60,  # allow for clock drift
        "exp": now + 9 * 60,  # max allowed is 10 minutes
        "iss": app_id,
    }
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


@dataclass
class _CachedToken:
    token: str
    expires_at: float  # unix timestamp


class InstallationTokenProvider:
    """Fetches and caches installation access tokens per installation id."""

    # Refresh this many seconds before actual expiry.
    REFRESH_MARGIN = 120

    def __init__(self, app_id: str, private_key_pem: str, api_url: str) -> None:
        self._app_id = app_id
        self._private_key = private_key_pem
        self._api_url = api_url.rstrip("/")
        self._cache: dict[int, _CachedToken] = {}
        self._lock = asyncio.Lock()

    async def get_token(self, installation_id: int) -> str:
        """Return a valid installation token, fetching a new one if needed."""
        async with self._lock:
            cached = self._cache.get(installation_id)
            if cached and cached.expires_at - time.time() > self.REFRESH_MARGIN:
                return cached.token

            token, expires_at = await self._fetch_token(installation_id)
            self._cache[installation_id] = _CachedToken(token, expires_at)
            return token

    async def _fetch_token(self, installation_id: int) -> tuple[str, float]:
        app_jwt = build_app_jwt(self._app_id, self._private_key)
        url = f"{self._api_url}/app/installations/{installation_id}/access_tokens"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {app_jwt}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        response.raise_for_status()
        data = response.json()
        # expires_at is ISO 8601; keep it simple with a fixed 55-minute window
        # (installation tokens live 60 minutes).
        expires_at = time.time() + 55 * 60
        logger.info("installation_token_fetched", installation_id=installation_id)
        return data["token"], expires_at
