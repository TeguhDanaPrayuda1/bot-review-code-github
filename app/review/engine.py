"""Review orchestration: fetch diff -> filter -> LLM -> post review."""

from __future__ import annotations

import json
import re
from typing import Any

from app.config import Settings
from app.diff.filters import chunk_file_diffs, should_skip_file, truncate_patch
from app.diff.parser import FileDiff, file_diff_from_api, nearest_commentable_line, parse_patch
from app.github.client import GitHubClient
from app.llm.base import LLMError, LLMProvider
from app.logging_config import get_logger
from app.models import Category, Finding, ReviewResult, Severity
from app.review.prompts import REVIEW_JSON_SCHEMA, build_system_prompt, build_user_prompt
from app.review.state import ReviewStateStore

logger = get_logger(__name__)

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


class ReviewEngine:
    """Runs one review pass for a PR head commit."""

    def __init__(
        self,
        settings: Settings,
        provider: LLMProvider,
        state: ReviewStateStore,
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._state = state

    async def review_pull_request(
        self,
        github: GitHubClient,
        owner: str,
        repo: str,
        pr_number: int,
        head_sha: str,
        pr_title: str,
        pr_body: str,
    ) -> None:
        """Review the given head commit of a PR and post the result.

        Idempotent: a SHA that was already reviewed (or is being reviewed) is
        skipped, so duplicate ``push`` + ``synchronize`` deliveries are safe.
        """
        repo_full = f"{owner}/{repo}"

        # Read the incremental baseline BEFORE claiming the new SHA.
        last_sha = self._state.last_reviewed_sha(repo_full, pr_number)

        if not self._state.try_claim(repo_full, pr_number, head_sha):
            logger.info("sha_already_reviewed_skip", repo=repo_full, pr=pr_number, sha=head_sha)
            return

        try:
            incremental = last_sha is not None and last_sha != head_sha
            files = await self._fetch_files(github, owner, repo, pr_number, last_sha, head_sha)
            selected, skipped = self._select_files(files)

            if not selected:
                logger.info("no_reviewable_files", repo=repo_full, pr=pr_number, sha=head_sha)
                return

            result = await self._run_llm_review(pr_title, pr_body, selected, incremental)
            await self._post_review(
                github, owner, repo, pr_number, head_sha, selected, skipped, result, incremental
            )
            logger.info(
                "review_posted",
                repo=repo_full,
                pr=pr_number,
                sha=head_sha,
                findings=len(result.findings),
                incremental=incremental,
            )
        except Exception:
            # Release the claim so a retry (redelivered webhook / next push)
            # can review this commit again.
            self._state.release(repo_full, pr_number, head_sha)
            logger.exception("review_failed", repo=repo_full, pr=pr_number, sha=head_sha)
            raise

    # ------------------------------------------------------------------ #

    async def _fetch_files(
        self,
        github: GitHubClient,
        owner: str,
        repo: str,
        pr_number: int,
        last_sha: str | None,
        head_sha: str,
    ) -> list[dict[str, Any]]:
        """Full PR diff for a first review, compare-diff for incremental ones."""
        if last_sha and last_sha != head_sha:
            try:
                files = await self._compare_files(github, owner, repo, last_sha, head_sha)
                if files:
                    return files
                logger.info("incremental_compare_empty_fallback_full", pr=pr_number)
            except Exception as exc:  # e.g. base commit gone after force-push
                logger.warning("compare_failed_fallback_full", pr=pr_number, error=str(exc))
        return await github.get_pr_files(owner, repo, pr_number)

    @staticmethod
    async def _compare_files(
        github: GitHubClient, owner: str, repo: str, base: str, head: str
    ) -> list[dict[str, Any]]:
        return await github.compare(owner, repo, base, head)

    def _select_files(self, api_files: list[dict[str, Any]]) -> tuple[list[FileDiff], list[str]]:
        """Apply skip patterns and size limits. Returns (selected, skipped_paths)."""
        patterns = self._settings.skip_pattern_list
        selected: list[FileDiff] = []
        skipped: list[str] = []

        for api_file in api_files:
            path = api_file["filename"]
            patch = api_file.get("patch") or ""

            if api_file.get("status") == "removed":
                continue
            if should_skip_file(path, patterns):
                skipped.append(path)
                continue
            if not patch:  # binary or too large for GitHub to render
                skipped.append(path)
                continue
            limit = self._settings.skip_file_over_chars
            if limit and len(patch) > limit:
                skipped.append(path)
                continue

            truncated_patch, was_truncated = truncate_patch(
                patch, self._settings.max_file_diff_chars
            )
            file_diff = file_diff_from_api(api_file)
            if was_truncated:
                # Re-parse hunks from the truncated patch so line validation
                # only accepts lines the LLM actually saw.
                file_diff = FileDiff(
                    path=file_diff.path,
                    status=file_diff.status,
                    previous_path=file_diff.previous_path,
                    patch=truncated_patch,
                    hunks=parse_patch(truncated_patch),
                )
            selected.append(file_diff)

        return selected, skipped

    async def _run_llm_review(
        self,
        pr_title: str,
        pr_body: str,
        files: list[FileDiff],
        incremental: bool,
    ) -> ReviewResult:
        """Call the LLM per chunk and merge results. A failing chunk is logged
        and skipped so one bad call never sinks the whole review."""
        language = self._settings.review_language
        system = build_system_prompt(language)
        chunks = chunk_file_diffs(files, self._settings.max_chunk_chars)

        summaries: list[str] = []
        findings: list[Finding] = []
        failed_chunks = 0

        for index, chunk in enumerate(chunks):
            user = build_user_prompt(language, pr_title, pr_body, chunk, incremental)
            try:
                raw = await self._provider.complete(system, user, json_schema=REVIEW_JSON_SCHEMA)
                partial = self._parse_result(raw)
            except (LLMError, ValueError) as exc:
                failed_chunks += 1
                logger.warning("chunk_review_failed", chunk=index, error=str(exc))
                continue
            if partial.summary:
                summaries.append(partial.summary)
            findings.extend(partial.findings)

        min_rank = self._settings.min_severity_enum.rank
        findings = [f for f in findings if f.severity.rank >= min_rank]

        summary = "\n\n".join(summaries)
        if failed_chunks:
            note = (
                f"⚠️ {failed_chunks}/{len(chunks)} bagian diff gagal direview (error LLM)."
                if language == "id"
                else f"⚠️ {failed_chunks}/{len(chunks)} diff chunk(s) failed to review (LLM error)."
            )
            summary = f"{summary}\n\n{note}" if summary else note

        return ReviewResult(summary=summary, findings=findings)

    @staticmethod
    def _parse_result(raw: str) -> ReviewResult:
        """Parse the model's JSON output leniently (handles code fences)."""
        text = raw.strip()
        match = _JSON_BLOCK_RE.search(text)
        if match:
            text = match.group(1)
        else:
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end > start:
                text = text[start : end + 1]

        data = json.loads(text)
        findings: list[Finding] = []
        for item in data.get("findings", []):
            try:
                findings.append(
                    Finding(
                        file=str(item["file"]),
                        line=int(item["line"]),
                        category=Category.parse(str(item["category"])),
                        severity=Severity.parse(str(item["severity"])),
                        problem=str(item["problem"]).strip(),
                        suggestion=str(item.get("suggestion", "")).strip(),
                    )
                )
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning("finding_dropped_malformed", error=str(exc), item=str(item)[:200])
        return ReviewResult(summary=str(data.get("summary", "")).strip(), findings=findings)

    async def _post_review(
        self,
        github: GitHubClient,
        owner: str,
        repo: str,
        pr_number: int,
        head_sha: str,
        files: list[FileDiff],
        skipped: list[str],
        result: ReviewResult,
        incremental: bool,
    ) -> None:
        by_path = {f.path: f for f in files}
        inline: list[dict[str, Any]] = []
        overflow: list[Finding] = []

        # Most severe findings first, so they win the inline-comment budget.
        ordered = sorted(result.findings, key=lambda f: -f.severity.rank)
        for finding in ordered:
            file_diff = by_path.get(finding.file)
            line = (
                nearest_commentable_line(file_diff, finding.line) if file_diff is not None else None
            )
            if line is not None and len(inline) < self._settings.max_inline_comments:
                inline.append(
                    {
                        "path": finding.file,
                        "line": line,
                        "side": "RIGHT",
                        "body": finding.format_comment(),
                    }
                )
            else:
                overflow.append(finding)

        body = self._build_summary_body(result, overflow, skipped, head_sha, incremental)
        await github.create_review(owner, repo, pr_number, head_sha, body, inline)

    def _build_summary_body(
        self,
        result: ReviewResult,
        overflow: list[Finding],
        skipped: list[str],
        head_sha: str,
        incremental: bool,
    ) -> str:
        language = self._settings.review_language
        indonesian = language == "id"

        title = "🤖 Review Kode Otomatis" if indonesian else "🤖 Automated Code Review"
        mode = (
            ("inkremental — hanya perubahan sejak review terakhir" if incremental else "penuh")
            if indonesian
            else ("incremental — changes since last review only" if incremental else "full")
        )
        parts = [f"## {title}", f"_Commit `{head_sha[:10]}` · review {mode}_", ""]

        if result.summary:
            parts += [result.summary, ""]

        if result.findings:
            counts: dict[Severity, int] = {}
            for finding in result.findings:
                counts[finding.severity] = counts.get(finding.severity, 0) + 1
            stats = " · ".join(
                f"{severity.value}: {counts[severity]}"
                for severity in sorted(counts, key=lambda s: -s.rank)
            )
            label = "Total temuan" if indonesian else "Total findings"
            parts += [f"**{label}: {len(result.findings)}** ({stats})", ""]
        else:
            parts += [
                "✅ Tidak ada temuan pada perubahan ini."
                if indonesian
                else "✅ No findings for these changes.",
                "",
            ]

        if overflow:
            header = (
                "### Temuan lain (di luar baris diff / melebihi batas komentar inline)"
                if indonesian
                else "### Other findings (outside the diff / over the inline-comment limit)"
            )
            parts.append(header)
            for finding in overflow:
                parts += [f"**`{finding.file}:{finding.line}`**", finding.format_comment(), ""]

        if skipped:
            label = "File dilewati" if indonesian else "Skipped files"
            shown = ", ".join(f"`{p}`" for p in skipped[:20])
            more = f" (+{len(skipped) - 20})" if len(skipped) > 20 else ""
            parts.append(f"<sub>{label}: {shown}{more}</sub>")

        return "\n".join(parts).strip()
