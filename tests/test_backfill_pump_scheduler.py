from __future__ import annotations

import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCHEDULER_PATH = ROOT / "scripts" / "backfill-pump-scheduler.py"


def load_scheduler_module():
    spec = importlib.util.spec_from_file_location("backfill_pump_scheduler", SCHEDULER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load backfill-pump scheduler")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BackfillPumpSchedulerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scheduler = load_scheduler_module()

    def parse_args(self, *args: str):
        return self.scheduler.build_parser().parse_args(list(args))

    def test_default_command_is_report_only_remote_and_review_comments_only(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            command, mode, _ = self.scheduler.build_backfill_pump_command(self.parse_args())

        self.assertEqual(mode, "report")
        self.assertIn("backfill-pump", command)
        self.assertIn("--remote-only", command)
        self.assertIn("--no-issue-comments", command)
        self.assertNotIn("--import-one", command)
        self.assertNotIn("--dry-run", command)
        self.assertEqual(command[command.index("--min-interval-minutes") + 1], "20")
        self.assertEqual(command[command.index("--retry-delay-minutes") + 1], "60")

    def test_import_one_mode_is_explicit_and_keeps_twenty_minute_gate(self) -> None:
        with mock.patch.dict(os.environ, {"LLREVIEW_BACKFILL_PUMP_MODE": "import-one"}, clear=True):
            command, mode, _ = self.scheduler.build_backfill_pump_command(self.parse_args())

        self.assertEqual(mode, "import-one")
        self.assertIn("--import-one", command)
        self.assertNotIn("--dry-run", command)
        self.assertEqual(command[command.index("--min-interval-minutes") + 1], "20")

    def test_dry_run_rejects_refresh_queue(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "LLREVIEW_BACKFILL_PUMP_MODE": "dry-run",
                "LLREVIEW_BACKFILL_PUMP_REFRESH_QUEUE": "1",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(SystemExit, "dry-run mode cannot refresh the queue"):
                self.scheduler.build_backfill_pump_command(self.parse_args())

    def test_legacy_mode_flags_are_mutually_exclusive(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "LLREVIEW_BACKFILL_PUMP_DRY_RUN": "1",
                "LLREVIEW_BACKFILL_PUMP_IMPORT_ONE": "1",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(SystemExit, "Use only one"):
                self.scheduler.build_backfill_pump_command(self.parse_args())

    def test_numeric_options_reject_negative_values(self) -> None:
        with mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse_args("--min-interval-minutes", "-1")

    def test_env_file_does_not_override_existing_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / "env"
            env_path.write_text(
                """
                LLREVIEW_BACKFILL_PUMP_MODE=import-one
                LLREVIEW_BACKFILL_PUMP_QUEUE_LIMIT=24
                """,
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"LLREVIEW_BACKFILL_PUMP_MODE": "report"},
                clear=True,
            ):
                self.scheduler.load_env_file(env_path)
                self.assertEqual(os.environ["LLREVIEW_BACKFILL_PUMP_MODE"], "report")
                self.assertEqual(os.environ["LLREVIEW_BACKFILL_PUMP_QUEUE_LIMIT"], "24")

    def test_notification_event_is_quiet_for_report_and_dry_run(self) -> None:
        report_payload = {
            "policy": {"import_one": False},
            "import": {"attempted": False, "dry_run": False, "error": ""},
            "before": {"queue": {"by_state": {}}, "external_items": {"total": 1}},
            "queue": {"by_state": {}},
            "external_items": {"total": 1},
        }
        dry_run_payload = {
            **report_payload,
            "policy": {"import_one": True},
            "import": {"attempted": True, "dry_run": True, "error": ""},
        }

        self.assertIsNone(self.scheduler.notification_event(returncode=0, payload=report_payload))
        self.assertIsNone(self.scheduler.notification_event(returncode=0, payload=dry_run_payload))

    def test_notification_event_reports_failures_and_meaningful_imports(self) -> None:
        failed_payload = {
            "policy": {"import_one": True},
            "import": {"attempted": True, "dry_run": False, "error": ""},
            "before": {"queue": {"by_state": {"failed_retryable": 0}}, "external_items": {"total": 1}},
            "queue": {"by_state": {"failed_retryable": 1}},
            "external_items": {"total": 1},
        }
        imported_payload = {
            "policy": {"import_one": True},
            "import": {"attempted": True, "dry_run": False, "error": ""},
            "before": {"queue": {"by_state": {"imported": 2}}, "external_items": {"total": 4}},
            "queue": {"by_state": {"imported": 3}},
            "external_items": {"total": 6},
        }

        self.assertEqual(
            self.scheduler.notification_event(returncode=0, payload=failed_payload)[0],
            "failure",
        )
        self.assertEqual(
            self.scheduler.notification_event(returncode=0, payload=imported_payload)[0],
            "milestone",
        )
        self.assertEqual(
            self.scheduler.notification_event(returncode=2, payload=None)[0],
            "failure",
        )
        self.assertEqual(
            self.scheduler.notification_event(
                returncode=0,
                payload=None,
                output_text="FAILED: preflight GitHub API check failed",
            )[0],
            "failure",
        )


if __name__ == "__main__":
    unittest.main()
