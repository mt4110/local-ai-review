#!/usr/bin/env python3
"""Drive the Codex app-server reviewer and write artifact-only output."""

from __future__ import annotations

import argparse
import json
import selectors
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def send(proc: subprocess.Popen[str], message: dict[str, Any]) -> None:
    if proc.stdin is None:
        raise RuntimeError("app-server stdin is closed")
    proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
    proc.stdin.flush()


def respond_decline(proc: subprocess.Popen[str], request_id: Any) -> None:
    send(proc, {"id": request_id, "result": {"decision": "decline"}})


def terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an app-server review harness")
    parser.add_argument("--codex-command", default="codex")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--events-jsonl", required=True)
    parser.add_argument("--app-server-stderr", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    prompt_path = Path(args.prompt)
    output_path = Path(args.output)
    events_path = Path(args.events_jsonl)
    stderr_path = Path(args.app_server_stderr)
    prompt = prompt_path.read_text(encoding="utf-8", errors="replace")

    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.parent.mkdir(parents=True, exist_ok=True)

    with stderr_path.open("w", encoding="utf-8") as app_stderr:
        proc = subprocess.Popen(
            [args.codex_command, "app-server"],
            cwd=args.workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=app_stderr,
            text=True,
            bufsize=1,
        )

        selector: selectors.DefaultSelector | None = None
        try:
            thread_id = ""
            turn_id = ""
            final_review = ""
            turn_status = ""
            started = time.time()
            selector = selectors.DefaultSelector()
            if proc.stdout is None:
                raise RuntimeError("app-server stdout is closed")
            selector.register(proc.stdout, selectors.EVENT_READ)

            client_info = {
                "name": "local_ai_review_app_developer_harness",
                "title": "Local AI Review App Developer Harness",
                "version": "0.1.0",
            }
            send(
                proc,
                {
                    "method": "initialize",
                    "id": 0,
                    "params": {
                        "clientInfo": client_info,
                        "capabilities": {"experimentalApi": True},
                    },
                },
            )
            send(proc, {"method": "initialized", "params": {}})

            developer_instructions = "\n".join(
                [
                    "You are running as an artifact-only teacher reviewer.",
                    "Do not invoke shell commands, file edits, repository scripts, tests, builds, package installs, network tools, or generated commands.",
                    "Use only the user-provided diff and instructions as evidence.",
                    "Do not post PR comments or mutate local or remote state.",
                ]
            )
            thread_params: dict[str, Any] = {
                "cwd": args.workspace,
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "developerInstructions": developer_instructions,
                "ephemeral": True,
            }
            if args.model.strip():
                thread_params["model"] = args.model.strip()
            send(proc, {"method": "thread/start", "id": 1, "params": thread_params})

            with events_path.open("w", encoding="utf-8") as events_file:
                while True:
                    if args.timeout_seconds > 0 and time.time() - started > args.timeout_seconds:
                        if thread_id and turn_id:
                            send(
                                proc,
                                {
                                    "method": "turn/interrupt",
                                    "id": 9001,
                                    "params": {"threadId": thread_id, "turnId": turn_id},
                                },
                            )
                        raise TimeoutError(f"app-server review exceeded {args.timeout_seconds}s")

                    if proc.poll() is not None and not selector.select(timeout=0):
                        break

                    ready = selector.select(timeout=1)
                    if not ready:
                        continue
                    line = proc.stdout.readline()
                    if not line:
                        continue
                    events_file.write(line)
                    events_file.flush()
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if "id" in message and "method" in message:
                        method = str(message.get("method") or "")
                        if method.endswith("/requestApproval") or method in {
                            "item/tool/requestUserInput",
                            "item/fileChange/requestApproval",
                            "item/commandExecution/requestApproval",
                        }:
                            respond_decline(proc, message["id"])
                        continue

                    if message.get("id") == 1:
                        if "error" in message:
                            raise RuntimeError(f"thread/start failed: {message['error']}")
                        thread = (message.get("result") or {}).get("thread") or {}
                        thread_id = str(thread.get("id") or "")
                        send(
                            proc,
                            {
                                "method": "turn/start",
                                "id": 2,
                                "params": {
                                    "threadId": thread_id,
                                    "cwd": args.workspace,
                                    "approvalPolicy": "never",
                                    "sandboxPolicy": {"type": "readOnly"},
                                    "model": args.model.strip() or None,
                                    "input": [{"type": "text", "text": prompt}],
                                },
                            },
                        )
                        continue

                    if message.get("id") == 2:
                        if "error" in message:
                            raise RuntimeError(f"turn/start failed: {message['error']}")
                        turn = (message.get("result") or {}).get("turn") or {}
                        turn_id = str(turn.get("id") or "")
                        continue

                    method = str(message.get("method") or "")
                    params = message.get("params") or {}
                    item = params.get("item") if isinstance(params, dict) else None
                    if method == "item/completed" and isinstance(item, dict):
                        if item.get("type") == "agentMessage":
                            final_review = str(item.get("text") or "")
                            output_path.write_text(final_review.rstrip() + "\n", encoding="utf-8")
                            if str(item.get("phase") or "") in {"final_answer", "final"}:
                                break
                    if method == "turn/completed" and isinstance(params, dict):
                        turn = params.get("turn") or {}
                        if isinstance(turn, dict):
                            turn_status = str(turn.get("status") or "")
                        if not final_review and isinstance(turn, dict):
                            for completed_item in turn.get("items") or []:
                                if isinstance(completed_item, dict) and completed_item.get("type") == "agentMessage":
                                    final_review = str(completed_item.get("text") or "")
                                    output_path.write_text(final_review.rstrip() + "\n", encoding="utf-8")
                                    break
                        if final_review or turn_status in {"completed", "failed", "interrupted"}:
                            break

            if not final_review:
                if turn_status:
                    output_path.write_text(f"Review did not produce output; turn status={turn_status}\n", encoding="utf-8")
                else:
                    output_path.write_text("Review did not produce output before app-server exited.\n", encoding="utf-8")
        finally:
            if selector is not None:
                selector.close()
            terminate_process(proc)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
