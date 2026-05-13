#!/usr/bin/env python3
"""Read-only aggregate snapshot for the local dashboard.

This script is the data boundary for the first dashboard scaffold. It opens the
review-history DB in read-only mode and returns counts, buckets, and command
suggestions only. It does not initialize schemas, write verdicts, run reviews,
post comments, read raw review bodies, or read raw diffs.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable

from review_db import (
    SQLITE_BACKEND,
    active_calibration_counts,
    backfill_queue_counts,
    connect_review_db_readonly,
    external_item_counts,
    review_db_config,
    review_run_counts,
    table_counts,
)


TOOL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = TOOL_ROOT / "out" / "review-history" / "local-ai-review.db"
DEFAULT_TARGET_NAME = "llreview-target.json"
DEFAULT_PORT = 3069
VALID_EXTERNAL_REASONS = {"teacher_model_valid", "external_valid"}
REVIEW_HISTORY_TABLES = (
    "review_runs",
    "reviewed_files",
    "findings",
    "watch_items",
    "run_feedback",
    "review_items",
    "external_items",
    "item_verdicts",
    "item_links",
    "rule_updates",
    "runtime_metrics",
    "artifacts",
    "workspace_state",
    "github_backfill_queue",
    "learning_calibrations",
)
POSTGRES_GATES = (
    ("review_items", "review_items >= 10,000", 10_000),
    ("training_ready_external_examples", "training-ready external examples >= 100", 100),
    ("external_items", "external_items >= 500", 500),
    ("sqlite_db_bytes", "SQLite DB size >= 50 MB", 50 * 1024 * 1024),
)


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def bytes_label(value: int) -> str:
    units = ("B", "KB", "MB", "GB")
    amount = float(max(0, value))
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


def shell_command(parts: list[Any]) -> str:
    return shlex.join(str(part) for part in parts if str(part) != "")


def empty_run_counts() -> dict[str, Any]:
    return {
        "total": 0,
        "unscored": 0,
        "zero_finding_runs": 0,
        "findings": 0,
        "watch_items": 0,
        "diff_bytes": 0,
        "average_elapsed_seconds": 0.0,
    }


def empty_external_counts() -> dict[str, Any]:
    return {
        "total": 0,
        "linked": 0,
        "unlinked": 0,
        "link_rate": "n/a",
        "verdict_rows": [],
    }


def empty_backfill_counts() -> dict[str, Any]:
    return {
        "total": 0,
        "signal": 0,
        "by_state": {},
        "by_source_state": {},
        "records": [],
    }


def empty_calibration_counts() -> dict[str, Any]:
    return {"active": 0, "recent": []}


def safe_calibration_counts(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "active": int(raw.get("active") or 0),
        "recent": [
            {
                "calibration_id": str(row.get("calibration_id") or "")[:12],
                "scope_repo": str(row.get("scope_repo") or ""),
                "path_class": str(row.get("path_class") or ""),
                "signal_kind": str(row.get("signal_kind") or ""),
                "evidence_count": int(row.get("evidence_count") or 0),
                "confidence": str(row.get("confidence") or ""),
                "updated_at": str(row.get("updated_at") or ""),
            }
            for row in raw.get("recent", [])
            if isinstance(row, dict)
        ],
    }


def safe_call(default: Any, callback: Callable[[], Any]) -> Any:
    try:
        return callback()
    except (sqlite3.Error, OSError, ValueError):
        return default


def sqlite_objects(connection: sqlite3.Connection) -> dict[str, set[str]]:
    rows = connection.execute(
        """
        SELECT type, name
        FROM sqlite_master
        WHERE type IN ('table', 'view')
          AND name NOT LIKE 'sqlite_%'
        """
    ).fetchall()
    objects: dict[str, set[str]] = {"table": set(), "view": set()}
    for row in rows:
        objects.setdefault(str(row["type"]), set()).add(str(row["name"]))
    return objects


def has_object(objects: dict[str, set[str]], name: str) -> bool:
    return name in objects.get("table", set()) or name in objects.get("view", set())


def latest_external_verdict_stats(
    connection: sqlite3.Connection,
    *,
    repo: str,
) -> dict[str, Any]:
    where = ""
    params: list[Any] = []
    if repo:
        where = "WHERE external_items.repo = ?"
        params.append(repo)
    rows = connection.execute(
        f"""
        SELECT
            verdict,
            reason,
            linked,
            COUNT(*) AS total
        FROM (
            SELECT
                COALESCE(NULLIF(verdicts.verdict, ''), 'unscored') AS verdict,
                COALESCE(NULLIF(verdicts.reason, ''), '(none)') AS reason,
                CASE WHEN linked.external_item_id IS NULL THEN 0 ELSE 1 END AS linked
            FROM external_items
            LEFT JOIN (
                SELECT external_item_id
                FROM item_links
                GROUP BY external_item_id
            ) AS linked
            ON linked.external_item_id = external_items.id
            LEFT JOIN (
                SELECT item_verdicts.*
                FROM item_verdicts
                JOIN (
                    SELECT target_kind, target_id, MAX(id) AS id
                    FROM item_verdicts
                    GROUP BY target_kind, target_id
                ) AS latest
                ON latest.id = item_verdicts.id
            ) AS verdicts
            ON verdicts.target_kind = 'external_item'
            AND verdicts.target_id = external_items.id
            {where}
        ) AS external_labels
        GROUP BY verdict, reason, linked
        """,
        params,
    ).fetchall()
    label_counts = {
        "training_ready_external_examples": 0,
        "human_gate_external_examples": 0,
        "covered_by_local": 0,
        "teacher_false_positive": 0,
        "needs_human_review": 0,
        "unlabeled_external_items": 0,
    }
    reason_counts: dict[str, int] = {}
    for row in rows:
        verdict = str(row["verdict"] or "unscored")
        reason = str(row["reason"] or "(none)")
        linked = int(row["linked"] or 0)
        count = int(row["total"] or 0)
        reason_counts[f"{verdict}/{reason}"] = reason_counts.get(f"{verdict}/{reason}", 0) + count
        if verdict == "covered_by_local" or linked:
            label_counts["covered_by_local"] += count
        elif verdict == "missed_by_local" and reason in VALID_EXTERNAL_REASONS:
            label_counts["training_ready_external_examples"] += count
        elif verdict == "missed_by_local":
            label_counts["human_gate_external_examples"] += count
        elif verdict == "teacher_false_positive":
            label_counts["teacher_false_positive"] += count
        elif verdict == "needs_human_review":
            label_counts["needs_human_review"] += count
            label_counts["human_gate_external_examples"] += count
        elif verdict == "unscored":
            label_counts["unlabeled_external_items"] += count
            label_counts["human_gate_external_examples"] += count
    return {
        **label_counts,
        "reason_counts": dict(sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))[:12]),
    }


def review_history_growth(
    connection: sqlite3.Connection,
    *,
    repo: str,
    limit: int,
) -> list[dict[str, Any]]:
    where = ""
    params: list[Any] = []
    if repo:
        where = "WHERE repo = ?"
        params.append(repo)
    rows = connection.execute(
        f"""
        SELECT
            substr(created_at, 1, 7) AS month,
            COUNT(*) AS runs,
            SUM(findings_count) AS findings,
            SUM(watch_items_count) AS watch_items,
            SUM(diff_bytes) AS diff_bytes
        FROM review_run_summary
        {where}
        GROUP BY month
        ORDER BY month DESC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    records = [
        {
            "month": str(row["month"] or "unknown"),
            "runs": int(row["runs"] or 0),
            "findings": int(row["findings"] or 0),
            "watch_items": int(row["watch_items"] or 0),
            "diff_bytes": int(row["diff_bytes"] or 0),
        }
        for row in rows
    ]
    records.reverse()
    return records


def workspace_state_records(
    connection: sqlite3.Connection,
    *,
    repo: str,
    requested_workspace: str,
    limit: int,
) -> list[dict[str, Any]]:
    where = ""
    params: list[Any] = []
    if requested_workspace:
        where = "WHERE workspace_path = ?"
        params.append(requested_workspace)
    elif repo:
        where = "WHERE repo = ?"
        params.append(repo)
    rows = connection.execute(
        f"""
        SELECT
            workspace_path,
            repo,
            branch,
            pr_number,
            base_ref,
            head_ref,
            head_sha,
            last_run_id,
            updated_at
        FROM workspace_state
        {where}
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    return [
        {
            "workspace_path": str(row["workspace_path"] or ""),
            "repo": str(row["repo"] or ""),
            "branch": str(row["branch"] or ""),
            "pr_number": int(row["pr_number"] or 0),
            "base_ref": str(row["base_ref"] or ""),
            "head_ref": str(row["head_ref"] or ""),
            "head_sha": str(row["head_sha"] or "")[:12],
            "last_run_id": int(row["last_run_id"] or 0),
            "updated_at": str(row["updated_at"] or ""),
        }
        for row in rows
    ]


def read_target_config(db_path: Path) -> dict[str, Any] | None:
    path = db_path.parent / DEFAULT_TARGET_NAME
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    project_dir = str(raw.get("project_dir") or "").strip()
    if not project_dir:
        return None
    return {
        "project_dir": project_dir,
        "repo": str(raw.get("repo") or "").strip(),
        "output": str(raw.get("output") or "").strip(),
        "updated_at": str(raw.get("updated_at") or "").strip(),
    }


def postgres_gate_status(
    *,
    table_count_map: dict[str, int | None],
    training_ready: int,
    sqlite_db_bytes: int,
) -> list[dict[str, Any]]:
    values = {
        "review_items": int(table_count_map.get("review_items") or 0),
        "training_ready_external_examples": training_ready,
        "external_items": int(table_count_map.get("external_items") or 0),
        "sqlite_db_bytes": sqlite_db_bytes,
    }
    return [
        {
            "key": key,
            "label": label,
            "current": values[key],
            "threshold": threshold,
            "ready": values[key] >= threshold,
        }
        for key, label, threshold in POSTGRES_GATES
    ]


def empty_snapshot_sections(*, postgres_status: str = "optional") -> dict[str, Any]:
    return {
        "workspace": {"saved_target": None, "recent": []},
        "tables": {},
        "runs": empty_run_counts(),
        "external": empty_external_counts(),
        "backfill_queue": empty_backfill_counts(),
        "calibrations": empty_calibration_counts(),
        "learning_readiness": {
            "training_ready_external_examples": 0,
            "human_gate_external_examples": 0,
            "covered_by_local": 0,
            "active_calibrations": 0,
            "postgres_optional_backend": postgres_status,
        },
        "backlog": {
            "unscored_runs": 0,
            "human_gate_external_examples": 0,
            "backfill_pending": 0,
            "unlinked_external_items": 0,
            "unlabeled_external_items": 0,
        },
        "growth": [],
        "postgres_readiness": postgres_gate_status(
            table_count_map={},
            training_ready=0,
            sqlite_db_bytes=0,
        ),
        "next_commands": [],
    }


def next_commands(payload: dict[str, Any]) -> list[dict[str, str]]:
    repo = str(payload.get("scope", {}).get("repo") or "")
    repo_parts = ["--repo", repo] if repo and repo != "global" else []
    commands: list[dict[str, str]] = []
    db_state = payload.get("db", {})
    if db_state.get("error"):
        return [
            {
                "label": "Check DB path",
                "command": "llreview status",
                "reason": str(db_state["error"]),
            }
        ]
    if not db_state.get("exists"):
        return [
            {
                "label": "Check workspace target",
                "command": "llreview status",
                "reason": "No SQLite review-history DB exists yet.",
            }
        ]
    backlog = payload.get("backlog", {})
    if int(backlog.get("unscored_runs") or 0) > 0:
        commands.append(
            {
                "label": "Drain scoring inbox",
                "command": shell_command(["llreview", "scoring-pump", *repo_parts]),
                "reason": f"{backlog['unscored_runs']} run(s) are still unscored.",
            }
        )
    if int(backlog.get("human_gate_external_examples") or 0) > 0:
        commands.append(
            {
                "label": "Stamp review gaps",
                "command": shell_command(["llreview", "review-gap-stamp-pump", *repo_parts]),
                "reason": f"{backlog['human_gate_external_examples']} external example(s) need an operator verdict.",
            }
        )
    if int(backlog.get("backfill_pending") or 0) > 0:
        commands.append(
            {
                "label": "Refresh backfill focus",
                "command": shell_command(["llreview", "backfill-pump", *repo_parts]),
                "reason": f"{backlog['backfill_pending']} pending backfill row(s) are visible.",
            }
        )
    if int(payload.get("learning_readiness", {}).get("active_calibrations") or 0) > 0:
        commands.append(
            {
                "label": "Audit active calibration",
                "command": shell_command(["llreview", "learn-audit", *repo_parts]),
                "reason": "Active DB calibrations should be checked against later evidence.",
            }
        )
    if not commands:
        commands.append(
            {
                "label": "Refresh daily scoreboard",
                "command": shell_command(["llreview", "daily", "--no-review", "--learning-scoreboard", *repo_parts]),
                "reason": "No immediate backlog is visible in this scope.",
            }
        )
    return commands[:5]


def base_payload(args: argparse.Namespace, *, db_target: str) -> dict[str, Any]:
    payload = {
        "schema_name": "local_ai_review.dashboard_snapshot",
        "schema_version": 1,
        "generated_at_utc": utc_now(),
        "loopback": {
            "host": "127.0.0.1",
            "port": int(args.port),
            "browser_actions_enabled": False,
        },
        "policy": {
            "read_only": True,
            "review_execution_enabled": False,
            "pr_comment_posting_enabled": False,
            "verdict_writes_enabled": False,
            "calibration_activation_enabled": False,
            "raw_private_rows_included": False,
            "raw_bodies_included": False,
            "raw_diffs_included": False,
        },
        "db": {
            "backend": "",
            "target": db_target,
            "path": "",
            "exists": False,
            "size_bytes": 0,
            "size_label": "0 B",
            "open_mode": "read-only",
            "error": "",
        },
        "scope": {
            "repo": str(args.repo or "") or "global",
            "requested_workspace": str(Path(args.workspace).expanduser().resolve()) if args.workspace else "",
            "source": "argument" if args.repo or args.workspace else "global",
        },
    }
    payload.update(empty_snapshot_sections())
    return payload


def dashboard_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    db_target = str(args.db)
    payload = base_payload(args, db_target=db_target)
    config = review_db_config(db_target)
    payload["db"]["backend"] = config.backend
    if config.backend != SQLITE_BACKEND:
        payload["db"]["error"] = "PostgreSQL dashboard reads are planned but not implemented; SQLite remains the default."
        payload["learning_readiness"]["postgres_optional_backend"] = "planned"
        payload["next_commands"] = [
            {
                "label": "Inspect migration plan",
                "command": "llreview db-plan --docker-parity",
                "reason": "PostgreSQL is still an optional backend path.",
            }
        ]
        return payload

    db_path = config.sqlite_path
    payload["db"].update(
        {
            "path": str(db_path),
            "exists": db_path.is_file(),
            "size_bytes": db_path.stat().st_size if db_path.is_file() else 0,
            "size_label": bytes_label(db_path.stat().st_size) if db_path.is_file() else "0 B",
        }
    )
    saved_target = read_target_config(db_path)
    if saved_target and not args.repo and not args.workspace:
        payload["scope"] = {
            **payload["scope"],
            "repo": saved_target.get("repo") or "global",
            "requested_workspace": saved_target.get("project_dir") or payload["scope"]["requested_workspace"],
            "source": "saved_target",
        }
    repo_scope = "" if payload["scope"]["repo"] == "global" else str(payload["scope"]["repo"])

    if not db_path.is_file():
        payload.update(
            {
                "workspace": {"saved_target": saved_target, "recent": []},
                "tables": {},
                "runs": empty_run_counts(),
                "external": empty_external_counts(),
                "backfill_queue": empty_backfill_counts(),
                "calibrations": empty_calibration_counts(),
                "learning_readiness": {
                    "training_ready_external_examples": 0,
                    "active_calibrations": 0,
                    "postgres_optional_backend": "not_ready",
                },
                "backlog": {
                    "unscored_runs": 0,
                    "human_gate_external_examples": 0,
                    "backfill_pending": 0,
                },
                "growth": [],
                "postgres_readiness": postgres_gate_status(
                    table_count_map={},
                    training_ready=0,
                    sqlite_db_bytes=0,
                ),
            }
        )
        payload["next_commands"] = next_commands(payload)
        return payload

    try:
        with connect_review_db_readonly(db_path) as connection:
            connection.row_factory = sqlite3.Row
            objects = sqlite_objects(connection)
            counts = table_counts(connection, REVIEW_HISTORY_TABLES)
            runs = safe_call(empty_run_counts(), lambda: review_run_counts(connection, repo=repo_scope))
            external = safe_call(empty_external_counts(), lambda: external_item_counts(connection, repo=repo_scope))
            backfill = safe_call(empty_backfill_counts(), lambda: backfill_queue_counts(connection, repo=repo_scope))
            calibrations = safe_calibration_counts(
                safe_call(empty_calibration_counts(), lambda: active_calibration_counts(connection, repo=repo_scope))
            )
            external_stats = (
                safe_call({}, lambda: latest_external_verdict_stats(connection, repo=repo_scope))
                if has_object(objects, "external_items")
                else {}
            )
            growth = (
                safe_call([], lambda: review_history_growth(connection, repo=repo_scope, limit=args.months))
                if has_object(objects, "review_run_summary")
                else []
            )
            workspace_rows = (
                safe_call(
                    [],
                    lambda: workspace_state_records(
                        connection,
                        repo=repo_scope,
                        requested_workspace=str(payload["scope"].get("requested_workspace") or ""),
                        limit=args.limit,
                    ),
                )
                if has_object(objects, "workspace_state")
                else []
            )
    except sqlite3.Error as exc:
        payload["db"]["error"] = str(exc)
        payload["next_commands"] = next_commands(payload)
        return payload

    training_ready = int(external_stats.get("training_ready_external_examples") or 0)
    human_gate = int(external_stats.get("human_gate_external_examples") or 0)
    payload.update(
        {
            "workspace": {
                "saved_target": saved_target,
                "recent": workspace_rows,
            },
            "tables": counts,
            "runs": runs,
            "external": external,
            "backfill_queue": backfill,
            "calibrations": calibrations,
            "learning_readiness": {
                **external_stats,
                "active_calibrations": int(calibrations.get("active") or 0),
                "postgres_optional_backend": "optional",
            },
            "backlog": {
                "unscored_runs": int(runs.get("unscored") or 0),
                "human_gate_external_examples": human_gate,
                "backfill_pending": int(backfill.get("by_state", {}).get("pending", 0) or 0),
                "unlinked_external_items": int(external.get("unlinked") or 0),
                "unlabeled_external_items": int(external_stats.get("unlabeled_external_items") or 0),
            },
            "growth": growth,
            "postgres_readiness": postgres_gate_status(
                table_count_map=counts,
                training_ready=training_ready,
                sqlite_db_bytes=int(payload["db"]["size_bytes"] or 0),
            ),
        }
    )
    payload["next_commands"] = next_commands(payload)
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Print a read-only local dashboard snapshot as JSON")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--repo", default="")
    parser.add_argument("--workspace", default="")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--months", type=int, default=12)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = dashboard_snapshot(args)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
