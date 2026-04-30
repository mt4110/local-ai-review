#!/usr/bin/env python3
"""Small daily CLI for local PR review.

The goal is a low-friction command:

    llreview

It detects the current Git workspace, looks for a matching open GitHub PR, and
falls back to a pre-PR diff when no PR exists yet.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TOOL_ROOT = Path(__file__).resolve().parents[1]
PRECISION_REVIEW = TOOL_ROOT / "scripts" / "local-ai-precision-review.py"
DEFAULT_DB = TOOL_ROOT / "out" / "review-history" / "local-ai-review.db"
DEFAULT_REPORT = TOOL_ROOT / "out" / "reviews" / "llreview-latest.md"
DEFAULT_BENCHMARK_REPORT = TOOL_ROOT / "out" / "reviews" / "benchmark-report.md"
DEFAULT_JSONL = TOOL_ROOT / "out" / "review-history" / "review-items.jsonl"
GITHUB_API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
PROGRESS_PREFIX = "LLREVIEW_EVENT "


@dataclass(frozen=True)
class GitHubRepo:
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class Workspace:
    root: Path
    repo: GitHubRepo
    branch: str
    head_sha: str
    base_ref: str
    dirty: bool
    open_pr: dict[str, Any] | None
    token_status: str


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> str:
    completed = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise SystemExit(f"{' '.join(cmd)} failed: {detail}")
    return completed.stdout.strip()


def git(root: Path, *args: str, check: bool = True, env: dict[str, str] | None = None) -> str:
    return run(["git", "-C", str(root), *args], env=env, check=check)


def discover_git_root(project_dir: Path) -> Path:
    output = run(["git", "-C", str(project_dir), "rev-parse", "--show-toplevel"])
    return Path(output).resolve()


def parse_github_remote(url: str) -> GitHubRepo:
    value = url.strip()
    if value.endswith(".git"):
        value = value[:-4]
    if value.startswith("git@"):
        match = re.match(r"git@([^:]+):([^/]+)/(.+)$", value)
        if match:
            return GitHubRepo(match.group(2), match.group(3))
    parsed = urllib.parse.urlparse(value)
    if parsed.path:
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2:
            return GitHubRepo(parts[-2], parts[-1])
    raise SystemExit(f"Could not infer GitHub owner/repo from remote: {url}")


def detect_repo(root: Path, override: str | None) -> GitHubRepo:
    if override:
        if "/" not in override:
            raise SystemExit("--repo must be owner/name")
        owner, name = override.split("/", 1)
        return GitHubRepo(owner, name)
    remote = git(root, "config", "--get", "remote.origin.url")
    return parse_github_remote(remote)


def github_token() -> tuple[str, str]:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token, "GITHUB_TOKEN"
    try:
        completed = subprocess.run(
            ["gh", "auth", "token"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return "", f"unavailable ({exc})"
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        return "", f"unavailable ({detail})"
    token = completed.stdout.strip()
    return token, "gh auth token"


def github_request(path: str, token: str) -> Any:
    request = urllib.request.Request(
        f"{GITHUB_API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "llreview",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read()
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def detect_base_ref(root: Path) -> str:
    origin_head = git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
    candidates = []
    if origin_head:
        candidates.append(origin_head)
        candidates.append(origin_head.removeprefix("origin/"))
    candidates.extend(["origin/main", "main", "origin/master", "master"])
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if git(root, "rev-parse", "--verify", candidate, check=False):
            return candidate
    return "main"


def find_open_pr(repo: GitHubRepo, branch: str, token: str) -> dict[str, Any] | None:
    if not token or not branch:
        return None
    query = urllib.parse.urlencode({"state": "open", "head": f"{repo.owner}:{branch}"})
    payload = github_request(f"/repos/{repo.full_name}/pulls?{query}", token)
    if isinstance(payload, list) and payload:
        return payload[0]
    return None


def fetch_pr(repo: GitHubRepo, pr_number: int) -> tuple[dict[str, Any] | None, str]:
    token, token_status = github_token()
    if not token:
        return None, token_status
    payload = github_request(f"/repos/{repo.full_name}/pulls/{pr_number}", token)
    return payload if isinstance(payload, dict) else None, token_status


def detect_workspace(project_dir: Path, repo_override: str | None = None) -> Workspace:
    root = discover_git_root(project_dir)
    repo = detect_repo(root, repo_override)
    branch = git(root, "branch", "--show-current", check=False)
    head_sha = git(root, "rev-parse", "HEAD")
    base_ref = detect_base_ref(root)
    dirty = bool(git(root, "status", "--porcelain", check=False))
    token, token_status = github_token()
    open_pr = None
    if token and branch:
        try:
            open_pr = find_open_pr(repo, branch, token)
        except Exception as exc:  # noqa: BLE001 - local pre-PR review can still work.
            token_status = f"{token_status}; PR lookup failed ({exc.__class__.__name__}: {exc})"
    if open_pr:
        base_ref = str((open_pr.get("base") or {}).get("ref") or base_ref)
        head_sha = str((open_pr.get("head") or {}).get("sha") or head_sha)
    return Workspace(
        root=root,
        repo=repo,
        branch=branch,
        head_sha=head_sha,
        base_ref=base_ref,
        dirty=dirty,
        open_pr=open_pr,
        token_status=token_status,
    )


def copy_git_index(root: Path, destination: Path) -> None:
    index_path = Path(git(root, "rev-parse", "--git-path", "index"))
    if not index_path.is_absolute():
        index_path = root / index_path
    if index_path.is_file():
        shutil.copyfile(index_path, destination)
    else:
        run(["git", "-C", str(root), "read-tree", f"--index-output={destination}", "HEAD"])


def build_pre_pr_diff(root: Path, base_ref: str, include_working_tree: bool) -> tuple[Path, bool]:
    diff_text = git(root, "diff", f"{base_ref}...HEAD", check=True)
    working_tree_text = ""
    working_tree_included = False
    with tempfile.NamedTemporaryFile(prefix="llreview-index.", delete=False) as index_file:
        index_path = Path(index_file.name)
    try:
        if include_working_tree:
            copy_git_index(root, index_path)
            env = os.environ.copy()
            env["GIT_INDEX_FILE"] = str(index_path)
            git(root, "add", "-N", "--", ".", env=env, check=False)
            working_tree_text = git(root, "diff", "HEAD", env=env, check=False)
            working_tree_included = bool(working_tree_text.strip())
    finally:
        index_path.unlink(missing_ok=True)

    with tempfile.NamedTemporaryFile(
        prefix="llreview-pre-pr.",
        suffix=".diff",
        mode="w",
        encoding="utf-8",
        delete=False,
    ) as diff_file:
        diff_file.write(diff_text)
        if working_tree_text:
            diff_file.write("\n")
            diff_file.write(working_tree_text)
        return Path(diff_file.name), working_tree_included


def human_bytes(value: int) -> str:
    if value < 1024:
        return f"{value}B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f}KB"
    return f"{value / 1024 / 1024:.1f}MB"


class ProgressRenderer:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.started = time.time()
        self.frame = 0
        self.phase = "starting"
        self.path = ""
        self.model_index = 0
        self.model_total = 0
        self.findings = 0
        self.watch_items = 0
        self.diff_bytes = 0
        self.changed_files = 0
        self.run_id: int | None = None
        self.db_path = ""
        self._last_line_len = 0

    def update(self, event: dict[str, Any]) -> None:
        name = str(event.get("event", ""))
        self.phase = name.replace("_", " ")
        if "path" in event:
            self.path = str(event.get("path") or "")
        if "diff_bytes" in event:
            self.diff_bytes = int(event.get("diff_bytes") or 0)
        if "changed_files" in event:
            self.changed_files = int(event.get("changed_files") or 0)
        if "findings" in event:
            self.findings = int(event.get("findings") or 0)
        if "watch_items" in event:
            self.watch_items = int(event.get("watch_items") or 0)
        if "index" in event:
            self.model_index = int(event.get("index") or 0)
        if "total" in event:
            self.model_total = int(event.get("total") or 0)
        if "model_files" in event:
            self.model_total = int(event.get("model_files") or 0)
        if "run_id" in event:
            self.run_id = int(event.get("run_id") or 0)
        if "db_path" in event:
            self.db_path = str(event.get("db_path") or "")

    def line(self) -> str:
        frames = "|/-\\"
        spinner = frames[self.frame % len(frames)]
        elapsed = int(time.time() - self.started)
        model = ""
        if self.model_total:
            model = f" model {self.model_index}/{self.model_total}"
        diff = f" diff {human_bytes(self.diff_bytes)}" if self.diff_bytes else ""
        files = f" files {self.changed_files}" if self.changed_files else ""
        current = f" {self.path}" if self.path else ""
        return (
            f"{spinner} llreview {elapsed:02d}s {self.phase}{model}"
            f" findings {self.findings} watch {self.watch_items}{files}{diff}{current}"
        )

    def tick(self) -> None:
        if not self.enabled:
            return
        self.frame += 1
        width = shutil.get_terminal_size((100, 20)).columns
        line = self.line()
        if len(line) >= width:
            line = line[: max(0, width - 4)] + "..."
        padded = line.ljust(self._last_line_len)
        self._last_line_len = len(line)
        sys.stderr.write("\r" + padded)
        sys.stderr.flush()

    def finish(self) -> None:
        if not self.enabled:
            return
        sys.stderr.write("\r" + " " * max(self._last_line_len, 1) + "\r")
        sys.stderr.flush()


def run_with_progress(cmd: list[str], *, tui: bool) -> tuple[str, int | None, str]:
    renderer = ProgressRenderer(enabled=tui)
    logs: list[str] = []
    with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", delete=False) as stdout_file:
        stdout_path = Path(stdout_file.name)
    stdout_handle = stdout_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        cmd,
        stdout=stdout_handle,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stderr, selectors.EVENT_READ)

    try:
        while process.poll() is None:
            for key, _ in selector.select(timeout=0.12):
                line = key.fileobj.readline()
                if not line:
                    continue
                handle_progress_line(line, renderer, logs, tui=tui)
            renderer.tick()
        for line in process.stderr:
            handle_progress_line(line, renderer, logs, tui=tui)
    finally:
        renderer.finish()
        selector.close()
        stdout_handle.close()
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
    stdout_path.unlink(missing_ok=True)
    if process.returncode != 0:
        if logs:
            print("\n".join(logs), file=sys.stderr)
        if stdout.strip():
            print(stdout, file=sys.stderr)
        raise SystemExit(process.returncode)
    return stdout, renderer.run_id, renderer.db_path


def handle_progress_line(
    line: str,
    renderer: ProgressRenderer,
    logs: list[str],
    *,
    tui: bool,
) -> None:
    stripped = line.rstrip("\n")
    if stripped.startswith(PROGRESS_PREFIX):
        try:
            event = json.loads(stripped[len(PROGRESS_PREFIX) :])
        except json.JSONDecodeError:
            logs.append(stripped)
            return
        renderer.update(event)
        if not tui:
            phase = str(event.get("event", "")).replace("_", " ")
            details = []
            if "index" in event and "total" in event:
                details.append(f"{event['index']}/{event['total']}")
            if "path" in event:
                details.append(str(event["path"]))
            print("llreview:", phase, " ".join(details), file=sys.stderr)
        return
    logs.append(stripped)
    if not tui:
        print(stripped, file=sys.stderr)


def update_workspace_state(db_path: Path, workspace: Workspace, run_id: int | None) -> None:
    if run_id is None or not db_path.is_file():
        return
    pr_number = int(workspace.open_pr["number"]) if workspace.open_pr else 0
    head_ref = str((workspace.open_pr or {}).get("head", {}).get("ref") or workspace.branch)
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
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
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(workspace_path) DO UPDATE SET
                repo = excluded.repo,
                branch = excluded.branch,
                pr_number = excluded.pr_number,
                base_ref = excluded.base_ref,
                head_ref = excluded.head_ref,
                head_sha = excluded.head_sha,
                last_run_id = excluded.last_run_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                str(workspace.root),
                workspace.repo.full_name,
                workspace.branch,
                pr_number,
                workspace.base_ref,
                head_ref,
                workspace.head_sha,
                run_id,
            ),
        )


def ensure_db_schema(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            sys.executable,
            str(PRECISION_REVIEW),
            "--init-db",
            "--db",
            str(db_path),
        ],
        check=True,
    )


def build_review_command(args: argparse.Namespace, workspace: Workspace) -> tuple[list[str], Path | None]:
    report_path = Path(args.output).expanduser().resolve() if args.output else DEFAULT_REPORT
    report_path.parent.mkdir(parents=True, exist_ok=True)
    db_path = Path(args.db).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(PRECISION_REVIEW),
        "--repo",
        workspace.repo.full_name,
        "--output",
        str(report_path),
        "--db",
        str(db_path),
        "--base-ref",
        workspace.base_ref,
        "--head-ref",
        workspace.branch,
        "--head-sha",
        workspace.head_sha,
        "--progress-events",
    ]
    if args.post:
        cmd.append("--post-comment")
    if args.static:
        cmd.extend(["--max-model-files", "0"])
    elif args.max_model_files is not None:
        cmd.extend(["--max-model-files", str(args.max_model_files)])

    if workspace.open_pr:
        pr_number = str(args.pr or workspace.open_pr["number"])
        cmd.extend(["--pr", pr_number])
        return cmd, None

    diff_path, working_tree_included = build_pre_pr_diff(
        workspace.root,
        workspace.base_ref,
        include_working_tree=not args.no_working_tree,
    )
    subject = workspace.branch or workspace.head_sha[:12]
    cmd.extend(
        [
            "--pr",
            "0",
            "--diff-file",
            str(diff_path),
            "--review-kind",
            "pre_pr",
            "--diff-source-label",
            f"pre_pr:{workspace.base_ref}...{subject}",
        ]
    )
    if working_tree_included:
        cmd.append("--working-tree-included")
    return cmd, diff_path


def command_review(args: argparse.Namespace) -> None:
    workspace = detect_workspace(Path(args.project_dir).expanduser().resolve(), args.repo)
    if args.pr:
        pr_payload, token_status = fetch_pr(workspace.repo, args.pr)
        base_ref = workspace.base_ref
        head_ref = workspace.branch
        head_sha = workspace.head_sha
        if pr_payload:
            base_ref = str((pr_payload.get("base") or {}).get("ref") or base_ref)
            head_ref = str((pr_payload.get("head") or {}).get("ref") or head_ref)
            head_sha = str((pr_payload.get("head") or {}).get("sha") or head_sha)
        workspace = Workspace(
            root=workspace.root,
            repo=workspace.repo,
            branch=head_ref,
            head_sha=head_sha,
            base_ref=base_ref,
            dirty=workspace.dirty,
            open_pr=pr_payload or {"number": args.pr, "head": {"ref": head_ref, "sha": head_sha}},
            token_status=token_status,
        )
    if args.post and not workspace.open_pr:
        raise SystemExit("--post requires an open PR; run without --post for pre-PR review")
    cmd, temp_diff = build_review_command(args, workspace)
    try:
        tui = sys.stderr.isatty() and not args.plain
        stdout, run_id, db_path_text = run_with_progress(cmd, tui=tui)
        db_path = Path(db_path_text).expanduser().resolve() if db_path_text else Path(args.db).expanduser().resolve()
        update_workspace_state(db_path, workspace, run_id)
        print(stdout.rstrip())
        subject = f"PR #{workspace.open_pr['number']}" if workspace.open_pr else "pre-PR diff"
        print(f"\nllreview saved {subject} run_id={run_id or 'unknown'}")
    finally:
        if temp_diff is not None:
            temp_diff.unlink(missing_ok=True)


def fetch_last_run(db_path: Path, workspace: Workspace) -> sqlite3.Row | None:
    if not db_path.is_file():
        return None
    pr_number = int(workspace.open_pr["number"]) if workspace.open_pr else 0
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        if pr_number:
            return connection.execute(
                """
                SELECT *
                FROM review_run_summary
                WHERE repo = ? AND pr_number = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (workspace.repo.full_name, pr_number),
            ).fetchone()
        return connection.execute(
            """
            SELECT *
            FROM review_run_summary
            WHERE repo = ? AND head_ref = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (workspace.repo.full_name, workspace.branch),
        ).fetchone()


def command_status(args: argparse.Namespace) -> None:
    workspace = detect_workspace(Path(args.project_dir).expanduser().resolve(), args.repo)
    db_path = Path(args.db).expanduser().resolve()
    ensure_db_schema(db_path)
    last_run = fetch_last_run(db_path, workspace)
    unscored = 0
    if db_path.is_file():
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM review_run_summary WHERE useful_findings_fixed IS NULL"
            ).fetchone()
            unscored = int(row[0] if row else 0)
    print(f"Workspace: {workspace.root}")
    print(f"Repository: {workspace.repo.full_name}")
    print(f"Branch: {workspace.branch or '(detached)'}")
    print(f"Base: {workspace.base_ref}")
    print(f"Head: {workspace.head_sha[:12]}")
    print(f"Dirty: {'yes' if workspace.dirty else 'no'}")
    print(f"GitHub auth: {workspace.token_status}")
    if workspace.open_pr:
        print(f"Open PR: #{workspace.open_pr['number']} {workspace.open_pr.get('html_url', '')}")
    else:
        print("Open PR: none detected; llreview will use pre-PR mode")
    if last_run:
        print(
            "Last run: "
            f"id={last_run['id']} findings={last_run['findings_count']} "
            f"watch={last_run['watch_items_count']} elapsed={last_run['elapsed_seconds']:.1f}s"
        )
    else:
        print("Last run: none")
    print(f"Unscored runs: {unscored}")
    print(f"DB: {db_path}")


def parse_non_negative(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise ValueError("expected a non-negative integer")
    return parsed


def prompt_int(label: str, default: int | None = None) -> int:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"{label}{suffix}: ").strip()
        if not raw and default is not None:
            return default
        try:
            return parse_non_negative(raw)
        except ValueError as exc:
            print(exc)


def prompt_bool(label: str, default: bool = True) -> int:
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        raw = input(f"{label}{suffix}: ").strip().lower()
        if not raw:
            return int(default)
        if raw in {"y", "yes", "1", "true"}:
            return 1
        if raw in {"n", "no", "0", "false"}:
            return 0
        print("expected yes or no")


def command_score(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser().resolve()
    ensure_db_schema(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        if args.run:
            row = connection.execute("SELECT * FROM review_run_summary WHERE id = ?", (args.run,)).fetchone()
        else:
            row = connection.execute(
                """
                SELECT *
                FROM review_run_summary
                WHERE useful_findings_fixed IS NULL
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            raise SystemExit("No run to score.")
        print(
            f"Scoring run {row['id']} {row['repo']} "
            f"#{row['pr_number'] or 'pre-PR'} findings={row['findings_count']} watch={row['watch_items_count']}"
        )
        useful = args.useful if args.useful is not None else prompt_int("Useful findings fixed", 0)
        false_positives = (
            args.false_positives
            if args.false_positives is not None
            else prompt_int("False positives", 0)
        )
        unclear = args.unclear if args.unclear is not None else prompt_int("Unclear findings", 0)
        remote_ready = (
            int(args.remote_ready)
            if args.remote_ready is not None
            else prompt_bool("Would request remote review now", True)
        )
        remote_findings = (
            args.remote_findings
            if args.remote_findings is not None
            else prompt_int("Remote findings count", 0)
        )
        note = args.note if args.note is not None else input("Note: ").strip()
        connection.execute(
            """
            INSERT INTO run_feedback (
                run_id,
                useful_findings_fixed,
                false_positives,
                unclear_findings,
                would_request_remote_review_now,
                remote_findings_count,
                note,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(run_id) DO UPDATE SET
                useful_findings_fixed = excluded.useful_findings_fixed,
                false_positives = excluded.false_positives,
                unclear_findings = excluded.unclear_findings,
                would_request_remote_review_now = excluded.would_request_remote_review_now,
                remote_findings_count = excluded.remote_findings_count,
                note = excluded.note,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                int(row["id"]),
                useful,
                false_positives,
                unclear,
                remote_ready,
                remote_findings,
                note,
            ),
        )
    print(f"OK: scored run_id={row['id']}")


def command_report(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser().resolve()
    ensure_db_schema(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT *
            FROM review_run_summary
            ORDER BY id DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        verdict_rows = connection.execute(
            """
            SELECT verdict, COUNT(*) AS count
            FROM item_verdicts
            GROUP BY verdict
            ORDER BY verdict
            """
        ).fetchall()
    lines = [
        "# Review Benchmark Report",
        "",
        f"- Runs: {len(rows)}",
        f"- DB: `{db_path}`",
        "",
        "## Runs",
        "",
        "| Run | Repo | PR | Findings | Watch | Useful | False positives | Unclear | Runtime |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| {id} | {repo} | {pr} | {findings} | {watch} | {useful} | {fp} | {unclear} | {elapsed:.1f}s |".format(
                id=row["id"],
                repo=row["repo"],
                pr=row["pr_number"] or 0,
                findings=row["findings_count"],
                watch=row["watch_items_count"],
                useful=row["useful_findings_fixed"] if row["useful_findings_fixed"] is not None else "",
                fp=row["false_positives"] if row["false_positives"] is not None else "",
                unclear=row["unclear_findings"] if row["unclear_findings"] is not None else "",
                elapsed=row["elapsed_seconds"],
            )
        )
    lines.extend(["", "## Item Verdicts", ""])
    if verdict_rows:
        for row in verdict_rows:
            lines.append(f"- {row['verdict']}: {row['count']}")
    else:
        lines.append("- No item verdicts recorded yet.")
    report = "\n".join(lines) + "\n"
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    print(report.rstrip())
    print(f"\nOK: wrote {output}")


def command_export_jsonl(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser().resolve()
    ensure_db_schema(db_path)
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection, output.open("w", encoding="utf-8") as file:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
                runs.repo,
                runs.pr_number,
                runs.review_kind,
                runs.base_ref,
                runs.head_ref,
                runs.head_sha,
                runs.model,
                runs.diff_bytes,
                runs.elapsed_seconds,
                items.item_type,
                items.source,
                items.severity,
                items.confidence,
                items.path,
                items.line,
                items.title,
                items.body,
                items.fix,
                items.verification,
                items.fingerprint
            FROM review_items AS items
            JOIN review_runs AS runs
            ON runs.id = items.run_id
            ORDER BY runs.id, items.item_type, items.ordinal
            """
        )
        count = 0
        for row in rows:
            file.write(json.dumps(dict(row), sort_keys=True, ensure_ascii=False) + "\n")
            count += 1
    print(f"OK: exported {count} review items to {output}")


def add_workspace_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-dir", default=os.getcwd(), help="Git workspace to inspect")
    parser.add_argument("--repo", help="Override GitHub repository as owner/name")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite review history DB")


def build_review_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Auto-detect and run local PR review",
        epilog="Subcommands: status, score, report, export-jsonl",
    )
    parser.set_defaults(func=command_review)
    parser.add_argument("pr", nargs="?", type=int, help="PR number. Omit to auto-detect.")
    add_workspace_options(parser)
    parser.add_argument("--output", default=str(DEFAULT_REPORT), help="Markdown report output path")
    parser.add_argument("--post", action="store_true", help="Post or update the marker PR comment")
    parser.add_argument("--plain", action="store_true", help="Disable TTY progress animation")
    parser.add_argument("--static", action="store_true", help="Run static checks only")
    parser.add_argument("--max-model-files", type=int, help="Override model-reviewed file limit")
    parser.add_argument("--no-working-tree", action="store_true", help="Do not include dirty working tree in pre-PR mode")
    return parser


def build_status_parser() -> argparse.ArgumentParser:
    status = argparse.ArgumentParser(description="Show detected workspace and review state")
    status.set_defaults(func=command_status)
    add_workspace_options(status)
    return status


def build_score_parser() -> argparse.ArgumentParser:
    score = argparse.ArgumentParser(description="Score the latest unscored run")
    score.set_defaults(func=command_score)
    score.add_argument("--db", default=str(DEFAULT_DB))
    score.add_argument("--run", type=int)
    score.add_argument("--useful", type=int)
    score.add_argument("--false-positives", type=int)
    score.add_argument("--unclear", type=int)
    score.add_argument("--remote-ready", type=int, choices=[0, 1])
    score.add_argument("--remote-findings", type=int)
    score.add_argument("--note")
    return score


def build_report_parser() -> argparse.ArgumentParser:
    report = argparse.ArgumentParser(description="Generate a benchmark report")
    report.set_defaults(func=command_report)
    report.add_argument("--db", default=str(DEFAULT_DB))
    report.add_argument("--limit", type=int, default=10)
    report.add_argument("--output", default=str(DEFAULT_BENCHMARK_REPORT))
    return report


def build_export_parser() -> argparse.ArgumentParser:
    export = argparse.ArgumentParser(description="Export review items as JSONL")
    export.set_defaults(func=command_export_jsonl)
    export.add_argument("--db", default=str(DEFAULT_DB))
    export.add_argument("--output", default=str(DEFAULT_JSONL))
    return export


COMMAND_PARSERS = {
    "status": build_status_parser,
    "score": build_score_parser,
    "report": build_report_parser,
    "export-jsonl": build_export_parser,
}


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in COMMAND_PARSERS:
        parser = COMMAND_PARSERS[sys.argv[1]]()
        args = parser.parse_args(sys.argv[2:])
    else:
        parser = build_review_parser()
        args = parser.parse_args(sys.argv[1:])
    args.func(args)


if __name__ == "__main__":
    main()
