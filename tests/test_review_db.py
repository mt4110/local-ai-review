from __future__ import annotations

import argparse
import contextlib
import io
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
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
from llreview import (  # noqa: E402
    POSTGRES_COPY_NULL,
    BACKFILL_DEFAULT_MAX_CHANGED_LINES,
    BACKFILL_DEFAULT_MIN_INTERVAL_MINUTES,
    command_import_github_history,
    command_import_github_reviews,
    ensure_db_schema,
    external_items_from_comments,
    learning_calibration_statuses_by_candidate,
    postgres_copy_value,
    review_path_class,
)


@contextlib.contextmanager
def sqlite_connection(db_path: Path):
    with contextlib.closing(sqlite3.connect(db_path)) as connection:
        with connection:
            yield connection


@contextlib.contextmanager
def sqlite_memory_connection():
    with contextlib.closing(sqlite3.connect(":memory:")) as connection:
        with connection:
            yield connection


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

    def test_learning_calibration_statuses_use_newest_row_per_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "review.db"
            with sqlite_connection(db_path) as connection:
                connection.row_factory = sqlite3.Row
                connection.executescript(
                    """
                    CREATE TABLE learning_calibrations (
                        id INTEGER PRIMARY KEY,
                        candidate_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    INSERT INTO learning_calibrations (
                        id,
                        candidate_id,
                        status,
                        updated_at
                    ) VALUES
                        (1, 'candidate-a', 'active', '2026-05-01T00:00:00Z'),
                        (2, 'candidate-a', 'retired', '2026-05-02T00:00:00Z'),
                        (3, 'candidate-b', 'paused', '2026-05-02T00:00:00Z'),
                        (4, 'candidate-b', 'active', '2026-05-02T00:00:00Z');
                    """
                )

                statuses = learning_calibration_statuses_by_candidate(connection)

            self.assertEqual(statuses["candidate-a"], "retired")
            self.assertEqual(statuses["candidate-b"], "active")


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


class ImportGithubHistoryTests(unittest.TestCase):
    def import_history_args(self, db_path: Path, *, dry_run: bool, one: bool = True) -> argparse.Namespace:
        return argparse.Namespace(
            project_dir=str(ROOT),
            repo=None,
            db=str(db_path),
            owner="mt4110",
            local_root=[],
            remote_repo_limit=0,
            remote_pr_limit=0,
            remote_per_repo_pr_limit=0,
            local_repo_limit=0,
            local_pr_limit=0,
            local_per_repo_pr_limit=0,
            limit=5,
            max_doc_ratio=0.70,
            max_generated_ratio=0.50,
            max_changed_lines=BACKFILL_DEFAULT_MAX_CHANGED_LINES,
            dry_run=dry_run,
            one=one,
            min_interval_minutes=BACKFILL_DEFAULT_MIN_INTERVAL_MINUTES,
            retry_delay_minutes=60,
            min_link_score=0.55,
            no_verdicts=False,
            pin_queue_head_sha=False,
            refresh_queue=False,
            remote_only=True,
            local_only=False,
            no_issue_comments=False,
        )

    def seed_remote_queue_row(
        self,
        db_path: Path,
        *,
        repo: str = "mt4110/example",
        state: str = "pending",
        doc_ratio: float = 0.0,
        generated_ratio: float = 0.0,
        changed_files: int = 1,
        changed_lines: int = 12,
        signal: int = 1,
    ) -> None:
        ensure_db_schema(db_path)
        with sqlite_connection(db_path) as connection:
            connection.execute(
                """
                INSERT INTO github_backfill_queue (
                    repo,
                    pr_number,
                    source_kind,
                    remote_state,
                    state,
                    priority,
                    updated_at_github,
                    merged_at,
                    head_sha,
                    doc_ratio,
                    generated_ratio,
                    changed_files,
                    changed_lines,
                    actionable_external_comments,
                    note
                ) VALUES (?, 42, 'remote_github', 'available', ?, 1,
                    '2026-05-01T00:00:00Z',
                    '2026-05-01T00:00:00Z',
                    'abc123',
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    'seed row'
                )
                """,
                (repo, state, doc_ratio, generated_ratio, changed_files, changed_lines, signal),
            )

    def test_issue_comment_signal_filter_requires_actionable_anchor(self) -> None:
        items = external_items_from_comments(
            repo="mt4110/example",
            pr_number=42,
            default_head_sha="abc123",
            import_head_sha="abc123",
            prefer_default_head_sha=True,
            comments=[
                {"id": 1, "body": "LGTM", "user": {"login": "reviewer"}},
                {"id": 2, "body": "Summary: looks fine\n\nTests not run", "user": {"login": "bot"}},
                {
                    "id": 3,
                    "body": "`scripts/app.py` is missing validation for the new config.",
                    "user": {"login": "reviewer"},
                },
                {"id": 4, "body": "Should we merge this?", "user": {"login": "reviewer"}},
                {"id": 5, "body": "Dockerfile is missing the runtime env.", "user": {"login": "reviewer"}},
                {"id": 6, "body": "Security looks fine.", "user": {"login": "reviewer"}},
                {"id": 7, "body": "README.md please.", "user": {"login": "reviewer"}},
            ],
            comment_kind="issue_comment",
        )

        self.assertEqual([item.github_comment_id for item in items], ["issue_comment:3", "issue_comment:5"])

    def test_dry_run_preview_without_refresh_queue_does_not_create_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            local_root = root / "local-root"
            local_root.mkdir()
            db_path = root / "missing.db"
            args = argparse.Namespace(
                project_dir=str(ROOT),
                repo=None,
                db=str(db_path),
                owner="mt4110",
                local_root=[str(local_root)],
                remote_repo_limit=0,
                remote_pr_limit=0,
                remote_per_repo_pr_limit=0,
                local_repo_limit=5,
                local_pr_limit=5,
                local_per_repo_pr_limit=5,
                limit=5,
                max_doc_ratio=0.70,
                max_generated_ratio=0.50,
                max_changed_lines=BACKFILL_DEFAULT_MAX_CHANGED_LINES,
                dry_run=True,
                one=False,
                min_interval_minutes=BACKFILL_DEFAULT_MIN_INTERVAL_MINUTES,
                retry_delay_minutes=60,
                min_link_score=0.55,
                no_verdicts=False,
                pin_queue_head_sha=False,
                refresh_queue=False,
                remote_only=False,
                local_only=True,
                no_issue_comments=False,
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                command_import_github_history(args)

            self.assertFalse(db_path.exists())
            self.assertIn("DRY RUN: queue not written", output.getvalue())

    def test_one_dry_run_without_queue_does_not_create_db_or_require_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "missing.db"
            args = argparse.Namespace(
                project_dir=str(ROOT),
                repo=None,
                db=str(db_path),
                owner="mt4110",
                local_root=[],
                remote_repo_limit=0,
                remote_pr_limit=0,
                remote_per_repo_pr_limit=0,
                local_repo_limit=0,
                local_pr_limit=0,
                local_per_repo_pr_limit=0,
                limit=5,
                max_doc_ratio=0.70,
                max_generated_ratio=0.50,
                max_changed_lines=BACKFILL_DEFAULT_MAX_CHANGED_LINES,
                dry_run=True,
                one=True,
                min_interval_minutes=BACKFILL_DEFAULT_MIN_INTERVAL_MINUTES,
                retry_delay_minutes=60,
                min_link_score=0.55,
                no_verdicts=False,
                pin_queue_head_sha=False,
                refresh_queue=False,
                remote_only=True,
                local_only=False,
                no_issue_comments=False,
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                command_import_github_history(args)

            self.assertFalse(db_path.exists())
            self.assertIn("No github_backfill_queue table", output.getvalue())

    def test_one_dry_run_rejects_refresh_queue(self) -> None:
        args = argparse.Namespace(
            project_dir=str(ROOT),
            repo=None,
            db=":memory:",
            owner="mt4110",
            local_root=[],
            remote_repo_limit=0,
            remote_pr_limit=0,
            remote_per_repo_pr_limit=0,
            local_repo_limit=0,
            local_pr_limit=0,
            local_per_repo_pr_limit=0,
            limit=5,
            max_doc_ratio=0.70,
            max_generated_ratio=0.50,
            max_changed_lines=BACKFILL_DEFAULT_MAX_CHANGED_LINES,
            dry_run=True,
            one=True,
            min_interval_minutes=BACKFILL_DEFAULT_MIN_INTERVAL_MINUTES,
            retry_delay_minutes=60,
            min_link_score=0.55,
            no_verdicts=False,
            pin_queue_head_sha=False,
            refresh_queue=True,
            remote_only=True,
            local_only=False,
            no_issue_comments=False,
        )

        with self.assertRaisesRegex(SystemExit, "dry-run must not change queue state"):
            command_import_github_history(args)

    def test_import_github_reviews_dry_run_does_not_create_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            comments_path = root / "comments.json"
            db_path = root / "missing.db"
            comments_path.write_text(
                """
                [
                  {
                    "id": 1001,
                    "body": "Missing validation for the new option.",
                    "path": "scripts/app.py",
                    "line": 12,
                    "user": {"login": "reviewer"}
                  }
                ]
                """,
                encoding="utf-8",
            )
            args = argparse.Namespace(
                project_dir=str(ROOT),
                repo="mt4110/example",
                db=str(db_path),
                pr=42,
                run=None,
                include_issue_comments=False,
                comments_json=str(comments_path),
                issue_comments_json=None,
                head_sha="",
                min_link_score=0.55,
                dry_run=True,
                no_verdicts=False,
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                command_import_github_reviews(args)

            self.assertFalse(db_path.exists())
            self.assertIn("DRY RUN: would import 1 external review items", output.getvalue())

    def test_one_dry_run_reads_queue_without_mutating_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "review.db"
            self.seed_remote_queue_row(db_path)
            args = self.import_history_args(db_path, dry_run=True)

            def fake_import(import_args: argparse.Namespace) -> None:
                self.assertTrue(import_args.dry_run)
                print("DRY RUN: would import 2 external review items from mt4110/example#42")
                print("Link candidate runs: 1")
                print("Link candidates: 3")
                print("Would create/update links: 1")
                print("Would remove stale external items: 0")
                print("Sources: human=2")

            output = io.StringIO()
            with mock.patch("llreview.command_import_github_reviews", side_effect=fake_import):
                with contextlib.redirect_stdout(output):
                    command_import_github_history(args)

            with sqlite_connection(db_path) as connection:
                row = connection.execute(
                    """
                    SELECT state, skip_reason, attempt_count, last_attempt_at, next_attempt_at
                    FROM github_backfill_queue
                    """
                ).fetchone()
                external_count = connection.execute("SELECT COUNT(*) FROM external_items").fetchone()[0]
                link_count = connection.execute("SELECT COUNT(*) FROM item_links").fetchone()[0]
                verdict_count = connection.execute("SELECT COUNT(*) FROM item_verdicts").fetchone()[0]

            self.assertEqual(row, ("pending", "", 0, None, None))
            self.assertEqual(external_count, 0)
            self.assertEqual(link_count, 0)
            self.assertEqual(verdict_count, 0)
            self.assertIn("Selected remote queue row", output.getvalue())
            self.assertIn("DRY RUN: external items will not be written", output.getvalue())
            self.assertIn("Would create/update links: 1", output.getvalue())

    def test_one_real_marks_owner_mismatch_skipped_without_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "review.db"
            self.seed_remote_queue_row(db_path, repo="other/example")
            args = self.import_history_args(db_path, dry_run=False)

            output = io.StringIO()
            with mock.patch("llreview.command_import_github_reviews") as importer:
                with contextlib.redirect_stdout(output):
                    command_import_github_history(args)

            importer.assert_not_called()
            with sqlite_connection(db_path) as connection:
                row = connection.execute(
                    """
                    SELECT state, skip_reason, attempt_count, last_attempt_at
                    FROM github_backfill_queue
                    """
                ).fetchone()

            self.assertEqual(row[0], "skipped")
            self.assertEqual(row[1], "skipped_owner_not_mt4110")
            self.assertEqual(row[2], 1)
            self.assertIsNotNone(row[3])
            self.assertIn("SKIPPED: one-at-a-time import stopped before writes", output.getvalue())

    def test_one_real_respects_twenty_minute_rate_gate_before_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "review.db"
            self.seed_remote_queue_row(db_path)
            with sqlite_connection(db_path) as connection:
                connection.execute(
                    """
                    UPDATE github_backfill_queue
                    SET last_attempt_at = CURRENT_TIMESTAMP,
                        attempt_count = 1
                    """
                )
            args = self.import_history_args(db_path, dry_run=False)

            output = io.StringIO()
            with mock.patch("llreview.github_token") as token:
                with mock.patch("llreview.command_import_github_reviews") as importer:
                    with contextlib.redirect_stdout(output):
                        command_import_github_history(args)

            token.assert_not_called()
            importer.assert_not_called()
            with sqlite_connection(db_path) as connection:
                row = connection.execute(
                    """
                    SELECT state, attempt_count
                    FROM github_backfill_queue
                    """
                ).fetchone()

            self.assertEqual(row, ("pending", 1))
            self.assertIn("DEFERRED: remote import rate limit is active", output.getvalue())

    def test_one_real_rechecks_fork_and_merged_gates_before_import(self) -> None:
        cases = [
            ("fork", {"fork": True}, {"merged_at": "2026-05-02T00:00:00Z"}, "skipped_fork"),
            ("not_merged", {"fork": False}, {"merged_at": ""}, "skipped_not_merged"),
        ]
        for name, repo_overrides, pr_overrides, expected_reason in cases:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmpdir:
                    db_path = Path(tmpdir) / "review.db"
                    self.seed_remote_queue_row(db_path)
                    args = self.import_history_args(db_path, dry_run=False)

                    def fake_github_request(path: str, token: str) -> object:
                        if path == "/repos/mt4110/example":
                            return {
                                "full_name": "mt4110/example",
                                "owner": {"login": "mt4110"},
                                **repo_overrides,
                            }
                        if path == "/repos/mt4110/example/pulls/42":
                            return {
                                "number": 42,
                                "updated_at": "2026-05-03T00:00:00Z",
                                "title": f"Preflight {name}",
                                "head": {"sha": "def456"},
                                **pr_overrides,
                            }
                        raise AssertionError(path)

                    output = io.StringIO()
                    with mock.patch("llreview.github_token", return_value=("token", "test token")):
                        with mock.patch("llreview.github_request", side_effect=fake_github_request):
                            with mock.patch("llreview.command_import_github_reviews") as importer:
                                with contextlib.redirect_stdout(output):
                                    command_import_github_history(args)

                    importer.assert_not_called()
                    with sqlite_connection(db_path) as connection:
                        row = connection.execute(
                            """
                            SELECT state, skip_reason, attempt_count
                            FROM github_backfill_queue
                            """
                        ).fetchone()

                    self.assertEqual(row, ("skipped", expected_reason, 1))
                    self.assertIn("SKIPPED: one-at-a-time import stopped before writes", output.getvalue())

    def test_one_real_skips_stale_head_when_latest_queue_row_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "review.db"
            self.seed_remote_queue_row(db_path, changed_lines=10, signal=1)
            with sqlite_connection(db_path) as connection:
                connection.execute(
                    """
                    INSERT INTO github_backfill_queue (
                        repo,
                        pr_number,
                        source_kind,
                        remote_state,
                        state,
                        priority,
                        updated_at_github,
                        merged_at,
                        head_sha,
                        doc_ratio,
                        generated_ratio,
                        changed_files,
                        changed_lines,
                        actionable_external_comments,
                        note
                    ) VALUES (
                        'mt4110/example',
                        42,
                        'remote_github',
                        'available',
                        'pending',
                        2,
                        '2026-05-03T00:00:00Z',
                        '2026-05-03T00:00:00Z',
                        'def456',
                        0,
                        0,
                        1,
                        20,
                        2,
                        'newer row'
                    )
                    """
                )
            args = self.import_history_args(db_path, dry_run=False)

            def fake_github_request(path: str, token: str) -> object:
                if path == "/repos/mt4110/example":
                    return {
                        "full_name": "mt4110/example",
                        "fork": False,
                        "owner": {"login": "mt4110"},
                    }
                if path == "/repos/mt4110/example/pulls/42":
                    return {
                        "number": 42,
                        "merged_at": "2026-05-03T00:00:00Z",
                        "updated_at": "2026-05-03T00:00:00Z",
                        "title": "Already refreshed",
                        "head": {"sha": "def456"},
                    }
                raise AssertionError(path)

            output = io.StringIO()
            with mock.patch("llreview.github_token", return_value=("token", "test token")):
                with mock.patch("llreview.github_request", side_effect=fake_github_request):
                    with mock.patch("llreview.github_paginated_request") as paginated:
                        with mock.patch("llreview.backfill_actionable_external_count") as count_comments:
                            with mock.patch("llreview.command_import_github_reviews") as importer:
                                with contextlib.redirect_stdout(output):
                                    command_import_github_history(args)

            paginated.assert_not_called()
            count_comments.assert_not_called()
            importer.assert_not_called()
            with sqlite_connection(db_path) as connection:
                rows = connection.execute(
                    """
                    SELECT head_sha, state, skip_reason, attempt_count
                    FROM github_backfill_queue
                    ORDER BY id
                    """
                ).fetchall()

            self.assertEqual(rows[0], ("abc123", "skipped", "skipped_duplicate_queue_head", 1))
            self.assertEqual(rows[1], ("def456", "pending", "", 0))
            self.assertIn("SKIPPED: one-at-a-time import stopped before writes", output.getvalue())

    def test_one_real_rechecks_docs_heavy_before_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "review.db"
            self.seed_remote_queue_row(db_path, doc_ratio=0.0, changed_lines=10, signal=1)
            args = self.import_history_args(db_path, dry_run=False)

            def fake_github_request(path: str, token: str) -> object:
                if path == "/repos/mt4110/example":
                    return {
                        "full_name": "mt4110/example",
                        "fork": False,
                        "owner": {"login": "mt4110"},
                    }
                if path == "/repos/mt4110/example/pulls/42":
                    return {
                        "number": 42,
                        "merged_at": "2026-05-02T00:00:00Z",
                        "updated_at": "2026-05-03T00:00:00Z",
                        "title": "Docs mostly changed",
                        "head": {"sha": "def456"},
                    }
                raise AssertionError(path)

            def fake_paginated_request(path: str, token: str) -> list[object]:
                self.assertEqual(path, "/repos/mt4110/example/pulls/42/files")
                return [
                    {
                        "filename": "README.md",
                        "changes": 100,
                        "additions": 80,
                        "deletions": 20,
                        "status": "modified",
                    }
                ]

            output = io.StringIO()
            with mock.patch("llreview.github_token", return_value=("token", "test token")):
                with mock.patch("llreview.github_request", side_effect=fake_github_request):
                    with mock.patch("llreview.github_paginated_request", side_effect=fake_paginated_request):
                        with mock.patch("llreview.command_import_github_reviews") as importer:
                            with contextlib.redirect_stdout(output):
                                command_import_github_history(args)

            importer.assert_not_called()
            with sqlite_connection(db_path) as connection:
                row = connection.execute(
                    """
                    SELECT state, skip_reason, attempt_count, head_sha, doc_ratio, changed_lines
                    FROM github_backfill_queue
                    """
                ).fetchone()

            self.assertEqual(row[0], "skipped")
            self.assertEqual(row[1], "skipped_docs_heavy")
            self.assertEqual(row[2], 1)
            self.assertEqual(row[3], "def456")
            self.assertEqual(row[4], 1.0)
            self.assertEqual(row[5], 100)
            self.assertIn("Preflight remote row: state=skipped", output.getvalue())

    def test_one_real_reports_large_diff_as_deferred_before_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "review.db"
            self.seed_remote_queue_row(db_path, changed_lines=10, signal=3)
            args = self.import_history_args(db_path, dry_run=False)
            args.max_changed_lines = 10

            def fake_github_request(path: str, token: str) -> object:
                if path == "/repos/mt4110/example":
                    return {
                        "full_name": "mt4110/example",
                        "fork": False,
                        "owner": {"login": "mt4110"},
                    }
                if path == "/repos/mt4110/example/pulls/42":
                    return {
                        "number": 42,
                        "merged_at": "2026-05-02T00:00:00Z",
                        "updated_at": "2026-05-03T00:00:00Z",
                        "title": "Too large for one-at-a-time import",
                        "head": {"sha": "def456"},
                    }
                raise AssertionError(path)

            output = io.StringIO()
            with mock.patch("llreview.github_token", return_value=("token", "test token")):
                with mock.patch("llreview.github_request", side_effect=fake_github_request):
                    with mock.patch(
                        "llreview.github_paginated_request",
                        return_value=[
                            {
                                "filename": "scripts/app.py",
                                "changes": 20,
                                "additions": 18,
                                "deletions": 2,
                                "status": "modified",
                            }
                        ],
                    ):
                        with mock.patch("llreview.backfill_actionable_external_count") as count_comments:
                            with mock.patch("llreview.command_import_github_reviews") as importer:
                                with contextlib.redirect_stdout(output):
                                    command_import_github_history(args)

            count_comments.assert_not_called()
            importer.assert_not_called()
            with sqlite_connection(db_path) as connection:
                row = connection.execute(
                    """
                    SELECT state, skip_reason, attempt_count, actionable_external_comments
                    FROM github_backfill_queue
                    """
                ).fetchone()

            self.assertEqual(row, ("deferred", "deferred_large_diff", 1, 3))
            self.assertIn("Preflight remote row: state=deferred", output.getvalue())
            self.assertIn("DEFERRED: one-at-a-time import stopped before writes", output.getvalue())
            self.assertNotIn("SKIPPED: one-at-a-time import stopped before writes", output.getvalue())

    def test_one_real_rechecks_generated_and_actionable_gates_before_import(self) -> None:
        cases = [
            (
                "generated",
                [
                    {
                        "filename": "package-lock.json",
                        "changes": 100,
                        "additions": 90,
                        "deletions": 10,
                        "status": "modified",
                    }
                ],
                2,
                "skipped_generated_heavy",
                0.0,
                1.0,
            ),
            (
                "no_actionable",
                [
                    {
                        "filename": "scripts/app.py",
                        "changes": 20,
                        "additions": 18,
                        "deletions": 2,
                        "status": "modified",
                    }
                ],
                0,
                "skipped_no_actionable_external_comments",
                0.0,
                0.0,
            ),
        ]
        for name, files, actionable_count, expected_reason, expected_doc, expected_generated in cases:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmpdir:
                    db_path = Path(tmpdir) / "review.db"
                    self.seed_remote_queue_row(db_path, changed_lines=10, signal=1)
                    args = self.import_history_args(db_path, dry_run=False)

                    def fake_github_request(path: str, token: str) -> object:
                        if path == "/repos/mt4110/example":
                            return {
                                "full_name": "mt4110/example",
                                "fork": False,
                                "owner": {"login": "mt4110"},
                            }
                        if path == "/repos/mt4110/example/pulls/42":
                            return {
                                "number": 42,
                                "merged_at": "2026-05-02T00:00:00Z",
                                "updated_at": "2026-05-03T00:00:00Z",
                                "title": f"Preflight {name}",
                                "head": {"sha": "def456"},
                            }
                        raise AssertionError(path)

                    output = io.StringIO()
                    with mock.patch("llreview.github_token", return_value=("token", "test token")):
                        with mock.patch("llreview.github_request", side_effect=fake_github_request):
                            with mock.patch("llreview.github_paginated_request", return_value=files):
                                with mock.patch(
                                    "llreview.backfill_actionable_external_count",
                                    return_value=actionable_count,
                                ):
                                    with mock.patch("llreview.command_import_github_reviews") as importer:
                                        with contextlib.redirect_stdout(output):
                                            command_import_github_history(args)

                    importer.assert_not_called()
                    with sqlite_connection(db_path) as connection:
                        row = connection.execute(
                            """
                            SELECT state, skip_reason, attempt_count, doc_ratio, generated_ratio
                            FROM github_backfill_queue
                            """
                        ).fetchone()

                    self.assertEqual(row[0], "skipped")
                    self.assertEqual(row[1], expected_reason)
                    self.assertEqual(row[2], 1)
                    self.assertEqual(row[3], expected_doc)
                    self.assertEqual(row[4], expected_generated)
                    self.assertIn("SKIPPED: one-at-a-time import stopped before writes", output.getvalue())

    def test_one_real_marks_duplicate_without_dropping_existing_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "review.db"
            self.seed_remote_queue_row(db_path, changed_lines=10, signal=3)
            with sqlite_connection(db_path) as connection:
                connection.execute(
                    """
                    INSERT INTO external_items (
                        repo,
                        pr_number,
                        source,
                        body,
                        fingerprint
                    ) VALUES (
                        'mt4110/example',
                        42,
                        'copilot',
                        'Imported already',
                        'fingerprint'
                    )
                    """
                )
            args = self.import_history_args(db_path, dry_run=False)

            def fake_github_request(path: str, token: str) -> object:
                if path == "/repos/mt4110/example":
                    return {
                        "full_name": "mt4110/example",
                        "fork": False,
                        "owner": {"login": "mt4110"},
                    }
                if path == "/repos/mt4110/example/pulls/42":
                    return {
                        "number": 42,
                        "merged_at": "2026-05-02T00:00:00Z",
                        "updated_at": "2026-05-03T00:00:00Z",
                        "title": "Already imported",
                        "head": {"sha": "def456"},
                    }
                raise AssertionError(path)

            output = io.StringIO()
            with mock.patch("llreview.github_token", return_value=("token", "test token")):
                with mock.patch("llreview.github_request", side_effect=fake_github_request):
                    with mock.patch(
                        "llreview.github_paginated_request",
                        return_value=[
                            {
                                "filename": "scripts/app.py",
                                "changes": 20,
                                "additions": 18,
                                "deletions": 2,
                                "status": "modified",
                            }
                        ],
                    ):
                        with mock.patch("llreview.backfill_actionable_external_count") as count_comments:
                            with mock.patch("llreview.command_import_github_reviews") as importer:
                                with contextlib.redirect_stdout(output):
                                    command_import_github_history(args)

            count_comments.assert_not_called()
            importer.assert_not_called()
            with sqlite_connection(db_path) as connection:
                row = connection.execute(
                    """
                    SELECT state, skip_reason, actionable_external_comments
                    FROM github_backfill_queue
                    """
                ).fetchone()

            self.assertEqual(row, ("skipped", "skipped_duplicate_import", 3))
            self.assertIn("SKIPPED: one-at-a-time import stopped before writes", output.getvalue())

    def test_one_real_imports_one_pending_row_after_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "review.db"
            self.seed_remote_queue_row(db_path, changed_lines=10, signal=1)
            args = self.import_history_args(db_path, dry_run=False)

            def fake_github_request(path: str, token: str) -> object:
                if path == "/repos/mt4110/example":
                    return {
                        "full_name": "mt4110/example",
                        "fork": False,
                        "owner": {"login": "mt4110"},
                    }
                if path == "/repos/mt4110/example/pulls/42":
                    return {
                        "number": 42,
                        "merged_at": "2026-05-02T00:00:00Z",
                        "updated_at": "2026-05-03T00:00:00Z",
                        "title": "Importable",
                        "head": {"sha": "def456"},
                    }
                raise AssertionError(path)

            def fake_import(import_args: argparse.Namespace) -> None:
                self.assertFalse(import_args.dry_run)
                self.assertEqual(import_args.repo, "mt4110/example")
                self.assertEqual(import_args.pr, 42)

            output = io.StringIO()
            with mock.patch("llreview.github_token", return_value=("token", "test token")):
                with mock.patch("llreview.github_request", side_effect=fake_github_request):
                    with mock.patch(
                        "llreview.github_paginated_request",
                        return_value=[
                            {
                                "filename": "scripts/app.py",
                                "changes": 20,
                                "additions": 18,
                                "deletions": 2,
                                "status": "modified",
                            }
                        ],
                    ):
                        with mock.patch("llreview.backfill_actionable_external_count", return_value=1):
                            with mock.patch("llreview.command_import_github_reviews", side_effect=fake_import) as importer:
                                with contextlib.redirect_stdout(output):
                                    command_import_github_history(args)

            importer.assert_called_once()
            with sqlite_connection(db_path) as connection:
                row = connection.execute(
                    """
                    SELECT state, skip_reason, attempt_count, head_sha, changed_lines
                    FROM github_backfill_queue
                    """
                ).fetchone()

            self.assertEqual(row, ("imported", "", 1, "def456", 20))
            self.assertIn("OK: marked github_backfill_queue", output.getvalue())


class ReviewDbAggregateTests(unittest.TestCase):
    def test_count_helpers_use_quoted_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "review.db"
            with sqlite_connection(db_path) as connection:
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
        with sqlite_memory_connection() as connection:
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
        with sqlite_memory_connection() as connection:
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
        with sqlite_memory_connection() as connection:
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
        with sqlite_memory_connection() as connection:
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
        with sqlite_memory_connection() as connection:
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
        with sqlite_memory_connection() as connection:
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
