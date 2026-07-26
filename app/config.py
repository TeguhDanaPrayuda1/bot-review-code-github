"""Application configuration loaded from environment variables (.env)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models import Severity


class Settings(BaseSettings):
    """All runtime configuration. Every field can be set via environment
    variable (case-insensitive) or a local ``.env`` file.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- GitHub App ---
    github_app_id: str
    github_private_key: str = ""
    github_private_key_path: str = ""
    github_webhook_secret: str
    github_api_url: str = "https://api.github.com"

    # --- LLM ---
    # "anthropic" (default) or "openai_compatible" (any /chat/completions gateway)
    llm_provider: str = "anthropic"
    anthropic_api_key: str = ""
    # Generic API key for non-Anthropic providers (falls back to anthropic_api_key).
    llm_api_key: str = ""
    # Custom endpoint. For "anthropic": an Anthropic-compatible base URL
    # (default: https://api.anthropic.com). For "openai_compatible": required,
    # e.g. https://api.gateway.example/v1
    llm_base_url: str = ""
    llm_model: str = "claude-opus-5"
    llm_max_tokens: int = 8192
    llm_timeout_seconds: float = 300.0
    llm_max_retries: int = 3

    # --- Review behaviour ---
    # Comma-separated glob patterns. A trailing "/" matches a directory prefix.
    skip_patterns: str = (
        "*.lock,package-lock.json,yarn.lock,pnpm-lock.yaml,poetry.lock,"
        "dist/,build/,node_modules/,vendor/,*.min.js,*.min.css,*.map,"
        "*.svg,*.png,*.jpg,*.jpeg,*.gif,*.ico,*.pdf,*.snap"
    )
    # Per-file diff cap; larger patches are truncated before being sent to the LLM.
    max_file_diff_chars: int = 30_000
    # Max characters of diff per LLM call; files are grouped into chunks under this.
    max_chunk_chars: int = 60_000
    # Files whose patch exceeds this are skipped entirely (0 = never skip).
    skip_file_over_chars: int = 100_000
    # "id" (Bahasa Indonesia) or "en" (English).
    review_language: str = "id"
    # Findings below this severity are dropped: critical|high|medium|low|info.
    min_severity: str = "low"
    # Max inline comments per review; the rest are folded into the summary.
    max_inline_comments: int = 30

    # --- State / infra ---
    state_db_path: str = "data/review_state.db"
    log_level: str = "INFO"

    @field_validator("review_language")
    @classmethod
    def _validate_language(cls, v: str) -> str:
        if v not in ("id", "en"):
            raise ValueError("review_language must be 'id' or 'en'")
        return v

    @property
    def min_severity_enum(self) -> Severity:
        return Severity(self.min_severity.lower())

    @property
    def skip_pattern_list(self) -> list[str]:
        return [p.strip() for p in self.skip_patterns.split(",") if p.strip()]

    def resolve_private_key(self) -> str:
        """Return the GitHub App private key PEM, from inline env or file path."""
        if self.github_private_key:
            # Support "\n"-escaped single-line keys commonly used in env vars.
            return self.github_private_key.replace("\\n", "\n")
        if self.github_private_key_path:
            return Path(self.github_private_key_path).read_text()
        raise RuntimeError(
            "Set GITHUB_PRIVATE_KEY (PEM content) or GITHUB_PRIVATE_KEY_PATH (file path)"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
