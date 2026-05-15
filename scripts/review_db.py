#!/usr/bin/env python3
"""Review-history DB connection boundary.

SQLite remains the default and only implemented backend here. This module exists
to keep new DB-facing code from growing more direct sqlite3.connect calls while
PostgreSQL stays an optional future backend.
"""

from __future__ import annotations

import sqlite3
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


SQLITE_BACKEND = "sqlite"
DEFAULT_BIND_BATCH_SIZE = 800


@dataclass(frozen=True)
class ReviewSqlDialect:
    name: str
    placeholder_token: str

    def placeholder(self, index: int = 1) -> str:
        if index <= 0:
            raise ValueError("placeholder index must be positive")
        return self.placeholder_token

    def placeholders(self, count: int) -> str:
        if count <= 0:
            raise ValueError("placeholder count must be positive")
        return ",".join(self.placeholder(index) for index in range(1, count + 1))

    def quote_identifier(self, value: str) -> str:
        return '"' + value.replace('"', '""') + '"'

    def count_sql(self, table: str) -> str:
        return f"SELECT COUNT(*) FROM {self.quote_identifier(table)}"


SQLITE_DIALECT = ReviewSqlDialect(name=SQLITE_BACKEND, placeholder_token="?")
POSTGRES_DIALECT = ReviewSqlDialect(name="postgresql", placeholder_token="%s")


class UnsupportedReviewDbBackendError(NotImplementedError):
    """Raised when a future backend is requested before implementation."""


@dataclass(frozen=True)
class ReviewDbConfig:
    backend: str
    target: str

    @property
    def sqlite_path(self) -> Path:
        if self.backend != SQLITE_BACKEND:
            raise ValueError(f"Review DB backend is not SQLite: {self.backend}")
        return Path(self.target).expanduser().resolve()


def review_db_config(value: str | Path) -> ReviewDbConfig:
    text = str(value)
    lowered = text.lower()
    if looks_like_postgres_target(lowered):
        return ReviewDbConfig(backend="postgresql", target=text)
    return ReviewDbConfig(backend=SQLITE_BACKEND, target=str(Path(text).expanduser().resolve()))


def looks_like_postgres_target(value: str) -> bool:
    lowered = value.lower()
    return lowered.startswith(("postgres://", "postgresql://")) or any(
        marker in lowered for marker in ("/postgres:/", "/postgresql:/", "postgres:/", "postgresql:/")
    )


def require_sqlite_config(value: str | Path) -> ReviewDbConfig:
    config = review_db_config(value)
    if config.backend != SQLITE_BACKEND:
        raise UnsupportedReviewDbBackendError(
            "PostgreSQL review DB connections are not implemented yet. "
            "Use `llreview db-plan --docker-parity` for the current migration dry-run."
        )
    return config


def sqlite_db_path(value: str | Path) -> Path:
    return require_sqlite_config(value).sqlite_path


def sqlite_readonly_uri(path: Path) -> str:
    quoted_path = urllib.parse.quote(str(path), safe="/")
    return f"file:{quoted_path}?mode=ro"


def connect_review_db(
    db: str | Path,
    *,
    read_only: bool = False,
    row_factory: bool = False,
    foreign_keys: bool = False,
    timeout: float = 5.0,
    **kwargs: Any,
) -> sqlite3.Connection:
    """Open the review-history DB.

    The implementation intentionally preserves SQLite behavior by default:
    row_factory and foreign_keys are opt-in because older call sites set those
    themselves and may rely on SQLite's defaults.
    """

    config = require_sqlite_config(db)
    path = config.sqlite_path
    if read_only:
        connection = sqlite3.connect(
            sqlite_readonly_uri(path),
            uri=True,
            timeout=timeout,
            **kwargs,
        )
    else:
        connection = sqlite3.connect(path, timeout=timeout, **kwargs)
    if row_factory:
        connection.row_factory = sqlite3.Row
    if foreign_keys:
        connection.execute("PRAGMA foreign_keys = ON")
    return connection


def connect_review_db_readonly(db: str | Path, *, row_factory: bool = True) -> sqlite3.Connection:
    return connect_review_db(db, read_only=True, row_factory=row_factory)


def batched_values(
    values: Iterable[int],
    *,
    batch_size: int = DEFAULT_BIND_BATCH_SIZE,
) -> list[list[int]]:
    if batch_size <= 0:
        raise ValueError("batch size must be positive")
    unique_values = list(dict.fromkeys(values))
    return [
        unique_values[index : index + batch_size]
        for index in range(0, len(unique_values), batch_size)
    ]


def count_rows(connection: sqlite3.Connection, table: str, *, dialect: ReviewSqlDialect = SQLITE_DIALECT) -> int:
    return int(connection.execute(dialect.count_sql(table)).fetchone()[0])


def table_counts(
    connection: sqlite3.Connection,
    tables: Iterable[str],
    *,
    dialect: ReviewSqlDialect = SQLITE_DIALECT,
) -> dict[str, int | None]:
    counts: dict[str, int | None] = {}
    for table in tables:
        try:
            counts[table] = count_rows(connection, table, dialect=dialect)
        except sqlite3.Error:
            counts[table] = None
    return counts


def percent(numerator: int | float, denominator: int | float) -> str:
    if not denominator:
        return "n/a"
    return f"{(numerator / denominator) * 100:.1f}%"


def compact_text(value: str, limit: int = 180) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)] + "..."


def optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def row_mapping(
    row: sqlite3.Row | tuple[Any, ...] | None,
    description: tuple[tuple[Any, ...], ...] | None,
) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}
    return {
        str(column[0]): row[index]
        for index, column in enumerate(description or ())
    }


def fetchone_mapping(cursor: sqlite3.Cursor) -> dict[str, Any]:
    return row_mapping(cursor.fetchone(), cursor.description)


def fetchall_mappings(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    description = cursor.description
    return [row_mapping(row, description) for row in cursor.fetchall()]


def table_has_columns(connection: sqlite3.Connection, table: str, required: set[str]) -> bool:
    try:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return False
    names = {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in rows
    }
    return required.issubset(names)


def local_coverage_source_filter(source_sql: str) -> str:
    return f"TRIM(LOWER(COALESCE({source_sql}, ''))) <> 'specbackfill'"


def specbackfill_source_filter(source_sql: str) -> str:
    return f"TRIM(LOWER(COALESCE({source_sql}, ''))) = 'specbackfill'"


def local_coverage_linked_external_subquery(
    connection: sqlite3.Connection,
    *,
    include_details: bool = False,
) -> str:
    select_columns = [
        "links.external_item_id",
    ]
    if include_details:
        relation_expr = (
            "GROUP_CONCAT(DISTINCT links.relation)"
            if table_has_columns(connection, "item_links", {"relation"})
            else "''"
        )
        select_columns.extend(
            [
                "COUNT(DISTINCT links.review_item_id) AS link_count",
                "GROUP_CONCAT(DISTINCT links.review_item_id) AS linked_review_item_ids",
                f"{relation_expr} AS link_relations",
            ]
        )
    select_sql = ",\n                    ".join(select_columns)
    if table_has_columns(connection, "review_items", {"id", "source"}):
        return f"""
                SELECT {select_sql}
                FROM item_links AS links
                JOIN review_items AS linked_items
                ON linked_items.id = links.review_item_id
                WHERE {local_coverage_source_filter("linked_items.source")}
                GROUP BY links.external_item_id
            """
    return f"""
                SELECT {select_sql}
                FROM item_links AS links
                GROUP BY links.external_item_id
            """


def local_coverage_link_absence_filter(connection: sqlite3.Connection, external_id_sql: str) -> str:
    links_sql = local_coverage_linked_external_subquery(connection)
    return f"""
            NOT EXISTS (
                SELECT 1
                FROM (
                    {links_sql}
                ) AS local_coverage_links
                WHERE local_coverage_links.external_item_id = {external_id_sql}
            )
        """


def review_run_counts(
    connection: sqlite3.Connection,
    *,
    repo: str = "",
    dialect: ReviewSqlDialect = SQLITE_DIALECT,
) -> dict[str, Any]:
    where = ""
    params: list[Any] = []
    if repo:
        where = f"WHERE repo = {dialect.placeholder()}"
        params.append(repo)
    row = fetchone_mapping(
        connection.execute(
            f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN useful_findings_fixed IS NULL THEN 1 ELSE 0 END) AS unscored,
                SUM(CASE WHEN findings_count = 0 THEN 1 ELSE 0 END) AS zero_finding_runs,
                SUM(findings_count) AS findings,
                SUM(watch_items_count) AS watch_items,
                SUM(diff_bytes) AS diff_bytes,
                AVG(elapsed_seconds) AS average_elapsed_seconds
            FROM review_run_summary
            {where}
            """,
            params,
        )
    )
    return {
        "total": int(row["total"] or 0),
        "unscored": int(row["unscored"] or 0),
        "zero_finding_runs": int(row["zero_finding_runs"] or 0),
        "findings": int(row["findings"] or 0),
        "watch_items": int(row["watch_items"] or 0),
        "diff_bytes": int(row["diff_bytes"] or 0),
        "average_elapsed_seconds": round(float(row["average_elapsed_seconds"] or 0.0), 1),
    }


def external_link_health_counts(
    connection: sqlite3.Connection,
    *,
    repo: str = "",
    limit: int = 12,
    dialect: ReviewSqlDialect = SQLITE_DIALECT,
) -> list[dict[str, Any]]:
    where = ""
    params: list[Any] = []
    if repo:
        where = f"WHERE external_items.repo = {dialect.placeholder()}"
        params.append(repo)
    linked_external_sql = local_coverage_linked_external_subquery(connection)
    rows = fetchall_mappings(
        connection.execute(
            f"""
            SELECT
                external_items.source,
                COALESCE(NULLIF(verdicts.verdict, ''), 'unscored') AS verdict,
                COALESCE(NULLIF(verdicts.reason, ''), '(none)') AS reason,
                COUNT(*) AS total,
                SUM(CASE WHEN linked_external.external_item_id IS NULL THEN 0 ELSE 1 END) AS linked
            FROM external_items
            LEFT JOIN (
                {linked_external_sql}
            ) AS linked_external
            ON linked_external.external_item_id = external_items.id
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
            GROUP BY external_items.source, verdict, reason
            ORDER BY total DESC, external_items.source, verdict, reason
            LIMIT {dialect.placeholder()}
            """,
            [*params, limit],
        )
    )
    return [
        {
            "source": str(row["source"] or ""),
            "verdict": str(row["verdict"] or ""),
            "reason": str(row["reason"] or ""),
            "total": int(row["total"] or 0),
            "linked": int(row["linked"] or 0),
        }
        for row in rows
    ]


def external_item_counts(
    connection: sqlite3.Connection,
    *,
    repo: str = "",
    verdict_limit: int = 12,
    dialect: ReviewSqlDialect = SQLITE_DIALECT,
) -> dict[str, Any]:
    where = ""
    params: list[Any] = []
    if repo:
        where = f"WHERE repo = {dialect.placeholder()}"
        params.append(repo)
    total = int(
        connection.execute(
            f"SELECT COUNT(*) FROM external_items {where}",
            params,
        ).fetchone()[0]
    )
    linked_filters: list[str] = []
    linked_review_items_join = ""
    if table_has_columns(connection, "review_items", {"id", "source"}):
        linked_review_items_join = """
            JOIN review_items AS linked_items
            ON linked_items.id = item_links.review_item_id
        """
        linked_filters.append(local_coverage_source_filter("linked_items.source"))
    linked_params: list[Any] = []
    if repo:
        linked_filters.append(f"external_items.repo = {dialect.placeholder()}")
        linked_params.append(repo)
    linked_where = "WHERE " + " AND ".join(linked_filters) if linked_filters else ""
    linked = int(
        connection.execute(
            f"""
            SELECT COUNT(DISTINCT item_links.external_item_id)
            FROM item_links
            {linked_review_items_join}
            JOIN external_items
            ON external_items.id = item_links.external_item_id
            {linked_where}
            """,
            linked_params,
        ).fetchone()[0]
    )
    return {
        "total": total,
        "linked": linked,
        "unlinked": max(0, total - linked),
        "link_rate": percent(linked, total),
        "verdict_rows": external_link_health_counts(
            connection,
            repo=repo,
            limit=verdict_limit,
            dialect=dialect,
        ),
    }


def backfill_queue_counts(
    connection: sqlite3.Connection,
    *,
    repo: str = "",
    record_limit: int = 12,
    dialect: ReviewSqlDialect = SQLITE_DIALECT,
) -> dict[str, Any]:
    where = ""
    params: list[Any] = []
    if repo:
        where = f"WHERE repo = {dialect.placeholder()}"
        params.append(repo)
    rows = fetchall_mappings(
        connection.execute(
            f"""
            SELECT
                source_kind,
                state,
                COALESCE(NULLIF(skip_reason, ''), state) AS reason,
                COUNT(*) AS count,
                SUM(actionable_external_comments) AS signal
            FROM github_backfill_queue
            {where}
            GROUP BY source_kind, state, reason
            ORDER BY count DESC, source_kind, state, reason
            """,
            params,
        )
    )
    by_state: dict[str, int] = {}
    by_source_state: dict[str, int] = {}
    signal_total = 0
    records: list[dict[str, Any]] = []
    for row in rows:
        state = str(row["state"] or "")
        source_kind = str(row["source_kind"] or "")
        count = int(row["count"] or 0)
        signal = int(row["signal"] or 0)
        by_state[state] = by_state.get(state, 0) + count
        by_source_state[f"{source_kind}/{state}"] = by_source_state.get(f"{source_kind}/{state}", 0) + count
        signal_total += signal
        records.append(
            {
                "source_kind": source_kind,
                "state": state,
                "reason": str(row["reason"] or ""),
                "count": count,
                "signal": signal,
            }
        )
    return {
        "total": sum(by_state.values()),
        "signal": signal_total,
        "by_state": by_state,
        "by_source_state": by_source_state,
        "records": records[:record_limit],
    }


def active_calibration_counts(
    connection: sqlite3.Connection,
    *,
    repo: str = "",
    limit: int = 12,
    instruction_limit: int = 140,
    dialect: ReviewSqlDialect = SQLITE_DIALECT,
) -> dict[str, Any]:
    repo_filter = ""
    params: list[Any] = []
    if repo:
        repo_filter = f"AND (scope_repo = '' OR scope_repo = {dialect.placeholder()})"
        params.append(repo)
    rows = fetchall_mappings(
        connection.execute(
            f"""
            SELECT *
            FROM learning_calibrations
            WHERE status = 'active'
              {repo_filter}
            ORDER BY updated_at DESC, id DESC
            LIMIT {dialect.placeholder()}
            """,
            [*params, limit],
        )
    )
    total = int(
        connection.execute(
            f"""
            SELECT COUNT(*)
            FROM learning_calibrations
            WHERE status = 'active'
              {repo_filter}
            """,
            params,
        ).fetchone()[0]
    )
    return {
        "active": total,
        "recent": [
            {
                "calibration_id": str(row["calibration_id"] or ""),
                "scope_repo": str(row["scope_repo"] or "global"),
                "path_class": str(row["path_class"] or ""),
                "signal_kind": str(row["signal_kind"] or ""),
                "evidence_count": int(row["evidence_count"] or 0),
                "confidence": str(row["confidence"] or ""),
                "updated_at": str(row["updated_at"] or ""),
                "instruction": compact_text(str(row["instruction"] or ""), instruction_limit),
            }
            for row in rows
        ],
    }


def recent_review_runs(
    connection: sqlite3.Connection,
    *,
    repo: str = "",
    limit: int = 12,
    dialect: ReviewSqlDialect = SQLITE_DIALECT,
) -> list[dict[str, Any]]:
    where = ""
    params: list[Any] = []
    if repo:
        where = f"WHERE repo = {dialect.placeholder()}"
        params.append(repo)
    rows = fetchall_mappings(
        connection.execute(
            f"""
            SELECT *
            FROM review_run_summary
            {where}
            ORDER BY id DESC
            LIMIT {dialect.placeholder()}
            """,
            [*params, limit],
        )
    )
    return [
        {
            "run_id": int(row["id"]),
            "created_at": str(row["created_at"] or ""),
            "repo": str(row["repo"] or ""),
            "pr_number": optional_int(row["pr_number"]) or 0,
            "head_ref": str(row["head_ref"] or ""),
            "head_sha": str(row["head_sha"] or "")[:12],
            "findings": int(row["findings_count"] or 0),
            "watch_items": int(row["watch_items_count"] or 0),
            "unscored": row["useful_findings_fixed"] is None,
            "useful_findings_fixed": optional_int(row["useful_findings_fixed"]),
            "false_positives": optional_int(row["false_positives"]),
            "unclear_findings": optional_int(row["unclear_findings"]),
            "elapsed_seconds": round(float(row["elapsed_seconds"] or 0.0), 1),
        }
        for row in rows
    ]


def recent_item_verdicts(
    connection: sqlite3.Connection,
    *,
    repo: str = "",
    limit: int = 12,
    path_classifier: Callable[[str], str] | None = None,
    dialect: ReviewSqlDialect = SQLITE_DIALECT,
) -> list[dict[str, Any]]:
    repo_filter = ""
    params: list[Any] = []
    if repo:
        repo_filter = f"AND (runs.repo = {dialect.placeholder()} OR external_items.repo = {dialect.placeholder()})"
        params.extend([repo, repo])
    rows = fetchall_mappings(
        connection.execute(
            f"""
            SELECT
                verdicts.id,
                verdicts.target_kind,
                verdicts.target_id,
                verdicts.verdict,
                COALESCE(NULLIF(verdicts.reason, ''), '(none)') AS reason,
                verdicts.scorer,
                verdicts.scored_at,
                COALESCE(runs.repo, external_items.repo, '') AS repo,
                COALESCE(items.path, external_items.path, '') AS path
            FROM item_verdicts AS verdicts
            LEFT JOIN review_items AS items
            ON items.id = verdicts.target_id
            AND verdicts.target_kind = 'review_item'
            LEFT JOIN review_runs AS runs
            ON runs.id = items.run_id
            LEFT JOIN external_items
            ON external_items.id = verdicts.target_id
            AND verdicts.target_kind = 'external_item'
            WHERE 1 = 1
              {repo_filter}
            ORDER BY verdicts.id DESC
            LIMIT {dialect.placeholder()}
            """,
            [*params, limit],
        )
    )
    records: list[dict[str, Any]] = []
    for row in rows:
        path = str(row["path"] or "")
        records.append(
            {
                "verdict_id": int(row["id"]),
                "target_kind": str(row["target_kind"] or ""),
                "target_id": int(row["target_id"] or 0),
                "verdict": str(row["verdict"] or ""),
                "reason": str(row["reason"] or ""),
                "scorer": str(row["scorer"] or ""),
                "scored_at": str(row["scored_at"] or ""),
                "repo": str(row["repo"] or ""),
                "path_class": path_classifier(path) if path_classifier is not None else "",
            }
        )
    return records
