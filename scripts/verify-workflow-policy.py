#!/usr/bin/env python3
"""Static safety checks for the local AI review workflow."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "local-llm-review.yml"


def fail(message: str) -> None:
    print(f"FAIL: {message}")
    sys.exit(1)


def require(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def main() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    lower = text.lower()

    require("pull_request_target:" in text, "workflow must use pull_request_target")
    require("pull_request:" not in text, "workflow must not use pull_request")
    require("actions/checkout" not in lower, "workflow must not use actions/checkout")
    require(not re.search(r"(?m)^\s*uses\s*:", text), "workflow must not use external actions")
    require("runs-on: [self-hosted, macOS, local-ai]" in text, "runner labels must be self-hosted, macOS, local-ai")
    require("contents: read" in text, "contents permission must be read-only")
    require("pull-requests: write" in text, "pull-requests write permission is required")
    require("issues: write" in text, "issues write permission is required for PR comments")
    require("local-ai-review" in text, "workflow must require the local-ai-review label")
    require("application/vnd.github.v3.diff" in text, "workflow must request PR diff only")
    require("OLLAMA_MODEL: qwen3-coder:30b-a3b-q4_K_M" in text, "default model must be Qwen3-Coder Q4")
    require("OPENAI_API_KEY" not in text, "workflow must not reference OpenAI API secrets")
    require("ANTHROPIC_API_KEY" not in text, "workflow must not reference Anthropic API secrets")
    require("secrets." not in text, "workflow must not read repository secrets")
    require("github.token" in text, "workflow should use the built-in GitHub token only")

    print("OK: local AI review workflow matches the v0.1 safety policy")


if __name__ == "__main__":
    main()
