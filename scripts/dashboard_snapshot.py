#!/usr/bin/env python3
"""Read-only aggregate snapshot for the local dashboard.

This script is the data boundary for the first dashboard scaffold. It opens the
review-history DB in read-only mode and returns counts, buckets, and command
suggestions only. It does not initialize schemas, write verdicts, run reviews,
post comments, read raw review bodies, or read raw diffs.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

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
AUTO_REVIEW_COOLDOWN_SECONDS = 30 * 60
AUTO_REVIEW_DIFF_BYTES_LIMIT = 150 * 1024
AUTO_REVIEW_CHANGED_FILES_LIMIT = 50
AUTO_REVIEW_MODEL_FILE_LIMIT = 50
VALID_EXTERNAL_REASONS = {"teacher_model_valid", "external_valid"}
LEARNING_CANDIDATE_THRESHOLD = 2
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


def percent_label(numerator: int | float, denominator: int | float) -> str:
    if not denominator:
        return "n/a"
    return f"{(numerator / denominator) * 100:.1f}%"


def shell_command(parts: list[Any]) -> str:
    return shlex.join(str(part) for part in parts if str(part) != "")


def git_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    if extra:
        env.update(extra)
    return env


def run_text(
    command: list[str],
    *,
    timeout: float = 6.0,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, "", str(exc)
    return completed.returncode, completed.stdout.rstrip("\n"), completed.stderr.strip()


def git_text(
    root: Path,
    *args: str,
    timeout: float = 6.0,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    return run_text(["git", "-C", str(root), *args], timeout=timeout, env=env or git_environment())


def copy_git_index(root: Path, destination: Path) -> str:
    code, stdout, stderr = git_text(root, "rev-parse", "--git-path", "index")
    if code != 0 or not stdout:
        return stderr or "git index path could not be resolved"
    index_path = Path(stdout)
    if not index_path.is_absolute():
        index_path = root / index_path
    if index_path.is_file():
        shutil.copyfile(index_path, destination)
        return ""
    code, _stdout, stderr = git_text(root, "read-tree", f"--index-output={destination}", "HEAD")
    return "" if code == 0 else stderr or "temporary git index could not be created"


def temporary_intent_to_add_env(root: Path) -> tuple[Path | None, dict[str, str] | None, str]:
    with tempfile.NamedTemporaryFile(prefix="llreview-dashboard-index.", delete=False) as index_file:
        index_path = Path(index_file.name)
    error = copy_git_index(root, index_path)
    if error:
        index_path.unlink(missing_ok=True)
        return None, None, error
    env = git_environment({"GIT_INDEX_FILE": str(index_path)})
    code, _stdout, stderr = git_text(root, "add", "-N", "--", ".", env=env)
    if code != 0:
        index_path.unlink(missing_ok=True)
        return None, None, stderr or "untracked files could not be added to the temporary git index"
    return index_path, env, ""


def discover_git_root(path: Path) -> tuple[Path | None, str]:
    code, stdout, stderr = run_text(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        env=git_environment(),
    )
    if code != 0 or not stdout:
        return None, stderr or "not a git repository"
    return Path(stdout).resolve(), ""


def repo_from_remote_url(value: str) -> str:
    remote = value.strip()
    if remote.endswith(".git"):
        remote = remote[:-4]
    if remote.startswith("git@"):
        match = re.match(r"git@[^:]+:([^/]+)/(.+)$", remote)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    parts = remote.rstrip("/").split("/")
    if len(parts) >= 2 and parts[-2] and parts[-1]:
        return f"{parts[-2]}/{parts[-1]}"
    return ""


def stable_fingerprint(*parts: Any) -> str:
    normalized = "\n".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalized_repo_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lower()


def dashboard_doc_path(path: str) -> bool:
    normalized = normalized_repo_path(path)
    name = normalized.rsplit("/", 1)[-1]
    return (
        normalized.startswith(("docs/", "adr/", ".private_docs/"))
        or name.startswith("readme")
        or name.endswith((".md", ".mdx", ".rst"))
    )


def dashboard_generated_path(path: str) -> bool:
    normalized = normalized_repo_path(path)
    name = normalized.rsplit("/", 1)[-1]
    if name in {"cargo.lock", "go.sum", "package-lock.json", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "yarn.lock"}:
        return True
    return normalized.startswith(
        (
            "build/",
            "coverage/",
            "dist/",
            "generated/",
            "testdata/golden/",
            "testdata/goldens/",
            "third_party/",
            "vendor/",
            "__snapshots__/",
        )
    ) or normalized.endswith((".golden", ".snap"))


def dashboard_path_class(path: str) -> str:
    normalized = normalized_repo_path(path)
    name = normalized.rsplit("/", 1)[-1]
    if not normalized:
        return "general"
    if dashboard_generated_path(normalized):
        return "generated"
    if dashboard_doc_path(normalized):
        return "docs"
    if (
        normalized.startswith((".github/", "config/", "infra/", "ops/", "scripts/"))
        or name in {"dockerfile", "makefile"}
        or name in {"package.json", "tsconfig.json", "jsconfig.json"}
        or name.startswith(("eslint.config.", "playwright.config.", "vite.config."))
        or name.endswith((".yml", ".yaml", ".toml", ".ini", ".env", ".env.example"))
    ):
        return "ops_config"
    if (
        "test" in normalized
        or "spec" in normalized
        or normalized.endswith(("_test.py", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx"))
    ):
        return "test"
    if any(part in normalized for part in ("schema", "migration", "openapi", "protobuf", "proto/")):
        return "schema"
    if any(part in normalized for part in ("auth", "permission", "security", "token", "secret")):
        return "auth"
    if any(part in normalized for part in ("api/", "routes/", "controller", "client", "server")):
        return "api"
    return "code"


def detect_repo_name(root: Path, repo_override: str) -> str:
    if repo_override:
        return repo_override
    for remote in ("origin", "upstream"):
        code, stdout, _stderr = git_text(root, "remote", "get-url", remote)
        if code == 0 and stdout:
            repo = repo_from_remote_url(stdout)
            if repo:
                return repo
    return f"local/{root.name}"


def detect_base_ref(root: Path) -> str:
    code, origin_head, _stderr = git_text(
        root,
        "symbolic-ref",
        "--quiet",
        "--short",
        "refs/remotes/origin/HEAD",
    )
    candidates: list[str] = []
    if code == 0 and origin_head:
        candidates.append(origin_head)
        candidates.append(origin_head.removeprefix("origin/"))
    candidates.extend(["origin/main", "main", "origin/master", "master"])
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        verify_code, _stdout, _stderr = git_text(root, "rev-parse", "--verify", candidate)
        if verify_code == 0:
            return candidate
    verify_code, _stdout, _stderr = git_text(root, "rev-parse", "--verify", "HEAD~1")
    if verify_code == 0:
        return "HEAD~1"
    return "HEAD"


def upstream_ahead_behind(root: Path) -> tuple[str, int, int]:
    code, upstream, _stderr = git_text(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if code != 0 or not upstream:
        return "", 0, 0
    code, counts, _stderr = git_text(root, "rev-list", "--left-right", "--count", f"{upstream}...HEAD")
    if code != 0:
        return upstream, 0, 0
    parts = counts.split()
    if len(parts) != 2:
        return upstream, 0, 0
    try:
        behind = int(parts[0])
        ahead = int(parts[1])
    except ValueError:
        return upstream, 0, 0
    return upstream, ahead, behind


def current_diff_digest(root: Path, base_ref: str) -> tuple[str, int, str]:
    errors: list[str] = []
    diff_text = ""
    if base_ref:
        code, stdout, stderr = git_text(
            root,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            f"{base_ref}...HEAD",
            timeout=12.0,
        )
        if code != 0:
            errors.append(stderr or "git diff failed")
        else:
            diff_text = stdout.strip()
    index_path: Path | None = None
    working_tree_env: dict[str, str] | None = None
    index_path, working_tree_env, index_error = temporary_intent_to_add_env(root)
    if index_error:
        errors.append(index_error)
    try:
        if working_tree_env is not None:
            code, stdout, stderr = git_text(
                root,
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "HEAD",
                timeout=12.0,
                env=working_tree_env,
            )
            if code != 0:
                errors.append(stderr or "git diff failed")
            else:
                working_tree_text = stdout.strip()
                if working_tree_text:
                    diff_text = f"{diff_text}\n{working_tree_text}"
    finally:
        if index_path is not None:
            index_path.unlink(missing_ok=True)
    if errors:
        return "", 0, "; ".join(errors)
    diff_bytes = len(diff_text.encode("utf-8"))
    return (hashlib.sha256(diff_text.encode("utf-8")).hexdigest() if diff_text else ""), diff_bytes, ""


def current_changed_files(root: Path, base_ref: str) -> tuple[list[str], str]:
    files: set[str] = set()
    errors: list[str] = []
    commands: list[list[str]] = []
    if base_ref:
        commands.append(["diff", "--no-ext-diff", "--no-textconv", "--name-only", f"{base_ref}...HEAD"])
    commands.append(["diff", "--no-ext-diff", "--no-textconv", "--name-only", "HEAD"])
    index_path: Path | None = None
    working_tree_env: dict[str, str] | None = None
    if commands:
        index_path, working_tree_env, index_error = temporary_intent_to_add_env(root)
        if index_error:
            errors.append(index_error)
    try:
        for args in commands:
            env = working_tree_env if args[-1] == "HEAD" and working_tree_env is not None else None
            code, stdout, stderr = git_text(root, *args, env=env)
            if code != 0:
                errors.append(stderr or "git diff --name-only failed")
                continue
            files.update(line.strip() for line in stdout.splitlines() if line.strip())
    finally:
        if index_path is not None:
            index_path.unlink(missing_ok=True)
    return sorted(files), "; ".join(errors)


def parse_status_porcelain(status: str) -> dict[str, Any]:
    lines = [line for line in status.splitlines() if line]
    untracked = [line[3:] for line in lines if line.startswith("?? ")]
    tracked_dirty = [line for line in lines if not line.startswith("?? ")]
    return {
        "dirty": bool(lines),
        "tracked_dirty": bool(tracked_dirty),
        "untracked_count": len(untracked),
        "untracked_examples": untracked[:5],
    }


def parse_utc_epoch(value: str) -> float | None:
    text = value.strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return float(calendar.timegm(time.strptime(text, fmt)))
        except ValueError:
            continue
    return None


def local_ollama_endpoint_status() -> dict[str, Any]:
    raw = os.environ.get("OLLAMA_BASE_URL") or os.environ.get("OLLAMA_HOST") or ""
    endpoint = raw or "http://127.0.0.1:11434"
    parsed = urlparse(endpoint if "://" in endpoint else f"//{endpoint}")
    host = (parsed.hostname or "").lower()
    local = not raw or host == "localhost"
    if host and not local:
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
    return {
        "endpoint": endpoint,
        "loopback": local,
    }


def empty_current_workspace() -> dict[str, Any]:
    return {
        "configured": False,
        "requested_path": "",
        "path": "",
        "exists": False,
        "is_git_repo": False,
        "repo": "",
        "branch": "",
        "head_sha": "",
        "base_ref": "",
        "upstream": "",
        "ahead": 0,
        "behind": 0,
        "dirty": False,
        "tracked_dirty": False,
        "untracked_count": 0,
        "untracked_examples": [],
        "changed_files": 0,
        "changed_file_examples": [],
        "diff_bytes": 0,
        "diff_size_label": "0 B",
        "diff_fingerprint": "",
        "diff_fingerprint_short": "",
        "diff_error": "",
        "last_run": None,
        "diff_changed_since_last_run": False,
        "ollama_endpoint": local_ollama_endpoint_status(),
        "error": "",
    }


def collect_current_workspace(requested_path: str, *, repo_override: str) -> dict[str, Any]:
    current = empty_current_workspace()
    if not requested_path:
        return current
    requested = Path(requested_path).expanduser()
    current["configured"] = True
    current["requested_path"] = str(requested.resolve())
    current["exists"] = requested.exists()
    if not requested.exists():
        current["error"] = "workspace path does not exist"
        return current
    root, error = discover_git_root(requested)
    if root is None:
        current["error"] = error
        return current
    current["path"] = str(root)
    current["is_git_repo"] = True
    current["repo"] = detect_repo_name(root, repo_override)
    _code, branch, _stderr = git_text(root, "branch", "--show-current")
    _code, head_sha, _stderr = git_text(root, "rev-parse", "HEAD")
    base_ref = detect_base_ref(root)
    upstream, ahead, behind = upstream_ahead_behind(root)
    _code, status, _stderr = git_text(root, "status", "--porcelain=v1", "--untracked-files=normal")
    status_bits = parse_status_porcelain(status)
    changed_files, changed_error = current_changed_files(root, base_ref)
    diff_fingerprint, diff_bytes, diff_error = current_diff_digest(root, base_ref)
    current.update(
        {
            "branch": branch,
            "head_sha": head_sha[:12],
            "base_ref": base_ref,
            "upstream": upstream,
            "ahead": ahead,
            "behind": behind,
            **status_bits,
            "changed_files": len(changed_files),
            "changed_file_examples": changed_files[:8],
            "diff_bytes": diff_bytes,
            "diff_size_label": bytes_label(diff_bytes),
            "diff_fingerprint": diff_fingerprint,
            "diff_fingerprint_short": diff_fingerprint[:12],
            "diff_error": diff_error or changed_error,
        }
    )
    return current


def empty_workspace_eligibility() -> dict[str, Any]:
    return {
        "status": "not_configured",
        "summary": "No workspace target is configured.",
        "review_recommended": False,
        "suggested_command": "llreview status",
        "limits": {
            "cooldown_seconds": AUTO_REVIEW_COOLDOWN_SECONDS,
            "diff_bytes": AUTO_REVIEW_DIFF_BYTES_LIMIT,
            "changed_files": AUTO_REVIEW_CHANGED_FILES_LIMIT,
            "model_files": AUTO_REVIEW_MODEL_FILE_LIMIT,
        },
        "gates": [],
    }


def empty_specbackfill_status() -> dict[str, Any]:
    path = shutil.which("specbackfill") or ""
    return {
        "available": bool(path),
        "path": path,
        "db_items": 0,
        "db_runs": 0,
        "last_seen_at": "",
        "last_run_id": 0,
        "status": "available_no_db_trace" if path else "missing",
        "summary": "specbackfill is available, but no aggregate DB trace is visible."
        if path
        else "specbackfill executable was not found on PATH.",
    }


def workspace_section(
    *,
    saved_target: dict[str, Any] | None = None,
    recent: list[dict[str, Any]] | None = None,
    current: dict[str, Any] | None = None,
    eligibility: dict[str, Any] | None = None,
    specbackfill: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "saved_target": saved_target,
        "recent": recent or [],
        "current": current or empty_current_workspace(),
        "eligibility": eligibility or empty_workspace_eligibility(),
        "specbackfill": specbackfill or empty_specbackfill_status(),
    }


def gate(key: str, label: str, status: str, detail: str, *, ok: bool | None = None) -> dict[str, Any]:
    if ok is None:
        ok = status in {"pass", "info"}
    return {
        "key": key,
        "label": label,
        "status": status,
        "ok": bool(ok),
        "detail": detail,
    }


def latest_workspace_run(
    connection: sqlite3.Connection,
    *,
    objects: dict[str, set[str]],
    current: dict[str, Any],
) -> dict[str, Any] | None:
    if not has_object(objects, "review_run_summary"):
        return None
    workspace_path = str(current.get("path") or current.get("requested_path") or "")
    if workspace_path and has_object(objects, "workspace_state"):
        row = connection.execute(
            """
            SELECT runs.*
            FROM workspace_state AS workspace
            JOIN review_run_summary AS runs
            ON runs.id = workspace.last_run_id
            WHERE workspace.workspace_path = ?
            ORDER BY workspace.updated_at DESC
            LIMIT 1
            """,
            (workspace_path,),
        ).fetchone()
        if row:
            return safe_run_row(row)
    repo = str(current.get("repo") or "")
    branch = str(current.get("branch") or "")
    head_sha = str(current.get("head_sha") or "")
    if not repo:
        return None
    conditions = ["repo = ?"]
    params: list[Any] = [repo]
    if branch:
        conditions.append("head_ref = ?")
        params.append(branch)
    if head_sha:
        conditions.append("substr(head_sha, 1, 12) = ?")
        params.append(head_sha[:12])
    row = connection.execute(
        f"""
        SELECT *
        FROM review_run_summary
        WHERE {' AND '.join(conditions)}
        ORDER BY id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    return safe_run_row(row) if row else None


def safe_run_row(row: sqlite3.Row) -> dict[str, Any]:
    diff_fingerprint = str(sqlite_row_get(row, "diff_fingerprint", "") or "")
    return {
        "run_id": int(sqlite_row_get(row, "id", 0) or 0),
        "created_at": str(sqlite_row_get(row, "created_at", "") or ""),
        "repo": str(sqlite_row_get(row, "repo", "") or ""),
        "head_ref": str(sqlite_row_get(row, "head_ref", "") or ""),
        "head_sha": str(sqlite_row_get(row, "head_sha", "") or "")[:12],
        "diff_fingerprint": diff_fingerprint,
        "diff_fingerprint_short": diff_fingerprint[:12],
        "diff_bytes": int(sqlite_row_get(row, "diff_bytes", 0) or 0),
        "changed_files": int(sqlite_row_get(row, "changed_files", 0) or 0),
        "findings": int(sqlite_row_get(row, "findings_count", 0) or 0),
        "watch_items": int(sqlite_row_get(row, "watch_items_count", 0) or 0),
        "elapsed_seconds": round(float(sqlite_row_get(row, "elapsed_seconds", 0.0) or 0.0), 1),
    }


def specbackfill_db_status(
    connection: sqlite3.Connection,
    *,
    objects: dict[str, set[str]],
    repo: str,
) -> dict[str, Any]:
    status = empty_specbackfill_status()
    if not (has_object(objects, "review_items") and has_object(objects, "review_runs")):
        return status
    where = "WHERE items.source = 'specbackfill'"
    params: list[Any] = []
    if repo:
        where += " AND runs.repo = ?"
        params.append(repo)
    row = connection.execute(
        f"""
        SELECT
            COUNT(*) AS items,
            COUNT(DISTINCT items.run_id) AS runs,
            MAX(items.created_at) AS last_seen_at,
            MAX(items.run_id) AS last_run_id
        FROM review_items AS items
        JOIN review_runs AS runs
        ON runs.id = items.run_id
        {where}
        """,
        params,
    ).fetchone()
    items = int(row["items"] or 0) if row else 0
    runs = int(row["runs"] or 0) if row else 0
    status.update(
        {
            "db_items": items,
            "db_runs": runs,
            "last_seen_at": str(row["last_seen_at"] or "") if row else "",
            "last_run_id": int(row["last_run_id"] or 0) if row else 0,
        }
    )
    if items:
        status["status"] = "db_trace_visible"
        status["summary"] = f"{items} aggregate specbackfill item(s) across {runs} run(s)."
    return status


def apply_last_run_to_workspace(current: dict[str, Any], last_run: dict[str, Any] | None) -> dict[str, Any]:
    updated = {**current, "last_run": last_run}
    fingerprint = str(current.get("diff_fingerprint") or "")
    previous = str((last_run or {}).get("diff_fingerprint") or "")
    updated["diff_changed_since_last_run"] = bool(fingerprint and fingerprint != previous)
    return updated


def workspace_review_command(current: dict[str, Any]) -> str:
    path = str(current.get("path") or current.get("requested_path") or "")
    repo = str(current.get("repo") or "")
    parts: list[Any] = ["llreview"]
    if path:
        parts.extend(["--project-dir", path])
    if repo and repo != "global":
        parts.extend(["--repo", repo])
    return shell_command(parts)


def workspace_status_command(current: dict[str, Any]) -> str:
    path = str(current.get("path") or current.get("requested_path") or "")
    repo = str(current.get("repo") or "")
    parts: list[Any] = ["llreview", "status"]
    if path:
        parts.extend(["--project-dir", path])
    if repo and repo != "global":
        parts.extend(["--repo", repo])
    return shell_command(parts)


def workspace_eligibility(current: dict[str, Any], specbackfill: dict[str, Any]) -> dict[str, Any]:
    result = empty_workspace_eligibility()
    gates: list[dict[str, Any]] = []
    configured = bool(current.get("configured"))
    is_git_repo = bool(current.get("is_git_repo"))
    changed_files = int(current.get("changed_files") or 0)
    diff_bytes = int(current.get("diff_bytes") or 0)
    last_run = current.get("last_run") if isinstance(current.get("last_run"), dict) else None
    fingerprint_changed = bool(current.get("diff_changed_since_last_run"))
    ollama = current.get("ollama_endpoint") if isinstance(current.get("ollama_endpoint"), dict) else {}

    gates.append(
        gate(
            "workspace_configured",
            "Workspace configured",
            "pass" if configured else "block",
            str(current.get("requested_path") or "Set a target with llreview target set."),
        )
    )
    gates.append(
        gate(
            "git_repository",
            "Git repository",
            "pass" if is_git_repo else "block",
            str(current.get("path") or current.get("error") or "No git repository detected."),
        )
    )
    if current.get("diff_error"):
        gates.append(gate("diff_scan", "Diff scan", "block", str(current["diff_error"])))
    elif changed_files:
        gates.append(
            gate(
                "diff_present",
                "Reviewable diff",
                "pass",
                f"{changed_files} file(s), {bytes_label(diff_bytes)}.",
            )
        )
    else:
        gates.append(gate("diff_present", "Reviewable diff", "info", "No tracked diff is visible."))

    if last_run:
        if fingerprint_changed:
            detail = f"Current {current.get('diff_fingerprint_short')} differs from run {last_run.get('run_id')}."
            gates.append(gate("diff_changed", "Diff fingerprint changed", "pass", detail))
        else:
            detail = f"Same as latest run {last_run.get('run_id')} ({last_run.get('diff_fingerprint_short') or 'no fingerprint'})."
            gates.append(gate("diff_changed", "Diff fingerprint changed", "info", detail, ok=False))
        created_epoch = parse_utc_epoch(str(last_run.get("created_at") or ""))
        if created_epoch is None:
            gates.append(gate("cooldown", "Cooldown", "info", "Latest run time is not parseable."))
        else:
            age_seconds = max(0, int(time.time() - created_epoch))
            if age_seconds >= AUTO_REVIEW_COOLDOWN_SECONDS:
                gates.append(gate("cooldown", "Cooldown", "pass", f"{age_seconds // 60} minute(s) since latest run."))
            else:
                remaining = AUTO_REVIEW_COOLDOWN_SECONDS - age_seconds
                gates.append(gate("cooldown", "Cooldown", "warn", f"Cooldown gate clears in about {max(1, remaining // 60)} minute(s)."))
    else:
        gates.append(gate("diff_changed", "Diff fingerprint changed", "pass", "No previous run is visible."))
        gates.append(gate("cooldown", "Cooldown", "pass", "No previous run is visible."))

    gates.append(
        gate(
            "diff_bytes",
            "Diff size",
            "pass" if diff_bytes <= AUTO_REVIEW_DIFF_BYTES_LIMIT else "block",
            f"{bytes_label(diff_bytes)} / {bytes_label(AUTO_REVIEW_DIFF_BYTES_LIMIT)} planned watch limit.",
        )
    )
    gates.append(
        gate(
            "changed_files",
            "Changed files",
            "pass" if changed_files <= AUTO_REVIEW_CHANGED_FILES_LIMIT else "block",
            f"{changed_files} / {AUTO_REVIEW_CHANGED_FILES_LIMIT} planned watch limit.",
        )
    )
    gates.append(
        gate(
            "model_file_budget",
            "Model file budget",
            "pass" if changed_files <= AUTO_REVIEW_MODEL_FILE_LIMIT else "block",
            f"{min(changed_files, AUTO_REVIEW_MODEL_FILE_LIMIT)} / {AUTO_REVIEW_MODEL_FILE_LIMIT} planned model file budget.",
        )
    )
    untracked_count = int(current.get("untracked_count") or 0)
    gates.append(
        gate(
            "untracked_files",
            "Untracked files",
            "warn" if untracked_count else "pass",
            f"{untracked_count} untracked file(s) excluded unless staged with git add -N.",
            ok=True,
        )
    )
    gates.append(
        gate(
            "specbackfill",
            "Specbackfill",
            "pass" if specbackfill.get("available") else "warn",
            str(specbackfill.get("summary") or ""),
            ok=bool(specbackfill.get("available")),
        )
    )
    gates.append(
        gate(
            "ollama_loopback",
            "Ollama loopback",
            "pass" if ollama.get("loopback", True) else "block",
            str(ollama.get("endpoint") or "http://127.0.0.1:11434"),
        )
    )
    gates.append(
        gate(
            "browser_actions",
            "Browser actions",
            "pass",
            "Dashboard remains read-only: no review execution, PR posting, verdict write, or activation.",
        )
    )

    blocked = any(row["status"] == "block" for row in gates)
    warns = any(row["status"] == "warn" for row in gates)
    review_recommended = bool(configured and is_git_repo and changed_files and (fingerprint_changed or not last_run))
    if not configured:
        status = "not_configured"
        summary = "Set an explicit workspace target before using watch/status."
        suggested = "llreview target show"
    elif blocked:
        status = "blocked"
        summary = "Manual review is blocked until the failing gate is fixed."
        suggested = workspace_status_command(current)
    elif not changed_files:
        status = "idle"
        summary = "No tracked diff is visible for this workspace."
        suggested = workspace_status_command(current)
    elif not review_recommended:
        status = "up_to_date"
        summary = "The latest visible diff appears already reviewed."
        suggested = workspace_status_command(current)
    elif warns:
        status = "manual_review_recommended"
        summary = "Manual CLI review is recommended; watch eligibility remains conservative."
        suggested = workspace_review_command(current)
    else:
        status = "ready"
        summary = "This workspace is eligible for a manual local review."
        suggested = workspace_review_command(current)

    result.update(
        {
            "status": status,
            "summary": summary,
            "review_recommended": review_recommended and not blocked,
            "suggested_command": suggested,
            "gates": gates,
        }
    )
    return result


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


def empty_review_health() -> dict[str, Any]:
    return {
        "status": "no_data",
        "summary": "No scored review evidence is visible in this scope.",
        "local_findings": 0,
        "scored_local_findings": 0,
        "useful": 0,
        "false_positive": 0,
        "unclear": 0,
        "watch_only": 0,
        "missed": 0,
        "covered": 0,
        "useful_rate": "n/a",
        "false_positive_rate": "n/a",
        "unclear_rate": "n/a",
        "missed_to_covered_ratio": "n/a",
        "local_item_verdicts": {},
        "top_local_reasons": [],
    }


def empty_stamp_stock() -> dict[str, Any]:
    return {
        "external_stamp_inbox": 0,
        "review_gap_stamp_inbox": 0,
        "unscored_runs": 0,
        "candidate_activation_inbox": 0,
        "candidate_needs_data": 0,
        "backfill_pending": 0,
        "total": 0,
    }


def empty_learning_candidate_stock() -> dict[str, Any]:
    return {
        "threshold": LEARNING_CANDIDATE_THRESHOLD,
        "total": 0,
        "proposed": 0,
        "active": 0,
        "paused": 0,
        "retired": 0,
        "needs_more_data": 0,
        "activation_inbox": 0,
        "by_signal": {},
    }


def empty_calibration_health() -> dict[str, Any]:
    return {
        "status": "no_active",
        "summary": "No active calibration is visible in this scope.",
        "active": 0,
        "supported": 0,
        "promising": 0,
        "insufficient_recent_runs": 0,
        "thin_evidence": 0,
        "watch_missed": 0,
        "watch_false_positives": 0,
        "needs_audit": 0,
        "with_recent_runs": 0,
        "recent": [],
    }


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
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
        return default


def sqlite_row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    return row[key] if key in row.keys() else default


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


def object_columns(connection: sqlite3.Connection, name: str) -> set[str]:
    try:
        rows = connection.execute(f"PRAGMA table_info({name})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row["name"] if isinstance(row, sqlite3.Row) else row[1]) for row in rows}


def has_columns(connection: sqlite3.Connection, name: str, required: set[str]) -> bool:
    return required.issubset(object_columns(connection, name))


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
        "missed_by_local": 0,
        "covered_by_local": 0,
        "teacher_false_positive": 0,
        "needs_human_review": 0,
        "unlabeled_external_items": 0,
        "external_stamp_inbox": 0,
        "review_gap_stamp_inbox": 0,
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
            label_counts["missed_by_local"] += count
            label_counts["training_ready_external_examples"] += count
        elif verdict == "missed_by_local":
            label_counts["missed_by_local"] += count
            label_counts["review_gap_stamp_inbox"] += count
            label_counts["human_gate_external_examples"] += count
        elif verdict == "teacher_false_positive":
            label_counts["teacher_false_positive"] += count
        elif verdict == "needs_human_review":
            label_counts["needs_human_review"] += count
            label_counts["review_gap_stamp_inbox"] += count
            label_counts["human_gate_external_examples"] += count
        elif verdict == "unscored":
            label_counts["unlabeled_external_items"] += count
            label_counts["external_stamp_inbox"] += count
            label_counts["human_gate_external_examples"] += count
    return {
        **label_counts,
        "reason_counts": dict(sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))[:12]),
    }


def local_item_verdict_stats(
    connection: sqlite3.Connection,
    *,
    objects: dict[str, set[str]],
    repo: str,
) -> dict[str, Any]:
    if not (
        has_object(objects, "item_verdicts")
        and has_object(objects, "review_items")
        and has_object(objects, "review_runs")
        and has_columns(connection, "review_items", {"id", "run_id", "source", "path"})
        and has_columns(connection, "review_runs", {"id", "repo"})
        and has_columns(connection, "item_verdicts", {"id", "target_kind", "target_id", "verdict", "reason"})
    ):
        return {"verdict_counts": {}, "reason_rows": []}
    repo_filter = ""
    params: list[Any] = []
    if repo:
        repo_filter = "AND runs.repo = ?"
        params.append(repo)
    rows = connection.execute(
        f"""
        SELECT
            verdicts.verdict,
            COALESCE(NULLIF(verdicts.reason, ''), '(none)') AS reason,
            items.source,
            items.path,
            COUNT(*) AS total
        FROM item_verdicts AS verdicts
        JOIN review_items AS items
        ON items.id = verdicts.target_id
        JOIN review_runs AS runs
        ON runs.id = items.run_id
        JOIN (
            SELECT target_kind, target_id, MAX(id) AS id
            FROM item_verdicts
            GROUP BY target_kind, target_id
        ) AS latest
        ON latest.id = verdicts.id
        WHERE verdicts.target_kind = 'review_item'
          {repo_filter}
        GROUP BY verdicts.verdict, reason, items.source, items.path
        ORDER BY total DESC, verdicts.verdict, reason, items.source, items.path
        """,
        params,
    ).fetchall()
    verdict_counts: dict[str, int] = {}
    reason_counts: dict[tuple[str, str], int] = {}
    for row in rows:
        verdict = str(row["verdict"] or "")
        reason = str(row["reason"] or "(none)")
        count = int(row["total"] or 0)
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + count
        if verdict in {"false_positive", "watch_only", "unclear"}:
            reason_counts[(verdict, reason)] = reason_counts.get((verdict, reason), 0) + count
    reason_rows = [
        {"verdict": verdict, "reason": reason, "count": count}
        for (verdict, reason), count in sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))
    ][:8]
    return {"verdict_counts": verdict_counts, "reason_rows": reason_rows}


def review_health_counts(
    connection: sqlite3.Connection,
    *,
    objects: dict[str, set[str]],
    repo: str,
    external_stats: dict[str, Any],
) -> dict[str, Any]:
    health = empty_review_health()
    if has_object(objects, "review_run_summary"):
        where = ""
        params: list[Any] = []
        if repo:
            where = "WHERE repo = ?"
            params.append(repo)
        row = connection.execute(
            f"""
            SELECT
                SUM(findings_count) AS local_findings,
                SUM(
                    CASE
                        WHEN useful_findings_fixed IS NOT NULL THEN findings_count
                        ELSE 0
                    END
                ) AS scored_local_findings,
                SUM(useful_findings_fixed) AS useful,
                SUM(false_positives) AS false_positive,
                SUM(unclear_findings) AS unclear
            FROM review_run_summary
            {where}
            """,
            params,
        ).fetchone()
        health.update(
            {
                "local_findings": int((row or {})["local_findings"] or 0) if row else 0,
                "scored_local_findings": int((row or {})["scored_local_findings"] or 0) if row else 0,
                "useful": int((row or {})["useful"] or 0) if row else 0,
                "false_positive": int((row or {})["false_positive"] or 0) if row else 0,
                "unclear": int((row or {})["unclear"] or 0) if row else 0,
            }
        )
    item_stats = local_item_verdict_stats(connection, objects=objects, repo=repo)
    item_verdicts = item_stats.get("verdict_counts", {}) if isinstance(item_stats, dict) else {}
    health["watch_only"] = int(item_verdicts.get("watch_only", 0) or 0)
    health["local_item_verdicts"] = item_verdicts
    health["top_local_reasons"] = item_stats.get("reason_rows", []) if isinstance(item_stats, dict) else []
    health["missed"] = int(external_stats.get("missed_by_local") or 0)
    health["covered"] = int(external_stats.get("covered_by_local") or 0)
    local_feedback_total = int(health["useful"]) + int(health["false_positive"]) + int(health["unclear"])
    scored_local_findings = int(health["scored_local_findings"])
    local_rate_denominator = scored_local_findings or local_feedback_total
    health["useful_rate"] = percent_label(int(health["useful"]), local_rate_denominator)
    health["false_positive_rate"] = percent_label(int(health["false_positive"]), local_rate_denominator)
    health["unclear_rate"] = percent_label(int(health["unclear"]), local_rate_denominator)
    external_total = int(health["missed"]) + int(health["covered"])
    health["missed_to_covered_ratio"] = percent_label(int(health["missed"]), external_total)

    if not int(health["local_findings"]) and not external_total:
        status = "no_data"
        summary = "No scored review evidence is visible in this scope."
    elif scored_local_findings == 0 and int(health["local_findings"]) > 0:
        status = "needs_scoring"
        summary = "Local findings exist, but run-level scoring is not complete yet."
    elif int(health["false_positive"]) > int(health["useful"]) and int(health["false_positive"]) >= 3:
        status = "watch_false_positive_rate"
        summary = "False positives are outweighing useful findings in scored runs."
    elif int(health["missed"]) > int(health["covered"]) and int(health["missed"]) >= 2:
        status = "watch_recall"
        summary = "Missed external examples are ahead of covered examples."
    else:
        status = "steady"
        summary = "Scored review evidence is balanced enough for normal calibration work."
    health["status"] = status
    health["summary"] = summary
    return health


def latest_calibration_statuses(connection: sqlite3.Connection) -> dict[str, str]:
    if not has_columns(connection, "learning_calibrations", {"candidate_id", "status", "updated_at", "id"}):
        return {}
    rows = connection.execute(
        """
        SELECT candidate_id, status, updated_at, id
        FROM learning_calibrations
        WHERE candidate_id != ''
        ORDER BY updated_at DESC, id DESC
        """
    ).fetchall()
    rank = {"active": 0, "paused": 1, "retired": 2}
    statuses: dict[str, tuple[int, str, str, int]] = {}
    for row in rows:
        candidate_id = str(row["candidate_id"] or "")
        status = str(row["status"] or "")
        if not candidate_id or not status:
            continue
        value = (rank.get(status, 9), status, str(row["updated_at"] or ""), int(row["id"] or 0))
        current = statuses.get(candidate_id)
        if current is None or value[0] < current[0] or (value[0] == current[0] and value[2:] > current[2:]):
            statuses[candidate_id] = value
    return {candidate_id: status for candidate_id, (_rank, status, _updated_at, _id) in statuses.items()}


def register_candidate(
    counts: dict[str, Any],
    *,
    candidate_id: str,
    candidate_kind: str,
    signal_kind: str,
    status: str,
) -> None:
    if status in {"active", "paused", "retired"}:
        effective_status = status
    elif candidate_kind == "needs_data":
        effective_status = "needs_more_data"
    else:
        effective_status = "proposed"
    counts["total"] += 1
    counts[effective_status] = int(counts.get(effective_status, 0) or 0) + 1
    counts["by_signal"][signal_kind] = counts["by_signal"].get(signal_kind, 0) + 1
    if candidate_kind in {"prompt_candidate", "rule_candidate"} and effective_status == "proposed":
        counts["activation_inbox"] += 1


def calibration_cutoff_at(calibration: sqlite3.Row) -> str:
    return str(sqlite_row_get(calibration, "created_at") or calibration["updated_at"] or "")


def learning_candidate_stock(
    connection: sqlite3.Connection,
    *,
    objects: dict[str, set[str]],
    repo: str,
    threshold: int = LEARNING_CANDIDATE_THRESHOLD,
) -> dict[str, Any]:
    counts = empty_learning_candidate_stock()
    counts["threshold"] = threshold
    statuses = latest_calibration_statuses(connection) if has_object(objects, "learning_calibrations") else {}
    repo_label = repo or "global"

    if (
        has_object(objects, "item_verdicts")
        and has_object(objects, "review_items")
        and has_object(objects, "review_runs")
        and has_columns(connection, "review_items", {"id", "run_id", "source", "path"})
        and has_columns(connection, "review_runs", {"id", "repo"})
        and has_columns(connection, "item_verdicts", {"id", "target_kind", "target_id", "verdict", "reason"})
    ):
        repo_filter = ""
        params: list[Any] = []
        if repo:
            repo_filter = "AND runs.repo = ?"
            params.append(repo)
        rows = connection.execute(
            f"""
            SELECT
                items.source,
                verdicts.verdict,
                COALESCE(NULLIF(verdicts.reason, ''), '(none)') AS reason,
                items.path,
                COUNT(*) AS total
            FROM item_verdicts AS verdicts
            JOIN review_items AS items
            ON items.id = verdicts.target_id
            JOIN review_runs AS runs
            ON runs.id = items.run_id
            JOIN (
                SELECT target_kind, target_id, MAX(id) AS id
                FROM item_verdicts
                GROUP BY target_kind, target_id
            ) AS latest
            ON latest.id = verdicts.id
            WHERE verdicts.target_kind = 'review_item'
              AND verdicts.verdict IN ('false_positive', 'watch_only', 'unclear')
              {repo_filter}
            GROUP BY items.source, verdicts.verdict, reason, items.path
            """,
            params,
        ).fetchall()
        grouped: dict[tuple[str, str, str, str], int] = {}
        for row in rows:
            key = (
                str(row["source"] or "model"),
                str(row["verdict"] or ""),
                str(row["reason"] or "(none)"),
                dashboard_path_class(str(row["path"] or "")),
            )
            grouped[key] = grouped.get(key, 0) + int(row["total"] or 0)
        for (source, verdict, reason, path_class), total in grouped.items():
            if total < threshold:
                continue
            candidate_kind = "rule_candidate" if source == "static" else "prompt_candidate"
            signal_kind = "local_false_positive" if verdict == "false_positive" else f"local_{verdict}"
            candidate_id = stable_fingerprint(
                "learning_candidate",
                repo_label,
                candidate_kind,
                signal_kind,
                source,
                verdict,
                reason,
                path_class,
            )
            register_candidate(
                counts,
                candidate_id=candidate_id,
                candidate_kind=candidate_kind,
                signal_kind=signal_kind,
                status=statuses.get(candidate_id, "proposed"),
            )

    if (
        has_object(objects, "external_items")
        and has_object(objects, "item_verdicts")
        and has_columns(connection, "external_items", {"id", "repo", "source", "path"})
        and has_columns(connection, "item_verdicts", {"id", "target_kind", "target_id", "verdict"})
    ):
        repo_filter = ""
        params = []
        if repo:
            repo_filter = "WHERE external_items.repo = ?"
            params.append(repo)
        rows = connection.execute(
            f"""
            SELECT
                COALESCE(NULLIF(verdicts.verdict, ''), 'unscored') AS verdict,
                external_items.source,
                external_items.path,
                COUNT(*) AS total
            FROM external_items
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
            {repo_filter}
            GROUP BY verdict, external_items.source, external_items.path
            """,
            params,
        ).fetchall()
        grouped: dict[tuple[str, str, str], int] = {}
        for row in rows:
            key = (
                str(row["verdict"] or "unscored"),
                str(row["source"] or "external"),
                dashboard_path_class(str(row["path"] or "")),
            )
            grouped[key] = grouped.get(key, 0) + int(row["total"] or 0)
        for (verdict, source, path_class), total in grouped.items():
            if total < threshold:
                continue
            if verdict == "missed_by_local":
                candidate_kind = "prompt_candidate"
                signal_kind = "external_missed"
                candidate_id = stable_fingerprint(
                    "learning_candidate",
                    repo_label,
                    candidate_kind,
                    signal_kind,
                    source,
                    verdict,
                    path_class,
                )
            elif verdict in {"unscored", "out_of_scope"}:
                candidate_kind = "needs_data"
                signal_kind = "external_unscored"
                candidate_id = stable_fingerprint(
                    "learning_candidate",
                    repo_label,
                    candidate_kind,
                    signal_kind,
                    source,
                    verdict,
                    path_class,
                )
            else:
                continue
            register_candidate(
                counts,
                candidate_id=candidate_id,
                candidate_kind=candidate_kind,
                signal_kind=signal_kind,
                status=statuses.get(candidate_id, "needs_more_data" if candidate_kind == "needs_data" else "proposed"),
            )

    if has_object(objects, "github_backfill_queue") and has_columns(
        connection,
        "github_backfill_queue",
        {"repo", "source_kind", "state", "skip_reason", "actionable_external_comments"},
    ):
        repo_filter = ""
        params = []
        if repo:
            repo_filter = "WHERE repo = ?"
            params.append(repo)
        rows = connection.execute(
            f"""
            SELECT
                source_kind,
                state,
                COALESCE(NULLIF(skip_reason, ''), state) AS reason,
                COUNT(*) AS row_count,
                SUM(actionable_external_comments) AS signal_count
            FROM github_backfill_queue
            {repo_filter}
            GROUP BY source_kind, state, reason
            """,
            params,
        ).fetchall()
        for row in rows:
            state = str(row["state"] or "")
            if state not in {"pending", "deferred", "failed_retryable"}:
                continue
            evidence = int(row["signal_count"] or 0) or int(row["row_count"] or 0)
            if evidence < threshold:
                continue
            signal_kind = "backfill_queue"
            candidate_id = stable_fingerprint(
                "learning_candidate",
                repo_label,
                "needs_data",
                signal_kind,
                str(row["source_kind"] or ""),
                state,
                str(row["reason"] or ""),
            )
            register_candidate(
                counts,
                candidate_id=candidate_id,
                candidate_kind="needs_data",
                signal_kind=signal_kind,
                status=statuses.get(candidate_id, "needs_more_data"),
            )
    return counts


def calibration_recent_target_evidence(
    connection: sqlite3.Connection,
    *,
    objects: dict[str, set[str]],
    calibration: sqlite3.Row,
    repo: str,
) -> int:
    signal_kind = str(calibration["signal_kind"] or "")
    path_class = str(calibration["path_class"] or "")
    cutoff_at = calibration_cutoff_at(calibration)
    scope_repo = str(calibration["scope_repo"] or "")
    repo_filter_value = repo or (scope_repo if scope_repo != "global" else "")

    if signal_kind in {"local_false_positive", "local_watch_only", "local_unclear"}:
        verdict = {
            "local_false_positive": "false_positive",
            "local_watch_only": "watch_only",
            "local_unclear": "unclear",
        }[signal_kind]
        if not (
            has_object(objects, "item_verdicts")
            and has_object(objects, "review_items")
            and has_object(objects, "review_runs")
            and has_columns(connection, "review_items", {"id", "run_id", "path"})
            and has_columns(connection, "review_runs", {"id", "repo"})
            and has_columns(connection, "item_verdicts", {"id", "target_kind", "target_id", "verdict", "scored_at"})
        ):
            return 0
        repo_filter = ""
        params: list[Any] = [cutoff_at, verdict]
        if repo_filter_value:
            repo_filter = "AND runs.repo = ?"
            params.append(repo_filter_value)
        rows = connection.execute(
            f"""
            SELECT items.path, COUNT(*) AS total
            FROM item_verdicts AS verdicts
            JOIN review_items AS items
            ON items.id = verdicts.target_id
            JOIN review_runs AS runs
            ON runs.id = items.run_id
            JOIN (
                SELECT target_kind, target_id, MAX(id) AS id
                FROM item_verdicts
                GROUP BY target_kind, target_id
            ) AS latest
            ON latest.id = verdicts.id
            WHERE verdicts.target_kind = 'review_item'
              AND verdicts.scored_at >= ?
              AND verdicts.verdict = ?
              {repo_filter}
            GROUP BY items.path
            """,
            params,
        ).fetchall()
        return sum(int(row["total"] or 0) for row in rows if dashboard_path_class(str(row["path"] or "")) == path_class)

    if signal_kind == "external_missed":
        if not (
            has_object(objects, "external_items")
            and has_object(objects, "item_verdicts")
            and has_columns(connection, "external_items", {"id", "repo", "path", "created_at"})
            and has_columns(
                connection,
                "item_verdicts",
                {"id", "target_kind", "target_id", "verdict"},
            )
        ):
            return 0
        repo_filter = ""
        params = [cutoff_at]
        if repo_filter_value:
            repo_filter = "AND external_items.repo = ?"
            params.append(repo_filter_value)
        rows = connection.execute(
            f"""
            SELECT external_items.path, verdicts.verdict, COUNT(*) AS total
            FROM external_items
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
            WHERE external_items.created_at >= ?
              AND verdicts.verdict = 'missed_by_local'
              {repo_filter}
            GROUP BY external_items.path, verdicts.verdict
            """,
            params,
        ).fetchall()
        return sum(int(row["total"] or 0) for row in rows if dashboard_path_class(str(row["path"] or "")) == path_class)
    return 0


def calibration_runs_after(
    connection: sqlite3.Connection,
    *,
    objects: dict[str, set[str]],
    calibration: sqlite3.Row,
    repo: str,
) -> int:
    if has_object(objects, "review_runs") and has_columns(connection, "review_runs", {"created_at", "repo"}):
        table = "review_runs"
    elif has_object(objects, "review_run_summary") and has_columns(
        connection,
        "review_run_summary",
        {"created_at", "repo"},
    ):
        table = "review_run_summary"
    else:
        return 0
    cutoff_at = calibration_cutoff_at(calibration)
    scope_repo = str(calibration["scope_repo"] or "")
    repo_filter_value = repo or (scope_repo if scope_repo != "global" else "")
    repo_filter = ""
    params: list[Any] = [cutoff_at]
    if repo_filter_value:
        repo_filter = "AND repo = ?"
        params.append(repo_filter_value)
    row = connection.execute(
        f"""
        SELECT COUNT(*) AS total
        FROM {table}
        WHERE created_at >= ?
          {repo_filter}
        """,
        params,
    ).fetchone()
    return int(row["total"] or 0) if row else 0


def calibration_health_counts(
    connection: sqlite3.Connection,
    *,
    objects: dict[str, set[str]],
    repo: str,
    limit: int,
) -> dict[str, Any]:
    health = empty_calibration_health()
    if not (
        has_object(objects, "learning_calibrations")
        and has_columns(
            connection,
            "learning_calibrations",
            {
                "calibration_id",
                "id",
                "scope_repo",
                "path_class",
                "signal_kind",
                "evidence_count",
                "confidence",
                "status",
                "updated_at",
            },
        )
    ):
        return health
    repo_filter = ""
    params: list[Any] = []
    if repo:
        repo_filter = "AND (scope_repo = '' OR scope_repo = ?)"
        params.append(repo)
    rows = connection.execute(
        f"""
        SELECT *
        FROM learning_calibrations
        WHERE status = 'active'
          {repo_filter}
        ORDER BY updated_at DESC, id DESC
        """,
        params,
    ).fetchall()
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
    health["active"] = total
    recent: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    for row in rows:
        evidence_count = int(row["evidence_count"] or 0)
        runs_after = calibration_runs_after(connection, objects=objects, calibration=row, repo=repo)
        target_after = calibration_recent_target_evidence(connection, objects=objects, calibration=row, repo=repo)
        if evidence_count < LEARNING_CANDIDATE_THRESHOLD:
            status = "thin_evidence"
        elif runs_after <= 0:
            status = "insufficient_recent_runs"
        elif target_after > 0 and str(row["signal_kind"] or "") == "external_missed":
            status = "watch_missed"
        elif target_after > 0 and str(row["signal_kind"] or "") in {
            "local_false_positive",
            "local_watch_only",
            "local_unclear",
        }:
            status = "watch_false_positives"
        else:
            status = "promising"
        status_counts[status] = status_counts.get(status, 0) + 1
        if runs_after > 0:
            health["with_recent_runs"] += 1
        recent.append(
            {
                "calibration_id": str(row["calibration_id"] or "")[:12],
                "scope_repo": str(row["scope_repo"] or "") or "global",
                "path_class": str(row["path_class"] or ""),
                "signal_kind": str(row["signal_kind"] or ""),
                "evidence_count": evidence_count,
                "confidence": str(row["confidence"] or ""),
                "updated_at": str(row["updated_at"] or ""),
                "runs_after": runs_after,
                "target_after": target_after,
                "status": status,
            }
        )
    for key in ("promising", "insufficient_recent_runs", "thin_evidence", "watch_missed", "watch_false_positives"):
        health[key] = int(status_counts.get(key, 0))
    health["supported"] = int(status_counts.get("promising", 0))
    health["needs_audit"] = int(health["active"]) - int(health["supported"])
    health["recent"] = recent[:limit] if limit > 0 else recent
    if not total:
        health["status"] = "no_active"
        health["summary"] = "No active calibration is visible in this scope."
    elif int(health["watch_missed"]) or int(health["watch_false_positives"]):
        health["status"] = "needs_audit"
        health["summary"] = "Some active calibrations have later same-class counter evidence."
    elif int(health["insufficient_recent_runs"]) or int(health["thin_evidence"]):
        health["status"] = "warming_up"
        health["summary"] = "Active calibrations need more later runs before they look settled."
    else:
        health["status"] = "supported"
        health["summary"] = "Active calibrations have later runs without same-class counter evidence."
    return health


def stamp_stock_counts(
    *,
    runs: dict[str, Any],
    external_stats: dict[str, Any],
    backfill: dict[str, Any],
    candidate_stock: dict[str, Any],
) -> dict[str, Any]:
    stock = {
        "external_stamp_inbox": int(external_stats.get("external_stamp_inbox") or 0),
        "review_gap_stamp_inbox": int(external_stats.get("review_gap_stamp_inbox") or 0),
        "unscored_runs": int(runs.get("unscored") or 0),
        "candidate_activation_inbox": int(candidate_stock.get("activation_inbox") or 0),
        "candidate_needs_data": int(candidate_stock.get("needs_more_data") or 0),
        "backfill_pending": int(backfill.get("by_state", {}).get("pending", 0) or 0),
    }
    stock["total"] = (
        stock["external_stamp_inbox"]
        + stock["review_gap_stamp_inbox"]
        + stock["unscored_runs"]
        + stock["candidate_activation_inbox"]
    )
    return stock


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
        "workspace": workspace_section(),
        "tables": {},
        "runs": empty_run_counts(),
        "external": empty_external_counts(),
        "review_health": empty_review_health(),
        "stamp_stock": empty_stamp_stock(),
        "backfill_queue": empty_backfill_counts(),
        "calibrations": empty_calibration_counts(),
        "calibration_health": empty_calibration_health(),
        "learning_candidates": empty_learning_candidate_stock(),
        "learning_readiness": {
            "training_ready_external_examples": 0,
            "human_gate_external_examples": 0,
            "covered_by_local": 0,
            "active_calibrations": 0,
            "candidate_activation_inbox": 0,
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
    workspace = payload.get("workspace") if isinstance(payload.get("workspace"), dict) else {}
    current = workspace.get("current") if isinstance(workspace.get("current"), dict) else {}
    eligibility = workspace.get("eligibility") if isinstance(workspace.get("eligibility"), dict) else {}
    specbackfill = workspace.get("specbackfill") if isinstance(workspace.get("specbackfill"), dict) else {}
    if current.get("configured") and current.get("is_git_repo"):
        if int(current.get("changed_files") or 0) > 0 and specbackfill.get("available"):
            commands.append(
                {
                    "label": "Run deterministic preflight",
                    "command": f"cd {shlex.quote(str(current.get('path') or current.get('requested_path') or '.'))} && specbackfill check --format json --fail-on off",
                    "reason": "Run specbackfill before any local review when it is installed.",
                }
            )
        if eligibility.get("review_recommended"):
            commands.append(
                {
                    "label": "Run local review",
                    "command": str(eligibility.get("suggested_command") or workspace_review_command(current)),
                    "reason": str(eligibility.get("summary") or "Workspace diff is ready for a manual CLI review."),
                }
            )
        elif eligibility.get("status") in {"idle", "up_to_date"}:
            commands.append(
                {
                    "label": "Verify workspace status",
                    "command": workspace_status_command(current),
                    "reason": str(eligibility.get("summary") or "No immediate review is recommended."),
                }
            )
    elif current.get("configured"):
        commands.append(
            {
                "label": "Fix workspace target",
                "command": "llreview target show",
                "reason": str(current.get("error") or "Configured workspace is not reviewable yet."),
            }
        )
    backlog = payload.get("backlog", {})
    stamp_stock = payload.get("stamp_stock") if isinstance(payload.get("stamp_stock"), dict) else {}
    review_health = payload.get("review_health") if isinstance(payload.get("review_health"), dict) else {}
    calibration_health = (
        payload.get("calibration_health")
        if isinstance(payload.get("calibration_health"), dict)
        else {}
    )
    unscored_runs = int(stamp_stock.get("unscored_runs", backlog.get("unscored_runs", 0)) or 0)
    if unscored_runs > 0:
        commands.append(
            {
                "label": "Drain scoring inbox",
                "command": shell_command(["llreview", "scoring-pump", *repo_parts]),
                "reason": f"{unscored_runs} run(s) are still unscored.",
            }
        )
    external_stamps = int(stamp_stock.get("external_stamp_inbox") or 0)
    if external_stamps > 0:
        commands.append(
            {
                "label": "Open external stamp inbox",
                "command": shell_command(["llreview", "learn-review", "--no-activate", *repo_parts]),
                "reason": f"{external_stamps} external item(s) need an operator verdict before learning export.",
            }
        )
    review_gap_stamps = int(stamp_stock.get("review_gap_stamp_inbox") or 0)
    if review_gap_stamps > 0:
        commands.append(
            {
                "label": "Stamp review gaps",
                "command": shell_command(["llreview", "review-gap-stamp-pump", *repo_parts]),
                "reason": f"{review_gap_stamps} review-gap example(s) need a human gate.",
            }
        )
    activation_inbox = int(stamp_stock.get("candidate_activation_inbox") or 0)
    if activation_inbox > 0:
        commands.append(
            {
                "label": "Preview next calibration",
                "command": shell_command(["llreview", "learn-next", *repo_parts]),
                "reason": f"{activation_inbox} proposed prompt/rule candidate(s) are waiting for risk review.",
            }
        )
    if review_health.get("status") in {"watch_false_positive_rate", "watch_recall"}:
        commands.append(
            {
                "label": "Refresh learning scoreboard",
                "command": shell_command(["llreview", "learning-scoreboard", *repo_parts]),
                "reason": str(review_health.get("summary") or "Review health needs aggregate inspection."),
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
                "reason": str(
                    calibration_health.get("summary")
                    or "Active DB calibrations should be checked against later evidence."
                ),
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
    current_workspace = collect_current_workspace(
        str(payload["scope"].get("requested_workspace") or ""),
        repo_override=repo_scope,
    )
    initial_specbackfill = empty_specbackfill_status()
    current_workspace = apply_last_run_to_workspace(current_workspace, None)
    initial_eligibility = workspace_eligibility(current_workspace, initial_specbackfill)
    payload["workspace"] = workspace_section(
        saved_target=saved_target,
        recent=[],
        current=current_workspace,
        eligibility=initial_eligibility,
        specbackfill=initial_specbackfill,
    )

    if not db_path.is_file():
        payload.update(
            {
                "workspace": workspace_section(
                    saved_target=saved_target,
                    recent=[],
                    current=current_workspace,
                    eligibility=initial_eligibility,
                    specbackfill=initial_specbackfill,
                ),
                "tables": {},
                "runs": empty_run_counts(),
                "external": empty_external_counts(),
                "review_health": empty_review_health(),
                "stamp_stock": empty_stamp_stock(),
                "backfill_queue": empty_backfill_counts(),
                "calibrations": empty_calibration_counts(),
                "calibration_health": empty_calibration_health(),
                "learning_candidates": empty_learning_candidate_stock(),
                "learning_readiness": {
                    "training_ready_external_examples": 0,
                    "human_gate_external_examples": 0,
                    "covered_by_local": 0,
                    "active_calibrations": 0,
                    "candidate_activation_inbox": 0,
                    "postgres_optional_backend": "not_ready",
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
            }
        )
        payload["next_commands"] = next_commands(payload)
        return payload

    connection: sqlite3.Connection | None = None
    try:
        connection = connect_review_db_readonly(db_path)
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
        review_health = safe_call(
            empty_review_health(),
            lambda: review_health_counts(
                connection,
                objects=objects,
                repo=repo_scope,
                external_stats=external_stats,
            ),
        )
        candidate_stock = safe_call(
            empty_learning_candidate_stock(),
            lambda: learning_candidate_stock(
                connection,
                objects=objects,
                repo=repo_scope,
            ),
        )
        calibration_health = safe_call(
            empty_calibration_health(),
            lambda: calibration_health_counts(
                connection,
                objects=objects,
                repo=repo_scope,
                limit=args.limit,
            ),
        )
        stamp_stock = stamp_stock_counts(
            runs=runs,
            external_stats=external_stats,
            backfill=backfill,
            candidate_stock=candidate_stock,
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
        specbackfill = safe_call(
            initial_specbackfill,
            lambda: specbackfill_db_status(connection, objects=objects, repo=repo_scope),
        )
        last_run = safe_call(
            None,
            lambda: latest_workspace_run(connection, objects=objects, current=current_workspace),
        )
    except sqlite3.Error as exc:
        payload["db"]["error"] = str(exc)
        payload["next_commands"] = next_commands(payload)
        return payload
    finally:
        if connection is not None:
            connection.close()

    current_workspace = apply_last_run_to_workspace(current_workspace, last_run)
    eligibility = workspace_eligibility(current_workspace, specbackfill)
    training_ready = int(external_stats.get("training_ready_external_examples") or 0)
    human_gate = int(external_stats.get("human_gate_external_examples") or 0)
    payload.update(
        {
            "workspace": workspace_section(
                saved_target=saved_target,
                recent=workspace_rows,
                current=current_workspace,
                eligibility=eligibility,
                specbackfill=specbackfill,
            ),
            "tables": counts,
            "runs": runs,
            "external": external,
            "review_health": review_health,
            "stamp_stock": stamp_stock,
            "backfill_queue": backfill,
            "calibrations": calibrations,
            "calibration_health": calibration_health,
            "learning_candidates": candidate_stock,
            "learning_readiness": {
                **external_stats,
                "active_calibrations": int(calibrations.get("active") or 0),
                "candidate_activation_inbox": int(candidate_stock.get("activation_inbox") or 0),
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
