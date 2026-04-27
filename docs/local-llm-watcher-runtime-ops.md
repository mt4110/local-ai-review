# ローカル LLM Watcher Runtime Ops

この runbook は、`scripts/local-ai-review-watcher.py` を実機運用に乗せるための手順です。

watcher の操作面は意図的に小さく保ちます。

- Discord から受け付ける command は `status` と `wake-if-down` だけ。
- PR checkout、PR code 実行、test 実行、GitHub label 変更、workflow file 変更はしない。
- Ollama API は localhost のみ。
- Discord interactions は署名、timestamp、guild、channel、user、command allowlist で fail-closed にする。

## 1. env file

launchd の plist に secret を直接書かないため、env file を使います。

```sh
install -d -m 700 ~/.config/local-ai-review-watcher
cp config/local-ai-review-watcher.env.example ~/.config/local-ai-review-watcher/env
chmod 600 ~/.config/local-ai-review-watcher/env
```

`~/.config/local-ai-review-watcher/env` を編集します。`WATCH_REPOS` は comma-separated です。

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

`GITHUB_TOKEN` は private repository を見る場合だけ必要です。`status` は workflow runs と open PR / issue label を読むだけなので、対象 repository への read 権限に留めます。write 権限は watcher には不要です。

既存の process environment に同じ key がある場合は、process environment が env file より優先されます。

repo 直下の `.env` は手動確認には便利ですが、常駐運用では `~/.config/local-ai-review-watcher/env` を推奨します。`.env` は Git から除外し、作成した場合は `chmod 600 .env` にします。

## 2. local self-test

まず依存なしの self-test を通します。

```sh
python3 scripts/local-ai-review-watcher.py self-test
```

期待値:

```text
OK: local AI review watcher self-test passed
```

env file の読み込みも確認します。

```sh
python3 scripts/local-ai-review-watcher.py status \
  --env-file ~/.config/local-ai-review-watcher/env
```

JSON で見る場合:

```sh
python3 scripts/local-ai-review-watcher.py status \
  --env-file ~/.config/local-ai-review-watcher/env \
  --format json
```

## 3. WATCH_REPOS / GITHUB_TOKEN live check

一時的な live check は、token 文字列を shell history に残さない形で実行します。

```sh
gh auth status
GITHUB_TOKEN="$(gh auth token)" \
  WATCH_REPOS=mt4110/local-ai-review \
  python3 scripts/local-ai-review-watcher.py status
```

常駐運用では、read 権限に絞った token を `~/.config/local-ai-review-watcher/env` の `GITHUB_TOKEN=` に設定して、`chmod 600` を再確認します。`gh auth token` の token は権限が広いことがあるので、可能なら watcher 専用の fine-grained token を使います。

```sh
chmod 600 ~/.config/local-ai-review-watcher/env
python3 scripts/local-ai-review-watcher.py status \
  --env-file ~/.config/local-ai-review-watcher/env
```

期待する見え方:

```text
- mt4110/local-ai-review: labelled PRs=0, latest workflow=completed / success
```

GitHub API が失敗しても watcher は PR や label を変更しません。`status` の repository entry に error を入れて返し、次回実行で再確認します。

## 4. Ollama wake-if-down live check

Ollama が起動済みなら no-op になります。

```sh
python3 scripts/local-ai-review-watcher.py wake-if-down \
  --env-file ~/.config/local-ai-review-watcher/env
```

期待値:

```text
- result: reachable
- changed: no
```

実際の wake を確認する場合は、まず Ollama を手動で止めます。

macOS app:

```sh
osascript -e 'quit app "Ollama"'
```

Homebrew service:

```sh
brew services stop ollama
```

その後、起動方式に合わせて env file の `OLLAMA_WAKE_METHOD` を設定します。

| 起動方式 | 設定 |
|---|---|
| macOS app | `OLLAMA_WAKE_METHOD=open` |
| Homebrew service | `OLLAMA_WAKE_METHOD=brew-service` |
| wake 禁止 | `OLLAMA_WAKE_METHOD=none` |

再実行します。

```sh
python3 scripts/local-ai-review-watcher.py wake-if-down \
  --env-file ~/.config/local-ai-review-watcher/env
```

`wake-if-down` は Ollama server の起動確認だけを行います。PR review の起動、label 変更、workflow rerun、test 実行はしません。

## 5. Discord App slash command setup

Discord Developer Portal で Application を作ります。

1. `General Information` の Public Key を `DISCORD_PUBLIC_KEY` に設定する。
2. `Interactions Endpoint URL` に `https://YOUR_DOMAIN/discord/interactions` を設定する。
3. endpoint 保存時の PING が成功することを確認する。
4. Application を `applications.commands` scope で対象 guild に install する。

command は guild command として登録すると反映が速く、テストしやすいです。

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

Discord 側の command permission も絞れるなら絞ります。ただし、最終的な許可判断は watcher 側の guild / channel / user allowlist で行います。

疎通確認:

1. `serve-discord` を起動する。
2. allowed channel で `/local-ai status` を実行する。
3. ephemeral reply で status が返ることを確認する。
4. allowed channel で `/local-ai wake-if-down` を実行する。
5. disallowed user / channel では `Command rejected.` になることを確認する。

ローカル起動:

```sh
python3 scripts/local-ai-review-watcher.py serve-discord \
  --env-file ~/.config/local-ai-review-watcher/env
```

## 6. launchd

Discord interactions endpoint を常駐させる場合だけ launchd 化します。

```sh
install -d -m 700 ~/Library/Logs/local-ai-review-watcher
cp launchd/dev.local-ai-review.watcher.discord.plist.example \
  ~/Library/LaunchAgents/dev.local-ai-review.watcher.discord.plist
```

plist 内の `YOU` と repository path を実ユーザーに合わせて変更します。secret は plist に書かず、env file に置きます。

起動:

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.local-ai-review.watcher.discord.plist
launchctl kickstart -k gui/$(id -u)/dev.local-ai-review.watcher.discord
launchctl print gui/$(id -u)/dev.local-ai-review.watcher.discord
```

ログ:

```sh
tail -f ~/Library/Logs/local-ai-review-watcher/discord.err.log
tail -f ~/Library/Logs/local-ai-review-watcher/discord.out.log
```

停止:

```sh
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/dev.local-ai-review.watcher.discord.plist
```

## 7. fail-closed checklist

| 状況 | 期待挙動 |
|---|---|
| `DISCORD_PUBLIC_KEY` 未設定 | `serve-discord` が起動失敗 |
| user / channel / guild allowlist 未設定 | `serve-discord` が起動失敗 |
| Discord 署名不正 | HTTP 401 |
| timestamp が古い | HTTP 401 |
| allowlist 外 user / channel / guild | `Command rejected.` |
| `DISCORD_ALLOWED_COMMANDS=status,review` | 起動失敗 |
| remote `OLLAMA_BASE_URL` | 起動失敗 |
| GitHub API failure | status に error 表示、mutation なし |
| Ollama wake failure | 結果に error 表示、workflow rerun なし |

## 参照

- [Discord Application Commands](https://docs.discord.com/developers/interactions/application-commands)
- [Discord Receiving and Responding to Interactions](https://docs.discord.com/developers/interactions/receiving-and-responding)
- [Discord Interactions Overview](https://docs.discord.com/developers/interactions/overview)
