#!/usr/bin/env python3
"""Small daily CLI for local PR review.

The goal is a low-friction command:

    llreview

It detects the current Git workspace, looks for a matching open GitHub PR, and
falls back to a pre-PR diff when no PR exists yet.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import html
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
import urllib.error
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
DEFAULT_INSTALL_PATH = Path.home() / ".local" / "bin" / "llreview"
GITHUB_API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
PROGRESS_PREFIX = "LLREVIEW_EVENT "
IMPORT_LINK_NOTE_PREFIX = "github_importer:"
LOCAL_ITEM_VERDICTS = {
    "u": "useful_fixed",
    "useful": "useful_fixed",
    "useful_fixed": "useful_fixed",
    "f": "false_positive",
    "fp": "false_positive",
    "false_positive": "false_positive",
    "c": "unclear",
    "unclear": "unclear",
    "w": "watch_only",
    "watch": "watch_only",
    "watch_only": "watch_only",
    "s": "skip",
    "skip": "skip",
}
REASON_ALIASES = {
    "1": "covered_by_existing_safeguard",
    "safeguard": "covered_by_existing_safeguard",
    "covered": "covered_by_existing_safeguard",
    "covered_by_existing_safeguard": "covered_by_existing_safeguard",
    "2": "intentional_behavior",
    "intentional": "intentional_behavior",
    "intentional_behavior": "intentional_behavior",
    "3": "environment_dependent",
    "env": "environment_dependent",
    "environment": "environment_dependent",
    "environment_dependent": "environment_dependent",
    "4": "covered_by_tests",
    "tests": "covered_by_tests",
    "covered_by_tests": "covered_by_tests",
    "5": "stale_or_already_fixed",
    "stale": "stale_or_already_fixed",
    "fixed": "stale_or_already_fixed",
    "stale_or_already_fixed": "stale_or_already_fixed",
    "6": "diagnostic_watch",
    "watch": "diagnostic_watch",
    "diagnostic": "diagnostic_watch",
    "diagnostic_watch": "diagnostic_watch",
    "7": "insufficient_context",
    "context": "insufficient_context",
    "insufficient_context": "insufficient_context",
    "8": "actual_issue",
    "actual": "actual_issue",
    "actual_issue": "actual_issue",
    "9": "other",
    "other": "other",
}
REASON_MENU = [
    ("1", "covered_by_existing_safeguard"),
    ("2", "intentional_behavior"),
    ("3", "environment_dependent"),
    ("4", "covered_by_tests"),
    ("5", "stale_or_already_fixed"),
    ("6", "diagnostic_watch"),
    ("7", "insufficient_context"),
    ("8", "actual_issue"),
    ("9", "other"),
]


@dataclass(frozen=True)
class GitHubRepo:
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def is_local(self) -> bool:
        return self.owner == "local"


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


@dataclass(frozen=True)
class ExternalReviewItem:
    repo: str
    pr_number: int
    head_sha: str
    source: str
    path: str
    line: int | None
    title: str
    body: str
    url: str
    github_comment_id: str
    github_thread_id: str
    fingerprint: str


@dataclass(frozen=True)
class LinkCandidate:
    id: int
    run_id: int
    item_type: str
    source: str
    path: str
    line: int | None
    title: str
    body: str
    fix: str
    verification: str
    fingerprint: str


@dataclass(frozen=True)
class LinkMatch:
    review_item_id: int
    external_item_id: int
    relation: str
    score: float
    note: str


class GitHubRequestError(Exception):
    """Readable GitHub API failure for CLI paths."""


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


def github_remotes(root: Path) -> list[tuple[str, GitHubRepo]]:
    remotes: list[tuple[str, GitHubRepo]] = []
    remote_names = git(root, "remote", check=False).splitlines()
    seen: set[str] = set()
    for name in remote_names:
        url = git(root, "remote", "get-url", name, check=False)
        if not url:
            continue
        try:
            repo = parse_github_remote(url)
        except SystemExit:
            continue
        if repo.full_name in seen:
            continue
        seen.add(repo.full_name)
        remotes.append((name, repo))
    return remotes


def detect_repo(root: Path, override: str | None) -> GitHubRepo:
    if override:
        if "/" not in override:
            raise SystemExit("--repo must be owner/name")
        owner, name = override.split("/", 1)
        return GitHubRepo(owner, name)
    remotes = github_remotes(root)
    for remote_name, repo in remotes:
        if remote_name == "origin":
            return repo
    if remotes:
        return remotes[0][1]
    return GitHubRepo("local", root.name)


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
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw_error = exc.read().decode("utf-8", errors="replace").strip()
        detail = f": {raw_error}" if raw_error else ""
        raise GitHubRequestError(f"GitHub API {path} failed with HTTP {exc.code}{detail}") from exc
    except urllib.error.URLError as exc:
        raise GitHubRequestError(f"GitHub API {path} failed: {exc.reason}") from exc
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def github_paginated_request(path: str, token: str) -> list[Any]:
    items: list[Any] = []
    page = 1
    separator = "&" if "?" in path else "?"
    while True:
        payload = github_request(f"{path}{separator}per_page=100&page={page}", token)
        if not isinstance(payload, list):
            return items
        items.extend(payload)
        if len(payload) < 100:
            return items
        page += 1


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
    fallback = "HEAD~1"
    if git(root, "rev-parse", "--verify", fallback, check=False):
        return fallback
    if git(root, "rev-parse", "--verify", "HEAD", check=False):
        return "HEAD"
    attempted = ", ".join(seen) or "(none)"
    raise SystemExit(
        "Could not resolve a base ref for pre-PR review. "
        f"Tried {attempted}, {fallback}, and HEAD. Pass an explicit PR number or set origin/HEAD."
    )


def find_open_pr(
    repo: GitHubRepo,
    branch: str,
    token: str,
    *,
    head_owner: str | None = None,
) -> dict[str, Any] | None:
    if not token or not branch:
        return None
    owner = head_owner or repo.owner
    query = urllib.parse.urlencode({"state": "open", "head": f"{owner}:{branch}"})
    payload = github_request(f"/repos/{repo.full_name}/pulls?{query}", token)
    if isinstance(payload, list) and payload:
        return payload[0]
    return None


def find_open_pr_across_remotes(
    repo: GitHubRepo,
    remotes: list[tuple[str, GitHubRepo]],
    branch: str,
    token: str,
) -> tuple[GitHubRepo, dict[str, Any] | None, str]:
    if repo.is_local or not token or not branch:
        return repo, None, ""
    base_candidates = [repo]
    for _, remote_repo in remotes:
        if remote_repo not in base_candidates:
            base_candidates.append(remote_repo)
    head_owners = [repo.owner]
    for remote_name, remote_repo in remotes:
        if remote_name == "origin" and remote_repo.owner not in head_owners:
            head_owners.append(remote_repo.owner)
    for _, remote_repo in remotes:
        if remote_repo.owner not in head_owners:
            head_owners.append(remote_repo.owner)

    errors: list[str] = []
    for base_repo in base_candidates:
        for head_owner in head_owners:
            try:
                pr = find_open_pr(base_repo, branch, token, head_owner=head_owner)
            except GitHubRequestError as exc:
                errors.append(str(exc))
                continue
            if pr:
                return base_repo, pr, ""
    return repo, None, "; ".join(errors)


def fetch_pr(repo: GitHubRepo, pr_number: int) -> tuple[dict[str, Any] | None, str]:
    token, token_status = github_token()
    if not token:
        return None, token_status
    try:
        payload = github_request(f"/repos/{repo.full_name}/pulls/{pr_number}", token)
    except GitHubRequestError as exc:
        raise SystemExit(str(exc)) from exc
    return payload if isinstance(payload, dict) else None, token_status


def detect_workspace(project_dir: Path, repo_override: str | None = None) -> Workspace:
    root = discover_git_root(project_dir)
    remotes = github_remotes(root)
    repo = detect_repo(root, repo_override)
    branch = git(root, "branch", "--show-current", check=False)
    head_sha = git(root, "rev-parse", "HEAD")
    base_ref = detect_base_ref(root)
    dirty = bool(git(root, "status", "--porcelain", check=False))
    token, token_status = github_token()
    open_pr = None
    if token and branch and not repo.is_local:
        pr_repo, open_pr, lookup_error = find_open_pr_across_remotes(repo, remotes, branch, token)
        repo = pr_repo
        if lookup_error:
            token_status = f"{token_status}; PR lookup failed ({lookup_error})"
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
        display_width = max(self._last_line_len, len(line))
        padded = line.ljust(display_width)
        self._last_line_len = display_width
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

    trusted_context_dirs: list[Path] = []
    for raw_dir in args.trusted_context_dir or []:
        trusted_context_dirs.append(Path(os.path.abspath(os.path.expanduser(raw_dir))))

    if workspace.open_pr:
        pr_number = str(args.pr or workspace.open_pr["number"])
        cmd.extend(["--pr", pr_number])
        for context_dir in trusted_context_dirs:
            cmd.extend(["--trusted-context-dir", str(context_dir)])
        return cmd, None

    diff_path, working_tree_included = build_pre_pr_diff(
        workspace.root,
        workspace.base_ref,
        include_working_tree=not args.no_working_tree,
    )
    if not args.no_trusted_context:
        local_context = workspace.root / ".private_docs"
        # Auto-loaded context must not follow workspace-controlled links outside the repo.
        if local_context.is_dir() and not local_context.is_symlink():
            trusted_context_dirs.insert(0, local_context.resolve())
    seen_context_dirs: set[Path] = set()
    for context_dir in trusted_context_dirs:
        if context_dir in seen_context_dirs:
            continue
        seen_context_dirs.add(context_dir)
        cmd.extend(["--trusted-context-dir", str(context_dir)])
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
    if args.update:
        command_update(
            argparse.Namespace(
                path=None,
                branch=args.update_branch,
                check=args.update_check,
                force=args.update_force,
            )
        )
        return
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
    external_total = 0
    external_linked = 0
    if db_path.is_file():
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM review_run_summary WHERE useful_findings_fixed IS NULL"
            ).fetchone()
            unscored = int(row[0] if row else 0)
            pr_number = int(workspace.open_pr["number"]) if workspace.open_pr else None
            external_total, external_linked = external_scope_counts(
                connection,
                repo=workspace.repo.full_name,
                pr_number=pr_number,
            )
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
    print(
        "External review items: "
        f"total={external_total} linked={external_linked} unlinked={external_total - external_linked}"
    )
    print(f"DB: {db_path}")


def parse_non_negative(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a non-negative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("expected a non-negative integer")
    return parsed


def parse_bool_value(value: str) -> int:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y"}:
        return 1
    if normalized in {"0", "false", "no", "n"}:
        return 0
    raise argparse.ArgumentTypeError("expected yes/no, true/false, or 1/0")


def prompt_int(label: str, default: int | None = None) -> int:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"{label}{suffix}: ").strip()
        if not raw and default is not None:
            return default
        try:
            return parse_non_negative(raw)
        except argparse.ArgumentTypeError as exc:
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


def prompt_item_verdict(existing: str = "") -> str:
    suffix = f" [{existing or 'skip'}]" if existing else " [skip]"
    prompt = "Verdict useful/fp/unclear/watch/skip"
    while True:
        raw = input(f"{prompt}{suffix}: ").strip().lower()
        if not raw:
            return existing or "skip"
        verdict = LOCAL_ITEM_VERDICTS.get(raw)
        if verdict:
            return verdict
        print("expected useful, fp, unclear, watch, or skip")


def default_reason_for_verdict(verdict: str) -> str:
    if verdict == "useful_fixed":
        return "actual_issue"
    if verdict == "watch_only":
        return "diagnostic_watch"
    if verdict == "unclear":
        return "insufficient_context"
    return "covered_by_existing_safeguard"


def prompt_reason(verdict: str, existing: str = "") -> str:
    default = existing or default_reason_for_verdict(verdict)
    print("Reason:")
    for key, reason in REASON_MENU:
        marker = " (default)" if reason == default else ""
        print(f"  {key}. {reason}{marker}")
    while True:
        raw = input(f"Reason [{default}]: ").strip().lower()
        if not raw:
            return default
        reason = REASON_ALIASES.get(raw, raw)
        if re.fullmatch(r"[a-z0-9_.-]+", reason):
            return reason
        print("expected a simple reason code")


def truncate_text(value: str, limit: int = 180) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)] + "..."


def markdown_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def percent(numerator: int | float, denominator: int | float) -> str:
    if not denominator:
        return "n/a"
    return f"{(numerator / denominator) * 100:.1f}%"


def stable_fingerprint(*parts: Any) -> str:
    normalized = "\n".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def as_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def strip_review_boilerplate(value: str) -> str:
    text = re.sub(r"<details\b.*?</details>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\n\s*Useful\?\s*React with.*$", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\n\s*---\s*\n.*$", "", text, flags=re.DOTALL)
    return text.strip()


def markdown_to_plain_text(value: str) -> str:
    text = html.unescape(strip_review_boilerplate(value))
    text = re.sub(r"!\[[^\]]*]\([^)]+\)", " ", text)
    text = re.sub(r"\[([^\]]+)]\([^)]+\)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[*>#~]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_review_text(value: str) -> str:
    text = markdown_to_plain_text(value).lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^a-z0-9_./:-]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def review_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9_./:-]{3,}", normalize_review_text(value))
        if token not in {"the", "and", "for", "with", "that", "this", "from", "into", "when"}
    }


def text_similarity(left: str, right: str) -> float:
    left_normalized = normalize_review_text(left)
    right_normalized = normalize_review_text(right)
    if not left_normalized or not right_normalized:
        return 0.0
    left_tokens = review_tokens(left_normalized)
    right_tokens = review_tokens(right_normalized)
    token_score = 0.0
    if left_tokens and right_tokens:
        token_score = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    sequence_score = difflib.SequenceMatcher(None, left_normalized, right_normalized).ratio()
    return max(token_score, sequence_score * 0.65)


def external_source_for_comment(comment: dict[str, Any]) -> str:
    login = str((comment.get("user") or {}).get("login", "")).lower()
    if "copilot" in login:
        return "copilot"
    if login.endswith("[bot]"):
        return "automated"
    if login.endswith("-bot"):
        return "bot_review"
    return "human"


def external_title_from_body(body: str) -> str:
    clean = strip_review_boilerplate(body)
    for raw_line in clean.splitlines():
        line = markdown_to_plain_text(raw_line)
        line = re.sub(r"\bP[0-3]\s+Badge\b", " ", line, flags=re.IGNORECASE)
        line = re.sub(r"^\s*\[?P[0-3]\]?\s+", "", line)
        line = re.sub(r"\s+", " ", line).strip(" :-")
        if line:
            return truncate_text(line, 140)
    return "External review comment"


def should_skip_issue_comment(body: str) -> bool:
    normalized = normalize_review_text(body)
    if not normalized:
        return True
    if "<!-- local-ai-precision-review -->" in body or "<!-- local-llm-review -->" in body:
        return True
    if re.search(r"(?im)^\s*@[-a-z0-9_]+\s+review\s*$", body):
        return True
    if re.fullmatch(r"@?[a-z0-9_-]+\s+review", normalized):
        return True
    if "didn t find any major issues" in normalized:
        return True
    return False


def external_item_fingerprint(item: ExternalReviewItem) -> str:
    return stable_fingerprint(
        "external",
        item.repo,
        item.pr_number,
        item.source,
        item.path,
        item.line or "",
        normalize_review_text(f"{item.title}\n{item.body}"),
    )


def link_match_fingerprint(path: str, line: int | None, text: str) -> str:
    normalized = normalize_review_text(text)
    if not normalized:
        return ""
    return stable_fingerprint("review-link-v1", path, line or "", normalized)


def link_match_fingerprints(path: str, line: int | None, texts: list[str]) -> set[str]:
    fingerprints: set[str] = set()
    for text in texts:
        fingerprint = link_match_fingerprint(path, line, text)
        if fingerprint:
            fingerprints.add(fingerprint)
    return fingerprints


def external_review_text(item: ExternalReviewItem) -> str:
    return "\n".join(part for part in (item.title, item.body) if part)


def external_link_match_fingerprints(item: ExternalReviewItem) -> set[str]:
    return link_match_fingerprints(
        item.path,
        item.line,
        [external_review_text(item), item.body, item.title],
    )


def external_item_from_comment(
    *,
    repo: str,
    pr_number: int,
    default_head_sha: str,
    comment: dict[str, Any],
    comment_kind: str,
) -> ExternalReviewItem | None:
    body = str(comment.get("body") or "")
    if comment_kind != "issue_comment" and comment.get("in_reply_to_id") is not None:
        return None
    if comment_kind == "issue_comment" and should_skip_issue_comment(body):
        return None
    clean_body = strip_review_boilerplate(body)
    if not normalize_review_text(clean_body):
        return None
    line = as_optional_int(comment.get("line"))
    if line is None:
        line = as_optional_int(comment.get("original_line"))
    github_id = str(comment.get("id") or comment.get("node_id") or "")
    if github_id:
        github_id = f"{comment_kind}:{github_id}"
    review_id = str(comment.get("pull_request_review_id") or "")
    thread_id = f"review:{review_id}" if review_id else ""
    item = ExternalReviewItem(
        repo=repo,
        pr_number=pr_number,
        head_sha=str(comment.get("commit_id") or default_head_sha or ""),
        source=external_source_for_comment(comment),
        path=str(comment.get("path") or ""),
        line=line,
        title=external_title_from_body(clean_body),
        body=clean_body,
        url=str(comment.get("html_url") or comment.get("url") or ""),
        github_comment_id=github_id,
        github_thread_id=thread_id,
        fingerprint="",
    )
    return ExternalReviewItem(
        repo=item.repo,
        pr_number=item.pr_number,
        head_sha=item.head_sha,
        source=item.source,
        path=item.path,
        line=item.line,
        title=item.title,
        body=item.body,
        url=item.url,
        github_comment_id=item.github_comment_id,
        github_thread_id=item.github_thread_id,
        fingerprint=external_item_fingerprint(item),
    )


def external_items_from_comments(
    *,
    repo: str,
    pr_number: int,
    default_head_sha: str,
    comments: list[Any],
    comment_kind: str,
) -> list[ExternalReviewItem]:
    items: list[ExternalReviewItem] = []
    seen: set[str] = set()
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        item = external_item_from_comment(
            repo=repo,
            pr_number=pr_number,
            default_head_sha=default_head_sha,
            comment=comment,
            comment_kind=comment_kind,
        )
        if item is None:
            continue
        dedupe_key = item.github_comment_id or item.fingerprint
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        items.append(item)
    return items


def load_json_list(path: Path) -> list[Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise SystemExit(f"{path} must contain a JSON array")
    return payload


def upsert_external_item(connection: sqlite3.Connection, item: ExternalReviewItem) -> tuple[int, bool]:
    existing = None
    if item.github_comment_id:
        existing = connection.execute(
            """
            SELECT id
            FROM external_items
            WHERE repo = ? AND pr_number = ? AND github_comment_id = ?
            """,
            (item.repo, item.pr_number, item.github_comment_id),
        ).fetchone()
    if existing is None:
        existing = connection.execute(
            """
            SELECT id
            FROM external_items
            WHERE repo = ? AND pr_number = ? AND fingerprint = ? AND github_comment_id = ''
            """,
            (item.repo, item.pr_number, item.fingerprint),
        ).fetchone()
    if existing:
        item_id = int(existing["id"])
        connection.execute(
            """
            UPDATE external_items
            SET
                head_sha = ?,
                source = ?,
                path = ?,
                line = ?,
                title = ?,
                body = ?,
                url = ?,
                github_comment_id = ?,
                github_thread_id = ?,
                fingerprint = ?
            WHERE id = ?
            """,
            (
                item.head_sha,
                item.source,
                item.path,
                item.line,
                item.title,
                item.body,
                item.url,
                item.github_comment_id,
                item.github_thread_id,
                item.fingerprint,
                item_id,
            ),
        )
        return item_id, False
    cursor = connection.execute(
        """
        INSERT INTO external_items (
            repo,
            pr_number,
            head_sha,
            source,
            path,
            line,
            title,
            body,
            url,
            github_comment_id,
            github_thread_id,
            fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item.repo,
            item.pr_number,
            item.head_sha,
            item.source,
            item.path,
            item.line,
            item.title,
            item.body,
            item.url,
            item.github_comment_id,
            item.github_thread_id,
            item.fingerprint,
        ),
    )
    return int(cursor.lastrowid), True


def load_link_candidates(
    connection: sqlite3.Connection,
    *,
    repo: str,
    pr_number: int,
    head_shas: set[str],
    head_ref: str,
    run_id: int | None,
) -> list[LinkCandidate]:
    params: list[Any] = []
    if run_id is not None:
        where_sql = "runs.id = ? AND runs.repo = ?"
        params.extend([run_id, repo])
    else:
        clauses: list[str] = []
        clean_head_shas = sorted(sha for sha in head_shas if sha)
        if pr_number > 0:
            if clean_head_shas:
                placeholders = ",".join("?" for _ in clean_head_shas)
                clauses.append(f"(runs.pr_number = ? AND runs.head_sha IN ({placeholders}))")
                params.append(pr_number)
                params.extend(clean_head_shas)
            else:
                clauses.append("runs.pr_number = ?")
                params.append(pr_number)
        if clean_head_shas:
            placeholders = ",".join("?" for _ in clean_head_shas)
            clauses.append(f"(runs.pr_number = 0 AND runs.head_sha IN ({placeholders}))")
            params.extend(clean_head_shas)
        if head_ref and not clean_head_shas:
            clauses.append("(runs.pr_number = 0 AND runs.head_ref = ?)")
            params.append(head_ref)
        if not clauses:
            return []
        where_sql = f"runs.repo = ? AND ({' OR '.join(clauses)})"
        params.insert(0, repo)
    rows = connection.execute(
        f"""
        SELECT
            items.id,
            items.run_id,
            items.item_type,
            items.source,
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
        WHERE {where_sql}
        ORDER BY runs.id DESC, items.item_type, items.ordinal
        """,
        params,
    ).fetchall()
    return [
        LinkCandidate(
            id=int(row["id"]),
            run_id=int(row["run_id"]),
            item_type=str(row["item_type"]),
            source=str(row["source"]),
            path=str(row["path"]),
            line=as_optional_int(row["line"]),
            title=str(row["title"]),
            body=str(row["body"]),
            fix=str(row["fix"]),
            verification=str(row["verification"]),
            fingerprint=str(row["fingerprint"]),
        )
        for row in rows
    ]


def candidate_review_text(candidate: LinkCandidate) -> str:
    return "\n".join(
        part
        for part in (
            candidate.title,
            candidate.body,
            candidate.fix,
            candidate.verification,
        )
        if part
    )


def candidate_link_match_fingerprints(candidate: LinkCandidate) -> set[str]:
    return link_match_fingerprints(
        candidate.path,
        candidate.line,
        [
            candidate_review_text(candidate),
            "\n".join(part for part in (candidate.title, candidate.body) if part),
            candidate.body,
            candidate.title,
        ],
    )


def line_match_score(left: int | None, right: int | None) -> float:
    if left is None or right is None:
        return 0.0
    distance = abs(left - right)
    if distance == 0:
        return 0.35
    if distance <= 2:
        return 0.25
    if distance <= 5:
        return 0.15
    return 0.0


def link_score(item: ExternalReviewItem, candidate: LinkCandidate) -> tuple[float, str]:
    if external_link_match_fingerprints(item) & candidate_link_match_fingerprints(candidate):
        return 1.0, "same_match_fingerprint"
    if item.path and candidate.path and item.path != candidate.path:
        return 0.0, "different_file"
    item_text = external_review_text(item)
    candidate_text = candidate_review_text(candidate)
    similarity = text_similarity(item_text, candidate_text)
    shared_tokens = review_tokens(item_text) & review_tokens(candidate_text)
    if similarity < 0.15 or (not shared_tokens and similarity < 0.35):
        return 0.0, "weak_match"
    score = 0.0
    same_file = bool(item.path and candidate.path and item.path == candidate.path)
    if same_file:
        score += 0.30
    line_score = line_match_score(item.line, candidate.line)
    score += line_score
    text_weight = 0.70 if not item.path and item.line is None else 0.45
    score += similarity * text_weight
    if same_file and item.line is not None and item.line == candidate.line:
        relation = "same_location"
    elif similarity >= 0.45:
        relation = "similar_text"
    elif line_score > 0.0:
        relation = "near_location"
    else:
        relation = "weak_match"
    return min(score, 0.99), relation


def build_link_matches(
    imported: list[tuple[int, ExternalReviewItem]],
    candidates: list[LinkCandidate],
    *,
    min_score: float,
) -> list[LinkMatch]:
    matches: list[LinkMatch] = []
    for external_id, item in imported:
        best_candidate: LinkCandidate | None = None
        best_score = 0.0
        best_relation = ""
        for candidate in candidates:
            score, relation = link_score(item, candidate)
            if score > best_score:
                best_candidate = candidate
                best_score = score
                best_relation = relation
        if best_candidate is None or best_score < min_score:
            continue
        note = (
            f"{IMPORT_LINK_NOTE_PREFIX} score={best_score:.2f} "
            f"run_id={best_candidate.run_id} path={item.path or '(none)'}"
        )
        matches.append(
            LinkMatch(
                review_item_id=best_candidate.id,
                external_item_id=external_id,
                relation=best_relation,
                score=best_score,
                note=note,
            )
        )
    return matches


def refresh_import_links(
    connection: sqlite3.Connection,
    imported_ids: list[int],
    matches: list[LinkMatch],
) -> None:
    if not imported_ids:
        return
    placeholders = ",".join("?" for _ in imported_ids)
    connection.execute(
        f"""
        DELETE FROM item_links
        WHERE external_item_id IN ({placeholders})
          AND note LIKE ?
        """,
        [*imported_ids, f"{IMPORT_LINK_NOTE_PREFIX}%"],
    )
    for match in matches:
        connection.execute(
            """
            INSERT INTO item_links (
                review_item_id,
                external_item_id,
                relation,
                note
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(review_item_id, external_item_id, relation) DO UPDATE SET
                note = excluded.note
            """,
            (
                match.review_item_id,
                match.external_item_id,
                match.relation,
                match.note,
            ),
        )


def latest_external_verdicts(
    connection: sqlite3.Connection,
    external_ids: list[int],
) -> dict[int, sqlite3.Row]:
    if not external_ids:
        return {}
    placeholders = ",".join("?" for _ in external_ids)
    rows = connection.execute(
        f"""
        SELECT verdicts.*
        FROM item_verdicts AS verdicts
        JOIN (
            SELECT target_id, MAX(id) AS id
            FROM item_verdicts
            WHERE target_kind = 'external_item'
              AND target_id IN ({placeholders})
            GROUP BY target_id
        ) AS latest
        ON latest.id = verdicts.id
        """,
        external_ids,
    ).fetchall()
    return {int(row["target_id"]): row for row in rows}


def write_external_verdicts(
    connection: sqlite3.Connection,
    imported_ids: list[int],
    matches: list[LinkMatch],
    *,
    candidates_exist: bool,
) -> int:
    if not imported_ids or not candidates_exist:
        return 0
    match_by_external = {match.external_item_id: match for match in matches}
    existing = latest_external_verdicts(connection, imported_ids)
    saved = 0
    for external_id in imported_ids:
        match = match_by_external.get(external_id)
        if match:
            verdict = "covered_by_local"
            reason = "linked_by_importer"
            note = f"{IMPORT_LINK_NOTE_PREFIX} {match.relation} score={match.score:.2f}"
        else:
            verdict = "missed_by_local"
            reason = "no_local_match"
            note = f"{IMPORT_LINK_NOTE_PREFIX} no link above threshold"
        current = existing.get(external_id)
        if (
            current
            and str(current["verdict"]) == verdict
            and str(current["reason"]) == reason
            and str(current["note"]) == note
        ):
            continue
        connection.execute(
            """
            INSERT INTO item_verdicts (
                target_kind,
                target_id,
                verdict,
                reason,
                note,
                scorer,
                scored_at
            ) VALUES ('external_item', ?, ?, ?, ?, 'github_importer', CURRENT_TIMESTAMP)
            """,
            (external_id, verdict, reason, note),
        )
        saved += 1
    return saved


def external_scope_counts(
    connection: sqlite3.Connection,
    *,
    repo: str,
    pr_number: int | None,
) -> tuple[int, int]:
    params: list[Any] = [repo]
    pr_filter = ""
    if pr_number is not None:
        pr_filter = " AND pr_number = ?"
        params.append(pr_number)
    total = int(
        connection.execute(
            f"SELECT COUNT(*) FROM external_items WHERE repo = ?{pr_filter}",
            params,
        ).fetchone()[0]
    )
    linked = int(
        connection.execute(
            f"""
            SELECT COUNT(DISTINCT external_items.id)
            FROM external_items
            JOIN item_links
            ON item_links.external_item_id = external_items.id
            WHERE external_items.repo = ?{pr_filter}
            """,
            params,
        ).fetchone()[0]
    )
    return total, linked


def external_scope_where_for_runs(rows: list[sqlite3.Row]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    seen: set[tuple[str, str, Any]] = set()
    for row in rows:
        repo = str(row["repo"] or "")
        if not repo:
            continue
        pr_number = as_optional_int(row["pr_number"])
        if pr_number is not None and pr_number > 0:
            key = ("pr", repo, pr_number)
            if key in seen:
                continue
            seen.add(key)
            clauses.append("(external_items.repo = ? AND external_items.pr_number = ?)")
            params.extend([repo, pr_number])
            continue
        head_sha = str(row["head_sha"] or "")
        if not head_sha:
            continue
        key = ("head", repo, head_sha)
        if key in seen:
            continue
        seen.add(key)
        clauses.append("(external_items.repo = ? AND external_items.head_sha = ?)")
        params.extend([repo, head_sha])
    if not clauses:
        return "0", []
    return "(" + " OR ".join(clauses) + ")", params


def external_report_counts(
    connection: sqlite3.Connection, rows: list[sqlite3.Row]
) -> tuple[int, int, list[sqlite3.Row]]:
    where_sql, params = external_scope_where_for_runs(rows)
    if not params:
        return 0, 0, []
    total = int(
        connection.execute(
            f"SELECT COUNT(*) FROM external_items WHERE {where_sql}",
            params,
        ).fetchone()[0]
    )
    linked = int(
        connection.execute(
            f"""
            SELECT COUNT(DISTINCT external_items.id)
            FROM external_items
            JOIN item_links
            ON item_links.external_item_id = external_items.id
            WHERE {where_sql}
            """,
            params,
        ).fetchone()[0]
    )
    verdict_rows = connection.execute(
        f"""
        SELECT verdicts.verdict, COUNT(*) AS count
        FROM item_verdicts AS verdicts
        JOIN external_items
        ON external_items.id = verdicts.target_id
        JOIN (
            SELECT target_kind, target_id, MAX(id) AS id
            FROM item_verdicts
            GROUP BY target_kind, target_id
        ) AS latest
        ON latest.id = verdicts.id
        WHERE verdicts.target_kind = 'external_item'
          AND {where_sql}
        GROUP BY verdicts.verdict
        ORDER BY verdicts.verdict
        """,
        params,
    ).fetchall()
    return total, linked, verdict_rows


def latest_item_verdicts(connection: sqlite3.Connection, target_ids: list[int]) -> dict[int, sqlite3.Row]:
    if not target_ids:
        return {}
    placeholders = ",".join("?" for _ in target_ids)
    rows = connection.execute(
        f"""
        SELECT verdicts.*
        FROM item_verdicts AS verdicts
        JOIN (
            SELECT target_id, MAX(id) AS id
            FROM item_verdicts
            WHERE target_kind = 'review_item'
              AND target_id IN ({placeholders})
            GROUP BY target_id
        ) AS latest
        ON latest.id = verdicts.id
        """,
        target_ids,
    ).fetchall()
    return {int(row["target_id"]): row for row in rows}


def score_review_items(connection: sqlite3.Connection, run_id: int) -> None:
    items = connection.execute(
        """
        SELECT *
        FROM review_items
        WHERE run_id = ? AND item_type = 'finding'
        ORDER BY ordinal
        """,
        (run_id,),
    ).fetchall()
    if not items:
        print("No finding items to score.")
        return
    existing = latest_item_verdicts(connection, [int(item["id"]) for item in items])
    print("")
    print("Item feedback")
    print("missed is reserved for external/human items; local findings use useful/fp/unclear/watch.")
    saved = 0
    for item in items:
        current = existing.get(int(item["id"]))
        current_verdict = str(current["verdict"]) if current else ""
        current_reason = str(current["reason"]) if current and "reason" in current.keys() else ""
        current_note = str(current["note"]) if current and "note" in current.keys() else ""
        location = item["path"]
        if item["line"] is not None:
            location = f"{location}:{item['line']}"
        print("")
        print(f"{item['ordinal']}. [{item['severity'] or 'watch'}] {item['title']}")
        print(f"   {location}")
        print(f"   why: {truncate_text(item['body'])}")
        if item["fix"]:
            print(f"   fix: {truncate_text(item['fix'])}")
        if current:
            print(f"   current: {current_verdict} reason={current_reason or '(none)'}")
            if current_note:
                print(f"   current note: {truncate_text(current_note)}")
        verdict = prompt_item_verdict(current_verdict)
        if verdict == "skip":
            continue
        reason = prompt_reason(verdict, current_reason)
        note_input = input("Item note [keep]: " if current else "Item note []: ").strip()
        note = current_note if current and note_input == "" else note_input
        if current and verdict == current_verdict and reason == current_reason and note == current_note:
            continue
        connection.execute(
            """
            INSERT INTO item_verdicts (
                target_kind,
                target_id,
                verdict,
                reason,
                note,
                scorer,
                scored_at
            ) VALUES ('review_item', ?, ?, ?, ?, 'manual', CURRENT_TIMESTAMP)
            """,
            (int(item["id"]), verdict, reason, note),
        )
        saved += 1
    if saved:
        print(f"OK: saved {saved} item verdicts")
    else:
        print("OK: no item verdict changes")


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
            args.remote_ready
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
        score_items = args.score_items
        if score_items is None:
            score_items = sys.stdin.isatty() and sys.stdout.isatty()
        if score_items:
            score_review_items(connection, int(row["id"]))
    print(f"OK: scored run_id={row['id']}")


def command_import_github_reviews(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser().resolve()
    ensure_db_schema(db_path)

    comments: list[Any] = []
    issue_comments: list[Any] = []
    head_ref = ""
    head_sha = str(args.head_sha or "")

    if args.comments_json:
        if not args.repo:
            raise SystemExit("--repo is required with --comments-json")
        if not args.pr:
            raise SystemExit("PR number is required with --comments-json")
        if "/" not in args.repo:
            raise SystemExit("--repo must be owner/name")
        owner, name = args.repo.split("/", 1)
        repo = GitHubRepo(owner, name)
        pr_number = int(args.pr)
        comments = load_json_list(Path(args.comments_json).expanduser().resolve())
    else:
        workspace = detect_workspace(Path(args.project_dir).expanduser().resolve(), args.repo)
        repo = workspace.repo
        pr_number = int(args.pr or (workspace.open_pr or {}).get("number") or 0)
        if pr_number <= 0:
            raise SystemExit("PR number is required when no open PR is detected")
        pr_payload, token_status = fetch_pr(repo, pr_number)
        if pr_payload is None:
            raise SystemExit(f"Could not fetch PR #{pr_number}: {token_status}")
        head_ref = str((pr_payload.get("head") or {}).get("ref") or "")
        head_sha = str((pr_payload.get("head") or {}).get("sha") or head_sha)
        token, token_source = github_token()
        if not token:
            raise SystemExit(f"GitHub auth unavailable: {token_source}")
        comments = github_paginated_request(
            f"/repos/{repo.full_name}/pulls/{pr_number}/comments",
            token,
        )
        if args.include_issue_comments:
            issue_comments = github_paginated_request(
                f"/repos/{repo.full_name}/issues/{pr_number}/comments",
                token,
            )

    imported_items = external_items_from_comments(
        repo=repo.full_name,
        pr_number=pr_number,
        default_head_sha=head_sha,
        comments=comments,
        comment_kind="review_comment",
    )
    if issue_comments:
        imported_items.extend(
            external_items_from_comments(
                repo=repo.full_name,
                pr_number=pr_number,
                default_head_sha=head_sha,
                comments=issue_comments,
                comment_kind="issue_comment",
            )
        )
    source_counts: dict[str, int] = {}
    for item in imported_items:
        source_counts[item.source] = source_counts.get(item.source, 0) + 1

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        head_shas = {head_sha} if head_sha else {item.head_sha for item in imported_items}
        candidates = load_link_candidates(
            connection,
            repo=repo.full_name,
            pr_number=pr_number,
            head_shas=head_shas,
            head_ref=head_ref,
            run_id=args.run,
        )
        dry_matches = build_link_matches(
            [(index + 1, item) for index, item in enumerate(imported_items)],
            candidates,
            min_score=args.min_link_score,
        )
        if args.dry_run:
            print(
                f"DRY RUN: would import {len(imported_items)} external review items "
                f"from {repo.full_name}#{pr_number}"
            )
            print(f"Link candidates: {len(candidates)}")
            print(f"Would create/update links: {len(dry_matches)}")
            if source_counts:
                print(
                    "Sources: "
                    + ", ".join(f"{source}={count}" for source, count in sorted(source_counts.items()))
                )
            return

        created = 0
        updated = 0
        imported: list[tuple[int, ExternalReviewItem]] = []
        for item in imported_items:
            item_id, was_created = upsert_external_item(connection, item)
            imported.append((item_id, item))
            if was_created:
                created += 1
            else:
                updated += 1
        imported_ids = [item_id for item_id, _ in imported]
        matches = build_link_matches(imported, candidates, min_score=args.min_link_score)
        refresh_import_links(connection, imported_ids, matches)
        verdicts = 0
        if not args.no_verdicts:
            verdicts = write_external_verdicts(
                connection,
                imported_ids,
                matches,
                candidates_exist=bool(candidates),
            )

    print(
        f"OK: imported {len(imported_items)} external review items "
        f"for {repo.full_name}#{pr_number} (created={created}, updated={updated})"
    )
    print(f"Links: {len(matches)} / candidates={len(candidates)}")
    if not args.no_verdicts:
        if candidates:
            print(f"External verdicts written: {verdicts}")
        else:
            print("External verdicts written: 0 (no matching local review run candidates)")
    if source_counts:
        print(
            "Sources: "
            + ", ".join(f"{source}={count}" for source, count in sorted(source_counts.items()))
        )


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
        run_ids = [int(row["id"]) for row in rows]
        total_findings = sum(int(row["findings_count"] or 0) for row in rows)
        total_watch = sum(int(row["watch_items_count"] or 0) for row in rows)
        scored_rows = [row for row in rows if row["useful_findings_fixed"] is not None]
        useful_total = sum(int(row["useful_findings_fixed"] or 0) for row in scored_rows)
        false_positive_total = sum(int(row["false_positives"] or 0) for row in scored_rows)
        unclear_total = sum(int(row["unclear_findings"] or 0) for row in scored_rows)
        remote_findings_total = sum(int(row["remote_findings_count"] or 0) for row in scored_rows)
        remote_ready_total = sum(
            1 for row in scored_rows if int(row["would_request_remote_review_now"] or 0)
        )
        avg_elapsed = (
            sum(float(row["elapsed_seconds"] or 0.0) for row in rows) / len(rows)
            if rows
            else 0.0
        )
        normalized_finding_items = 0
        verdict_rows: list[sqlite3.Row] = []
        reason_rows: list[sqlite3.Row] = []
        candidate_rows: list[sqlite3.Row] = []
        external_total, external_linked, external_verdict_rows = external_report_counts(
            connection, rows
        )
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            normalized_finding_items = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM review_items
                    WHERE run_id IN ({placeholders})
                      AND item_type = 'finding'
                    """,
                    run_ids,
                ).fetchone()[0]
            )
            verdict_rows = connection.execute(
                f"""
                SELECT verdicts.verdict, COUNT(*) AS count
                FROM item_verdicts AS verdicts
                JOIN review_items AS items
                ON items.id = verdicts.target_id
                JOIN (
                    SELECT target_kind, target_id, MAX(id) AS id
                    FROM item_verdicts
                    GROUP BY target_kind, target_id
                ) AS latest
                ON latest.id = verdicts.id
                WHERE verdicts.target_kind = 'review_item'
                  AND items.run_id IN ({placeholders})
                GROUP BY verdicts.verdict
                ORDER BY verdict
                """,
                run_ids,
            ).fetchall()
            reason_rows = connection.execute(
                f"""
                SELECT
                    verdicts.verdict,
                    COALESCE(NULLIF(verdicts.reason, ''), '(none)') AS reason,
                    COUNT(*) AS count
                FROM item_verdicts AS verdicts
                JOIN review_items AS items
                ON items.id = verdicts.target_id
                JOIN (
                    SELECT target_kind, target_id, MAX(id) AS id
                    FROM item_verdicts
                    GROUP BY target_kind, target_id
                ) AS latest
                ON latest.id = verdicts.id
                WHERE verdicts.target_kind = 'review_item'
                  AND items.run_id IN ({placeholders})
                  AND verdicts.verdict IN ('false_positive', 'watch_only', 'unclear')
                GROUP BY verdicts.verdict, reason
                ORDER BY count DESC, verdicts.verdict, reason
                """,
                run_ids,
            ).fetchall()
            candidate_rows = connection.execute(
                f"""
                SELECT
                    COALESCE(NULLIF(verdicts.reason, ''), '(none)') AS reason,
                    COUNT(*) AS count
                FROM item_verdicts AS verdicts
                JOIN review_items AS items
                ON items.id = verdicts.target_id
                JOIN (
                    SELECT target_kind, target_id, MAX(id) AS id
                    FROM item_verdicts
                    GROUP BY target_kind, target_id
                ) AS latest
                ON latest.id = verdicts.id
                WHERE verdicts.target_kind = 'review_item'
                  AND items.run_id IN ({placeholders})
                  AND verdicts.verdict IN ('false_positive', 'watch_only')
                GROUP BY reason
                HAVING count >= ?
                ORDER BY count DESC, reason
                """,
                [*run_ids, args.rule_threshold],
            ).fetchall()
    scored_item_total = sum(int(row["count"] or 0) for row in verdict_rows)
    run_feedback_total = useful_total + false_positive_total + unclear_total
    remote_ready_display = f"{remote_ready_total}/{len(scored_rows)}" if scored_rows else "n/a"
    lines = [
        "# Review Benchmark Report",
        "",
        f"- Runs: {len(rows)}",
        f"- Scored runs: {len(scored_rows)}",
        f"- DB: `{db_path}`",
        f"- Average runtime: `{avg_elapsed:.1f}s`",
        "",
        "## Summary",
        "",
        f"- Local findings: {total_findings}",
        f"- Watch items: {total_watch}",
        f"- Useful fixed: {useful_total} ({percent(useful_total, run_feedback_total)})",
        f"- False positives: {false_positive_total} ({percent(false_positive_total, run_feedback_total)})",
        f"- Unclear: {unclear_total} ({percent(unclear_total, run_feedback_total)})",
        f"- Remote review requested: {remote_ready_display}",
        f"- Remote findings: {remote_findings_total}",
        f"- Normalized item verdict coverage: {scored_item_total}/{normalized_finding_items}",
        f"- Imported external review items: {external_total}",
        f"- Linked external review items: {external_linked}/{external_total}",
        "",
        "## Runs",
        "",
        "| Run | Repo | PR | Findings | Watch | Useful | False positives | Unclear | Remote | Runtime | Note |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        lines.append(
            "| {id} | {repo} | {pr} | {findings} | {watch} | {useful} | {fp} | {unclear} | {remote} | {elapsed:.1f}s | {note} |".format(
                id=row["id"],
                repo=markdown_cell(row["repo"]),
                pr=row["pr_number"] or 0,
                findings=row["findings_count"],
                watch=row["watch_items_count"],
                useful=row["useful_findings_fixed"] if row["useful_findings_fixed"] is not None else "",
                fp=row["false_positives"] if row["false_positives"] is not None else "",
                unclear=row["unclear_findings"] if row["unclear_findings"] is not None else "",
                remote=row["remote_findings_count"] if row["remote_findings_count"] is not None else "",
                elapsed=row["elapsed_seconds"],
                note=markdown_cell(truncate_text(str(row["note"] or ""), 90)),
            )
        )
    lines.extend(["", "## Item Verdicts", ""])
    if verdict_rows:
        for row in verdict_rows:
            lines.append(f"- {row['verdict']}: {row['count']}")
    else:
        lines.append("- No item verdicts recorded yet.")
    lines.extend(["", "## False Positive / Watch Reasons", ""])
    if reason_rows:
        for row in reason_rows:
            lines.append(f"- {row['verdict']} / {row['reason']}: {row['count']}")
    else:
        lines.append("- No reason-coded item verdicts recorded yet.")
    lines.extend(["", "## External Review Items", ""])
    if external_total:
        lines.append(f"- Imported: {external_total}")
        lines.append(f"- Linked to local items: {external_linked}")
        lines.append(f"- Unlinked: {external_total - external_linked}")
        if external_verdict_rows:
            for row in external_verdict_rows:
                lines.append(f"- {row['verdict']}: {row['count']}")
    else:
        lines.append("- No external review items imported yet.")
    lines.extend(["", "## Prompt And Rule Candidates", ""])
    if candidate_rows:
        lines.append("Review these manually before turning them into prompt or local-rule changes.")
        lines.append("")
        for row in candidate_rows:
            lines.append(f"- {row['reason']}: {row['count']} matching verdicts")
    else:
        lines.append(
            f"- No false-positive/watch reason has reached the threshold ({args.rule_threshold}) yet."
        )
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
        context_digest_rows = connection.execute(
            """
            SELECT run_id, sha256
            FROM artifacts
            WHERE kind = 'context_digest'
            ORDER BY path
            """
        ).fetchall()
        context_digests: dict[int, list[str]] = {}
        for digest_row in context_digest_rows:
            context_digests.setdefault(int(digest_row["run_id"]), []).append(str(digest_row["sha256"]))
        link_rows = connection.execute(
            """
            SELECT review_item_id, external_item_id, relation
            FROM item_links
            ORDER BY review_item_id, external_item_id, relation
            """
        ).fetchall()
        links_by_review_item: dict[int, list[sqlite3.Row]] = {}
        links_by_external_item: dict[int, list[sqlite3.Row]] = {}
        for link_row in link_rows:
            links_by_review_item.setdefault(int(link_row["review_item_id"]), []).append(link_row)
            links_by_external_item.setdefault(int(link_row["external_item_id"]), []).append(link_row)
        rows = connection.execute(
            """
            SELECT
                runs.id AS run_id,
                runs.repo,
                runs.pr_number,
                runs.review_kind,
                runs.base_ref,
                runs.head_ref,
                runs.head_sha,
                runs.model,
                runs.prompt_family,
                runs.prompt_version,
                runs.prompt_hash,
                runs.model_options_hash,
                runs.diff_fingerprint,
                runs.diff_bytes,
                runs.elapsed_seconds,
                feedback.useful_findings_fixed,
                feedback.false_positives,
                feedback.unclear_findings,
                feedback.would_request_remote_review_now,
                feedback.remote_findings_count,
                feedback.note AS run_note,
                items.id AS item_id,
                items.item_type,
                items.ordinal,
                items.source,
                items.severity,
                items.confidence,
                items.path,
                items.line,
                items.title,
                items.body,
                items.fix,
                items.verification,
                items.fingerprint,
                verdicts.verdict,
                verdicts.reason AS verdict_reason,
                verdicts.note AS verdict_note,
                verdicts.scorer AS verdict_scorer,
                verdicts.scored_at AS verdict_scored_at
            FROM review_items AS items
            JOIN review_runs AS runs
            ON runs.id = items.run_id
            LEFT JOIN run_feedback AS feedback
            ON feedback.run_id = runs.id
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
            ON verdicts.target_kind = 'review_item'
            AND verdicts.target_id = items.id
            ORDER BY runs.id, items.item_type, items.ordinal
            """
        )
        review_count = 0
        for row in rows:
            record = dict(row)
            record["record_kind"] = "review_item"
            record["context_digests"] = context_digests.get(int(row["run_id"]), [])
            linked_rows = links_by_review_item.get(int(row["item_id"]), [])
            record["linked_external_item_ids"] = [
                int(link_row["external_item_id"]) for link_row in linked_rows
            ]
            record["link_relations"] = [str(link_row["relation"]) for link_row in linked_rows]
            file.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
            review_count += 1

        external_rows = connection.execute(
            """
            SELECT
                external_items.id AS external_item_id,
                external_items.repo,
                external_items.pr_number,
                external_items.head_sha,
                external_items.source,
                external_items.path,
                external_items.line,
                external_items.title,
                external_items.body,
                external_items.url,
                external_items.github_comment_id,
                external_items.github_thread_id,
                external_items.fingerprint,
                external_items.created_at,
                verdicts.verdict,
                verdicts.reason AS verdict_reason,
                verdicts.note AS verdict_note,
                verdicts.scorer AS verdict_scorer,
                verdicts.scored_at AS verdict_scored_at
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
            ORDER BY external_items.repo, external_items.pr_number, external_items.id
            """
        )
        external_count = 0
        for row in external_rows:
            record = dict(row)
            external_id = int(row["external_item_id"])
            linked_rows = links_by_external_item.get(external_id, [])
            record.update(
                {
                    "record_kind": "external_item",
                    "review_kind": "",
                    "base_ref": "",
                    "head_ref": "",
                    "model": "",
                    "prompt_family": "",
                    "prompt_version": "",
                    "prompt_hash": "",
                    "model_options_hash": "",
                    "diff_fingerprint": "",
                    "diff_bytes": None,
                    "elapsed_seconds": None,
                    "run_id": None,
                    "item_id": None,
                    "item_type": "external",
                    "ordinal": None,
                    "severity": "",
                    "confidence": "",
                    "fix": "",
                    "verification": "",
                    "context_digests": [],
                    "linked_review_item_ids": [
                        int(link_row["review_item_id"]) for link_row in linked_rows
                    ],
                    "link_relations": [
                        str(link_row["relation"]) for link_row in linked_rows
                    ],
                }
            )
            file.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
            external_count += 1
    print(
        f"OK: exported {review_count} review items and {external_count} external items to {output}"
    )


def install_paths(path_value: str) -> tuple[Path, Path]:
    source = TOOL_ROOT / "llreview"
    target = Path(os.path.abspath(os.path.expanduser(path_value)))
    return source, target


def invoked_install_path() -> str:
    source = (TOOL_ROOT / "llreview").resolve()
    invoked = os.environ.get("LLREVIEW_INVOKED_PATH", "").strip()
    if invoked:
        candidate = Path(os.path.abspath(os.path.expanduser(invoked)))
        if candidate.is_symlink() and candidate.resolve() == source:
            return str(candidate)
    return str(DEFAULT_INSTALL_PATH)


def validate_install_target(source: Path, target: Path, *, force: bool) -> None:
    if target.parent.exists() and not target.parent.is_dir():
        raise SystemExit(f"{target.parent} is not a directory; choose another install path")
    if not (target.exists() or target.is_symlink()):
        return
    current = target.resolve() if target.is_symlink() else target
    if current == source.resolve():
        return
    if target.is_dir() and not target.is_symlink():
        raise SystemExit(f"{target} is a directory; remove it before installing llreview")
    if not force:
        raise SystemExit(f"{target} already exists; pass --force to replace it")


def command_install(args: argparse.Namespace) -> None:
    source, target = install_paths(args.path)
    target.parent.mkdir(parents=True, exist_ok=True)
    validate_install_target(source, target, force=args.force)
    if target.exists() or target.is_symlink():
        current = target.resolve() if target.is_symlink() else target
        if current == source.resolve():
            print(f"OK: llreview is already installed at {target}")
            return
        target.unlink()
    target.symlink_to(source)
    print(f"OK: installed llreview at {target}")
    if str(target.parent) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"Note: add {target.parent} to PATH to run `llreview` without a path.")


def command_update(args: argparse.Namespace) -> None:
    branch = args.branch or "main"
    install_path = args.path or invoked_install_path()
    force_install = bool(getattr(args, "force", False))
    before = git(TOOL_ROOT, "rev-parse", "--short", "HEAD")
    current_branch = git(TOOL_ROOT, "branch", "--show-current", check=False) or "(detached)"
    remote_ref = f"origin/{branch}"
    dirty = git(TOOL_ROOT, "status", "--porcelain", check=False)
    if args.check:
        print(f"Tool root: {TOOL_ROOT}")
        print(f"Current branch: {current_branch}")
        print(f"Current commit: {before}")
        print(f"Update target: {remote_ref}")
        print(f"Working tree: {'dirty' if dirty else 'clean'}")
        print(f"Install path: {install_path}")
        print(f"Install force: {'yes' if force_install else 'no'}")
        return
    if dirty:
        raise SystemExit(
            f"llreview tool repository has uncommitted changes at {TOOL_ROOT}. "
            "Commit or stash them before running update."
        )
    if current_branch != branch:
        hint = f"check out {branch} before running update."
        if current_branch != "(detached)":
            hint = f"check out {branch} or pass --branch {current_branch} explicitly."
        raise SystemExit(f"Refusing to update {TOOL_ROOT} while on {current_branch}; {hint}")

    source, target = install_paths(install_path)
    validate_install_target(source, target, force=force_install)

    git(TOOL_ROOT, "fetch", "origin", branch)
    git(TOOL_ROOT, "merge", "--ff-only", remote_ref)
    after = git(TOOL_ROOT, "rev-parse", "--short", "HEAD")
    command_install(argparse.Namespace(path=install_path, force=force_install))
    if before == after:
        print(f"OK: llreview is already up to date at {after}")
    else:
        print(f"OK: updated llreview {before}..{after}")


def add_workspace_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-dir", default=os.getcwd(), help="Git workspace to inspect")
    parser.add_argument("--repo", help="Override GitHub repository as owner/name")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite review history DB")


def build_review_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Auto-detect and run local PR review",
        epilog=(
            "Subcommands: status, score, report, export-jsonl, "
            "import-github-reviews, install, update"
        ),
    )
    parser.set_defaults(func=command_review)
    parser.add_argument("pr", nargs="?", type=int, help="PR number. Omit to auto-detect.")
    add_workspace_options(parser)
    parser.add_argument("--update", action="store_true", help="Update the installed llreview command and exit")
    parser.add_argument("--update-check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--update-branch", help=argparse.SUPPRESS)
    parser.add_argument("--update-force", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--output", default=str(DEFAULT_REPORT), help="Markdown report output path")
    parser.add_argument("--post", action="store_true", help="Post or update the marker PR comment")
    parser.add_argument("--plain", action="store_true", help="Disable TTY progress animation")
    parser.add_argument("--static", action="store_true", help="Run static checks only")
    parser.add_argument("--max-model-files", type=int, help="Override model-reviewed file limit")
    parser.add_argument("--no-working-tree", action="store_true", help="Do not include dirty working tree in pre-PR mode")
    parser.add_argument(
        "--trusted-context-dir",
        action="append",
        default=[],
        help="Trusted markdown context directory to include in model review",
    )
    parser.add_argument(
        "--no-trusted-context",
        action="store_true",
        help="Do not auto-load .private_docs in pre-PR mode",
    )
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
    score.add_argument("--run", type=parse_non_negative)
    score.add_argument("--useful", type=parse_non_negative)
    score.add_argument("--false-positives", type=parse_non_negative)
    score.add_argument("--unclear", type=parse_non_negative)
    score.add_argument("--remote-ready", type=parse_bool_value)
    score.add_argument("--remote-findings", type=parse_non_negative)
    score.add_argument("--note")
    score.add_argument(
        "--items",
        dest="score_items",
        action="store_true",
        default=None,
        help="Prompt for per-finding verdicts after run-level scoring",
    )
    score.add_argument(
        "--no-items",
        dest="score_items",
        action="store_false",
        help="Only save run-level scoring",
    )
    return score


def build_report_parser() -> argparse.ArgumentParser:
    report = argparse.ArgumentParser(description="Generate a benchmark report")
    report.set_defaults(func=command_report)
    report.add_argument("--db", default=str(DEFAULT_DB))
    report.add_argument("--limit", type=int, default=10)
    report.add_argument("--rule-threshold", type=parse_non_negative, default=2)
    report.add_argument("--output", default=str(DEFAULT_BENCHMARK_REPORT))
    return report


def build_export_parser() -> argparse.ArgumentParser:
    export = argparse.ArgumentParser(description="Export review items as JSONL")
    export.set_defaults(func=command_export_jsonl)
    export.add_argument("--db", default=str(DEFAULT_DB))
    export.add_argument("--output", default=str(DEFAULT_JSONL))
    return export


def build_import_github_reviews_parser() -> argparse.ArgumentParser:
    importer = argparse.ArgumentParser(description="Import GitHub PR review comments into the review DB")
    importer.set_defaults(func=command_import_github_reviews)
    importer.add_argument("pr", nargs="?", type=parse_non_negative, help="PR number. Omit for current open PR.")
    add_workspace_options(importer)
    importer.add_argument("--run", type=parse_non_negative, help="Only link against one local review run")
    importer.add_argument(
        "--include-issue-comments",
        action="store_true",
        help="Also import top-level PR conversation comments after filtering command/no-op comments",
    )
    importer.add_argument(
        "--comments-json",
        help="Read a saved GitHub /pulls/comments JSON array instead of calling GitHub",
    )
    importer.add_argument(
        "--head-sha",
        help="Head SHA to store when using --comments-json or overriding GitHub metadata",
    )
    importer.add_argument(
        "--min-link-score",
        type=float,
        default=0.55,
        help="Minimum fuzzy score for item_links",
    )
    importer.add_argument("--dry-run", action="store_true", help="Fetch and match without writing to the DB")
    importer.add_argument(
        "--no-verdicts",
        action="store_true",
        help="Do not write covered_by_local/missed_by_local verdicts for imported external items",
    )
    return importer


def build_install_parser() -> argparse.ArgumentParser:
    install = argparse.ArgumentParser(description="Install llreview into a local PATH directory")
    install.set_defaults(func=command_install)
    install.add_argument("--path", default=str(DEFAULT_INSTALL_PATH), help="Command path to create")
    install.add_argument("--force", action="store_true", help="Replace an existing path")
    return install


def build_update_parser() -> argparse.ArgumentParser:
    update = argparse.ArgumentParser(description="Update the installed llreview command")
    update.set_defaults(func=command_update)
    update.add_argument("--path", help="Command path to verify")
    update.add_argument("--force", action="store_true", help="Replace an existing install path")
    update.add_argument("--branch", help="Tool repository branch to fast-forward from origin")
    update.add_argument("--check", action="store_true", help="Show update state without changing files")
    return update


COMMAND_PARSERS = {
    "status": build_status_parser,
    "score": build_score_parser,
    "report": build_report_parser,
    "export-jsonl": build_export_parser,
    "import-github-reviews": build_import_github_reviews_parser,
    "install": build_install_parser,
    "update": build_update_parser,
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
