#!/usr/bin/env python3
"""Upsert manual feedback for a saved review run."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def parse_bool_flag(value: str) -> int:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y"}:
        return 1
    if normalized in {"0", "false", "no", "n"}:
        return 0
    raise argparse.ArgumentTypeError("expected yes/no, true/false, or 1/0")


def parse_non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a non-negative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("expected a non-negative integer")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Path to the SQLite review DB")
    parser.add_argument("--run-id", required=True, type=int, help="review_runs.id to score")
    parser.add_argument("--useful-findings-fixed", required=True, type=parse_non_negative_int)
    parser.add_argument("--false-positives", required=True, type=parse_non_negative_int)
    parser.add_argument("--unclear-findings", required=True, type=parse_non_negative_int)
    parser.add_argument("--would-request-remote-review-now", required=True, type=parse_bool_flag)
    parser.add_argument("--remote-findings-count", type=parse_non_negative_int)
    parser.add_argument("--note", default="", help="Short manual note about the run")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    if not db_path.is_file():
        raise SystemExit(f"DB file does not exist: {db_path}")

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        row = connection.execute(
            "select id, repo from review_runs where id = ?",
            (args.run_id,),
        ).fetchone()
        if row is None:
            raise SystemExit(f"run_id={args.run_id} was not found in {db_path}")

        connection.execute(
            """
            insert into run_feedback (
                run_id,
                useful_findings_fixed,
                false_positives,
                unclear_findings,
                would_request_remote_review_now,
                remote_findings_count,
                note,
                updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, current_timestamp)
            on conflict(run_id) do update set
                useful_findings_fixed = excluded.useful_findings_fixed,
                false_positives = excluded.false_positives,
                unclear_findings = excluded.unclear_findings,
                would_request_remote_review_now = excluded.would_request_remote_review_now,
                remote_findings_count = excluded.remote_findings_count,
                note = excluded.note,
                updated_at = current_timestamp
            """,
            (
                args.run_id,
                args.useful_findings_fixed,
                args.false_positives,
                args.unclear_findings,
                args.would_request_remote_review_now,
                args.remote_findings_count,
                args.note,
            ),
        )

        summary = connection.execute(
            """
            select
                id,
                repo,
                useful_findings_fixed,
                false_positives,
                unclear_findings,
                would_request_remote_review_now,
                remote_findings_count,
                note
            from review_run_summary
            where id = ?
            """,
            (args.run_id,),
        ).fetchone()

    print(
        "OK: scored run_id={id} repo={repo} useful={useful} false_positives={fp} unclear={unclear} remote_ready={ready} remote_findings={remote}".format(
            id=summary[0],
            repo=summary[1],
            useful=summary[2],
            fp=summary[3],
            unclear=summary[4],
            ready=summary[5],
            remote=summary[6],
        )
    )
    if summary[7]:
        print(f"note: {summary[7]}")


if __name__ == "__main__":
    main()
