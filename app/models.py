"""Domain models shared across the application."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class Severity(str, enum.Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        """Higher number means more severe."""
        order = [Severity.INFO, Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
        return order.index(self)

    @classmethod
    def parse(cls, value: str) -> "Severity":
        return cls(value.strip().lower())


class Category(str, enum.Enum):
    BUG = "Bug"
    SECURITY = "Security"
    PERFORMANCE = "Performance"
    CODE_QUALITY = "Code Quality"
    BEST_PRACTICE = "Best Practice"
    STYLE = "Style"

    @classmethod
    def parse(cls, value: str) -> "Category":
        normalized = value.strip().lower()
        for member in cls:
            if member.value.lower() == normalized:
                return member
        raise ValueError(f"Unknown category: {value!r}")


@dataclass
class Finding:
    """A single review finding produced by the LLM."""

    file: str
    line: int
    category: Category
    severity: Severity
    problem: str
    suggestion: str

    def format_comment(self) -> str:
        """Render this finding as a GitHub comment body (markdown)."""
        emoji = {
            Severity.CRITICAL: "🔴",
            Severity.HIGH: "🟠",
            Severity.MEDIUM: "🟡",
            Severity.LOW: "🔵",
            Severity.INFO: "⚪",
        }[self.severity]
        header = f"{emoji} **{self.category.value}** · `{self.severity.value}`"
        body = f"{header}\n\n{self.problem}"
        if self.suggestion:
            body += f"\n\n**Saran / Suggestion:**\n{self.suggestion}"
        return body


@dataclass
class ReviewResult:
    """Aggregated output of one review pass."""

    summary: str
    findings: list[Finding] = field(default_factory=list)
