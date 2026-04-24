# Local LLM Watcher Design

This document proposes an optional watcher for local AI review operations.

It must stay separate from the MVP review workflow. The workflow remains `pull_request_target`, diff-only, no checkout, and no PR code execution.

## Summary

Run a small macOS `launchd` watcher that periodically checks:

- Ollama server health.
- Whether the review model is loaded.
- GitHub PRs with the `local-ai-review` label.
- Local review workflow status.
- Idle time since the last review.
- Optional Discord commands through polling or an IP-restricted interactions endpoint.

Discord webhooks are notification-only. To accept commands from Discord, use an opt-in polling bot or an IP-restricted interactions endpoint. IP allowlisting is only a defense layer; Discord request signature verification is still required.

## Recommended Architecture

```text
Mac mini
  |
  | launchd
  v
local-ai-review-watcher
  |
  | GitHub API
  | Ollama API on localhost
  | optional Discord command intake
  v
Discord webhook notification
```

## Safe Defaults

| Name | Default |
|---|---:|
| `WATCH_INTERVAL_SECONDS` | `60` |
| `IDLE_UNLOAD_MINUTES` | `20` |
| `IDLE_SERVER_STOP_MINUTES` | `0` |
| `WATCH_LABEL` | `local-ai-review` |
| `OLLAMA_MODEL` | `qwen3-coder:30b-a3b-q4_K_M` |

The safest default is to unload only the model after idle time. Keep the Ollama server running unless there is a maintenance reason to stop it.

## Security Rules

- Do not open inbound ports on the Mac.
- Do not expose Ollama outside localhost.
- Do not execute shell commands from Discord text.
- Do not rely on source IP allowlisting alone.
- Verify Discord request signatures for HTTP interactions.
- Do not checkout PR code.
- Do not run PR scripts, builds, tests, or package installs.
- Store Discord and GitHub tokens only on the runner host.
- Allowlist repository names, Discord channel IDs, and Discord user IDs.
- Record the last processed Discord message ID to avoid duplicate command execution.

## Discord Commands

The initial command set is intentionally limited to two commands:

```text
!local-ai status
!local-ai wake-if-down
```

`status` reports watcher / Ollama / model / workflow state.

`wake-if-down` checks Ollama health and tries to start the Ollama server only if it is down. If Ollama is already running, it is a no-op.

Do not include `sleep`, `unload`, or label mutation in the first implementation. Those can be added later as explicit opt-ins.

## IP-Restricted Discord Interactions

If you want slash commands or buttons, use this shape:

```text
Discord
  |
  | HTTPS POST
  | source IP allowlist
  | Discord signature verification
  v
command intake endpoint
  |
  | append allowed command only
  v
command queue
  |
  | local watcher reads queue
  v
Ollama status / wake-if-down notification
```

Required guards:

- HTTPS only.
- Source IP allowlist.
- Discord signature verification.
- Fixed allowlists for guild ID, channel ID, and user ID.
- Fixed command allowlist.
- No shell execution from Discord input.
- The endpoint should enqueue commands only.
- The watcher performs only `status` or `wake-if-down` in the first implementation.

`wake-if-down` must not mutate GitHub labels, checkout PR branches, run tests, edit workflow files, delete models, or execute arbitrary shell commands. In the initial version, humans trigger reviews from GitHub by adding the `local-ai-review` label.

The safer variant is to host the public endpoint outside the Mac and let the Mac watcher poll a queue. Directly exposing the Mac is possible, but it should be treated as a higher-risk deployment.

## Stable Home IP Usage

If the home public IP is mostly stable, it is useful as an additional restriction, but it is not Discord authentication.

Good uses:

- Allow a public command queue or small endpoint to accept watcher polling / ack traffic only from the home IP.
- Restrict an admin dashboard to the home IP.
- Restrict SSH, metrics, or health endpoints to the home IP.

Do not use it to assume Discord interactions come from the home IP. Discord interaction requests come from Discord infrastructure. The home IP should be used to restrict your own management traffic, while Discord commands still require request signature verification plus guild, channel, user, and command allowlists.

## Implementation Order

1. Discord webhook notifications.
2. GitHub PR label and workflow run monitoring.
3. Idle `ollama stop` model unload.
4. Ollama server wake.
5. Optional Discord bot polling or IP-restricted Discord interactions.
