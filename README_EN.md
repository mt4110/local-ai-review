# local-ai-review

Diff-only local PR review for GitHub Actions self-hosted runners.

This repository contains the MVP workflow for a label-triggered local AI review. The workflow uses `pull_request_target`, does not checkout PR code, fetches only the PR diff through the GitHub API, sends that diff to local Ollama, and creates or updates one PR comment.

Japanese version: [README.md](README.md)

## Security Contract

- Run only on `pull_request_target`.
- Require the `local-ai-review` PR label.
- Use a self-hosted runner labeled `self-hosted`, `macOS`, and `local-ai`.
- Do not use `actions/checkout`.
- Do not use external Actions.
- Do not execute PR code, repository scripts, build steps, tests, or package installs.
- Do not pass repository secrets to the job.
- Send only PR diff text to Ollama.
- Treat the diff as untrusted text in the model prompt.
- Update one marker comment instead of creating a new comment on every run.
- Keep the watcher separate from the MVP workflow and accept only `status` / `wake-if-down` from Discord.

## Files

- `.github/workflows/local-llm-review.yml`: MVP GitHub Actions workflow.
- `scripts/verify-workflow-policy.py`: static safety check for the workflow.
- `scripts/local-ai-review-watcher.py`: watcher limited to `status` / `wake-if-down`.
- `config/local-ai-review-watcher.env.example`: example local env file for the watcher.
- `launchd/dev.local-ai-review.watcher.discord.plist.example`: launchd example for the Discord interactions endpoint.
- `docs/local-llm-shutdown-runbook-en.md`: detailed runbook for stopping the local LLM.
- `docs/local-llm-watcher-design-en.md`: watcher, Discord notification, and idle unload design.
- `docs/local-llm-watcher-runtime-ops-en.md`: env file, Discord App, live status, and launchd operations.
- `ai-review-infra-design/`: v0.1 design pack used as the implementation spec.

## Runner Setup

Use this runner kit to register and run the self-hosted runner:

- [mt4110/ci-self-runner](https://github.com/mt4110/ci-self-runner)

This repository assumes the runner is already running and only configures Ollama plus the workflow execution requirements.

1. Install Ollama on the Mac self-hosted runner.
2. Pull the default review model:

   ```sh
   ollama pull qwen3-coder:30b-a3b-q4_K_M
   ```

3. Confirm Ollama is reachable only on localhost:

   ```sh
   curl http://127.0.0.1:11434/api/tags
   ```

4. Confirm the runner has these labels:

   ```text
   self-hosted
   macOS
   local-ai
   ```

5. Keep the runner user non-admin and avoid storing SSH keys, cloud credentials, or production credentials in that user account.

## Workflow Settings

The workflow defaults are intentionally conservative:

| Name | Default |
|---|---:|
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` |
| `OLLAMA_MODEL` | `qwen3-coder:30b-a3b-q4_K_M` |
| `OLLAMA_NUM_CTX` | `65536` |
| `OLLAMA_TEMPERATURE` | `0.1` |
| `OLLAMA_TIMEOUT_SECONDS` | `1800` |
| `MAX_DIFF_BYTES` | `350000` |
| `MAX_FINDINGS` | `8` |

Adjust these in `.github/workflows/local-llm-review.yml` after the first benchmark run.

## Watcher

The watcher is a separate process from the workflow. It does not checkout PRs, run PR code, run tests, edit GitHub labels, or edit workflow files.

Local status:

```sh
python3 scripts/local-ai-review-watcher.py status
```

With an env file:

```sh
python3 scripts/local-ai-review-watcher.py status \
  --env-file ~/.config/local-ai-review-watcher/env
```

The repo-local `.env` file can be used for manual local checks and is ignored by Git. For launchd operation, prefer `~/.config/local-ai-review-watcher/env` to reduce accidental commits.

Wake Ollama only when it is down:

```sh
python3 scripts/local-ai-review-watcher.py wake-if-down
```

The default wake method is the macOS app command `open -a Ollama`. Use the Homebrew service mode like this:

```sh
OLLAMA_WAKE_METHOD=brew-service python3 scripts/local-ai-review-watcher.py wake-if-down
```

For a Discord interactions endpoint, signature verification and allowlists are required. The server fails closed when they are missing.

```sh
export DISCORD_PUBLIC_KEY="..."
export DISCORD_ALLOWED_USER_IDS="123456789012345678"
export DISCORD_ALLOWED_CHANNEL_IDS="234567890123456789"
export DISCORD_ALLOWED_GUILD_IDS="345678901234567890"
export DISCORD_ALLOWED_COMMANDS="status,wake-if-down"

python3 scripts/local-ai-review-watcher.py serve-discord
```

`DISCORD_ALLOWED_COMMANDS` fails startup if it contains anything other than `status` / `wake-if-down`. To include GitHub workflow state in `status`, set `WATCH_REPOS=owner/repo` and, if needed, `GITHUB_TOKEN` on the runner host.

For production env file, Discord App slash command, launchd, and fail-closed checks, see [docs/local-llm-watcher-runtime-ops-en.md](docs/local-llm-watcher-runtime-ops-en.md).

## Usage

Create the trigger label in the target repository:

```sh
gh label create local-ai-review \
  --color "0e8a16" \
  --description "Run local AI PR diff review"
```

Add `local-ai-review` to a non-draft PR. The workflow also reruns on `synchronize`, `reopened`, and `ready_for_review` while the label remains present.

The PR comment is identified by this marker:

```text
<!-- local-llm-review -->
```

If the marker comment already exists from `github-actions[bot]`, the workflow updates it. Otherwise it creates one comment.

## Test Procedure

Run the static safety check before enabling the runner:

```sh
python3 scripts/verify-workflow-policy.py
```

Expected output:

```text
OK: local AI review workflow matches the v0.1 safety policy
```

Then run one live smoke test:

1. Confirm `ollama list` includes `qwen3-coder:30b-a3b-q4_K_M`.
2. Open a small PR with a harmless text change.
3. Add the `local-ai-review` label.
4. Confirm the Actions job runs on the `self-hosted`, `macOS`, `local-ai` runner.
5. Confirm the workflow log has no checkout step and does not print the PR diff.
6. Confirm the PR receives one `Local AI PR Review` comment.
7. Push another small commit to the same PR.
8. Confirm the existing marker comment is updated instead of creating a second review comment.

## Diff Size Gate

When the diff exceeds `MAX_DIFF_BYTES`, the workflow skips model review and updates the PR comment with a split/deep-review recommendation. This keeps the local runner responsive and avoids unstable context pressure.
