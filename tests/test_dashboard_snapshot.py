from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import dashboard_snapshot  # noqa: E402


class DashboardSnapshotTests(unittest.TestCase):
    def make_args(self, db_path: Path, *, repo: str = "owner/repo") -> argparse.Namespace:
        return argparse.Namespace(
            db=str(db_path),
            repo=repo,
            workspace="",
            port=3069,
            limit=5,
            months=6,
        )

    def test_missing_db_returns_read_only_status_without_creating_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "missing.db"

            payload = dashboard_snapshot.dashboard_snapshot(self.make_args(db_path))

            self.assertFalse(db_path.exists())
            self.assertFalse(payload["db"]["exists"])
            self.assertTrue(payload["policy"]["read_only"])
            self.assertFalse(payload["policy"]["review_execution_enabled"])
            self.assertEqual(payload["next_commands"][0]["command"], "llreview status")

    def test_unsupported_postgres_target_still_returns_dashboard_shape(self) -> None:
        payload = dashboard_snapshot.dashboard_snapshot(self.make_args(Path("postgresql://localhost/llreview")))

        self.assertEqual(payload["db"]["backend"], "postgresql")
        self.assertIn("PostgreSQL dashboard reads are planned", payload["db"]["error"])
        self.assertEqual(payload["runs"]["total"], 0)
        self.assertEqual(payload["external"]["total"], 0)
        self.assertEqual(payload["backlog"]["unscored_runs"], 0)
        self.assertEqual(payload["postgres_readiness"][0]["key"], "review_items")
        self.assertEqual(payload["next_commands"][0]["command"], "llreview db-plan --docker-parity")

    def test_corrupt_sqlite_file_still_returns_dashboard_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "broken.db"
            db_path.write_text("not sqlite", encoding="utf-8")

            payload = dashboard_snapshot.dashboard_snapshot(self.make_args(db_path))

            self.assertTrue(payload["db"]["exists"])
            self.assertIn("file is not a database", payload["db"]["error"])
            self.assertEqual(payload["runs"]["total"], 0)
            self.assertEqual(payload["external"]["total"], 0)
            self.assertEqual(payload["backfill_queue"]["total"], 0)
            self.assertEqual(payload["growth"], [])
            self.assertEqual(payload["next_commands"][0]["command"], "llreview status")

    def test_snapshot_uses_aggregate_rows_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "review.db"
            with sqlite3.connect(db_path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE review_run_summary (
                        id INTEGER PRIMARY KEY,
                        created_at TEXT,
                        repo TEXT,
                        pr_number INTEGER,
                        head_ref TEXT,
                        head_sha TEXT,
                        useful_findings_fixed INTEGER,
                        false_positives INTEGER,
                        unclear_findings INTEGER,
                        findings_count INTEGER,
                        watch_items_count INTEGER,
                        diff_bytes INTEGER,
                        elapsed_seconds REAL
                    );
                    CREATE TABLE external_items (
                        id INTEGER PRIMARY KEY,
                        repo TEXT NOT NULL,
                        source TEXT NOT NULL,
                        path TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE item_links (
                        id INTEGER PRIMARY KEY,
                        review_item_id INTEGER NOT NULL,
                        external_item_id INTEGER NOT NULL,
                        relation TEXT NOT NULL DEFAULT ''
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
                    CREATE TABLE github_backfill_queue (
                        id INTEGER PRIMARY KEY,
                        repo TEXT NOT NULL,
                        source_kind TEXT NOT NULL,
                        state TEXT NOT NULL,
                        skip_reason TEXT NOT NULL DEFAULT '',
                        actionable_external_comments INTEGER NOT NULL DEFAULT 0
                    );
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
                    );
                    CREATE TABLE workspace_state (
                        workspace_path TEXT PRIMARY KEY,
                        repo TEXT NOT NULL,
                        branch TEXT NOT NULL DEFAULT '',
                        pr_number INTEGER,
                        base_ref TEXT NOT NULL DEFAULT '',
                        head_ref TEXT NOT NULL DEFAULT '',
                        head_sha TEXT NOT NULL DEFAULT '',
                        last_run_id INTEGER,
                        updated_at TEXT NOT NULL DEFAULT ''
                    );
                    """
                )
                connection.execute(
                    """
                    INSERT INTO review_run_summary (
                        id,
                        created_at,
                        repo,
                        pr_number,
                        head_ref,
                        head_sha,
                        useful_findings_fixed,
                        false_positives,
                        unclear_findings,
                        findings_count,
                        watch_items_count,
                        diff_bytes,
                        elapsed_seconds
                    ) VALUES (1, '2026-05-01T00:00:00Z', 'owner/repo', 12, 'feature', 'abcdef123456', NULL, NULL, NULL, 2, 1, 2048, 3.2)
                    """
                )
                connection.executemany(
                    "INSERT INTO external_items (id, repo, source, path) VALUES (?, ?, ?, ?)",
                    [
                        (1, "owner/repo", "teacher_model", "src/app.py"),
                        (2, "owner/repo", "github", "docs/readme.md"),
                    ],
                )
                connection.execute(
                    """
                    INSERT INTO item_verdicts (
                        id,
                        target_kind,
                        target_id,
                        verdict,
                        reason,
                        scorer,
                        scored_at
                    ) VALUES (1, 'external_item', 1, 'missed_by_local', 'teacher_model_valid', 'me', '2026-05-02T00:00:00Z')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO github_backfill_queue (
                        repo,
                        source_kind,
                        state,
                        actionable_external_comments
                    ) VALUES ('owner/repo', 'pr', 'pending', 2)
                    """
                )
                connection.execute(
                    """
                    INSERT INTO learning_calibrations (
                        calibration_id,
                        scope_repo,
                        path_class,
                        signal_kind,
                        instruction,
                        evidence_count,
                        confidence,
                        status,
                        updated_at
                    ) VALUES ('cal-1', 'owner/repo', 'api', 'missed', 'aggregate guidance', 3, 'medium', 'active', '2026-05-03T00:00:00Z')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO workspace_state (
                        workspace_path,
                        repo,
                        branch,
                        pr_number,
                        base_ref,
                        head_ref,
                        head_sha,
                        last_run_id,
                        updated_at
                    ) VALUES ('/work/repo', 'owner/repo', 'feature', 12, 'main', 'feature', 'abcdef123456', 1, '2026-05-04T00:00:00Z')
                    """
                )

            payload = dashboard_snapshot.dashboard_snapshot(self.make_args(db_path))

            self.assertTrue(payload["db"]["exists"])
            self.assertEqual(payload["runs"]["total"], 1)
            self.assertEqual(payload["runs"]["unscored"], 1)
            self.assertEqual(payload["learning_readiness"]["training_ready_external_examples"], 1)
            self.assertEqual(payload["learning_readiness"]["human_gate_external_examples"], 1)
            self.assertEqual(payload["backlog"]["backfill_pending"], 1)
            self.assertEqual(payload["calibrations"]["active"], 1)
            self.assertNotIn("instruction", payload["calibrations"]["recent"][0])
            self.assertEqual(payload["growth"][0]["month"], "2026-05")
            self.assertIn("scoring-pump", payload["next_commands"][0]["command"])
            self.assertFalse(payload["policy"]["raw_bodies_included"])
            self.assertFalse(payload["policy"]["raw_diffs_included"])


if __name__ == "__main__":
    unittest.main()
