#!/usr/bin/env python3
"""Local AI review watcher with tightly-scoped Discord commands.

The Discord surface intentionally exposes only two actions:

- status
- wake-if-down

It does not checkout repositories, run PR code, mutate GitHub labels, edit
workflow files, run tests, or execute shell text received from Discord.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


HARD_ALLOWED_COMMANDS = frozenset({"status", "wake-if-down"})
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen3-coder:30b-a3b-q4_K_M"
DEFAULT_WORKFLOW_FILE = "local-llm-review.yml"
DEFAULT_WATCH_LABEL = "local-ai-review"
DISCORD_EPHEMERAL_FLAG = 1 << 6
DISCORD_API_BASE = "https://discord.com/api/v10"
MAX_DISCORD_CONTENT = 1900


class WatcherError(Exception):
    """Expected operational error."""


@dataclass(frozen=True)
class Config:
    ollama_base_url: str
    ollama_model: str
    ollama_wake_method: str
    ollama_app_name: str
    homebrew_bin: str
    wake_timeout_seconds: float
    http_timeout_seconds: float
    github_api_url: str
    github_token: str
    watch_repos: tuple[str, ...]
    watch_workflow_file: str
    watch_label: str
    discord_public_key: str
    discord_allowed_user_ids: frozenset[str]
    discord_allowed_channel_ids: frozenset[str]
    discord_allowed_guild_ids: frozenset[str]
    discord_allowed_commands: frozenset[str]
    discord_timestamp_skew_seconds: int
    discord_host: str
    discord_port: int
    discord_ephemeral: bool


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def parse_csv_set(value: str) -> frozenset[str]:
    return frozenset(parse_csv(value))


def parse_bool(value: str, *, default: bool) -> bool:
    if value == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise WatcherError(f"invalid boolean value: {value!r}")


def require_local_ollama_url(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    allowed_hosts = {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme not in {"http", "https"}:
        raise WatcherError("OLLAMA_BASE_URL must use http or https")
    if parsed.hostname not in allowed_hosts:
        raise WatcherError("OLLAMA_BASE_URL must point to localhost")
    if parsed.username or parsed.password:
        raise WatcherError("OLLAMA_BASE_URL must not contain credentials")
    return base_url.rstrip("/")


def load_config() -> Config:
    discord_allowed_commands = parse_csv_set(
        os.environ.get("DISCORD_ALLOWED_COMMANDS", "status,wake-if-down")
    )
    unknown_commands = discord_allowed_commands - HARD_ALLOWED_COMMANDS
    if unknown_commands:
        names = ", ".join(sorted(unknown_commands))
        raise WatcherError(f"DISCORD_ALLOWED_COMMANDS contains unsupported command(s): {names}")
    if not discord_allowed_commands:
        raise WatcherError("DISCORD_ALLOWED_COMMANDS must allow at least one command")

    wake_method = os.environ.get("OLLAMA_WAKE_METHOD", "open").strip()
    if wake_method not in {"open", "brew-service", "none"}:
        raise WatcherError("OLLAMA_WAKE_METHOD must be one of: open, brew-service, none")

    return Config(
        ollama_base_url=require_local_ollama_url(
            os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL)
        ),
        ollama_model=os.environ.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
        ollama_wake_method=wake_method,
        ollama_app_name=os.environ.get("OLLAMA_APP_NAME", "Ollama"),
        homebrew_bin=os.environ.get("HOMEBREW_BIN", "brew"),
        wake_timeout_seconds=float(os.environ.get("OLLAMA_WAKE_TIMEOUT_SECONDS", "30")),
        http_timeout_seconds=float(os.environ.get("WATCHER_HTTP_TIMEOUT_SECONDS", "5")),
        github_api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
        github_token=os.environ.get("GITHUB_TOKEN", ""),
        watch_repos=parse_csv(os.environ.get("WATCH_REPOS", "")),
        watch_workflow_file=os.environ.get("WATCH_WORKFLOW_FILE", DEFAULT_WORKFLOW_FILE),
        watch_label=os.environ.get("WATCH_LABEL", DEFAULT_WATCH_LABEL),
        discord_public_key=os.environ.get("DISCORD_PUBLIC_KEY", "").strip(),
        discord_allowed_user_ids=parse_csv_set(os.environ.get("DISCORD_ALLOWED_USER_IDS", "")),
        discord_allowed_channel_ids=parse_csv_set(os.environ.get("DISCORD_ALLOWED_CHANNEL_IDS", "")),
        discord_allowed_guild_ids=parse_csv_set(os.environ.get("DISCORD_ALLOWED_GUILD_IDS", "")),
        discord_allowed_commands=discord_allowed_commands,
        discord_timestamp_skew_seconds=int(
            os.environ.get("DISCORD_TIMESTAMP_SKEW_SECONDS", "300")
        ),
        discord_host=os.environ.get("DISCORD_INTERACTIONS_HOST", "127.0.0.1"),
        discord_port=int(os.environ.get("DISCORD_INTERACTIONS_PORT", "8089")),
        discord_ephemeral=parse_bool(
            os.environ.get("DISCORD_EPHEMERAL", "true"),
            default=True,
        ),
    )


def validate_discord_config(config: Config) -> None:
    if not config.discord_public_key:
        raise WatcherError("DISCORD_PUBLIC_KEY is required for serve-discord")
    if not config.discord_allowed_user_ids:
        raise WatcherError("DISCORD_ALLOWED_USER_IDS is required for serve-discord")
    if not config.discord_allowed_channel_ids:
        raise WatcherError("DISCORD_ALLOWED_CHANNEL_IDS is required for serve-discord")
    if not config.discord_allowed_guild_ids:
        raise WatcherError("DISCORD_ALLOWED_GUILD_IDS is required for serve-discord")


def http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: Any | None = None,
    timeout: float = 5,
) -> Any:
    payload = None
    request_headers = dict(headers or {})
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(
        url,
        data=payload,
        headers=request_headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def model_names(payload: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for item in payload.get("models") or []:
        if not isinstance(item, dict):
            continue
        for key in ("name", "model"):
            value = item.get(key)
            if isinstance(value, str) and value:
                names.add(value)
    return names


def fetch_ollama_json(config: Config, path: str) -> tuple[bool, Any]:
    try:
        payload = http_json(
            f"{config.ollama_base_url}{path}",
            timeout=config.http_timeout_seconds,
        )
        return True, payload
    except Exception as exc:  # noqa: BLE001 - surfaced as status JSON.
        return False, f"{exc.__class__.__name__}: {exc}"


def collect_ollama_status(config: Config) -> dict[str, Any]:
    tags_ok, tags_payload = fetch_ollama_json(config, "/api/tags")
    ps_ok, ps_payload = fetch_ollama_json(config, "/api/ps") if tags_ok else (False, "offline")

    installed_models = model_names(tags_payload) if tags_ok and isinstance(tags_payload, dict) else set()
    loaded_models = model_names(ps_payload) if ps_ok and isinstance(ps_payload, dict) else set()

    return {
        "base_url": config.ollama_base_url,
        "server_up": tags_ok,
        "model": config.ollama_model,
        "model_installed": config.ollama_model in installed_models,
        "model_loaded": config.ollama_model in loaded_models,
        "installed_models": sorted(installed_models),
        "loaded_models": sorted(loaded_models),
        "error": None if tags_ok else tags_payload,
    }


def github_headers(config: Config) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "local-ai-review-watcher",
    }
    if config.github_token:
        headers["Authorization"] = f"Bearer {config.github_token}"
    return headers


def github_get(config: Config, path: str) -> Any:
    return http_json(
        f"{config.github_api_url}{path}",
        headers=github_headers(config),
        timeout=config.http_timeout_seconds,
    )


def collect_repo_status(config: Config, repo: str) -> dict[str, Any]:
    quoted_repo = urllib.parse.quote(repo, safe="/")
    workflow_id = urllib.parse.quote(config.watch_workflow_file, safe="")
    label = urllib.parse.quote(config.watch_label)

    workflow_runs = github_get(
        config,
        f"/repos/{quoted_repo}/actions/workflows/{workflow_id}/runs?per_page=5",
    ).get("workflow_runs", [])
    labelled_issues = github_get(
        config,
        f"/repos/{quoted_repo}/issues?state=open&labels={label}&per_page=20",
    )
    labelled_prs = [
        {
            "number": item.get("number"),
            "title": item.get("title"),
            "url": item.get("html_url"),
        }
        for item in labelled_issues
        if isinstance(item, dict) and item.get("pull_request")
    ]

    return {
        "repo": repo,
        "workflow_file": config.watch_workflow_file,
        "label": config.watch_label,
        "open_labelled_prs": labelled_prs,
        "recent_workflow_runs": [
            {
                "id": run.get("id"),
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "event": run.get("event"),
                "html_url": run.get("html_url"),
                "created_at": run.get("created_at"),
            }
            for run in workflow_runs[:5]
            if isinstance(run, dict)
        ],
    }


def collect_github_status(config: Config) -> dict[str, Any]:
    if not config.watch_repos:
        return {"configured": False, "repos": []}

    repos: list[dict[str, Any]] = []
    for repo in config.watch_repos:
        try:
            repos.append(collect_repo_status(config, repo))
        except Exception as exc:  # noqa: BLE001 - status should degrade, not crash.
            repos.append(
                {
                    "repo": repo,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )
    return {"configured": True, "repos": repos}


def collect_status(config: Config) -> dict[str, Any]:
    return {
        "generated_at": utc_now(),
        "watcher": {
            "ok": True,
            "commands": sorted(HARD_ALLOWED_COMMANDS),
        },
        "ollama": collect_ollama_status(config),
        "github": collect_github_status(config),
    }


def run_wake_command(config: Config) -> dict[str, Any]:
    if config.ollama_wake_method == "none":
        return {
            "method": "none",
            "returncode": None,
            "stdout": "",
            "stderr": "wake disabled by OLLAMA_WAKE_METHOD=none",
        }

    if config.ollama_wake_method == "open":
        args = ["open", "-a", config.ollama_app_name]
    elif config.ollama_wake_method == "brew-service":
        args = [config.homebrew_bin, "services", "start", "ollama"]
    else:
        raise WatcherError(f"unsupported wake method: {config.ollama_wake_method}")

    completed = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
        shell=False,
    )
    return {
        "method": config.ollama_wake_method,
        "args": args,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def wait_for_ollama(config: Config) -> bool:
    deadline = time.monotonic() + config.wake_timeout_seconds
    while time.monotonic() <= deadline:
        status = collect_ollama_status(config)
        if status["server_up"]:
            return True
        time.sleep(1)
    return False


def wake_if_down(config: Config) -> dict[str, Any]:
    before = collect_ollama_status(config)
    if before["server_up"]:
        return {
            "generated_at": utc_now(),
            "action": "wake-if-down",
            "changed": False,
            "message": "Ollama is already reachable; no action taken.",
            "before": before,
            "after": before,
        }

    wake_result = run_wake_command(config)
    reachable = False if wake_result["method"] == "none" else wait_for_ollama(config)
    after = collect_ollama_status(config)

    return {
        "generated_at": utc_now(),
        "action": "wake-if-down",
        "changed": reachable,
        "message": "Ollama wake attempted.",
        "wake": wake_result,
        "before": before,
        "after": after,
    }


def render_status_text(status: dict[str, Any]) -> str:
    ollama = status["ollama"]
    lines = [
        "local-ai-review watcher status",
        f"- generated: {status['generated_at']}",
        f"- ollama: {'up' if ollama['server_up'] else 'down'}",
        f"- model: {ollama['model']}",
        f"- model installed: {'yes' if ollama['model_installed'] else 'no'}",
        f"- model loaded: {'yes' if ollama['model_loaded'] else 'no'}",
    ]

    github = status["github"]
    if not github["configured"]:
        lines.append("- github: not configured")
    else:
        for repo in github["repos"]:
            if repo.get("error"):
                lines.append(f"- {repo['repo']}: {repo['error']}")
                continue
            runs = repo.get("recent_workflow_runs") or []
            labelled = repo.get("open_labelled_prs") or []
            latest = runs[0] if runs else {}
            latest_text = "none"
            if latest:
                conclusion = latest.get("conclusion") or "-"
                latest_text = f"{latest.get('status')} / {conclusion}"
            lines.append(
                f"- {repo['repo']}: labelled PRs={len(labelled)}, latest workflow={latest_text}"
            )

    if ollama.get("error"):
        lines.append(f"- ollama error: {ollama['error']}")
    return "\n".join(lines)


def render_wake_text(result: dict[str, Any]) -> str:
    after = result["after"]
    lines = [
        "local-ai-review watcher wake-if-down",
        f"- generated: {result['generated_at']}",
        f"- result: {'reachable' if after['server_up'] else 'still down'}",
        f"- changed: {'yes' if result['changed'] else 'no'}",
        f"- message: {result['message']}",
    ]
    wake = result.get("wake")
    if wake:
        lines.append(f"- wake method: {wake['method']}")
        if wake.get("returncode") is not None:
            lines.append(f"- wake returncode: {wake['returncode']}")
        if wake.get("stderr"):
            lines.append(f"- wake stderr: {wake['stderr']}")
    if after.get("error"):
        lines.append(f"- ollama error: {after['error']}")
    return "\n".join(lines)


def render_command(command: str, result: dict[str, Any], *, output_format: str) -> str:
    if output_format == "json":
        return json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True)
    if command == "status":
        return render_status_text(result)
    if command == "wake-if-down":
        return render_wake_text(result)
    raise WatcherError(f"unsupported command: {command}")


def limit_discord_content(content: str) -> str:
    if len(content) <= MAX_DISCORD_CONTENT:
        return content
    suffix = "\n... truncated"
    return content[: MAX_DISCORD_CONTENT - len(suffix)] + suffix


# Minimal RFC 8032 Ed25519 verifier. Keeping this dependency-free makes the
# watcher usable on a plain macOS Python install while still failing closed.
P = 2**255 - 19
Q = 2**252 + 27742317777372353535851937790883648493
D = (-121665 * pow(121666, P - 2, P)) % P
I = pow(2, (P - 1) // 4, P)


def ed25519_xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(D * y * y + 1, P - 2, P)
    x = pow(xx, (P + 3) // 8, P)
    if (x * x - xx) % P != 0:
        x = (x * I) % P
    if x % 2 != 0:
        x = P - x
    return x


B = (ed25519_xrecover(4 * pow(5, P - 2, P) % P), 4 * pow(5, P - 2, P) % P)


def ed25519_is_on_curve(point: tuple[int, int]) -> bool:
    x, y = point
    return (-x * x + y * y - 1 - D * x * x * y * y) % P == 0


def ed25519_decode_point(data: bytes) -> tuple[int, int]:
    if len(data) != 32:
        raise ValueError("encoded point must be 32 bytes")
    y = int.from_bytes(data, "little") & ((1 << 255) - 1)
    sign = data[31] >> 7
    if y >= P:
        raise ValueError("non-canonical point")
    x = ed25519_xrecover(y)
    if x & 1 != sign:
        x = P - x
    point = (x, y)
    if not ed25519_is_on_curve(point):
        raise ValueError("point is not on curve")
    return point


def ed25519_add(point_a: tuple[int, int], point_b: tuple[int, int]) -> tuple[int, int]:
    x1, y1 = point_a
    x2, y2 = point_b
    dxxyy = D * x1 * x2 * y1 * y2
    x3 = ((x1 * y2 + x2 * y1) * pow(1 + dxxyy, P - 2, P)) % P
    y3 = ((y1 * y2 + x1 * x2) * pow(1 - dxxyy, P - 2, P)) % P
    return (x3, y3)


def ed25519_scalar_mult(point: tuple[int, int], scalar: int) -> tuple[int, int]:
    result = (0, 1)
    addend = point
    while scalar:
        if scalar & 1:
            result = ed25519_add(result, addend)
        addend = ed25519_add(addend, addend)
        scalar >>= 1
    return result


def verify_ed25519(public_key: bytes, message: bytes, signature: bytes) -> bool:
    if len(public_key) != 32 or len(signature) != 64:
        return False
    try:
        public_point = ed25519_decode_point(public_key)
        r_point = ed25519_decode_point(signature[:32])
    except ValueError:
        return False
    s_value = int.from_bytes(signature[32:], "little")
    if s_value >= Q:
        return False
    challenge = int.from_bytes(
        hashlib.sha512(signature[:32] + public_key + message).digest(),
        "little",
    ) % Q
    left = ed25519_scalar_mult(B, s_value)
    right = ed25519_add(r_point, ed25519_scalar_mult(public_point, challenge))
    return left == right


def verify_discord_signature(
    public_key_hex: str,
    signature_hex: str,
    timestamp: str,
    body: bytes,
) -> bool:
    try:
        public_key = bytes.fromhex(public_key_hex)
        signature = bytes.fromhex(signature_hex)
    except ValueError:
        return False
    return verify_ed25519(public_key, timestamp.encode("utf-8") + body, signature)


def timestamp_is_fresh(timestamp: str, max_skew_seconds: int) -> bool:
    try:
        request_time = int(timestamp)
    except ValueError:
        return False
    return abs(int(time.time()) - request_time) <= max_skew_seconds


def extract_discord_user_id(interaction: dict[str, Any]) -> str:
    member = interaction.get("member") if isinstance(interaction.get("member"), dict) else {}
    member_user = member.get("user") if isinstance(member.get("user"), dict) else {}
    top_user = interaction.get("user") if isinstance(interaction.get("user"), dict) else {}
    return str(member_user.get("id") or top_user.get("id") or "")


def extract_discord_command(interaction: dict[str, Any]) -> str | None:
    data = interaction.get("data") if isinstance(interaction.get("data"), dict) else {}
    name = data.get("name")
    options = data.get("options") if isinstance(data.get("options"), list) else []

    if name == "local-ai" and options:
        first = options[0]
        if isinstance(first, dict) and first.get("type") == 1:
            subcommand = first.get("name")
            return subcommand if isinstance(subcommand, str) else None

    if isinstance(name, str) and name in HARD_ALLOWED_COMMANDS:
        return name

    return None


def authorize_interaction(
    config: Config,
    interaction: dict[str, Any],
) -> tuple[bool, str, str | None]:
    command = extract_discord_command(interaction)
    if command not in config.discord_allowed_commands:
        return False, "command is not allowed", command

    channel_id = str(interaction.get("channel_id") or "")
    if channel_id not in config.discord_allowed_channel_ids:
        return False, "channel is not allowed", command

    user_id = extract_discord_user_id(interaction)
    if user_id not in config.discord_allowed_user_ids:
        return False, "user is not allowed", command

    guild_id = str(interaction.get("guild_id") or "")
    if guild_id not in config.discord_allowed_guild_ids:
        return False, "guild is not allowed", command

    return True, "allowed", command


def run_command(command: str, config: Config) -> dict[str, Any]:
    if command == "status":
        return collect_status(config)
    if command == "wake-if-down":
        return wake_if_down(config)
    raise WatcherError(f"unsupported command: {command}")


def post_discord_followup(
    application_id: str,
    token: str,
    content: str,
    *,
    ephemeral: bool,
) -> None:
    body: dict[str, Any] = {"content": limit_discord_content(content)}
    if ephemeral:
        body["flags"] = DISCORD_EPHEMERAL_FLAG
    http_json(
        f"{DISCORD_API_BASE}/webhooks/{application_id}/{token}",
        method="POST",
        headers={"User-Agent": "local-ai-review-watcher"},
        body=body,
        timeout=10,
    )


def execute_and_followup(config: Config, interaction: dict[str, Any], command: str) -> None:
    application_id = str(interaction.get("application_id") or "")
    token = str(interaction.get("token") or "")
    try:
        result = run_command(command, config)
        content = render_command(command, result, output_format="text")
    except Exception as exc:  # noqa: BLE001 - report failure back to Discord.
        content = f"local-ai-review watcher {command} failed\n- error: {exc.__class__.__name__}: {exc}"

    if not application_id or not token:
        print(content, file=sys.stderr)
        return

    try:
        post_discord_followup(
            application_id,
            token,
            content,
            ephemeral=config.discord_ephemeral,
        )
    except Exception as exc:  # noqa: BLE001 - endpoint should not crash.
        print(f"failed to post Discord follow-up: {exc}", file=sys.stderr)


def interaction_response(response_type: int, content: str | None = None, *, ephemeral: bool = True) -> bytes:
    payload: dict[str, Any] = {"type": response_type}
    if content is not None or (ephemeral and response_type in {4, 5}):
        data: dict[str, Any] = {}
        if content is not None:
            data["content"] = limit_discord_content(content)
        if ephemeral:
            data["flags"] = DISCORD_EPHEMERAL_FLAG
        payload["data"] = data
    return json.dumps(payload).encode("utf-8")


class DiscordInteractionHandler(BaseHTTPRequestHandler):
    server_version = "LocalAIReviewWatcher/1.0"

    def do_POST(self) -> None:  # noqa: N802 - stdlib hook name.
        config: Config = self.server.config  # type: ignore[attr-defined]
        if urllib.parse.urlparse(self.path).path != "/discord/interactions":
            self.send_error(404)
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if content_length <= 0 or content_length > 65536:
            self.send_error(400)
            return

        body = self.rfile.read(content_length)
        timestamp = self.headers.get("X-Signature-Timestamp", "")
        signature = self.headers.get("X-Signature-Ed25519", "")

        if not timestamp_is_fresh(timestamp, config.discord_timestamp_skew_seconds):
            self.send_error(401)
            return
        if not verify_discord_signature(config.discord_public_key, signature, timestamp, body):
            self.send_error(401)
            return

        try:
            interaction = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(400)
            return

        if interaction.get("type") == 1:
            self.write_json(200, interaction_response(1))
            return

        if interaction.get("type") != 2:
            self.write_json(
                200,
                interaction_response(
                    4,
                    "Command rejected.",
                    ephemeral=config.discord_ephemeral,
                ),
            )
            return

        allowed, reason, command = authorize_interaction(config, interaction)
        if not allowed or command is None:
            print(f"Discord command rejected: {reason}", file=sys.stderr)
            self.write_json(
                200,
                interaction_response(
                    4,
                    "Command rejected.",
                    ephemeral=config.discord_ephemeral,
                ),
            )
            return

        self.write_json(
            200,
            interaction_response(5, ephemeral=config.discord_ephemeral),
        )
        thread = threading.Thread(
            target=execute_and_followup,
            args=(config, interaction, command),
            daemon=True,
        )
        thread.start()

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    def write_json(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def serve_discord(config: Config) -> None:
    validate_discord_config(config)
    server = ThreadingHTTPServer((config.discord_host, config.discord_port), DiscordInteractionHandler)
    server.config = config  # type: ignore[attr-defined]
    print(
        f"Serving Discord interactions on http://{config.discord_host}:{config.discord_port}/discord/interactions",
        file=sys.stderr,
    )
    server.serve_forever()


def run_self_test() -> None:
    # RFC 8032 test vector 1.
    public_key = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    signature = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    )
    assert verify_ed25519(public_key, b"", signature)
    assert not verify_ed25519(public_key, b"x", signature)
    assert verify_discord_signature(public_key.hex(), signature.hex(), "", b"")
    assert not verify_discord_signature(public_key.hex(), signature.hex(), "1", b"")

    interaction = {
        "type": 2,
        "channel_id": "200",
        "guild_id": "300",
        "member": {"user": {"id": "100"}},
        "data": {"name": "local-ai", "options": [{"type": 1, "name": "status"}]},
    }
    env_backup = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update(
            {
                "DISCORD_ALLOWED_USER_IDS": "100",
                "DISCORD_ALLOWED_CHANNEL_IDS": "200",
                "DISCORD_ALLOWED_GUILD_IDS": "300",
                "DISCORD_ALLOWED_COMMANDS": "status,wake-if-down",
            }
        )
        config = load_config()
        allowed, _, command = authorize_interaction(config, interaction)
        assert allowed
        assert command == "status"

        denied = dict(interaction)
        denied["channel_id"] = "201"
        allowed, reason, _ = authorize_interaction(config, denied)
        assert not allowed
        assert reason == "channel is not allowed"

        denied = dict(interaction)
        denied["guild_id"] = "301"
        allowed, reason, _ = authorize_interaction(config, denied)
        assert not allowed
        assert reason == "guild is not allowed"

        config_without_guild = replace(
            config,
            discord_public_key="00" * 32,
            discord_allowed_guild_ids=frozenset(),
        )
        try:
            validate_discord_config(config_without_guild)
        except WatcherError:
            pass
        else:
            raise AssertionError("serve-discord accepted a missing guild allowlist")

        os.environ["DISCORD_ALLOWED_COMMANDS"] = "status,review"
        try:
            load_config()
        except WatcherError:
            pass
        else:
            raise AssertionError("unsupported Discord command was accepted")

        os.environ["DISCORD_ALLOWED_COMMANDS"] = "status"
        os.environ["OLLAMA_BASE_URL"] = "https://example.com"
        try:
            load_config()
        except WatcherError:
            pass
        else:
            raise AssertionError("remote Ollama URL was accepted")
    finally:
        os.environ.clear()
        os.environ.update(env_backup)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local AI review watcher")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Report watcher, Ollama, and workflow status")
    status.add_argument("--format", choices=("text", "json"), default="text")

    wake = subparsers.add_parser("wake-if-down", help="Start Ollama only when it is down")
    wake.add_argument("--format", choices=("text", "json"), default="text")

    subparsers.add_parser(
        "serve-discord",
        help="Serve a Discord interactions endpoint for status and wake-if-down only",
    )
    subparsers.add_parser("self-test", help="Run dependency-free watcher self tests")

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.command == "self-test":
        run_self_test()
        print("OK: local AI review watcher self-test passed")
        return 0

    try:
        config = load_config()
        if args.command == "status":
            result = collect_status(config)
            print(render_command("status", result, output_format=args.format))
            return 0
        if args.command == "wake-if-down":
            result = wake_if_down(config)
            print(render_command("wake-if-down", result, output_format=args.format))
            return 0
        if args.command == "serve-discord":
            serve_discord(config)
            return 0
    except WatcherError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    print(f"FAIL: unsupported command {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
