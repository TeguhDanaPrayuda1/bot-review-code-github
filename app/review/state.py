"""Idempotency state: which commit SHAs have already been reviewed per PR.

Backed by SQLite so state survives restarts and a single commit is never
reviewed twice, even when both a ``push`` and a ``pull_request.synchronize``
event fire for the same head SHA.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class ReviewStateStore:
    """Small thread-safe SQLite wrapper for review bookkeeping."""

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reviewed_commits (
                    repo        TEXT NOT NULL,
                    pr_number   INTEGER NOT NULL,
                    sha         TEXT NOT NULL,
                    reviewed_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (repo, pr_number, sha)
                )
                """
            )
            self._conn.commit()

    def is_reviewed(self, repo: str, pr_number: int, sha: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM reviewed_commits WHERE repo=? AND pr_number=? AND sha=?",
                (repo, pr_number, sha),
            ).fetchone()
        return row is not None

    def try_claim(self, repo: str, pr_number: int, sha: str) -> bool:
        """Atomically mark a SHA as being reviewed.

        Returns False when it was already claimed (someone else reviewed or is
        reviewing it) — the caller must then skip the review.
        """
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO reviewed_commits (repo, pr_number, sha) VALUES (?, ?, ?)",
                    (repo, pr_number, sha),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def release(self, repo: str, pr_number: int, sha: str) -> None:
        """Undo a claim after a failed review so it can be retried later."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM reviewed_commits WHERE repo=? AND pr_number=? AND sha=?",
                (repo, pr_number, sha),
            )
            self._conn.commit()

    def last_reviewed_sha(self, repo: str, pr_number: int) -> str | None:
        """Most recently reviewed SHA for a PR (basis for incremental review)."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT sha FROM reviewed_commits
                WHERE repo=? AND pr_number=?
                ORDER BY reviewed_at DESC, rowid DESC LIMIT 1
                """,
                (repo, pr_number),
            ).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
