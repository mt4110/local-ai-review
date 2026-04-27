# local-ai-review

GitHub Actions の self-hosted runner で動かす、diff-only のローカル PR レビュー基盤です。

このリポジトリには、ラベルで起動するローカル AI レビューの MVP workflow が入っています。workflow は `pull_request_target` で動作し、PR のコードは checkout せず、GitHub API から PR diff だけを取得します。その diff をローカルの Ollama に送り、PR コメントを 1 件だけ作成または更新します。

英語版: [README_EN.md](README_EN.md)

## セキュリティ契約

- `pull_request_target` でのみ実行する。
- PR に `local-ai-review` ラベルが付いている場合だけ実行する。
- `self-hosted`, `macOS`, `local-ai` ラベルを持つ self-hosted runner で実行する。
- `actions/checkout` を使わない。
- 外部 Action を使わない。
- PR 由来のコード、repository script、build、test、package install を実行しない。
- repository secrets を job に渡さない。
- Ollama に送るのは PR diff text のみとする。
- model prompt では diff を未信頼 text として扱う。
- 再実行のたびにコメントを増やさず、marker 付きコメントを更新する。
- watcher は MVP workflow と分離し、Discord からは `status` / `wake-if-down` だけを受け付ける。

## ファイル

- `.github/workflows/local-llm-review.yml`: MVP の GitHub Actions workflow。
- `scripts/verify-workflow-policy.py`: workflow の静的セキュリティチェック。
- `scripts/local-ai-review-watcher.py`: `status` / `wake-if-down` だけを実行する watcher。
- `config/local-ai-review-watcher.env.example`: watcher のローカル env file 例。
- `launchd/dev.local-ai-review.watcher.discord.plist.example`: Discord interactions endpoint 用の launchd 例。
- `docs/local-llm-shutdown-runbook.md`: ローカル LLM を止めるための詳細手順書。
- `docs/local-llm-watcher-design.md`: 常時監視 watcher / Discord 通知 / idle unload の設計。
- `docs/local-llm-watcher-runtime-ops.md`: env file、Discord App、live status、launchd の運用手順。
- `docs/local-ai-precision-review.md`: file-by-file の高精度 diff-only review 手順。
- `ai-review-infra-design/`: 実装仕様として使った v0.1 設計パック。

## Runner セットアップ

self-hosted runner の登録・起動は、以下の runner kit を使います。

- [mt4110/ci-self-runner](https://github.com/mt4110/ci-self-runner)

この repository では、runner が起動済みである前提で、Ollama と workflow 実行条件だけを設定します。

1. Mac の self-hosted runner に Ollama をインストールする。
2. デフォルトのレビューモデルを取得する。

   ```sh
   ollama pull qwen3-coder:30b-a3b-q4_K_M
   ```

3. Ollama が localhost からのみ到達できることを確認する。

   ```sh
   curl http://127.0.0.1:11434/api/tags
   ```

4. runner に以下のラベルが付いていることを確認する。

   ```text
   self-hosted
   macOS
   local-ai
   ```

5. runner 用ユーザーは non-admin にし、SSH key、cloud credentials、production credentials を置かない。

## Workflow 設定

初期値は安定性を優先して控えめにしています。

| Name | Default |
|---|---:|
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` |
| `OLLAMA_MODEL` | `qwen3-coder:30b-a3b-q4_K_M` |
| `OLLAMA_NUM_CTX` | `65536` |
| `OLLAMA_TEMPERATURE` | `0.1` |
| `OLLAMA_TIMEOUT_SECONDS` | `1800` |
| `MAX_DIFF_BYTES` | `350000` |
| `MAX_FINDINGS` | `8` |

最初の benchmark 後、必要に応じて `.github/workflows/local-llm-review.yml` 内で調整します。

## Watcher

watcher は workflow とは別プロセスです。PR checkout、PR code 実行、test 実行、GitHub label 変更、workflow file 変更は行いません。

ローカル確認:

```sh
python3 scripts/local-ai-review-watcher.py status
```

env file を使う場合:

```sh
python3 scripts/local-ai-review-watcher.py status \
  --env-file ~/.config/local-ai-review-watcher/env
```

repo 直下の `.env` はローカル手動確認用として使えます。この file は Git から除外します。launchd で常駐させる場合は、誤 commit を避けるため `~/.config/local-ai-review-watcher/env` を使う運用を推奨します。

Ollama が落ちている場合だけ起こす:

```sh
python3 scripts/local-ai-review-watcher.py wake-if-down
```

デフォルトの起動方式は macOS app の `open -a Ollama` です。Homebrew service を使う場合は次のようにします。

```sh
OLLAMA_WAKE_METHOD=brew-service python3 scripts/local-ai-review-watcher.py wake-if-down
```

Discord interactions endpoint を使う場合は、署名検証と allowlist が必須です。未設定なら起動しません。

```sh
export DISCORD_PUBLIC_KEY="..."
export DISCORD_ALLOWED_USER_IDS="123456789012345678"
export DISCORD_ALLOWED_CHANNEL_IDS="234567890123456789"
export DISCORD_ALLOWED_GUILD_IDS="345678901234567890"
export DISCORD_ALLOWED_COMMANDS="status,wake-if-down"

python3 scripts/local-ai-review-watcher.py serve-discord
```

`DISCORD_ALLOWED_COMMANDS` に `status` / `wake-if-down` 以外を入れると起動時に失敗します。GitHub workflow 状態も `status` に含めたい場合は、runner host 側で `WATCH_REPOS=owner/repo` と必要に応じて `GITHUB_TOKEN` を設定します。

実運用の env file、Discord App slash command、launchd、fail-closed 確認は [docs/local-llm-watcher-runtime-ops.md](docs/local-llm-watcher-runtime-ops.md) を参照してください。

## 使い方

対象 repository に trigger label を作成します。

```sh
gh label create local-ai-review \
  --color "0e8a16" \
  --description "Run local AI PR diff review"
```

draft ではない PR に `local-ai-review` ラベルを付けると workflow が実行されます。ラベルが付いたままなら、`synchronize`, `reopened`, `ready_for_review` でも再実行されます。

PR コメントは以下の marker で識別します。

```text
<!-- local-llm-review -->
```

`github-actions[bot]` が投稿した marker 付きコメントが既にある場合は、そのコメントを更新します。存在しない場合だけ新規コメントを作成します。

## テスト手順

runner を有効化する前に、静的セキュリティチェックを実行します。

```sh
python3 scripts/verify-workflow-policy.py
```

期待される出力:

```text
OK: local AI review workflow matches the v0.1 safety policy
```

次に live smoke test を 1 回実行します。

1. `ollama list` に `qwen3-coder:30b-a3b-q4_K_M` が含まれることを確認する。
2. 無害な text 変更だけを含む小さな PR を作る。
3. `local-ai-review` ラベルを付ける。
4. Actions job が `self-hosted`, `macOS`, `local-ai` runner で実行されることを確認する。
5. workflow log に checkout step がなく、PR diff が出力されていないことを確認する。
6. PR に `Local AI PR Review` コメントが 1 件投稿されることを確認する。
7. 同じ PR に小さな commit を追加で push する。
8. 2 件目の review comment が増えず、既存の marker comment が更新されることを確認する。

## Diff サイズ制限

diff が `MAX_DIFF_BYTES` を超えた場合、model review は skip され、PR 分割または deep review を促すコメントに更新されます。これにより local runner の応答性を保ち、context pressure による不安定化を避けます。
