"""Unified-diff (patch) parsing utilities.

GitHub's PR "files" and "compare" APIs return each file's changes as a
unified-diff ``patch`` string (without the ``diff --git`` header). This module
parses those patches into hunks, tracks new-file line numbers, and renders an
annotated view for the LLM so it can report accurate line numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


@dataclass
class DiffLine:
    """One line inside a hunk."""

    kind: str  # "add" | "del" | "context"
    content: str
    old_lineno: int | None
    new_lineno: int | None


@dataclass
class Hunk:
    """A single @@ ... @@ hunk."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str
    lines: list[DiffLine] = field(default_factory=list)


@dataclass
class FileDiff:
    """A parsed per-file diff."""

    path: str
    status: str = "modified"  # added | removed | modified | renamed
    previous_path: str | None = None
    patch: str = ""
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def added_lines(self) -> set[int]:
        """New-file line numbers of added ('+') lines."""
        return {
            line.new_lineno
            for hunk in self.hunks
            for line in hunk.lines
            if line.kind == "add" and line.new_lineno is not None
        }

    @property
    def commentable_lines(self) -> set[int]:
        """New-file line numbers on which GitHub accepts an inline review
        comment (any RIGHT-side line that appears in a hunk: added or context).
        """
        return {
            line.new_lineno
            for hunk in self.hunks
            for line in hunk.lines
            if line.new_lineno is not None
        }


def parse_patch(patch: str) -> list[Hunk]:
    """Parse a unified-diff patch body (as returned by the GitHub API) into hunks.

    Lines that do not belong to any hunk (e.g. ``\\ No newline at end of file``)
    are ignored. Returns an empty list for empty/None patches (e.g. binary files).
    """
    if not patch:
        return []

    hunks: list[Hunk] = []
    current: Hunk | None = None
    old_lineno = new_lineno = 0

    for raw_line in patch.splitlines():
        match = HUNK_HEADER_RE.match(raw_line)
        if match:
            current = Hunk(
                old_start=int(match.group("old_start")),
                old_count=int(match.group("old_count") or 1),
                new_start=int(match.group("new_start")),
                new_count=int(match.group("new_count") or 1),
                header=raw_line,
            )
            hunks.append(current)
            old_lineno = current.old_start
            new_lineno = current.new_start
            continue

        if current is None:
            continue  # preamble noise before the first hunk

        if raw_line.startswith("+"):
            current.lines.append(DiffLine("add", raw_line[1:], None, new_lineno))
            new_lineno += 1
        elif raw_line.startswith("-"):
            current.lines.append(DiffLine("del", raw_line[1:], old_lineno, None))
            old_lineno += 1
        elif raw_line.startswith("\\"):
            continue  # "\ No newline at end of file"
        else:
            content = raw_line[1:] if raw_line.startswith(" ") else raw_line
            current.lines.append(DiffLine("context", content, old_lineno, new_lineno))
            old_lineno += 1
            new_lineno += 1

    return hunks


def file_diff_from_api(api_file: dict) -> FileDiff:
    """Build a :class:`FileDiff` from one entry of the GitHub files/compare API."""
    patch = api_file.get("patch") or ""
    return FileDiff(
        path=api_file["filename"],
        status=api_file.get("status", "modified"),
        previous_path=api_file.get("previous_filename"),
        patch=patch,
        hunks=parse_patch(patch),
    )


def render_annotated_diff(file_diff: FileDiff) -> str:
    """Render a diff with explicit new-file line numbers for the LLM prompt.

    Format per line: ``<new_lineno>|<marker> <content>`` where marker is one of
    ``+``, ``-``, or a space. Deleted lines have no new line number.
    """
    out: list[str] = [f"### File: {file_diff.path} ({file_diff.status})"]
    if file_diff.previous_path:
        out.append(f"(renamed from {file_diff.previous_path})")

    for hunk in file_diff.hunks:
        out.append(hunk.header)
        for line in hunk.lines:
            if line.kind == "add":
                out.append(f"{line.new_lineno}|+ {line.content}")
            elif line.kind == "del":
                out.append(f"    |- {line.content}")
            else:
                out.append(f"{line.new_lineno}|  {line.content}")
    return "\n".join(out)


def nearest_commentable_line(file_diff: FileDiff, line: int, tolerance: int = 3) -> int | None:
    """Return ``line`` if commentable; otherwise the closest commentable line
    within ``tolerance``; otherwise ``None`` (caller should fall back to the
    summary comment).
    """
    commentable = file_diff.commentable_lines
    if not commentable:
        return None
    if line in commentable:
        return line
    best = min(commentable, key=lambda candidate: abs(candidate - line))
    return best if abs(best - line) <= tolerance else None
