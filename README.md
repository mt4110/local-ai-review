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
- `docs/local-ai-precision-review-en.md`: English version of the precision review runbook.
- `sql/review-history-example-queries.sql`: SQLite に保存した review 結果を評価するための SQL 例。
- `docker-compose.review-db.yml`: review history をブラウザで見る Datasette 用 compose。
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

## Precision review history (SQLite)

この章は label-triggered workflow ではなく、手元で実行する precision review の履歴保存について説明します。precision reviewer の PR comment marker は以下です。

```text
<!-- local-ai-precision-review -->
```

日常利用では、まず薄い CLI の `llreview` を使います。現在の Git workspace から repository、branch、open PR を自動検知し、PR が見つからない場合は `BASE...HEAD` と working tree を pre-PR review として扱います。

```sh
./llreview install
llreview status
llreview target set --project-dir /absolute/path/to/repo --repo owner/name
llreview daily
llreview backup
llreview
llreview second-opinion
llreview async-status
llreview calibration
llreview 42
llreview --post
llreview update
llreview score
llreview import-github-reviews 42
llreview import-github-history --dry-run
llreview backfill-pump
llreview matcher-explain
llreview training-export-splitter
llreview rule-candidate-extractor
llreview learning-scoreboard
llreview learn-preview
llreview learn-candidates
llreview learn-pump
llreview learn-review
llreview learn-propose --candidate <candidate-id>
llreview learn-next
llreview learn-apply --proposal <proposal-id> --dry-run
llreview learn-audit
llreview calibration-risk-gate
llreview prompt-regression-audit
llreview notify-test
llreview report
llreview export-jsonl
```

`./llreview install` は `~/.local/bin/llreview` に symlink を作ります。`~/.local/bin` が PATH に入っていれば、その後は repository root 以外からも `llreview` として実行できます。
`llreview update` はこの repository に `origin/main` を fast-forward で取り込み、install 済み command の symlink を確認します。既存の install path を置き換える場合は `llreview update --force` を使います。`llreview --update` は通常更新用のショートカットです。作業中の変更がある場合や、更新対象 branch にいない場合は止まります。

`llreview target set --project-dir /path/to/repo --repo owner/name` は、tool repository から短い command で同じ対象を扱うための local target を `out/review-history/llreview-target.json` に保存します。保存後は `llreview status`、`llreview`、`llreview learn-preview`、`llreview learn-candidates` がその workspace/repo を自動で使います。明示的な `--project-dir` / `--repo` は常に保存 target より優先されます。解除する場合は `llreview target clear` を使います。

`llreview daily` は日常用のまとめ入口です。`status` を表示し、前回runが無い、HEAD が変わった、または working tree が dirty の場合だけ通常 review を実行し、その後に軽量な artifact-only calibration、`learn-preview`、`learn-candidates` を表示します。review を必ず回す場合は `--force-review`、学習側だけ見る場合は `--no-review`、calibration artifact を止める場合は `--no-calibration` を使います。日次出力のあとに人間のハンコだけ押す場合は `llreview learn-review` を使います。学習候補を毎回手で有効化したくない場合は、明示 opt-in の `llreview daily --auto-activate-learning` または `LLREVIEW_DAILY_AUTO_ACTIVATE_LEARNING=1` で、最高順位の proposed prompt/rule candidate を 1 件だけ active DB calibration にできます。完了済み teacher artifact の import、未採点 run、外部 item のハンコ待ち、link 診断を 1 枚にまとめたい場合は `llreview learn-pump`、daily に組み込む場合は `llreview daily --learning-pump` または `LLREVIEW_DAILY_LEARNING_PUMP=1` を使います。未採点 run の回収 inbox も daily に出す場合は `llreview daily --scoring-pump` または `LLREVIEW_DAILY_SCORING_PUMP=1` を使います。human-gate 待ち review gap のハンコ inbox は `llreview daily --review-gap-stamp-pump` または `LLREVIEW_DAILY_REVIEW_GAP_STAMP_PUMP=1` で daily に含められます。missed_by_local の弱点クラスタは `llreview daily --recall-pattern-miner` または `LLREVIEW_DAILY_RECALL_PATTERN_MINER=1` で daily に含められます。watch があるのに finding に届かなかった境界は `llreview daily --watch-sharpener` または `LLREVIEW_DAILY_WATCH_SHARPENER=1` で daily に含められます。active 化前の learning candidate risk は `llreview daily --calibration-risk-gate` または `LLREVIEW_DAILY_CALIBRATION_RISK_GATE=1` で daily に含められます。active calibration 後の regression 監査は `llreview daily --prompt-regression-audit` または `LLREVIEW_DAILY_PROMPT_REGRESSION_AUDIT=1` で daily に含められます。教材不足を少しずつ埋める backfill queue report は `llreview daily --backfill-pump` または `LLREVIEW_DAILY_BACKFILL_PUMP=1` で daily に含められ、rate limit 付きで一件だけ進める場合は `llreview daily --backfill-pump-import-one` または `LLREVIEW_DAILY_BACKFILL_PUMP_IMPORT_ONE=1` を使います。link 失敗理由の分解は `llreview daily --matcher-explain` または `LLREVIEW_DAILY_MATCHER_EXPLAIN=1` で daily に含められます。training-ready だけの train/val/test export は `llreview daily --training-export-splitter` または `LLREVIEW_DAILY_TRAINING_EXPORT_SPLITTER=1` で daily に含められます。deterministic rule にできそうな missed pattern の抽出は `llreview daily --rule-candidate-extractor` または `LLREVIEW_DAILY_RULE_CANDIDATE_EXTRACTOR=1` で daily に含められます。これらの状態を 1 画面で見る read-only scoreboard は `llreview daily --learning-scoreboard` または `LLREVIEW_DAILY_LEARNING_SCOREBOARD=1` で daily に含められます。重い second-opinion は既定では含めず、直列で待つ場合は `llreview daily --second-opinion`、background job として逃がす場合は `llreview daily --async-second-opinion` を使います。Warp などで長い出力を追いにくい場合は、`llreview daily --notify` で完了・失敗・中断時に macOS のローカル通知を出せます。毎回使う場合は `LLREVIEW_DAILY_NOTIFY=1`、音も出す場合は `--notify-sound Glass` または `LLREVIEW_NOTIFY_SOUND=Glass` を指定します。通知経路だけ確認する場合は `llreview notify-test` を使います。

常用運用では、shell rc に daily 設定を入れます。実証実験として app-developer teacher review harness を毎回 background 起動したい場合は `LLREVIEW_DAILY_APP_DEVELOPER_REVIEW=1` を入れます。重い local second-opinion はマシンが空いている時だけ `llreview daily --async-second-opinion` で個別に起動します。macOS の通常 shell は zsh なので、まずは `~/.zshrc` に入れます。bash を使う場合は同じ block を `~/.bashrc` に入れます。

```sh
export LLREVIEW_DAILY_AUTO_ACTIVATE_LEARNING=1
export LLREVIEW_DAILY_NOTIFY=1
export LLREVIEW_NOTIFY_SOUND=Glass
export LLREVIEW_DAILY_APP_DEVELOPER_REVIEW=1
export LLREVIEW_DAILY_LEARNING_PUMP=1
export LLREVIEW_DAILY_SCORING_PUMP=1
export LLREVIEW_DAILY_REVIEW_GAP_STAMP_PUMP=1
export LLREVIEW_DAILY_RECALL_PATTERN_MINER=1
export LLREVIEW_DAILY_WATCH_SHARPENER=1
export LLREVIEW_DAILY_CALIBRATION_RISK_GATE=1
export LLREVIEW_DAILY_PROMPT_REGRESSION_AUDIT=1
export LLREVIEW_DAILY_BACKFILL_PUMP=1
# export LLREVIEW_DAILY_BACKFILL_PUMP_IMPORT_ONE=1
export LLREVIEW_DAILY_MATCHER_EXPLAIN=1
export LLREVIEW_DAILY_TRAINING_EXPORT_SPLITTER=1
export LLREVIEW_DAILY_RULE_CANDIDATE_EXTRACTOR=1
export LLREVIEW_DAILY_LEARNING_SCOREBOARD=1
export LLREVIEW_APP_DEVELOPER_REVIEW_MODEL=gpt-5.4
```

second-opinion の memory gate は `daily` では既定で通常 reviewer model (`LLREVIEW_PRIMARY_REVIEW_MODEL`、未指定なら `OLLAMA_MODEL`) を `ollama stop` してから判定します。止めたくない場合は `llreview daily --no-stop-primary-before-second-opinion` を使います。毎回の調整は `LLREVIEW_SECOND_OPINION_MODEL`、`LLREVIEW_SECOND_OPINION_NUM_CTX`、`LLREVIEW_SECOND_OPINION_MAX_MODEL_FILES`、`LLREVIEW_SECOND_OPINION_MODEL_MEMORY_GB`、`LLREVIEW_SECOND_OPINION_MAX_MEMORY_PERCENT` で shell rc に保存できます。64GB 級のマシンでは既定の `qwen3-coder-next:q4_K_M` が memory gate を通らないことがあります。その場合は daily 常用から外し、通したい時だけ軽い model へ差し替えるか、意図的な空き時間に限って `--force-second-opinion` を使います。

その後は次の短い command で daily review、artifact-only calibration、learning activation/preview/candidates、app-developer teacher artifact、通知まで流せます。

```sh
llreview daily
llreview async-status
```

さらに打鍵を短くするなら alias/function も使えます。

```sh
alias llrd='llreview daily'
alias llra='llreview async-status'

llr-demote() {
  if [ -z "${1:-}" ]; then
    echo "usage: llr-demote <run-id> [extra llreview score args...]" >&2
    return 2
  fi
  local run_id="$1"
  shift
  llreview score --run "$run_id" --demote-findings "$@"
}
```

一時的に background second-opinion だけ止める場合は `llreview daily --no-async-second-opinion`、学習自動有効化だけ止める場合は `llreview daily --no-auto-activate-learning`、通知だけ止める場合は `llreview daily --no-notify` を使います。

一時的に app-developer teacher review harness だけ止める場合は `llreview daily --no-app-developer-review` を使います。この harness は `llreview` が捕捉した pre-PR diff を prompt artifact に埋め込み、background client が `codex app-server` を起動して `initialize`、`initialized`、`thread/start`、`turn/start` を JSONL で送ります。既定 model は現在の app-server 互換性を優先して `gpt-5.4` です。`out/app-developer-review/runs/<job-id>/` に diff、prompt、review output、events JSONL、stdout/stderr、manifest を保存します。PR comment 投稿、PR code checkout/execution、remote review import はしません。

`llreview daily` は次回以降の実行で完了済み app-developer job を拾い、`review.md` を structured item に正規化して high-confidence finding だけを `external_items(source='teacher_model')` に import します。そのうえで local primary の `review_items` と deterministic matcher で `item_links` を張り、`comparison-report.md` と calibration report を再生成します。状態確認と手動 import は `llreview app-developer-review-status --import-completed` で行えます。teacher output は正解ではなく、local primary review と比較する calibration evidence として扱います。

`llreview learn-pump` は、完了済み app-developer job の import、軽量 calibration refresh、`learn-candidates`、未採点 run、外部 item のハンコ待ち、link health / diagnostics、backfill queue focus をまとめた operator inbox を `out/review-history/learning-pump/` に書きます。あわせて、teacher/external item と local finding/watch 候補の距離、operator verdict、label quality、次の learning target を `latest-review-gap-examples.jsonl` に保存します。threshold 未満の単発 teacher gap も `Review Gap Stamp Inbox` に表示し、`external-verdict` のハンコ command を出します。これは将来の review 専用 ML に渡すための feature dataset であり、human gate なしに training-ready とは扱いません。非同期 teacher の終了を少し待つ場合は `llreview learn-pump --wait-app-developer-review 600` を使います。この command は追加 model 呼び出し、PR comment 投稿、PR code checkout/execution、remote review 取得をしません。

`llreview scoring-pump` は、未採点 run を `quick_drain_zero_findings` と `manual_finding_score` に分け、すぐ流せる `llreview score` command と手採点すべき run を `out/review-history/scoring-pump/` にまとめます。既定では read-only です。finding 0 の run だけを run-level feedback として明示的に排水する場合は `llreview scoring-pump --apply-zero-findings` を使います。finding がある run は item verdict を確認してから `llreview score --run <id> --items` または `--demote-findings` を使います。

`llreview review-gap-stamp-pump` は、`review-gap-examples.jsonl` のうち human-gate 待ちの teacher/external gap を、短い根拠、local finding/watch との距離、`external-verdict` command 付きで `out/review-history/review-gap-stamp-pump/` にまとめます。既定では read-only です。連続でハンコを押す場合は TTY で `llreview review-gap-stamp-pump --stamp` を使い、`y` valid、`f` not actionable、`c` covered、`n` unsure、`s` skip、`q` quit を入力します。`y` は diff-local かつ actionable と判断できる時だけ使います。

`llreview recall-pattern-miner` は、`missed_by_local` の review gap を path class、learning target、path bucket、title token の近さでクラスタ化し、`out/review-history/recall-pattern-miner/` に弱点パターン report を書きます。これは優先順位付けの evidence であり、そのまま prompt/rule update にはしません。training-ready と human-gate の内訳を見て、次に stamp するべき塊や rule 化できそうな塊を選ぶための command です。

`llreview watch-sharpener` は、local watch item が存在したのに teacher/external finding へ届かなかった review gap を `out/review-history/watch-sharpener/` にまとめます。近い watch がある場合は watch から finding へ上げる条件を示し、近い watch が無い場合は「watch が多いだけで具体欠陥を見ていない recall gap」として扱います。これは自動昇格ではなく、prompt calibration や review 方針のための境界 evidence です。

`llreview matcher-explain` は、unlinked external item について deterministic matcher がなぜ link しなかったかを `out/review-history/matcher-explain/` に分解します。比較対象 run の有無、finding/watch 候補数、path 一致、line 距離、title/body 類似度、token overlap、threshold margin を出し、`no_comparable_local_run`、`watch_only_no_finding`、`path_mismatch`、`near_below_threshold` などに分類します。既定は read-only で、raw body は出さず digest と短い title excerpt だけを使います。local-only で本文 excerpt を見たい時だけ `--show-text` を使います。

`llreview training-export-splitter` は、`review_gap_example` のうち `training_ready=true` かつ human-gate 不要の record だけを `out/review-history/training-export/` 配下へ train/val/test JSONL として書き出します。既定では raw diff/code/body を含めず、raw path も `path_class` と `path_digest` に落とします。`.private_docs` と generated/snapshot path は除外し、必要な場合だけ `--include-paths` や `--include-title-excerpts` を明示します。これは training-ready dataset の配管であり、human-gate 待ちを学習に混ぜないための splitter です。

`llreview rule-candidate-extractor` は、training-ready の `missed_by_local` を path class、title token、known mechanical family で束ね、deterministic rule にできそうな候補を `out/review-history/rule-candidate-extractor/` に書き出します。既定では human-gate 待ちを除外し、raw body/diff は出しません。`path_containment`、`shell_quoting`、`state_normalization`、`reserved_config` などの機械的 trigger へ落とせる時だけ `proposed_rule_candidate` とし、それ以外は prompt/watch evidence として残します。rule code や prompt source は変更しません。

`llreview learning-scoreboard` は、learning pump、scoring pump、review-gap stamp pump、recall miner、watch sharpener、risk gate、regression audit、backfill pump、matcher explain、training export、rule extractor の latest artifact と DB 集計を 1 画面にまとめます。read-only で、review 実行、teacher import、calibration activation、raw private text export は行いません。

`llreview calibration-risk-gate` は、proposed prompt/rule candidate を active DB calibration にする前に、training-ready support、human-gate 残、false-positive counter-evidence、missed counter-evidence を `out/review-history/calibration-risk-gate/` にまとめます。`learn-next --activate`、`learn-apply --activate`、`learn-review` の activation 直前にも同じ gate を表示し、block 判定は `--force-risk` がない限り active 化しません。daily auto activation は block された候補を skip します。

`llreview prompt-regression-audit` は、active calibration 後の同一 scope/path class で missed external item や local false positive が減ったかを `out/review-history/prompt-regression-audit/` にまとめます。効いていない可能性が高い校正は `stale_candidate` として出しますが、自動で pause / retire はしません。これは active calibration を育て続けるための事後監査です。

import 後の teacher item は `llreview external-verdict <external_item_id> --verdict teacher_false_positive` や `--verdict missed_by_local` で operator 判断を残せます。candidate inspection の sample 番号から採点する場合は `llreview external-verdict --candidate <candidate-id> --sample 1 --verdict missed_by_local ...` のように書けます。

`llreview calibration` は、既存の SQLite history だけを読み、直近または `--run <id>` の review run から `out/calibration/runs/<calibration-run-id>/` に manifest、normalized item JSONL、alignment JSONL、verdict candidate JSONL、Markdown/JSON report を書き出します。この step は追加の model 呼び出し、PR comment 投稿、PR code checkout/execution、remote review 取得をしません。daily では既定で実行され、生成 artifact の digest だけを `artifacts(kind='calibration_*')` に保存します。

`llreview backup` は、local SSD 上の review DB を正として、iCloud などの backup folder に timestamped snapshot を保存します。SQLite は `.backup` 相当の API でコピーし、`export-jsonl` を更新して `review-items.<timestamp>.jsonl` も保存します。`benchmark-report.md` がある場合は一緒に保存します。DB を iCloud 上で常時運用するのではなく、snapshot だけを保存するための command です。最新ファイルも更新したい場合は `llreview backup --latest`、保存先確認だけなら `llreview backup --dry-run` を使います。`llreview daily --offer-backup` は、その daily run で学習 row が増えた場合だけ interactive shell で backup を提案します。

`llreview second-opinion` は、重い second-opinion model (`qwen3-coder-next:q4_K_M`) を既定で2ファイルだけ実行します。macOS の物理メモリを見積もり、model load 後の使用量が `--max-memory-percent` (既定90%) を超えそうな場合は skip します。意図的に空き時間で回す場合だけ `--force` を付けます。実行後は既定で `ollama stop` して model を unload します。`daily` 経由では、second-opinion gate 前に通常 reviewer model を止めて、primary model が残っているだけで skip される状況を減らします。

`llreview daily --async-second-opinion` は、同じ memory gate を通したうえで `second-opinion` を background process として起動し、daily 自体は job id、PID、manifest、log、output path を返して終了します。job manifest は `out/async-review/runs/<job-id>/manifest.json`、結果は既定で同じ directory の `second-opinion.md` に保存されます。状態確認は `llreview async-status` を使います。この async launcher は shell を使わず、PR comment 投稿、PR code checkout/execution、remote review 取得をしません。

TTY では進行中の phase、elapsed、model-reviewed file count、finding/watch count を spinner 付きの 1 行で更新します。CI や log 保存では `--plain` を使うと通常の行ログになります。spinner が使えない環境では、既定で10秒ごとに still-running heartbeat を出します。止めたい場合は `--progress-heartbeat-seconds 0` を使います。

`llreview score --run <id> --items` は findings を1件ずつ採点します。全部の finding を「blocker ではなく watch/calibration evidence」として一括で落とす場合は、長い対話の代わりに `llreview score --run <id> --demote-findings` を使えます。既定では run-level を `useful=0 / false_positives=<findings_count> / unclear=0` にし、item-level は `watch_only / diagnostic_watch` として保存します。item verdict を false positive に寄せる場合は `--demote-verdict false_positive --demote-reason insufficient_context` のように明示します。

precision review の評価データを SQLite に溜める場合は、以下を使います。

```sh
make pre-pr-review \
  REPO=mt4110/geo-line-ranker \
  PROJECT_DIR=/absolute/path/to/geo-line-ranker \
  BASE=main
make review-db-init
make review-db-stats
make review-db-web
make review-db-score RUN=6 USEFUL=0 FALSE_POSITIVES=0 UNCLEAR=1 REMOTE_READY=yes NOTE='Static-only looked clean enough for PR.'
make review-db-down
```

`pre-pr-review` は PR 作成前の branch 差分も `review_kind=pre_pr` として保存します。DB には `BASE...HEAD` の base/head、head SHA、未commit差分を含めたかどうかも残るため、PR 作成後の remote review 結果と同じ run を照合しやすくなります。

`llreview` の pre-PR mode では、対象 workspace 直下に `.private_docs/` がある場合、その Markdown を compact な trusted design context として model prompt に追加します。context は finding の根拠そのものにはせず、diff に見えている evidence を解釈するためだけに使います。各 context document の path と sha256 は DB の `artifacts(kind='context_digest')` に保存されます。無効化する場合は `llreview --no-trusted-context`、明示的に別 directory を渡す場合は `llreview --trusted-context-dir /path/to/.private_docs` を使います。

`llreview import-github-reviews 42` は GitHub の inline PR review comments を読み込み、Copilot / automated / human の指摘を `external_items` に保存します。既存の local `review_items` とは fingerprint、file、line、normalized text のゆるい一致で `item_links` に対応付けます。同じ comment id は update されるため、同じ PR を再 import しても外部項目は増殖しません。再 import では、今回の GitHub comment snapshot に含まれない古い GitHub 由来の external item も片付けます。top-level PR conversation comments も教材にしたい場合だけ `--include-issue-comments` を付けます。保存済み JSON で再現 import する場合、issue comments は `--issue-comments-json` で別ファイルを渡し、`--head-sha` は特定の local run SHA に固定したい場合だけ使います。

`llreview import-github-history --dry-run` は、過去の merged PR と local git 履歴を教材候補として preview します。remote GitHub PR は `--remote-repo-limit` / `--remote-pr-limit` / `--remote-per-repo-pr-limit` で API 量を制限し、local git は `--local-repo-limit` / `--local-pr-limit` / `--local-per-repo-pr-limit` で CPU/output 量だけを制限します。local scan は GitHub API token を要求せず、preferred GitHub remote の owner が `mt4110` ではない repository は `skipped_owner_not_mt4110` で block します。skip reason を DB の `github_backfill_queue` に残したい場合は `--refresh-queue` を明示します。queue から remote candidate を1件だけ実 import する場合は `llreview import-github-history --one` を使います。`--one --dry-run` は選択されるPRと import/link 件数だけを確認し、実 import は20分に1件の limiter を既定で守ります。`llreview backfill-pump` はこの queue を日常運用向けにまとめ、before/after、rate gate、次候補、external item delta を `out/review-history/backfill-pump/` に保存します。通常は report-only、`--import-one` で一件だけ進め、`--import-one --dry-run` は external item / link / verdict / queue state を書きません。`llreview report` と `llreview export-jsonl` は queue state / skip reason も出力します。

`llreview learn-preview` は DB 内の review items、external items、backfill queue を集計し、次回 review に使われる aggregate calibration を preview します。通常の `llreview` は、raw comment や raw diff ではなく verdict reason / external verdict / path class / queue state の集計だけを一時ファイル経由で reviewer prompt に追加します。無効化する場合は `llreview --no-history-calibration` を使います。この段階では prompt や rule を自動で書き換えず、単発の逸話ではなく繰り返し出た evidence だけを次回レビューの優先度調整に使います。

`llreview learn-candidates` は、その aggregate evidence から `prompt_candidate` / `rule_candidate` / `needs_data` を導出します。candidate には evidence count、path class、reason/source、confidence、status、recommended action が付きます。`proposed` は提案だけで、`active` は既に operator-approved DB calibration として次回 prompt に入る状態です。prompt や rule の source file は変更しません。一覧には短い ID と行番号が出ます。`llreview learn-candidates --inspect` は先頭候補、`--inspect 2` は2行目、`--inspect <candidate-id>` は指定候補の根拠 sample を安全に表示します。既定では本文全文を出さず body digest を表示し、短いローカル確認用 excerpt が必要な場合だけ `--show-text` を付けます。外部 item の sample は inspection 出力内の shortcut から `external-verdict --candidate <candidate-id> --sample <n>` で採点できます。削除済み repository や他 repository の queue も含めて見る場合は `--all-repos` を付けます。`llreview report` と `llreview export-jsonl` にも candidate preview が含まれます。

`llreview learn-review` は、まだハンコ待ちの候補だけを短く出す採点・承認用 command です。local review や app-developer teacher review は実行しません。先に `llreview daily`、新しい local review を必ず作る場合は `llreview daily --force-review` を実行します。teacher/external sample には `y` valid missed、`c` covered、`f` not actionable、`n` unsure、`s` skip、`q` quit で operator verdict を保存します。prompt/rule candidate は instruction preview を見たうえで `y` approve、`v` view、`s` skip で active DB calibration にできます。既定では本文全文を出さず body digest も隠し、詳細を戻す場合だけ `--verbose` や `--include-active` を使います。実行前に流れだけ見る場合は `llreview learn-review --dry-run` を使います。

`llreview learn-propose --candidate <candidate-id>` は、candidate と supporting sample から deterministic な proposal markdown/json を `out/review-history/learning-proposals/` に書き出します。proposal は `applied=false` で、prompt や rule は変更しません。raw body は保存せず、sample id / body digest / title excerpt / guardrails / validation command だけを残します。既存 proposal を上書きする場合は `--force` を付けます。

`llreview learn-next` は、最高順位の `prompt_candidate` / `rule_candidate` を自動選択し、proposal を作成または再利用して、active calibration の dry-run preview まで表示します。DB は変更しません。納得した proposal だけ `llreview learn-next --candidate <candidate-id> --activate` で有効化します。daily にこの activation を組み込む場合は `llreview daily --auto-activate-learning` を使います。データ収集候補も眺めたい場合だけ `--include-needs-data` を付けます。この場合は `pending` を `deferred` より優先し、`needs_data` は preview-only として扱うため activate 案内は出しません。

`llreview learn-apply --proposal <proposal-id> --dry-run` は、proposal からどの active calibration が作られるかを preview します。`--activate` を付けた場合だけ DB の `learning_calibrations` に `status=active` として保存します。prompt/rule の source file は変更しません。通常の `llreview` は active calibration を repo/path class scope に沿って次回 review prompt へ自動注入します。

`llreview learn-audit` は active calibration 後の run 数、同じ path class の missed external item、local false positive を集計します。まだ軽量な事後監査ですが、効いていない calibration を `pause` / `retire` 候補にするための入口です。

`make review-db-web` は Datasette を Docker でバックグラウンド起動し、`http://127.0.0.1:8003` を開きます。Datasette のデフォルトは `8001` なので、この repository では衝突を避けて `8003` にしています。停止は `make review-db-down` です。Datasette は read-only で立てているため、手動採点は `make review-db-score ...` で入れます。手元の DB client を使いたい場合は `out/review-history/local-ai-review.db` を DBeaver で開けます。

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
