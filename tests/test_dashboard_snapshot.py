from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import dashboard_snapshot  # noqa: E402


class DashboardSnapshotTests(unittest.TestCase):
    def git(self, root: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    def pre_pr_diff_text(self, root: Path, base_ref: str = "main") -> str:
        base_diff = subprocess.run(
            ["git", "-C", str(root), "diff", "--no-ext-diff", "--no-textconv", f"{base_ref}...HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        index_path, env, error = dashboard_snapshot.temporary_intent_to_add_env(root)
        self.assertEqual(error, "")
        self.assertIsNotNone(index_path)
        self.assertIsNotNone(env)
        try:
            working_diff = subprocess.run(
                ["git", "-C", str(root), "diff", "--no-ext-diff", "--no-textconv", "HEAD"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            ).stdout.strip()
        finally:
            index_path.unlink(missing_ok=True)
        if working_diff:
            return f"{base_diff}\n{working_diff}"
        return base_diff

    def test_run_text_replaces_invalid_utf8_output(self) -> None:
        code, stdout, stderr = dashboard_snapshot.run_text(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'bad:\\xff\\n')"]
        )

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "bad:\ufffd")
        self.assertEqual(stderr, "")

    def test_ollama_loopback_uses_parsed_host(self) -> None:
        previous_base = os.environ.get("OLLAMA_BASE_URL")
        previous_host = os.environ.get("OLLAMA_HOST")
        try:
            os.environ["OLLAMA_BASE_URL"] = "https://localhost.example.com"
            os.environ.pop("OLLAMA_HOST", None)
            self.assertFalse(dashboard_snapshot.local_ollama_endpoint_status()["loopback"])

            os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:11434"
            self.assertTrue(dashboard_snapshot.local_ollama_endpoint_status()["loopback"])

            os.environ["OLLAMA_BASE_URL"] = "http://[::1]:11434"
            self.assertTrue(dashboard_snapshot.local_ollama_endpoint_status()["loopback"])

            os.environ.pop("OLLAMA_BASE_URL", None)
            os.environ["OLLAMA_HOST"] = "127.0.0.1:11434"
            self.assertTrue(dashboard_snapshot.local_ollama_endpoint_status()["loopback"])
        finally:
            if previous_base is None:
                os.environ.pop("OLLAMA_BASE_URL", None)
            else:
                os.environ["OLLAMA_BASE_URL"] = previous_base
            if previous_host is None:
                os.environ.pop("OLLAMA_HOST", None)
            else:
                os.environ["OLLAMA_HOST"] = previous_host

    def test_dashboard_path_class_preserves_dot_prefixed_paths(self) -> None:
        self.assertEqual(
            dashboard_snapshot.dashboard_path_class(".github/workflows/ci.yml"),
            "ops_config",
        )
        self.assertEqual(
            dashboard_snapshot.dashboard_path_class("./.github/workflows/ci.yml"),
            "ops_config",
        )
        self.assertEqual(dashboard_snapshot.dashboard_path_class(".private_docs/roadmap.md"), "docs")
        self.assertEqual(dashboard_snapshot.dashboard_path_class(".env.example"), "ops_config")

    def test_workspace_status_disables_external_diff_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True, text=True)
            (root / "app.py").write_text("print('base')\n", encoding="utf-8")
            self.git(root, "add", "app.py")
            self.git(root, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "initial")
            (root / "app.py").write_text("print('changed')\n", encoding="utf-8")
            marker = Path(tmpdir) / "external-diff-called"
            helper = Path(tmpdir) / "external-diff.sh"
            helper.write_text(f"#!/bin/sh\necho called > {marker}\nexit 0\n", encoding="utf-8")
            helper.chmod(0o755)
            previous_external = os.environ.get("GIT_EXTERNAL_DIFF")
            try:
                os.environ["GIT_EXTERNAL_DIFF"] = str(helper)
                payload = dashboard_snapshot.dashboard_snapshot(
                    self.make_args(Path(tmpdir) / "missing.db", repo="owner/repo", workspace=str(root))
                )
            finally:
                if previous_external is None:
                    os.environ.pop("GIT_EXTERNAL_DIFF", None)
                else:
                    os.environ["GIT_EXTERNAL_DIFF"] = previous_external

            self.assertGreater(payload["workspace"]["current"]["diff_bytes"], 0)
            self.assertFalse(marker.exists())

    def make_args(self, db_path: Path, *, repo: str = "owner/repo", workspace: str = "") -> argparse.Namespace:
        return argparse.Namespace(
            db=str(db_path),
            repo=repo,
            workspace=workspace,
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

    def test_workspace_argument_takes_precedence_over_saved_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "missing.db"
            target_path = Path(tmpdir) / dashboard_snapshot.DEFAULT_TARGET_NAME
            target_path.write_text(
                """
                {
                    "project_dir": "/saved/workspace",
                    "repo": "saved/repo",
                    "output": "saved.db",
                    "updated_at": "2026-05-01T00:00:00Z"
                }
                """,
                encoding="utf-8",
            )
            explicit_workspace = "/explicit/workspace"

            payload = dashboard_snapshot.dashboard_snapshot(
                self.make_args(db_path, repo="", workspace=explicit_workspace)
            )

            self.assertEqual(payload["scope"]["source"], "argument")
            self.assertEqual(payload["scope"]["repo"], "global")
            self.assertEqual(payload["scope"]["requested_workspace"], str(Path(explicit_workspace).resolve()))
            self.assertEqual(payload["workspace"]["saved_target"]["repo"], "saved/repo")

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
                        (3, "owner/repo", "teacher_model", "src/worker.py"),
                    ],
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
                    ) VALUES (?, 'external_item', ?, 'missed_by_local', 'teacher_model_valid', 'me', '2026-05-02T00:00:00Z')
                    """,
                    [
                        (1, 1),
                        (2, 3),
                    ],
                )
                connection.execute(
                    "INSERT INTO item_links (review_item_id, external_item_id, relation) VALUES (100, 3, 'covered')"
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
            self.assertEqual(payload["external"]["total"], 3)
            self.assertEqual(payload["external"]["linked"], 1)
            self.assertEqual(payload["learning_readiness"]["training_ready_external_examples"], 1)
            self.assertEqual(payload["learning_readiness"]["human_gate_external_examples"], 1)
            self.assertEqual(payload["learning_readiness"]["covered_by_local"], 1)
            self.assertEqual(payload["review_health"]["status"], "needs_scoring")
            self.assertEqual(payload["review_health"]["missed"], 1)
            self.assertEqual(payload["review_health"]["covered"], 1)
            self.assertEqual(payload["stamp_stock"]["external_stamp_inbox"], 1)
            self.assertEqual(payload["stamp_stock"]["unscored_runs"], 1)
            self.assertEqual(payload["stamp_stock"]["candidate_activation_inbox"], 1)
            self.assertEqual(payload["backlog"]["backfill_pending"], 1)
            self.assertEqual(payload["calibrations"]["active"], 1)
            self.assertEqual(payload["calibration_health"]["status"], "warming_up")
            self.assertNotIn("instruction", payload["calibrations"]["recent"][0])
            self.assertEqual(payload["growth"][0]["month"], "2026-05")
            self.assertIn("scoring-pump", payload["next_commands"][0]["command"])
            self.assertFalse(payload["policy"]["raw_bodies_included"])
            self.assertFalse(payload["policy"]["raw_diffs_included"])

    def test_calibration_health_uses_activation_time_not_last_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "review.db"
            with sqlite3.connect(db_path) as connection:
                connection.row_factory = sqlite3.Row
                connection.executescript(
                    """
                    CREATE TABLE review_runs (
                        id INTEGER PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        repo TEXT NOT NULL
                    );
                    CREATE TABLE learning_calibrations (
                        id INTEGER PRIMARY KEY,
                        calibration_id TEXT NOT NULL,
                        scope_repo TEXT NOT NULL DEFAULT '',
                        path_class TEXT NOT NULL DEFAULT '',
                        signal_kind TEXT NOT NULL DEFAULT '',
                        evidence_count INTEGER NOT NULL DEFAULT 0,
                        confidence TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'active',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    INSERT INTO review_runs (
                        id,
                        created_at,
                        repo
                    ) VALUES (1, '2026-05-03T00:00:00Z', 'owner/repo');
                    INSERT INTO learning_calibrations (
                        id,
                        calibration_id,
                        scope_repo,
                        path_class,
                        signal_kind,
                        evidence_count,
                        confidence,
                        status,
                        created_at,
                        updated_at
                    ) VALUES (
                        1,
                        'cal-created-at',
                        'owner/repo',
                        'code',
                        'external_missed',
                        3,
                        'medium',
                        'active',
                        '2026-05-01T00:00:00Z',
                        '2026-05-10T00:00:00Z'
                    );
                    """
                )
                objects = dashboard_snapshot.sqlite_objects(connection)

                health = dashboard_snapshot.calibration_health_counts(
                    connection,
                    objects=objects,
                    repo="owner/repo",
                    limit=5,
                )

            self.assertEqual(health["status"], "supported")
            self.assertEqual(health["with_recent_runs"], 1)
            self.assertEqual(health["recent"][0]["runs_after"], 1)

    def test_review_health_rates_use_scored_local_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "review.db"
            with sqlite3.connect(db_path) as connection:
                connection.row_factory = sqlite3.Row
                connection.executescript(
                    """
                    CREATE TABLE review_run_summary (
                        id INTEGER PRIMARY KEY,
                        repo TEXT NOT NULL,
                        findings_count INTEGER,
                        useful_findings_fixed INTEGER,
                        false_positives INTEGER,
                        unclear_findings INTEGER
                    );
                    INSERT INTO review_run_summary (
                        id,
                        repo,
                        findings_count,
                        useful_findings_fixed,
                        false_positives,
                        unclear_findings
                    ) VALUES (1, 'owner/repo', 4, 1, 1, 0);
                    """
                )
                objects = dashboard_snapshot.sqlite_objects(connection)

                health = dashboard_snapshot.review_health_counts(
                    connection,
                    objects=objects,
                    repo="owner/repo",
                    external_stats={},
                )

            self.assertEqual(health["scored_local_findings"], 4)
            self.assertEqual(health["useful_rate"], "25.0%")
            self.assertEqual(health["false_positive_rate"], "25.0%")
            self.assertEqual(health["unclear_rate"], "0.0%")

    def test_calibration_health_uses_external_verdict_time_for_missed_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "review.db"
            with sqlite3.connect(db_path) as connection:
                connection.row_factory = sqlite3.Row
                connection.executescript(
                    """
                    CREATE TABLE review_runs (
                        id INTEGER PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        repo TEXT NOT NULL
                    );
                    CREATE TABLE external_items (
                        id INTEGER PRIMARY KEY,
                        repo TEXT NOT NULL,
                        source TEXT NOT NULL,
                        path TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE item_verdicts (
                        id INTEGER PRIMARY KEY,
                        target_kind TEXT NOT NULL,
                        target_id INTEGER NOT NULL,
                        verdict TEXT NOT NULL,
                        reason TEXT NOT NULL DEFAULT '',
                        scored_at TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE learning_calibrations (
                        id INTEGER PRIMARY KEY,
                        calibration_id TEXT NOT NULL,
                        scope_repo TEXT NOT NULL DEFAULT '',
                        path_class TEXT NOT NULL DEFAULT '',
                        signal_kind TEXT NOT NULL DEFAULT '',
                        evidence_count INTEGER NOT NULL DEFAULT 0,
                        confidence TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'active',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    INSERT INTO review_runs (
                        id,
                        created_at,
                        repo
                    ) VALUES (1, '2026-05-03T00:00:00Z', 'owner/repo');
                    INSERT INTO external_items (
                        id,
                        repo,
                        source,
                        path,
                        created_at
                    ) VALUES (1, 'owner/repo', 'teacher_model', 'src/app.py', '2026-04-30T00:00:00Z');
                    INSERT INTO item_verdicts (
                        id,
                        target_kind,
                        target_id,
                        verdict,
                        reason,
                        scored_at
                    ) VALUES (
                        1,
                        'external_item',
                        1,
                        'missed_by_local',
                        'teacher_model_valid',
                        '2026-05-04T00:00:00Z'
                    );
                    INSERT INTO learning_calibrations (
                        id,
                        calibration_id,
                        scope_repo,
                        path_class,
                        signal_kind,
                        evidence_count,
                        confidence,
                        status,
                        created_at,
                        updated_at
                    ) VALUES (
                        1,
                        'cal-external-verdict-time',
                        'owner/repo',
                        'code',
                        'external_missed',
                        3,
                        'medium',
                        'active',
                        '2026-05-01T00:00:00Z',
                        '2026-05-01T00:00:00Z'
                    );
                    """
                )
                objects = dashboard_snapshot.sqlite_objects(connection)

                health = dashboard_snapshot.calibration_health_counts(
                    connection,
                    objects=objects,
                    repo="owner/repo",
                    limit=5,
                )

            self.assertEqual(health["status"], "needs_audit")
            self.assertEqual(health["recent"][0]["status"], "watch_missed")
            self.assertEqual(health["recent"][0]["target_after"], 1)

    def test_workspace_status_hashes_diff_without_exposing_body_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True, text=True)
            (root / "app.py").write_text("print('base')\n", encoding="utf-8")
            self.git(root, "add", "app.py")
            self.git(root, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "initial")
            self.git(root, "checkout", "-b", "feature/status")
            head_sha = self.git(root, "rev-parse", "HEAD")
            (root / "app.py").write_text("print('SECRET_DIFF_BODY')\n", encoding="utf-8")
            (root / "notes.txt").write_text("untracked private note\n", encoding="utf-8")

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
                        diff_fingerprint TEXT,
                        diff_bytes INTEGER,
                        changed_files INTEGER,
                        findings_count INTEGER,
                        watch_items_count INTEGER,
                        elapsed_seconds REAL,
                        useful_findings_fixed INTEGER,
                        false_positives INTEGER,
                        unclear_findings INTEGER
                    );
                    CREATE TABLE review_runs (
                        id INTEGER PRIMARY KEY,
                        repo TEXT NOT NULL
                    );
                    CREATE TABLE review_items (
                        id INTEGER PRIMARY KEY,
                        run_id INTEGER NOT NULL,
                        source TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
                        diff_fingerprint,
                        diff_bytes,
                        changed_files,
                        findings_count,
                        watch_items_count,
                        elapsed_seconds,
                        useful_findings_fixed,
                        false_positives,
                        unclear_findings
                    ) VALUES (1, '2026-05-01 00:00:00', 'owner/repo', 0, 'feature/status', ?, 'old-fingerprint', 10, 1, 0, 0, 1.0, 0, 0, 0)
                    """,
                    (head_sha,),
                )
                connection.execute("INSERT INTO review_runs (id, repo) VALUES (1, 'owner/repo')")
                connection.execute(
                    "INSERT INTO review_items (run_id, source, created_at) VALUES (1, 'specbackfill', '2026-05-01 00:00:00')"
                )

            payload = dashboard_snapshot.dashboard_snapshot(
                self.make_args(db_path, repo="owner/repo", workspace=str(root))
            )
            current = payload["workspace"]["current"]

            self.assertTrue(current["configured"])
            self.assertTrue(current["is_git_repo"])
            self.assertTrue(current["dirty"])
            self.assertEqual(current["repo"], "owner/repo")
            self.assertEqual(current["branch"], "feature/status")
            self.assertEqual(current["changed_files"], 2)
            self.assertEqual(current["untracked_count"], 1)
            self.assertGreater(current["diff_bytes"], 0)
            self.assertEqual(len(current["diff_fingerprint"]), 64)
            expected_diff = self.pre_pr_diff_text(root)
            self.assertEqual(current["diff_fingerprint"], hashlib.sha256(expected_diff.encode("utf-8")).hexdigest())
            self.assertEqual(current["diff_bytes"], len(expected_diff.encode("utf-8")))
            self.assertTrue(current["diff_changed_since_last_run"])
            self.assertTrue(payload["workspace"]["eligibility"]["review_recommended"])
            self.assertEqual(payload["workspace"]["specbackfill"]["db_items"], 1)
            self.assertNotIn("SECRET_DIFF_BODY", json.dumps(payload))
            self.assertNotIn("untracked private note", json.dumps(payload))

    def test_workspace_status_includes_untracked_only_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True, text=True)
            (root / "app.py").write_text("print('base')\n", encoding="utf-8")
            self.git(root, "add", "app.py")
            self.git(root, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "initial")
            (root / "new_file.py").write_text("print('UNTRACKED_SECRET_BODY')\n", encoding="utf-8")

            payload = dashboard_snapshot.dashboard_snapshot(
                self.make_args(Path(tmpdir) / "missing.db", repo="owner/repo", workspace=str(root))
            )
            current = payload["workspace"]["current"]

            self.assertEqual(current["changed_files"], 1)
            self.assertEqual(current["changed_file_examples"], ["new_file.py"])
            self.assertEqual(current["untracked_count"], 1)
            self.assertGreater(current["diff_bytes"], 0)
            self.assertEqual(len(current["diff_fingerprint"]), 64)
            self.assertTrue(payload["workspace"]["eligibility"]["review_recommended"])
            self.assertNotIn("UNTRACKED_SECRET_BODY", json.dumps(payload))

    def test_workspace_status_matches_last_run_pipeline_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True, text=True)
            (root / "app.py").write_text("print('base')\n", encoding="utf-8")
            self.git(root, "add", "app.py")
            self.git(root, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "initial")
            self.git(root, "checkout", "-b", "feature/status")
            head_sha = self.git(root, "rev-parse", "HEAD")
            (root / "app.py").write_text("print('changed')\n", encoding="utf-8")
            expected_diff = self.pre_pr_diff_text(root)
            fingerprint = hashlib.sha256(expected_diff.encode("utf-8")).hexdigest()
            db_path = Path(tmpdir) / "review.db"
            with sqlite3.connect(db_path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE review_run_summary (
                        id INTEGER PRIMARY KEY,
                        created_at TEXT,
                        repo TEXT,
                        head_ref TEXT,
                        head_sha TEXT,
                        diff_fingerprint TEXT,
                        diff_bytes INTEGER,
                        changed_files INTEGER,
                        findings_count INTEGER,
                        watch_items_count INTEGER,
                        elapsed_seconds REAL
                    );
                    """
                )
                connection.execute(
                    """
                    INSERT INTO review_run_summary (
                        id,
                        created_at,
                        repo,
                        head_ref,
                        head_sha,
                        diff_fingerprint,
                        diff_bytes,
                        changed_files,
                        findings_count,
                        watch_items_count,
                        elapsed_seconds
                    ) VALUES (1, '2026-05-01 00:00:00', 'owner/repo', 'feature/status', ?, ?, ?, 1, 0, 0, 1.0)
                    """,
                    (head_sha, fingerprint, len(expected_diff.encode("utf-8"))),
                )

            payload = dashboard_snapshot.dashboard_snapshot(
                self.make_args(db_path, repo="owner/repo", workspace=str(root))
            )

            current = payload["workspace"]["current"]
            self.assertEqual(current["diff_fingerprint"], fingerprint)
            self.assertFalse(current["diff_changed_since_last_run"])
            self.assertFalse(payload["workspace"]["eligibility"]["review_recommended"])
            self.assertEqual(payload["workspace"]["eligibility"]["status"], "up_to_date")


if __name__ == "__main__":
    unittest.main()
