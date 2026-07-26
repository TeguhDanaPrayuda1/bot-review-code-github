"""File filtering, diff truncation, and chunking for LLM calls."""

from __future__ import annotations

from fnmatch import fnmatch

from app.diff.parser import FileDiff

TRUNCATION_MARKER = "\n... [diff dipotong / diff truncated] ..."


def should_skip_file(path: str, patterns: list[str]) -> bool:
    """Return True when ``path`` matches any skip pattern.

    Pattern semantics:
    - ``dir/``            -> skips everything under a directory with that name,
                             at any depth (``node_modules/`` matches
                             ``a/node_modules/b.js``).
    - ``*.lock``          -> matched against the basename and the full path.
    - ``docs/*.md``       -> matched against the full path.
    """
    normalized = path.lstrip("/")
    basename = normalized.rsplit("/", 1)[-1]

    for pattern in patterns:
        if pattern.endswith("/"):
            directory = pattern.rstrip("/")
            parts = normalized.split("/")[:-1]  # directories only
            if directory in parts:
                return True
            if fnmatch(normalized, f"{pattern}*"):
                return True
        else:
            if fnmatch(basename, pattern) or fnmatch(normalized, pattern):
                return True
    return False


def truncate_patch(patch: str, max_chars: int) -> tuple[str, bool]:
    """Truncate a patch to ``max_chars`` at a line boundary.

    Returns ``(patch, was_truncated)``.
    """
    if max_chars <= 0 or len(patch) <= max_chars:
        return patch, False
    cut = patch.rfind("\n", 0, max_chars)
    if cut <= 0:
        cut = max_chars
    return patch[:cut] + TRUNCATION_MARKER, True


def chunk_file_diffs(files: list[FileDiff], max_chunk_chars: int) -> list[list[FileDiff]]:
    """Group files into chunks whose combined patch size stays under
    ``max_chunk_chars``. A single file larger than the limit gets its own chunk
    (the caller is expected to have truncated per-file patches already).
    """
    chunks: list[list[FileDiff]] = []
    current: list[FileDiff] = []
    current_size = 0

    for file_diff in files:
        size = len(file_diff.patch)
        if current and current_size + size > max_chunk_chars:
            chunks.append(current)
            current = []
            current_size = 0
        current.append(file_diff)
        current_size += size

    if current:
        chunks.append(current)
    return chunks
