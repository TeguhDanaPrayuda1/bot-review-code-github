"""FastAPI application: GitHub webhook endpoint and event dispatch."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response

from app.config import Settings, get_settings
from app.github.auth import InstallationTokenProvider
from app.github.client import GitHubClient
from app.github.webhook import verify_signature
from app.llm.base import create_provider
from app.logging_config import configure_logging, get_logger
from app.review.engine import ReviewEngine
from app.review.state import ReviewStateStore

logger = get_logger(__name__)

PR_ACTIONS = {"opened", "reopened", "synchronize"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)

    app.state.settings = settings
    app.state.token_provider = InstallationTokenProvider(
        app_id=settings.github_app_id,
        private_key_pem=settings.resolve_private_key(),
        api_url=settings.github_api_url,
    )
    app.state.review_state = ReviewStateStore(settings.state_db_path)
    app.state.engine = ReviewEngine(
        settings=settings,
        provider=create_provider(settings),
        state=app.state.review_state,
    )
    # Keep references to in-flight review tasks so they aren't GC'd.
    app.state.tasks = set()

    logger.info("startup_complete", provider=settings.llm_provider, model=settings.llm_model)
    yield

    for task in list(app.state.tasks):
        task.cancel()
    app.state.review_state.close()


app = FastAPI(title="GitHub Code Reviewer Bot", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
) -> Response:
    settings: Settings = app.state.settings
    body = await request.body()

    if not verify_signature(settings.github_webhook_secret, body, x_hub_signature_256):
        logger.warning("webhook_bad_signature", delivery=x_github_delivery)
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()
    logger.info("webhook_received", gh_event=x_github_event, delivery=x_github_delivery)

    if x_github_event == "ping":
        return Response(content='{"msg": "pong"}', media_type="application/json")

    if x_github_event == "pull_request":
        _handle_pull_request_event(payload)
    elif x_github_event == "push":
        _schedule(_handle_push_event(payload))
    else:
        logger.info("webhook_event_ignored", gh_event=x_github_event)

    # Always ack quickly; the actual review runs in the background so LLM
    # latency or failures can never make webhook delivery fail.
    return Response(status_code=202)


def _handle_pull_request_event(payload: dict[str, Any]) -> None:
    action = payload.get("action")
    if action not in PR_ACTIONS:
        logger.info("pr_action_ignored", action=action)
        return

    pr = payload["pull_request"]
    if pr.get("draft"):
        logger.info("draft_pr_skipped", pr=pr.get("number"))
        return

    repo = payload["repository"]
    installation_id = payload["installation"]["id"]
    _schedule(
        _run_review(
            installation_id=installation_id,
            owner=repo["owner"]["login"],
            repo_name=repo["name"],
            pr_number=pr["number"],
            head_sha=pr["head"]["sha"],
            pr_title=pr.get("title", ""),
            pr_body=pr.get("body") or "",
        )
    )


async def _handle_push_event(payload: dict[str, Any]) -> None:
    """On push, review any open PR whose head branch matches the pushed ref.

    This overlaps with ``pull_request.synchronize``; the idempotency store
    guarantees each SHA is reviewed only once.
    """
    ref: str = payload.get("ref", "")
    if not ref.startswith("refs/heads/") or payload.get("deleted"):
        return
    branch = ref.removeprefix("refs/heads/")

    repo = payload["repository"]
    owner = repo["owner"]["login"]
    repo_name = repo["name"]
    installation_id = payload["installation"]["id"]

    github = _github_client(installation_id)
    try:
        prs = await github.list_open_prs_by_head(owner, repo_name, branch)
    except Exception:
        logger.exception("push_pr_lookup_failed", repo=f"{owner}/{repo_name}", branch=branch)
        return

    for pr in prs:
        if pr.get("draft"):
            continue
        await _run_review(
            installation_id=installation_id,
            owner=owner,
            repo_name=repo_name,
            pr_number=pr["number"],
            head_sha=pr["head"]["sha"],
            pr_title=pr.get("title", ""),
            pr_body=pr.get("body") or "",
        )


async def _run_review(
    installation_id: int,
    owner: str,
    repo_name: str,
    pr_number: int,
    head_sha: str,
    pr_title: str,
    pr_body: str,
) -> None:
    engine: ReviewEngine = app.state.engine
    github = _github_client(installation_id)
    try:
        await engine.review_pull_request(
            github, owner, repo_name, pr_number, head_sha, pr_title, pr_body
        )
    except Exception:
        # Already logged inside the engine; never propagate out of a task.
        pass


def _github_client(installation_id: int) -> GitHubClient:
    settings: Settings = app.state.settings
    return GitHubClient(
        token_provider=app.state.token_provider,
        installation_id=installation_id,
        api_url=settings.github_api_url,
    )


def _schedule(coro) -> None:
    """Run a coroutine in the background, keeping a strong reference."""
    task = asyncio.create_task(coro)
    app.state.tasks.add(task)
    task.add_done_callback(app.state.tasks.discard)
