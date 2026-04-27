# Local LLM Watcher Runtime Ops

This runbook moves `scripts/local-ai-review-watcher.py` from local checks to real host operation.

The watcher keeps a deliberately small action surface:

- Discord accepts only `status` and `wake-if-down`.
- It does not checkout PRs, run PR code, run tests, edit GitHub labels, or edit workflow files.
- Ollama must stay on localhost.
- Discord interactions fail closed through signature, timestamp, guild, channel, user, and command allowlists.

## 1. Env File

Use an env file so secrets do not live in the launchd plist.

```sh
install -d -m 700 ~/.config/local-ai-review-watcher
cp config/local-ai-review-watcher.env.example ~/.config/local-ai-review-watcher/env
chmod 600 ~/.config/local-ai-review-watcher/env
```

Edit `~/.config/local-ai-review-watcher/env`. `WATCH_REPOS` is comma-separated.

```text
WATCH_REPOS=mt4110/local-ai-review
WATCH_WORKFLOW_FILE=local-llm-review.yml
WATCH_LABEL=local-ai-review
GITHUB_TOKEN=

OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3-coder:30b-a3b-q4_K_M
OLLAMA_WAKE_METHOD=open

DISCORD_PUBLIC_KEY=
DISCORD_ALLOWED_USER_IDS=
DISCORD_ALLOWED_CHANNEL_IDS=
DISCORD_ALLOWED_GUILD_IDS=
DISCORD_ALLOWED_COMMANDS=status,wake-if-down
DISCORD_INTERACTIONS_HOST=127.0.0.1
DISCORD_INTERACTIONS_PORT=8089
```

`GITHUB_TOKEN` is only needed for private repositories or higher API limits. `status` only reads workflow runs and open PR / issue labels, so keep the token read-only for the watched repositories. The watcher does not need write permission.

If the same key already exists in the process environment, the process environment wins over the env file.

The repo-local `.env` file is convenient for manual checks, but persistent operation should use `~/.config/local-ai-review-watcher/env`. `.env` is ignored by Git; if you create it, set `chmod 600 .env`.

## 2. Local Self-Test

Run the dependency-free self-test first.

```sh
python3 scripts/local-ai-review-watcher.py self-test
```

Expected:

```text
OK: local AI review watcher self-test passed
```

Confirm env file loading:

```sh
python3 scripts/local-ai-review-watcher.py status \
  --env-file ~/.config/local-ai-review-watcher/env
```

JSON output:

```sh
python3 scripts/local-ai-review-watcher.py status \
  --env-file ~/.config/local-ai-review-watcher/env \
  --format json
```

## 3. WATCH_REPOS / GITHUB_TOKEN Live Check

For a temporary live check, avoid putting the token string itself into shell history.

```sh
gh auth status
GITHUB_TOKEN="$(gh auth token)" \
  WATCH_REPOS=mt4110/local-ai-review \
  python3 scripts/local-ai-review-watcher.py status
```

For persistent operation, set a read-only token in `GITHUB_TOKEN=` inside `~/.config/local-ai-review-watcher/env`, then confirm permissions. The token returned by `gh auth token` can be broad, so prefer a dedicated fine-grained token for the watcher when possible.

```sh
chmod 600 ~/.config/local-ai-review-watcher/env
python3 scripts/local-ai-review-watcher.py status \
  --env-file ~/.config/local-ai-review-watcher/env
```

Expected shape:

```text
- mt4110/local-ai-review: labelled PRs=0, latest workflow=completed / success
```

If the GitHub API fails, the watcher does not mutate PRs or labels. It returns the error in the repository status entry and retries on the next run.

## 4. Ollama Wake-If-Down Live Check

If Ollama is already running, this is a no-op.

```sh
python3 scripts/local-ai-review-watcher.py wake-if-down \
  --env-file ~/.config/local-ai-review-watcher/env
```

Expected:

```text
- result: reachable
- changed: no
```

To test a real wake, stop Ollama manually first.

macOS app:

```sh
osascript -e 'quit app "Ollama"'
```

Homebrew service:

```sh
brew services stop ollama
```

Set `OLLAMA_WAKE_METHOD` for your install:

| Install style | Setting |
|---|---|
| macOS app | `OLLAMA_WAKE_METHOD=open` |
| Homebrew service | `OLLAMA_WAKE_METHOD=brew-service` |
| wake disabled | `OLLAMA_WAKE_METHOD=none` |

Run again:

```sh
python3 scripts/local-ai-review-watcher.py wake-if-down \
  --env-file ~/.config/local-ai-review-watcher/env
```

`wake-if-down` only checks and starts the Ollama server. It does not trigger reviews, edit labels, rerun workflows, or run tests.

## 5. Discord App Slash Command Setup

Create an Application in the Discord Developer Portal.

1. Set the `General Information` Public Key as `DISCORD_PUBLIC_KEY`.
2. Set `Interactions Endpoint URL` to `https://YOUR_DOMAIN/discord/interactions`.
3. Confirm the save-time PING succeeds.
4. Install the Application into the target guild with the `applications.commands` scope.

Registering a guild command is fastest for testing.

```sh
export DISCORD_APPLICATION_ID="..."
export DISCORD_GUILD_ID="..."
export DISCORD_BOT_TOKEN="..."

curl -fsS -X PUT \
  "https://discord.com/api/v10/applications/${DISCORD_APPLICATION_ID}/guilds/${DISCORD_GUILD_ID}/commands" \
  -H "Authorization: Bot ${DISCORD_BOT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d @- <<'JSON'
[
  {
    "name": "local-ai",
    "type": 1,
    "description": "Check local AI review runtime",
    "options": [
      {
        "type": 1,
        "name": "status",
        "description": "Show watcher, Ollama, and workflow status"
      },
      {
        "type": 1,
        "name": "wake-if-down",
        "description": "Start Ollama only if it is down"
      }
    ]
  }
]
JSON
```

Use Discord-side command permissions too when available, but keep watcher-side guild / channel / user allowlists as the final authorization layer.

Connectivity check:

1. Start `serve-discord`.
2. Run `/local-ai status` in the allowed channel.
3. Confirm the ephemeral status reply.
4. Run `/local-ai wake-if-down` in the allowed channel.
5. Confirm a disallowed user or channel receives `Command rejected.`.

Local server:

```sh
python3 scripts/local-ai-review-watcher.py serve-discord \
  --env-file ~/.config/local-ai-review-watcher/env
```

## 6. launchd

Use launchd only when running the Discord interactions endpoint persistently.

```sh
install -d -m 700 ~/Library/Logs/local-ai-review-watcher
cp launchd/dev.local-ai-review.watcher.discord.plist.example \
  ~/Library/LaunchAgents/dev.local-ai-review.watcher.discord.plist
```

Edit `YOU` and the repository path in the plist. Keep secrets in the env file, not the plist.

Start:

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.local-ai-review.watcher.discord.plist
launchctl kickstart -k gui/$(id -u)/dev.local-ai-review.watcher.discord
launchctl print gui/$(id -u)/dev.local-ai-review.watcher.discord
```

Logs:

```sh
tail -f ~/Library/Logs/local-ai-review-watcher/discord.err.log
tail -f ~/Library/Logs/local-ai-review-watcher/discord.out.log
```

Stop:

```sh
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/dev.local-ai-review.watcher.discord.plist
```

## 7. Fail-Closed Checklist

| Situation | Expected behavior |
|---|---|
| Missing `DISCORD_PUBLIC_KEY` | `serve-discord` fails startup |
| Missing user / channel / guild allowlist | `serve-discord` fails startup |
| Bad Discord signature | HTTP 401 |
| Stale timestamp | HTTP 401 |
| Disallowed user / channel / guild | `Command rejected.` |
| `DISCORD_ALLOWED_COMMANDS=status,review` | startup failure |
| Remote `OLLAMA_BASE_URL` | startup failure |
| GitHub API failure | status shows error, no mutation |
| Ollama wake failure | result shows error, no workflow rerun |

## References

- [Discord Application Commands](https://docs.discord.com/developers/interactions/application-commands)
- [Discord Receiving and Responding to Interactions](https://docs.discord.com/developers/interactions/receiving-and-responding)
- [Discord Interactions Overview](https://docs.discord.com/developers/interactions/overview)
