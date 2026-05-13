from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from review_db import (  # noqa: E402
    POSTGRES_DIALECT,
    SQLITE_DIALECT,
    UnsupportedReviewDbBackendError,
    active_calibration_counts,
    backfill_queue_counts,
    batched_values,
    connect_review_db,
    count_rows,
    external_item_counts,
    external_link_health_counts,
    recent_item_verdicts,
    recent_review_runs,
    review_db_config,
    review_run_counts,
    sqlite_db_path,
    table_counts,
)
from llreview import POSTGRES_COPY_NULL, postgres_copy_value, review_path_class  # noqa: E402


class ReviewDbDialectTests(unittest.TestCase):
    def test_sqlite_dialect_matches_existing_placeholders(self) -> None:
        self.assertEqual(SQLITE_DIALECT.placeholder(), "?")
        self.assertEqual(SQLITE_DIALECT.placeholders(3), "?,?,?")

    def test_postgres_dialect_is_available_but_not_connected(self) -> None:
        self.assertEqual(POSTGRES_DIALECT.placeholder(), "%s")
        self.assertEqual(POSTGRES_DIALECT.placeholders(3), "%s,%s,%s")

    def test_identifier_quote_escapes_double_quotes(self) -> None:
        self.assertEqual(SQLITE_DIALECT.quote_identifier('a"b'), '"a""b"')

    def test_batched_values_deduplicates_in_order(self) -> None:
        self.assertEqual(batched_values([1, 2, 1, 3], batch_size=2), [[1, 2], [3]])

    def test_review_path_class_preserves_dot_prefixed_paths(self) -> None:
        self.assertEqual(review_path_class(".github/workflows/ci.yml"), "ops_config")
        self.assertEqual(
            review_path_class("./.github/workflows/ci.yml"),
            "ops_config",
        )
        self.assertEqual(review_path_class(".private_docs/roadmap.md"), "docs")
        self.assertEqual(review_path_class(".env.example"), "ops_config")


class ReviewDbConfigTests(unittest.TestCase):
    def test_sqlite_path_resolves_normally(self) -> None:
        config = review_db_config("out/review-history/local-ai-review.db")
        self.assertEqual(config.backend, "sqlite")
        self.assertTrue(str(config.sqlite_path).endswith("out/review-history/local-ai-review.db"))

    def test_postgres_dsn_is_detected_before_path_resolution(self) -> None:
        config = review_db_config("postgresql://localhost/llreview")
        self.assertEqual(config.backend, "postgresql")
        self.assertEqual(config.target, "postgresql://localhost/llreview")

    def test_postgres_dsn_pathified_by_mistake_still_fails_closed(self) -> None:
        pathified = Path("postgresql://localhost/llreview").expanduser().resolve()
        with self.assertRaises(UnsupportedReviewDbBackendError):
            sqlite_db_path(pathified)

    def test_connect_postgres_dsn_fails_with_clear_backend_error(self) -> None:
        with self.assertRaises(UnsupportedReviewDbBackendError):
            connect_review_db("postgresql://localhost/llreview")


class ReviewDbAggregateTests(unittest.TestCase):
    def test_count_helpers_use_quoted_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "review.db"
            with sqlite3.connect(db_path) as connection:
                connection.execute('CREATE TABLE "odd table" (id INTEGER PRIMARY KEY)')
                connection.executemany('INSERT INTO "odd table" (id) VALUES (?)', [(1,), (2,)])
                connection.execute("CREATE TABLE empty_table (id INTEGER PRIMARY KEY)")
            with connect_review_db(db_path) as connection:
                self.assertEqual(count_rows(connection, "odd table"), 2)
                self.assertEqual(
                    table_counts(connection, ["odd table", "empty_table", "missing"]),
                    {"odd table": 2, "empty_table": 0, "missing": None},
                )

    def test_review_run_counts_support_repo_filter(self) -> None:
        with sqlite3.connect(":memory:") as connection:
            connection.execute(
                """
                CREATE TABLE review_run_summary (
                    repo TEXT,
                    useful_findings_fixed INTEGER,
                    findings_count INTEGER,
                    watch_items_count INTEGER,
                    diff_bytes INTEGER,
                    elapsed_seconds REAL
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO review_run_summary (
                    repo,
                    useful_findings_fixed,
                    findings_count,
                    watch_items_count,
                    diff_bytes,
                    elapsed_seconds
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    ("owner/repo", None, 2, 3, 100, 1.0),
                    ("owner/repo", 1, 0, 4, 200, 2.0),
                    ("other/repo", None, 5, 6, 300, 9.0),
                ],
            )

            self.assertEqual(
                review_run_counts(connection, repo="owner/repo"),
                {
                    "total": 2,
                    "unscored": 1,
                    "zero_finding_runs": 1,
                    "findings": 2,
                    "watch_items": 7,
                    "diff_bytes": 300,
                    "average_elapsed_seconds": 1.5,
                },
            )

    def test_external_item_counts_return_link_and_latest_verdict_mix(self) -> None:
        with sqlite3.connect(":memory:") as connection:
            connection.executescript(
                """
                CREATE TABLE external_items (
                    id INTEGER PRIMARY KEY,
                    repo TEXT NOT NULL,
                    source TEXT NOT NULL
                );
                CREATE TABLE item_links (
                    id INTEGER PRIMARY KEY,
                    review_item_id INTEGER NOT NULL,
                    external_item_id INTEGER NOT NULL
                );
                CREATE TABLE item_verdicts (
                    id INTEGER PRIMARY KEY,
                    target_kind TEXT NOT NULL,
                    target_id INTEGER NOT NULL,
                    verdict TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT ''
                );
                """
            )
            connection.executemany(
                "INSERT INTO external_items (id, repo, source) VALUES (?, ?, ?)",
                [
                    (1, "owner/repo", "github"),
                    (2, "owner/repo", "github"),
                    (3, "other/repo", "github"),
                ],
            )
            connection.executemany(
                "INSERT INTO item_links (review_item_id, external_item_id) VALUES (?, ?)",
                [(10, 1), (11, 1), (12, 3)],
            )
            connection.executemany(
                """
                INSERT INTO item_verdicts (id, target_kind, target_id, verdict, reason)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (1, "external_item", 1, "unclear", "old"),
                    (2, "external_item", 1, "useful", "fixed"),
                ],
            )

            counts = external_item_counts(connection, repo="owner/repo")

            self.assertEqual(counts["total"], 2)
            self.assertEqual(counts["linked"], 1)
            self.assertEqual(counts["unlinked"], 1)
            self.assertEqual(counts["link_rate"], "50.0%")
            self.assertEqual(
                counts["verdict_rows"],
                [
                    {
                        "source": "github",
                        "verdict": "unscored",
                        "reason": "(none)",
                        "total": 1,
                        "linked": 0,
                    },
                    {
                        "source": "github",
                        "verdict": "useful",
                        "reason": "fixed",
                        "total": 1,
                        "linked": 1,
                    },
                ],
            )
            self.assertEqual(external_link_health_counts(connection, repo="missing"), [])

    def test_backfill_queue_counts_group_records_and_totals(self) -> None:
        with sqlite3.connect(":memory:") as connection:
            connection.execute(
                """
                CREATE TABLE github_backfill_queue (
                    repo TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    skip_reason TEXT NOT NULL DEFAULT '',
                    actionable_external_comments INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO github_backfill_queue (
                    repo,
                    source_kind,
                    state,
                    skip_reason,
                    actionable_external_comments
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    ("owner/repo", "pr", "pending", "", 2),
                    ("owner/repo", "pr", "pending", "", 3),
                    ("owner/repo", "issue", "deferred", "too_large", 0),
                    ("other/repo", "pr", "pending", "", 7),
                ],
            )

            counts = backfill_queue_counts(connection, repo="owner/repo")

            self.assertEqual(counts["total"], 3)
            self.assertEqual(counts["signal"], 5)
            self.assertEqual(counts["by_state"], {"pending": 2, "deferred": 1})
            self.assertEqual(counts["by_source_state"], {"pr/pending": 2, "issue/deferred": 1})
            self.assertEqual(
                counts["records"],
                [
                    {
                        "source_kind": "pr",
                        "state": "pending",
                        "reason": "pending",
                        "count": 2,
                        "signal": 5,
                    },
                    {
                        "source_kind": "issue",
                        "state": "deferred",
                        "reason": "too_large",
                        "count": 1,
                        "signal": 0,
                    },
                ],
            )

    def test_active_calibration_counts_include_global_and_repo_scope(self) -> None:
        with sqlite3.connect(":memory:") as connection:
            connection.execute(
                """
                CREATE TABLE learning_calibrations (
                    id INTEGER PRIMARY KEY,
                    calibration_id TEXT NOT NULL,
                    scope_repo TEXT NOT NULL DEFAULT '',
                    path_class TEXT NOT NULL DEFAULT '',
                    signal_kind TEXT NOT NULL DEFAULT '',
                    instruction TEXT NOT NULL,
                    evidence_count INTEGER NOT NULL DEFAULT 0,
                    confidence TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    updated_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO learning_calibrations (
                    id,
                    calibration_id,
                    scope_repo,
                    path_class,
                    signal_kind,
                    instruction,
                    evidence_count,
                    confidence,
                    status,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (1, "cal-global", "", "docs", "missed", "global instruction", 3, "medium", "active", "2026-01-01"),
                    (2, "cal-local", "owner/repo", "py", "fp", "repo instruction with extra words", 4, "high", "active", "2026-01-02"),
                    (3, "cal-other", "other/repo", "js", "fp", "other instruction", 9, "high", "active", "2026-01-03"),
                    (4, "cal-paused", "owner/repo", "py", "fp", "paused instruction", 1, "low", "paused", "2026-01-04"),
                ],
            )

            counts = active_calibration_counts(
                connection,
                repo="owner/repo",
                limit=10,
                instruction_limit=12,
            )

            self.assertEqual(counts["active"], 2)
            self.assertEqual(
                [row["calibration_id"] for row in counts["recent"]],
                ["cal-local", "cal-global"],
            )
            self.assertEqual(counts["recent"][0]["scope_repo"], "owner/repo")
            self.assertEqual(counts["recent"][0]["instruction"], "repo inst...")

    def test_recent_review_runs_return_dashboard_rows(self) -> None:
        with sqlite3.connect(":memory:") as connection:
            connection.execute(
                """
                CREATE TABLE review_run_summary (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT,
                    repo TEXT,
                    pr_number INTEGER,
                    head_ref TEXT,
                    head_sha TEXT,
                    findings_count INTEGER,
                    watch_items_count INTEGER,
                    useful_findings_fixed INTEGER,
                    false_positives INTEGER,
                    unclear_findings INTEGER,
                    elapsed_seconds REAL
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO review_run_summary (
                    id,
                    created_at,
                    repo,
                    pr_number,
                    head_ref,
                    head_sha,
                    findings_count,
                    watch_items_count,
                    useful_findings_fixed,
                    false_positives,
                    unclear_findings,
                    elapsed_seconds
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (1, "2026-01-01", "owner/repo", 7, "main", "abc123456789ffff", 2, 3, None, None, None, 1.24),
                    (2, "2026-01-02", "owner/repo", 8, "feature", "def123456789ffff", 0, 1, 1, 0, 0, 2.56),
                    (3, "2026-01-03", "other/repo", 9, "other", "ghi123456789ffff", 4, 5, None, None, None, 9.0),
                ],
            )

            rows = recent_review_runs(connection, repo="owner/repo", limit=1)

            self.assertEqual(
                rows,
                [
                    {
                        "run_id": 2,
                        "created_at": "2026-01-02",
                        "repo": "owner/repo",
                        "pr_number": 8,
                        "head_ref": "feature",
                        "head_sha": "def123456789",
                        "findings": 0,
                        "watch_items": 1,
                        "unscored": False,
                        "useful_findings_fixed": 1,
                        "false_positives": 0,
                        "unclear_findings": 0,
                        "elapsed_seconds": 2.6,
                    }
                ],
            )

    def test_recent_item_verdicts_join_local_and_external_targets(self) -> None:
        with sqlite3.connect(":memory:") as connection:
            connection.executescript(
                """
                CREATE TABLE review_runs (
                    id INTEGER PRIMARY KEY,
                    repo TEXT NOT NULL
                );
                CREATE TABLE review_items (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER NOT NULL,
                    path TEXT NOT NULL
                );
                CREATE TABLE external_items (
                    id INTEGER PRIMARY KEY,
                    repo TEXT NOT NULL,
                    path TEXT NOT NULL
                );
                CREATE TABLE item_verdicts (
                    id INTEGER PRIMARY KEY,
                    target_kind TEXT NOT NULL,
                    target_id INTEGER NOT NULL,
                    verdict TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    scorer TEXT NOT NULL DEFAULT '',
                    scored_at TEXT NOT NULL DEFAULT ''
                );
                """
            )
            connection.executemany(
                "INSERT INTO review_runs (id, repo) VALUES (?, ?)",
                [(1, "owner/repo"), (2, "other/repo")],
            )
            connection.executemany(
                "INSERT INTO review_items (id, run_id, path) VALUES (?, ?, ?)",
                [(10, 1, "tests/foo_test.py"), (11, 2, "src/other.py")],
            )
            connection.executemany(
                "INSERT INTO external_items (id, repo, path) VALUES (?, ?, ?)",
                [(20, "owner/repo", "docs/guide.md"), (21, "other/repo", "src/other.ts")],
            )
            connection.executemany(
                """
                INSERT INTO item_verdicts (
                    id,
                    target_kind,
                    target_id,
                    verdict,
                    reason,
                    scorer,
                    scored_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (1, "review_item", 10, "useful", "", "me", "2026-01-01"),
                    (2, "external_item", 20, "missed_by_local", "teacher_model_valid", "me", "2026-01-02"),
                    (3, "review_item", 11, "false_positive", "noise", "me", "2026-01-03"),
                    (4, "external_item", 21, "unclear", "needs_context", "me", "2026-01-04"),
                ],
            )

            rows = recent_item_verdicts(
                connection,
                repo="owner/repo",
                limit=10,
                path_classifier=lambda path: "docs" if path.endswith(".md") else "test",
            )

            self.assertEqual(
                rows,
                [
                    {
                        "verdict_id": 2,
                        "target_kind": "external_item",
                        "target_id": 20,
                        "verdict": "missed_by_local",
                        "reason": "teacher_model_valid",
                        "scorer": "me",
                        "scored_at": "2026-01-02",
                        "repo": "owner/repo",
                        "path_class": "docs",
                    },
                    {
                        "verdict_id": 1,
                        "target_kind": "review_item",
                        "target_id": 10,
                        "verdict": "useful",
                        "reason": "(none)",
                        "scorer": "me",
                        "scored_at": "2026-01-01",
                        "repo": "owner/repo",
                        "path_class": "test",
                    },
                ],
            )


class ReviewDbPostgresParityTests(unittest.TestCase):
    def test_postgres_copy_value_uses_collision_checked_null_marker(self) -> None:
        self.assertEqual(postgres_copy_value(None), POSTGRES_COPY_NULL)
        self.assertEqual(postgres_copy_value("literal"), "literal")
        with self.assertRaises(SystemExit):
            postgres_copy_value(POSTGRES_COPY_NULL)


if __name__ == "__main__":
    unittest.main()
