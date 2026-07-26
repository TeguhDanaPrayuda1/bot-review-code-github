"""Tests for unified-diff parsing and line-number mapping."""

from app.diff.parser import (
    FileDiff,
    file_diff_from_api,
    nearest_commentable_line,
    parse_patch,
    render_annotated_diff,
)

SIMPLE_PATCH = """\
@@ -1,4 +1,5 @@
 def add(a, b):
-    return a+b
+    result = a + b
+    return result

 def sub(a, b):"""

MULTI_HUNK_PATCH = """\
@@ -10,3 +10,4 @@ def foo():
 x = 1
+y = 2
 z = 3
@@ -50,2 +51,3 @@ def bar():
 a = 1
+b = 2
 c = 3"""


class TestParsePatch:
    def test_empty_patch_returns_no_hunks(self):
        assert parse_patch("") == []
        assert parse_patch(None) == []  # type: ignore[arg-type]

    def test_single_hunk_header_parsed(self):
        hunks = parse_patch(SIMPLE_PATCH)
        assert len(hunks) == 1
        hunk = hunks[0]
        assert (hunk.old_start, hunk.old_count) == (1, 4)
        assert (hunk.new_start, hunk.new_count) == (1, 5)

    def test_line_kinds_and_numbers(self):
        hunk = parse_patch(SIMPLE_PATCH)[0]
        kinds = [line.kind for line in hunk.lines]
        assert kinds == ["context", "del", "add", "add", "context", "context"]

        # Added lines get consecutive new-file numbers.
        added = [line.new_lineno for line in hunk.lines if line.kind == "add"]
        assert added == [2, 3]

        # Deleted lines only carry the old number.
        deleted = [line for line in hunk.lines if line.kind == "del"][0]
        assert deleted.old_lineno == 2
        assert deleted.new_lineno is None

        # Context lines carry both.
        first = hunk.lines[0]
        assert (first.old_lineno, first.new_lineno) == (1, 1)

    def test_multiple_hunks_track_numbers_independently(self):
        hunks = parse_patch(MULTI_HUNK_PATCH)
        assert len(hunks) == 2
        added_second = [l.new_lineno for l in hunks[1].lines if l.kind == "add"]
        assert added_second == [52]

    def test_hunk_header_without_counts(self):
        hunks = parse_patch("@@ -1 +1 @@\n-old\n+new")
        assert hunks[0].old_count == 1
        assert hunks[0].new_count == 1
        assert [l.kind for l in hunks[0].lines] == ["del", "add"]

    def test_no_newline_marker_ignored(self):
        patch = "@@ -1 +1 @@\n-old\n+new\n\\ No newline at end of file"
        hunk = parse_patch(patch)[0]
        assert len(hunk.lines) == 2


class TestFileDiff:
    def test_added_and_commentable_lines(self):
        diff = FileDiff(path="a.py", patch=SIMPLE_PATCH, hunks=parse_patch(SIMPLE_PATCH))
        assert diff.added_lines == {2, 3}
        # Every RIGHT-side line in the hunk is commentable.
        assert diff.commentable_lines == {1, 2, 3, 4, 5}

    def test_from_api_payload(self):
        api_file = {
            "filename": "src/new_name.py",
            "status": "renamed",
            "previous_filename": "src/old_name.py",
            "patch": SIMPLE_PATCH,
        }
        diff = file_diff_from_api(api_file)
        assert diff.path == "src/new_name.py"
        assert diff.status == "renamed"
        assert diff.previous_path == "src/old_name.py"
        assert len(diff.hunks) == 1

    def test_from_api_payload_binary_file(self):
        diff = file_diff_from_api({"filename": "logo.png", "status": "added", "patch": None})
        assert diff.hunks == []
        assert diff.commentable_lines == set()


class TestNearestCommentableLine:
    def _diff(self) -> FileDiff:
        return FileDiff(path="a.py", patch=MULTI_HUNK_PATCH, hunks=parse_patch(MULTI_HUNK_PATCH))

    def test_exact_match(self):
        assert nearest_commentable_line(self._diff(), 11) == 11

    def test_nearby_line_snaps(self):
        # 14 is not in a hunk; 12 is the closest commentable line (distance 2).
        assert nearest_commentable_line(self._diff(), 14) == 12

    def test_far_line_returns_none(self):
        assert nearest_commentable_line(self._diff(), 30) is None

    def test_empty_diff_returns_none(self):
        empty = FileDiff(path="a.py", patch="", hunks=[])
        assert nearest_commentable_line(empty, 5) is None


class TestRenderAnnotatedDiff:
    def test_annotation_format(self):
        diff = FileDiff(path="a.py", status="modified", patch=SIMPLE_PATCH,
                        hunks=parse_patch(SIMPLE_PATCH))
        rendered = render_annotated_diff(diff)
        assert "### File: a.py (modified)" in rendered
        assert "2|+ " in rendered  # added line with new number
        assert "    |- " in rendered  # deleted line has no new number
        assert "1|  def add(a, b):" in rendered  # context line

    def test_rename_note_included(self):
        diff = FileDiff(path="b.py", status="renamed", previous_path="a.py",
                        patch=SIMPLE_PATCH, hunks=parse_patch(SIMPLE_PATCH))
        assert "(renamed from a.py)" in render_annotated_diff(diff)
