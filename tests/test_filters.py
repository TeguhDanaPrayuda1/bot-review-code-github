"""Tests for file skipping, patch truncation, and chunking."""

from app.diff.filters import (
    TRUNCATION_MARKER,
    chunk_file_diffs,
    should_skip_file,
    truncate_patch,
)
from app.diff.parser import FileDiff

PATTERNS = [
    "*.lock",
    "package-lock.json",
    "dist/",
    "node_modules/",
    "*.min.js",
    "*.png",
    "docs/*.md",
]


class TestShouldSkipFile:
    def test_extension_glob(self):
        assert should_skip_file("poetry.lock", PATTERNS)
        assert should_skip_file("backend/Cargo.lock", PATTERNS)
        assert not should_skip_file("src/app.py", PATTERNS)

    def test_exact_filename(self):
        assert should_skip_file("package-lock.json", PATTERNS)
        assert should_skip_file("frontend/package-lock.json", PATTERNS)
        assert not should_skip_file("package.json", PATTERNS)

    def test_directory_pattern_at_root(self):
        assert should_skip_file("dist/bundle.js", PATTERNS)
        assert should_skip_file("dist/assets/main.css", PATTERNS)

    def test_directory_pattern_nested(self):
        assert should_skip_file("frontend/node_modules/react/index.js", PATTERNS)
        assert not should_skip_file("src/node_modules.py", PATTERNS)

    def test_minified_assets(self):
        assert should_skip_file("static/js/app.min.js", PATTERNS)
        assert should_skip_file("logo.png", PATTERNS)

    def test_path_glob(self):
        assert should_skip_file("docs/guide.md", PATTERNS)
        assert not should_skip_file("README.md", PATTERNS)

    def test_no_patterns_skips_nothing(self):
        assert not should_skip_file("anything.lock", [])

    def test_leading_slash_normalized(self):
        assert should_skip_file("/dist/bundle.js", PATTERNS)


class TestTruncatePatch:
    def test_short_patch_untouched(self):
        patch, truncated = truncate_patch("+short line", 100)
        assert patch == "+short line"
        assert truncated is False

    def test_truncates_at_line_boundary(self):
        patch = "\n".join(f"+line {i}" for i in range(100))
        result, truncated = truncate_patch(patch, 200)
        assert truncated is True
        assert result.endswith(TRUNCATION_MARKER)
        # Content before the marker must be complete lines from the original.
        content = result.removesuffix(TRUNCATION_MARKER)
        assert len(content) <= 200
        for line in content.splitlines():
            assert line.startswith("+line ")

    def test_zero_limit_disables_truncation(self):
        patch = "x" * 10_000
        result, truncated = truncate_patch(patch, 0)
        assert result == patch
        assert truncated is False


def _file(path: str, size: int) -> FileDiff:
    return FileDiff(path=path, patch="x" * size)


class TestChunkFileDiffs:
    def test_all_fit_in_one_chunk(self):
        files = [_file("a", 100), _file("b", 100)]
        chunks = chunk_file_diffs(files, 1000)
        assert len(chunks) == 1
        assert [f.path for f in chunks[0]] == ["a", "b"]

    def test_splits_when_over_limit(self):
        files = [_file("a", 600), _file("b", 600), _file("c", 600)]
        chunks = chunk_file_diffs(files, 1000)
        assert len(chunks) == 3

    def test_groups_greedily(self):
        files = [_file("a", 400), _file("b", 400), _file("c", 400)]
        chunks = chunk_file_diffs(files, 900)
        assert [len(c) for c in chunks] == [2, 1]

    def test_oversized_file_gets_own_chunk(self):
        files = [_file("small", 10), _file("huge", 5000), _file("tiny", 10)]
        chunks = chunk_file_diffs(files, 1000)
        assert [f.path for f in chunks[0]] == ["small"]
        assert [f.path for f in chunks[1]] == ["huge"]
        assert [f.path for f in chunks[2]] == ["tiny"]

    def test_empty_input(self):
        assert chunk_file_diffs([], 1000) == []

    def test_no_file_lost(self):
        files = [_file(str(i), 300) for i in range(10)]
        chunks = chunk_file_diffs(files, 1000)
        flattened = [f.path for chunk in chunks for f in chunk]
        assert flattened == [str(i) for i in range(10)]
