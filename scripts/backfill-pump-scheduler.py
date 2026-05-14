#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


TOOL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LLREVIEW = TOOL_ROOT / "llreview"
DEFAULT_DB = TOOL_ROOT / "out" / "review-history" / "local-ai-review.db"
DEFAULT_OUTPUT_DIR = TOOL_ROOT / "out" / "review-history" / "backfill-pump"
DEFAULT_LOCK_FILE = TOOL_ROOT / "out" / "review-history" / "backfill-pump.scheduler.lock"
DEFAULT_OWNER = "mt4110"
DEFAULT_MIN_INTERVAL_MINUTES = 20
DEFAULT_RETRY_DELAY_MINUTES = 60


def parse_env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def env_text(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


def non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def decode_env_value(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    try:
        parsed = shlex.split(value, comments=False, posix=True)
    except ValueError:
        return value.strip("\"'")
    if not parsed:
        return ""
    return parsed[0]


def load_env_file(path: Path) -> None:
    if not path.is_file():
        raise SystemExit(f"env file not found: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        if "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = decode_env_value(raw_value)


def scheduler_mode(args: argparse.Namespace) -> str:
    explicit_modes = [
        bool(args.report_only),
        bool(args.dry_run),
        bool(args.import_one),
    ]
    if sum(explicit_modes) > 1:
        raise SystemExit("Use only one of --report-only, --dry-run, or --import-one.")
    if args.report_only:
        return "report"
    if args.dry_run:
        return "dry-run"
    if args.import_one:
        return "import-one"
    mode = env_text("LLREVIEW_BACKFILL_PUMP_MODE", "")
    if not mode:
        dry_run_flag = parse_env_flag("LLREVIEW_BACKFILL_PUMP_DRY_RUN", False)
        import_one_flag = parse_env_flag("LLREVIEW_BACKFILL_PUMP_IMPORT_ONE", False)
        if dry_run_flag and import_one_flag:
            raise SystemExit(
                "Use only one of LLREVIEW_BACKFILL_PUMP_DRY_RUN or "
                "LLREVIEW_BACKFILL_PUMP_IMPORT_ONE."
            )
        if dry_run_flag:
            return "dry-run"
        if import_one_flag:
            return "import-one"
        return "report"
    normalized = mode.strip().lower().replace("_", "-")
    aliases = {
        "report": "report",
        "report-only": "report",
        "dry-run": "dry-run",
        "dryrun": "dry-run",
        "import": "import-one",
        "import-one": "import-one",
    }
    if normalized not in aliases:
        raise SystemExit(
            "LLREVIEW_BACKFILL_PUMP_MODE must be one of report, dry-run, or import-one."
        )
    return aliases[normalized]


def bool_setting(args: argparse.Namespace, attr: str, env_name: str, default: bool = False) -> bool:
    if getattr(args, attr, False):
        return True
    return parse_env_flag(env_name, default)


def path_setting(args: argparse.Namespace, attr: str, env_name: str, default: Path) -> Path:
    value = getattr(args, attr, None)
    if value:
        return Path(value).expanduser().resolve()
    return Path(env_text(env_name, str(default))).expanduser().resolve()


def build_backfill_pump_command(args: argparse.Namespace) -> tuple[list[str], str, Path]:
    mode = scheduler_mode(args)
    if mode == "dry-run" and bool_setting(args, "refresh_queue", "LLREVIEW_BACKFILL_PUMP_REFRESH_QUEUE"):
        raise SystemExit(
            "dry-run mode cannot refresh the queue; use report or import-one when queue writes are intended."
        )
    llreview_value = getattr(args, "llreview", None)
    if not llreview_value:
        llreview_value = env_text("LLREVIEW_BACKFILL_PUMP_LLREVIEW", str(DEFAULT_LLREVIEW))
    llreview = Path(llreview_value).expanduser()
    db_path = path_setting(args, "db", "LLREVIEW_BACKFILL_PUMP_DB", DEFAULT_DB)
    output_dir = path_setting(
        args,
        "output_dir",
        "LLREVIEW_BACKFILL_PUMP_OUTPUT_DIR",
        DEFAULT_OUTPUT_DIR,
    )
    owner = env_text("LLREVIEW_BACKFILL_PUMP_OWNER", getattr(args, "owner", "") or DEFAULT_OWNER)
    min_interval = getattr(args, "min_interval_minutes", None)
    if min_interval is None:
        min_interval = env_int(
            "LLREVIEW_BACKFILL_PUMP_MIN_INTERVAL_MINUTES",
            DEFAULT_MIN_INTERVAL_MINUTES,
        )
    retry_delay = getattr(args, "retry_delay_minutes", None)
    if retry_delay is None:
        retry_delay = env_int(
            "LLREVIEW_BACKFILL_PUMP_RETRY_DELAY_MINUTES",
            DEFAULT_RETRY_DELAY_MINUTES,
        )
    queue_limit = getattr(args, "queue_limit", None)
    if queue_limit is None:
        queue_limit = env_int("LLREVIEW_BACKFILL_PUMP_QUEUE_LIMIT", 12)
    min_link_score = env_text("LLREVIEW_BACKFILL_PUMP_MIN_LINK_SCORE", str(args.min_link_score))
    command = [
        str(llreview),
        "backfill-pump",
        "--owner",
        owner,
        "--db",
        str(db_path),
        "--output-dir",
        str(output_dir),
        "--queue-limit",
        str(queue_limit),
        "--min-interval-minutes",
        str(min_interval),
        "--retry-delay-minutes",
        str(retry_delay),
        "--min-link-score",
        min_link_score,
    ]
    if bool_setting(args, "refresh_queue", "LLREVIEW_BACKFILL_PUMP_REFRESH_QUEUE"):
        command.append("--refresh-queue")
    if mode in {"dry-run", "import-one"}:
        command.append("--import-one")
    if mode == "dry-run":
        command.append("--dry-run")
    if bool_setting(args, "pin_queue_head_sha", "LLREVIEW_BACKFILL_PUMP_PIN_QUEUE_HEAD_SHA"):
        command.append("--pin-queue-head-sha")
    if bool_setting(args, "no_verdicts", "LLREVIEW_BACKFILL_PUMP_NO_VERDICTS"):
        command.append("--no-verdicts")
    include_issue_comments = bool_setting(
        args,
        "include_issue_comments",
        "LLREVIEW_BACKFILL_PUMP_INCLUDE_ISSUE_COMMENTS",
    )
    if not include_issue_comments:
        command.append("--no-issue-comments")
    allow_local_scan = bool_setting(args, "allow_local_scan", "LLREVIEW_BACKFILL_PUMP_ALLOW_LOCAL_SCAN")
    if not allow_local_scan:
        command.append("--remote-only")
    return command, mode, output_dir


def shell_command(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def read_latest_payload(output_dir: Path, *, started_at: float) -> dict[str, Any] | None:
    latest_json = output_dir / "latest.json"
    if not latest_json.is_file():
        return None
    try:
        if latest_json.stat().st_mtime < started_at - 1:
            return None
        return json.loads(latest_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def nested_int(payload: dict[str, Any], path: list[str]) -> int:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return 0
        current = current.get(key)
    try:
        return int(current or 0)
    except (TypeError, ValueError):
        return 0


def queue_state_delta(payload: dict[str, Any], state: str) -> int:
    before = nested_int(payload, ["before", "queue", "by_state", state])
    after = nested_int(payload, ["queue", "by_state", state])
    return after - before


def external_item_delta(payload: dict[str, Any]) -> int:
    before = nested_int(payload, ["before", "external_items", "total"])
    after = nested_int(payload, ["external_items", "total"])
    return after - before


def notification_event(
    *,
    returncode: int,
    payload: dict[str, Any] | None,
    output_text: str = "",
) -> tuple[str, str] | None:
    if returncode != 0:
        return "failure", f"exit={returncode}"
    if "FAILED:" in output_text:
        return "failure", "backfill-pump reported FAILED"
    if "GitHub auth unavailable" in output_text:
        return "failure", "GitHub auth unavailable"
    if not payload:
        return None
    import_payload = payload.get("import") or {}
    import_error = str(import_payload.get("error") or "")
    if import_error:
        return "failure", import_error
    failed_delta = queue_state_delta(payload, "failed_retryable")
    if failed_delta > 0:
        return "failure", f"failed_retryable +{failed_delta}"
    policy = payload.get("policy") or {}
    import_attempted = bool(import_payload.get("attempted"))
    import_dry_run = bool(import_payload.get("dry_run"))
    if not import_attempted or import_dry_run or not bool(policy.get("import_one")):
        return None
    imported_delta = queue_state_delta(payload, "imported")
    item_delta = external_item_delta(payload)
    if imported_delta > 0 or item_delta > 0:
        return "milestone", f"imported +{imported_delta}; external_items {item_delta:+d}"
    return None


def selected_label(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    selected = payload.get("import_selected_remote") or payload.get("selected_remote") or {}
    if not isinstance(selected, dict):
        return ""
    repo = str(selected.get("repo") or "")
    pr_number = selected.get("pr_number") or ""
    if repo and pr_number:
        return f"{repo}#{pr_number}"
    return repo


def send_macos_notification(*, title: str, subtitle: str, message: str, sound: str) -> bool:
    if sys.platform != "darwin":
        print("WARNING: notification skipped; macOS is required.", file=sys.stderr)
        return False
    terminal_notifier = shutil.which("terminal-notifier")
    if terminal_notifier:
        command = [
            terminal_notifier,
            "-title",
            title,
            "-subtitle",
            subtitle,
            "-message",
            message,
            "-group",
            "llreview-backfill-pump",
        ]
        if sound:
            command.extend(["-sound", sound])
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        if completed.returncode == 0:
            return True
    osascript = shutil.which("osascript")
    if not osascript:
        print("WARNING: notification skipped; osascript was not found.", file=sys.stderr)
        return False
    script = """
on run argv
  set notificationTitle to item 1 of argv
  set notificationSubtitle to item 2 of argv
  set notificationMessage to item 3 of argv
  display notification notificationMessage with title notificationTitle subtitle notificationSubtitle
end run
""".strip()
    command = [osascript, "-e", script, title, subtitle, message]
    if sound:
        script = """
on run argv
  set notificationTitle to item 1 of argv
  set notificationSubtitle to item 2 of argv
  set notificationMessage to item 3 of argv
  set notificationSound to item 4 of argv
  display notification notificationMessage with title notificationTitle subtitle notificationSubtitle sound name notificationSound
end run
""".strip()
        command = [osascript, "-e", script, title, subtitle, message, sound]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        print(f"WARNING: notification failed: {detail}", file=sys.stderr)
        return False
    return True


def notification_enabled(args: argparse.Namespace) -> bool:
    if getattr(args, "no_notify", False):
        return False
    if getattr(args, "notify", False):
        return True
    return parse_env_flag("LLREVIEW_BACKFILL_PUMP_NOTIFY", False)


def maybe_notify(
    args: argparse.Namespace,
    *,
    event: tuple[str, str] | None,
    payload: dict[str, Any] | None,
    elapsed_seconds: float,
) -> None:
    if event is None or not notification_enabled(args):
        return
    event_kind, detail = event
    label = selected_label(payload)
    elapsed = f"{elapsed_seconds:.0f}s"
    if event_kind == "failure":
        title = "llreview backfill-pump failed"
        message = f"{detail}; elapsed={elapsed}"
    else:
        title = "llreview backfill-pump imported evidence"
        message = f"{detail}; elapsed={elapsed}"
    if label:
        message = f"{label}; {message}"
    send_macos_notification(
        title=title,
        subtitle="scheduled backfill",
        message=message,
        sound=env_text("LLREVIEW_BACKFILL_PUMP_NOTIFY_SOUND", env_text("LLREVIEW_NOTIFY_SOUND", "")),
    )


def run_once(args: argparse.Namespace) -> int:
    command, mode, output_dir = build_backfill_pump_command(args)
    if getattr(args, "print_command", False):
        print(shell_command(command))
        return 0
    lock_file = path_setting(args, "lock_file", "LLREVIEW_BACKFILL_PUMP_LOCK_FILE", DEFAULT_LOCK_FILE)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    started_at = time.time()
    with lock_file.open("a+", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"SKIP: another backfill-pump scheduler run holds {lock_file}")
            return 0
        lock_handle.seek(0)
        lock_handle.truncate()
        lock_handle.write(f"pid={os.getpid()} started_at={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
        lock_handle.flush()
        print(f"# backfill-pump scheduler ({mode})")
        print(shell_command(command))
        completed = subprocess.run(
            command,
            cwd=str(TOOL_ROOT),
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.stdout:
            sys.stdout.write(completed.stdout)
            if not completed.stdout.endswith("\n"):
                sys.stdout.write("\n")
        if completed.stderr:
            sys.stderr.write(completed.stderr)
            if not completed.stderr.endswith("\n"):
                sys.stderr.write("\n")
        payload = read_latest_payload(output_dir, started_at=started_at)
        elapsed = time.time() - started_at
        event = notification_event(
            returncode=completed.returncode,
            payload=payload,
            output_text="\n".join(part for part in (completed.stdout, completed.stderr) if part),
        )
        maybe_notify(args, event=event, payload=payload, elapsed_seconds=elapsed)
        return int(completed.returncode)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Launchd-safe wrapper for llreview backfill-pump."
    )
    parser.add_argument("--env-file", help="Load scheduler settings from an env file")
    parser.add_argument("--llreview", help="Path to the llreview command")
    parser.add_argument("--db", help="SQLite review DB path")
    parser.add_argument("--output-dir", help="Backfill pump artifact directory")
    parser.add_argument("--lock-file", help="Non-blocking scheduler lock file")
    parser.add_argument("--owner", default=DEFAULT_OWNER)
    parser.add_argument("--queue-limit", type=non_negative_int)
    parser.add_argument("--min-interval-minutes", type=non_negative_int)
    parser.add_argument("--retry-delay-minutes", type=non_negative_int)
    parser.add_argument("--min-link-score", default="0.55")
    parser.add_argument("--report-only", action="store_true", help="Write report artifacts only")
    parser.add_argument("--dry-run", action="store_true", help="Preview one import without queue/evidence writes")
    parser.add_argument("--import-one", action="store_true", help="Import at most one eligible remote PR")
    parser.add_argument("--refresh-queue", action="store_true", help="Refresh the queue ledger before the run")
    parser.add_argument("--pin-queue-head-sha", action="store_true")
    parser.add_argument("--no-verdicts", action="store_true")
    parser.add_argument("--include-issue-comments", action="store_true")
    parser.add_argument("--allow-local-scan", action="store_true")
    parser.add_argument("--notify", action="store_true", help="Notify only on failure or meaningful milestone")
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--print-command", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.env_file:
        load_env_file(Path(args.env_file).expanduser())
    raise SystemExit(run_once(args))


if __name__ == "__main__":
    main()
