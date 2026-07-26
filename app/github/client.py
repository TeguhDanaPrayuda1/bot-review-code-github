"""Async GitHub REST API client with rate-limit-aware retries."""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx

from app.github.auth import InstallationTokenProvider
from app.logging_config import get_logger

logger = get_logger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 4
BASE_DELAY = 2.0
MAX_DELAY = 60.0


class GitHubAPIError(Exception):
    """Raised when a GitHub API call fails after retries."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubClient:
    """Minimal GitHub REST client scoped to one installation."""

    def __init__(
        self,
        token_provider: InstallationTokenProvider,
        installation_id: int,
        api_url: str,
    ) -> None:
        self._token_provider = token_provider
        self._installation_id = installation_id
        self._api_url = api_url.rstrip("/")

    async def _headers(self) -> dict[str, str]:
        token = await self._token_provider.get_token(self._installation_id)
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Perform a request with exponential-backoff retries.

        Retries on 5xx, 429, and secondary-rate-limit 403s, honouring
        ``Retry-After`` / ``X-RateLimit-Reset`` headers when present.
        """
        url = f"{self._api_url}{path}"
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.request(
                        method, url, params=params, json=json_body, headers=await self._headers()
                    )
            except httpx.HTTPError as exc:
                last_error = exc
                delay = min(BASE_DELAY * (2**attempt) + random.uniform(0, 1), MAX_DELAY)
                logger.warning("github_network_error", url=url, attempt=attempt, error=str(exc))
                await asyncio.sleep(delay)
                continue

            if response.status_code < 400:
                return response

            rate_limited_403 = (
                response.status_code == 403
                and response.headers.get("X-RateLimit-Remaining") == "0"
            )
            if response.status_code in RETRYABLE_STATUS or rate_limited_403:
                delay = self._retry_delay(response, attempt)
                logger.warning(
                    "github_retryable_error",
                    url=url,
                    status=response.status_code,
                    attempt=attempt,
                    delay=round(delay, 1),
                )
                last_error = GitHubAPIError(
                    f"{method} {path} -> {response.status_code}", response.status_code
                )
                await asyncio.sleep(delay)
                continue

            raise GitHubAPIError(
                f"{method} {path} -> {response.status_code}: {response.text[:500]}",
                response.status_code,
            )

        raise GitHubAPIError(f"{method} {path} failed after {MAX_RETRIES + 1} attempts") from last_error

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), MAX_DELAY)
        reset = response.headers.get("X-RateLimit-Reset")
        if reset and reset.isdigit():
            wait = float(reset) - time.time()
            if 0 < wait <= MAX_DELAY:
                return wait
        return min(BASE_DELAY * (2**attempt) + random.uniform(0, 1), MAX_DELAY)

    # ------------------------------------------------------------------ #
    # High-level operations                                              #
    # ------------------------------------------------------------------ #

    async def get_pull_request(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        response = await self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}")
        return response.json()

    async def get_pr_files(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        """List all changed files of a PR (paginated)."""
        files: list[dict[str, Any]] = []
        page = 1
        while True:
            response = await self._request(
                "GET",
                f"/repos/{owner}/{repo}/pulls/{number}/files",
                params={"per_page": 100, "page": page},
            )
            batch = response.json()
            files.extend(batch)
            if len(batch) < 100:
                return files
            page += 1

    async def compare(self, owner: str, repo: str, base: str, head: str) -> list[dict[str, Any]]:
        """Return changed files between two commits (for incremental review)."""
        response = await self._request(
            "GET", f"/repos/{owner}/{repo}/compare/{base}...{head}", params={"per_page": 100}
        )
        return response.json().get("files", [])

    async def list_open_prs_by_head(
        self, owner: str, repo: str, branch: str
    ) -> list[dict[str, Any]]:
        response = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls",
            params={"state": "open", "head": f"{owner}:{branch}", "per_page": 100},
        )
        return response.json()

    async def create_review(
        self,
        owner: str,
        repo: str,
        number: int,
        commit_id: str,
        body: str,
        comments: list[dict[str, Any]],
    ) -> None:
        """Post a review (event=COMMENT — never approve/request changes).

        If GitHub rejects the inline comments (422 — e.g. a line fell outside
        the diff), fall back to a summary-only review so the result is never
        lost.
        """
        payload: dict[str, Any] = {
            "commit_id": commit_id,
            "body": body,
            "event": "COMMENT",
        }
        if comments:
            payload["comments"] = comments
        try:
            await self._request(
                "POST", f"/repos/{owner}/{repo}/pulls/{number}/reviews", json_body=payload
            )
        except GitHubAPIError as exc:
            if exc.status_code == 422 and comments:
                logger.warning("inline_comments_rejected_fallback_to_summary", error=str(exc))
                inline_dump = "\n\n---\n\n".join(
                    f"**`{c['path']}:{c.get('line', '?')}`**\n\n{c['body']}" for c in comments
                )
                await self._request(
                    "POST",
                    f"/repos/{owner}/{repo}/pulls/{number}/reviews",
                    json_body={
                        "commit_id": commit_id,
                        "body": f"{body}\n\n---\n\n{inline_dump}",
                        "event": "COMMENT",
                    },
                )
            else:
                raise
