# Local AI Precision Review

この runbook は、diff-only の local review を file-by-file で校正するための手順です。

最初の MVP workflow は PR diff 全体を一度だけ model に渡します。安全ですが、file 単位で見ると拾いやすい小さな review comment を落とすことがあります。precision reviewer は同じ safety contract を保ったまま、小さな diff chunk に分けて確認します。

## Safety Contract

- PR code を checkout しない。
- PR code を実行しない。
- PR branch の test を実行しない。
- label、workflow file、repository content を変更しない。
- GitHub API から PR diff と review comment だけを取得する。
- diff text は local Ollama にだけ送る。

## 使い方

```sh
python3 scripts/local-ai-precision-review.py \
  --repo mt4110/geo-line-ranker \
  --pr 23 \
  --output /tmp/geo-line-ranker-pr23-precision-review.md
```

各 run はデフォルトで SQLite にも保存されます。

```sh
python3 scripts/local-ai-precision-review.py \
  --repo mt4110/geo-line-ranker \
  --pr 23 \
  --output out/reviews/geo-line-ranker-pr23.md \
  --db out/review-history/local-ai-review.db
```

PR に marker comment を投稿または更新する場合:

```sh
python3 scripts/local-ai-precision-review.py \
  --repo mt4110/geo-line-ranker \
  --pr 23 \
  --post-comment
```

高速な static-only calibration pass:

```sh
python3 scripts/local-ai-precision-review.py \
  --repo mt4110/geo-line-ranker \
  --pr 23 \
  --max-model-files 0
```

同梱の make target も使えます。

```sh
make precision-review REPO=mt4110/geo-line-ranker PR=23
make pre-pr-review \
  REPO=mt4110/geo-line-ranker \
  PROJECT_DIR=/absolute/path/to/geo-line-ranker \
  BASE=main
make review-db-stats
make review-db-web
make review-db-score RUN=6 USEFUL=0 FALSE_POSITIVES=0 UNCLEAR=1 REMOTE_READY=yes NOTE='Static-only looked clean enough for PR.'
make review-db-down
```

pre-PR の static-only pass:

```sh
make pre-pr-review-static \
  REPO=mt4110/geo-line-ranker \
  PROJECT_DIR=/absolute/path/to/geo-line-ranker \
  BASE=main
```

`pre-pr-review` は対象 repository で `BASE...HEAD` から一時 diff を作り、デフォルトでは `git diff HEAD` の未commit差分も追加します。commit済み差分だけを見たい場合は `INCLUDE_WORKING_TREE=0` を指定します。remote default branch を baseline にしたい場合は `BASE=origin/main` を渡します。

pre-PR run は DB に `review_kind=pre_pr` として保存されます。`base_ref`、`head_ref`、`head_sha`、`working_tree_included` も残るため、PR 作成前の空振り・実指摘・手動評価を、後で remote review の結果と照合できます。

`llreview` の pre-PR mode では、対象 workspace の `.private_docs/` が存在する場合だけ、Markdown を compact な trusted design context に要約して model prompt に渡します。これは設計意図の参照用であり、finding は必ず diff に見える evidence に基づけます。使用した context document は本文ではなく `artifacts(kind='context_digest')` として path と sha256 を残します。無効化は `llreview --no-trusted-context`、明示指定は `llreview --trusted-context-dir /path/to/.private_docs` です。`scripts/local-ai-precision-review.py` を直接使う場合も `--trusted-context-dir` を渡せます。

## Calibration Rules

`mt4110/geo-line-ranker` の高信号 review comment では、役に立つ指摘は小さく、具体的で、diff に根拠があります。

- API/schema drift。特に code では non-optional なのに generated OpenAPI では optional になる public field。
- recoverable な configuration / database setup failure が panic になる変更。
- test/helper 内の hard-coded local service URL。
- shell strict-mode と command substitution / pipeline の罠。
- script、docs、compose の env/config mismatch。
- read-only container、tmpfs、non-root user、writable path 不足による runtime breakage。
- 検証すべき behavior を mock してしまう test。
- label、status、operating lane の vocabulary drift。

generic な best-practice comment は filtered out するか watch item に落とします。例: fixed container UID、Docker `COPY` の missing error handling、`/usr/local/bin` PATH、telemetry environment variable。

fixture / test 内の `cdn.example.com` や `blob:` は、それ自体を実URL依存やruntime不具合として扱いません。値の形を検証するだけの fixture なら空振りです。

`toPersistableImageValue()` のような persistable value guard は、`src` を絶対URLに限定しない場合があります。相対path、CDN URL、durable reference を受ける契約なら、「valid URLでない」というだけでは finding にしません。`mimeType` も、この関数が upload/content-type trust boundary であると diff から分かる場合だけ strict syntax validation を要求します。

diff に implementation と focused tests が見えていて具体的な不一致がない場合、「新しいschema/docs/READMEが実装と一致するか確認して」という汎用 watch item は出しません。CLI default workspace id、timeout秒数、example verification command も、overrideやinvalid-value testが見えているなら finding/watch にしません。

`shlex.split()` した argv を `subprocess.run(..., shell=False)` へ渡す verification command は、それだけでは shell injection と扱いません。危険な shell 呼び出し、untrusted input 由来、または `shell=True` が見える場合だけ指摘します。

`covered_by_existing_safeguard` が続く場合は、まず prompt/calibration を更新します。とくに path traversal、injection、unsafe file access のような security finding は、diff に見えている downstream validation、safe path helper、artifact-root containment を読んでから finding にします。既存対策が見えている場合は finding ではなく、negative test や runtime 確認の watch item に落とします。

`checksums.txt` のような artifact 内整合性用 manifest は、それ自体を trust anchor として扱いません。既知ハッシュで自己認証しろ、という指摘は基本的に空振りです。path validation 後にも成立する具体的な bypass が見える場合だけ finding にします。

## Output の読み方

`Findings` は PR comment にできる程度に actionable なものです。

`Watch Items` は findings ではありません。read-only filesystem hardening 後の container smoke test など、runtime または manual verification の確認点です。

## SQLite History

history DB は、remote review の前に local review が実際に役立ったかを測るためのものです。run metadata、pre-PR context、findings、watch items、reviewed files、後から更新できる feedback row を保存します。

run metadata には `prompt_family`、`prompt_version`、`prompt_hash`、`model_options_hash`、`diff_fingerprint`、trusted context の document count / summary bytes も保存します。これは後から「同じ diff / 同じ prompt profile / 同じ context digest で何が起きたか」を追跡するための再現性 metadata です。

v1.0 evidence loop 用に、従来の `findings` / `watch_items` に加えて `review_items` へ item 単位でも保存します。外部・人間レビューで見つかった指摘は `external_items`、採点は `item_verdicts` に分けて入れる設計です。`missed` は local finding の verdict ではなく、外部・人間側の item verdict として扱います。

日常利用の入口は `llreview` です。

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

`llreview update` は通常の更新入口です。既存の install path を置き換えたい場合は `llreview update --force` を使います。`llreview --update` は通常更新だけを行うショートカットです。

`llreview target set --project-dir /path/to/repo --repo owner/name` は、よく使う review 対象を `out/review-history/llreview-target.json` に保存します。tool repository から `llreview status` や `llreview` を短く実行した場合、その target が自動で使われます。明示的な `--project-dir` / `--repo` は保存 target より優先され、解除は `llreview target clear` です。

`llreview daily` は、日常の基本ループをまとめます。`status` を表示し、前回runが無い、HEAD が変わった、または working tree が dirty の場合だけ通常 review を実行し、その後に軽量な artifact-only calibration、`learn-preview`、`learn-candidates` を表示します。review を必ず回す場合は `--force-review`、学習側だけ見る場合は `--no-review`、calibration artifact を止める場合は `--no-calibration` を使います。日次出力のあとに人間のハンコだけ押す場合は `llreview learn-review` を使います。学習候補を毎回手で有効化したくない場合は、明示 opt-in の `llreview daily --auto-activate-learning` または `LLREVIEW_DAILY_AUTO_ACTIVATE_LEARNING=1` で、最高順位の proposed prompt/rule candidate を 1 件だけ active DB calibration にできます。完了済み teacher artifact の import、未採点 run、外部 item のハンコ待ち、link 診断を 1 枚にまとめたい場合は `llreview learn-pump`、daily に組み込む場合は `llreview daily --learning-pump` または `LLREVIEW_DAILY_LEARNING_PUMP=1` を使います。未採点 run の回収 inbox は `llreview daily --scoring-pump` または `LLREVIEW_DAILY_SCORING_PUMP=1` で daily に含められます。human-gate 待ち review gap のハンコ inbox は `llreview daily --review-gap-stamp-pump` または `LLREVIEW_DAILY_REVIEW_GAP_STAMP_PUMP=1` で daily に含められます。missed_by_local の弱点クラスタは `llreview daily --recall-pattern-miner` または `LLREVIEW_DAILY_RECALL_PATTERN_MINER=1` で daily に含められます。watch があるのに finding に届かなかった境界は `llreview daily --watch-sharpener` または `LLREVIEW_DAILY_WATCH_SHARPENER=1` で daily に含められます。active 化前の learning candidate risk は `llreview daily --calibration-risk-gate` または `LLREVIEW_DAILY_CALIBRATION_RISK_GATE=1` で daily に含められます。active calibration 後の regression 監査は `llreview daily --prompt-regression-audit` または `LLREVIEW_DAILY_PROMPT_REGRESSION_AUDIT=1` で daily に含められます。backfill queue の教材補給 report は `llreview daily --backfill-pump` または `LLREVIEW_DAILY_BACKFILL_PUMP=1` で daily に含められ、rate limit 付きで一件だけ進める場合は `llreview daily --backfill-pump-import-one` または `LLREVIEW_DAILY_BACKFILL_PUMP_IMPORT_ONE=1` を使います。link 失敗理由の分解は `llreview daily --matcher-explain` または `LLREVIEW_DAILY_MATCHER_EXPLAIN=1` で daily に含められます。training-ready だけの train/val/test export は `llreview daily --training-export-splitter` または `LLREVIEW_DAILY_TRAINING_EXPORT_SPLITTER=1` で daily に含められます。deterministic rule にできそうな missed pattern の抽出は `llreview daily --rule-candidate-extractor` または `LLREVIEW_DAILY_RULE_CANDIDATE_EXTRACTOR=1` で daily に含められます。これらの状態を 1 画面で見る read-only scoreboard は `llreview daily --learning-scoreboard` または `LLREVIEW_DAILY_LEARNING_SCOREBOARD=1` で daily に含められます。重い second-opinion は既定では含めず、直列で待つ場合は `llreview daily --second-opinion`、background job として逃がす場合は `llreview daily --async-second-opinion` を使います。Warp などで長い出力を追いにくい場合は、`llreview daily --notify` で完了・失敗・中断時に macOS のローカル通知を出せます。毎回使う場合は `LLREVIEW_DAILY_NOTIFY=1`、音も出す場合は `--notify-sound Glass` または `LLREVIEW_NOTIFY_SOUND=Glass` を指定します。通知経路だけ確認する場合は `llreview notify-test` を使います。

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

import 後の teacher item は `llreview external-verdict <external_item_id> --verdict teacher_false_positive` や `--verdict missed_by_local` で operator 判断を残せます。candidate inspection の sample 番号から採点する場合は `llreview external-verdict --candidate <candidate-id> --sample 1 --verdict missed_by_local ...` のように書けます。

`llreview calibration` は、既存の SQLite history だけを読み、直近または `--run <id>` の review run から `out/calibration/runs/<calibration-run-id>/` に manifest、normalized item JSONL、alignment JSONL、verdict candidate JSONL、Markdown/JSON report を書き出します。この step は追加の model 呼び出し、PR comment 投稿、PR code checkout/execution、remote review 取得をしません。daily では既定で実行され、生成 artifact の digest だけを `artifacts(kind='calibration_*')` に保存します。

`llreview backup` は、local SSD 上の review DB を正として、iCloud などの backup folder に timestamped snapshot を保存します。SQLite は `.backup` 相当の API でコピーし、`export-jsonl` を更新して `review-items.<timestamp>.jsonl` も保存します。`benchmark-report.md` がある場合は一緒に保存します。DB を iCloud 上で常時運用するのではなく、snapshot だけを保存するための command です。最新ファイルも更新したい場合は `llreview backup --latest`、保存先確認だけなら `llreview backup --dry-run` を使います。`llreview daily --offer-backup` は、その daily run で学習 row が増えた場合だけ interactive shell で backup を提案します。

`llreview second-opinion` は、重い second-opinion model を通常 reviewer の代わりではなく「AIレビューキラー用の虫眼鏡」として使います。既定では `qwen3-coder-next:q4_K_M` / `OLLAMA_NUM_CTX=12288` / `--max-model-files 2` で動き、macOS の物理メモリ見積もりが `--max-memory-percent` (既定90%) を超えそうな場合は skip します。意図的に空き時間で回す場合だけ `--force` を使います。実行後は既定で `ollama stop` します。`daily` 経由では、second-opinion gate 前に通常 reviewer model を止めて、primary model が残っているだけで skip される状況を減らします。

`llreview daily --async-second-opinion` は、同じ memory gate を通したうえで `second-opinion` を background process として起動し、daily 自体は job id、PID、manifest、log、output path を返して終了します。job manifest は `out/async-review/runs/<job-id>/manifest.json`、結果は既定で同じ directory の `second-opinion.md` に保存されます。状態確認は `llreview async-status` を使います。この async launcher は shell を使わず、PR comment 投稿、PR code checkout/execution、remote review 取得をしません。

TTY では `llreview` が phase、elapsed、model-reviewed file count、finding/watch count を spinner 付きの 1 行で更新します。CI や log 保存では `--plain` を使うと通常の行ログになります。spinner が使えない環境では、既定で10秒ごとに still-running heartbeat を出します。止めたい場合は `--progress-heartbeat-seconds 0` を使います。

`llreview scoring-pump` は、未採点 run を `quick_drain_zero_findings` と `manual_finding_score` に分け、すぐ使える `llreview score` command を `out/review-history/scoring-pump/` に書きます。既定では read-only です。finding 0 の run だけを明示的に排水する場合は `llreview scoring-pump --apply-zero-findings` を使います。finding がある run は item verdict を確認してから `llreview score --run <id> --items` または `--demote-findings` を使います。

`llreview review-gap-stamp-pump` は、human-gate 待ちの review gap を、短い根拠、local finding/watch との距離、`external-verdict` command 付きで `out/review-history/review-gap-stamp-pump/` に書きます。既定では read-only です。TTY で連続ハンコを押す場合は `llreview review-gap-stamp-pump --stamp` を使い、`y` valid、`f` not actionable、`c` covered、`n` unsure、`s` skip、`q` quit を入力します。

`llreview recall-pattern-miner` は、`missed_by_local` の review gap を path class、learning target、path bucket、title token の近さでクラスタ化し、`out/review-history/recall-pattern-miner/` に弱点パターン report を書きます。これは優先順位付けの evidence であり、そのまま prompt/rule update にはしません。

`llreview watch-sharpener` は、local watch item が存在したのに teacher/external finding へ届かなかった review gap を `out/review-history/watch-sharpener/` にまとめます。近い watch がある場合は watch から finding へ上げる条件を示し、近い watch が無い場合は「watch が多いだけで具体欠陥を見ていない recall gap」として扱います。これは自動昇格ではなく、prompt calibration や review 方針のための境界 evidence です。

`llreview matcher-explain` は、unlinked external item について deterministic matcher がなぜ link しなかったかを `out/review-history/matcher-explain/` に分解します。比較対象 run の有無、finding/watch 候補数、path 一致、line 距離、title/body 類似度、token overlap、threshold margin を出し、`no_comparable_local_run`、`watch_only_no_finding`、`path_mismatch`、`near_below_threshold` などに分類します。既定は read-only で、raw body は出さず digest と短い title excerpt だけを使います。local-only で本文 excerpt を見たい時だけ `--show-text` を使います。

`llreview training-export-splitter` は、`review_gap_example` のうち `training_ready=true` かつ human-gate 不要の record だけを `out/review-history/training-export/` 配下へ train/val/test JSONL として書き出します。既定では raw diff/code/body を含めず、raw path も `path_class` と `path_digest` に落とします。`.private_docs` と generated/snapshot path は除外し、必要な場合だけ `--include-paths` や `--include-title-excerpts` を明示します。これは training-ready dataset の配管であり、human-gate 待ちを学習に混ぜないための splitter です。

`llreview rule-candidate-extractor` は、training-ready の `missed_by_local` を path class、title token、known mechanical family で束ね、deterministic rule にできそうな候補を `out/review-history/rule-candidate-extractor/` に書き出します。既定では human-gate 待ちを除外し、raw body/diff は出しません。`path_containment`、`shell_quoting`、`state_normalization`、`reserved_config` などの機械的 trigger へ落とせる時だけ `proposed_rule_candidate` とし、それ以外は prompt/watch evidence として残します。rule code や prompt source は変更しません。

`llreview learning-scoreboard` は、learning pump、scoring pump、review-gap stamp pump、recall miner、watch sharpener、risk gate、regression audit、backfill pump、matcher explain、training export、rule extractor の latest artifact と DB 集計を 1 画面にまとめます。read-only で、review 実行、teacher import、calibration activation、raw private text export は行いません。

`llreview calibration-risk-gate` は、proposed prompt/rule candidate を active DB calibration にする前に、training-ready support、human-gate 残、false-positive counter-evidence、missed counter-evidence を `out/review-history/calibration-risk-gate/` にまとめます。`learn-next --activate`、`learn-apply --activate`、`learn-review` の activation 直前にも同じ gate を表示し、block 判定は `--force-risk` がない限り active 化しません。daily auto activation は block された候補を skip します。

`llreview prompt-regression-audit` は、active calibration 後の同一 scope/path class で missed external item や local false positive が減ったかを `out/review-history/prompt-regression-audit/` にまとめます。効いていない可能性が高い校正は `stale_candidate` として出しますが、自動で pause / retire はしません。これは active calibration を育て続けるための事後監査です。

`llreview score` は直近の未採点 run を選び、run 単位の `useful` / `false_positive` / `unclear` count を保存します。TTY では続けて finding 単位の verdict も入力できます。local finding の verdict は `useful_fixed` / `false_positive` / `unclear` / `watch_only` に限定し、`missed` は外部・人間レビューで見つかった `external_items` 側にだけ付けます。全部の finding を「blocker ではなく watch/calibration evidence」として一括で落とす場合は、長い対話の代わりに `llreview score --run <id> --demote-findings` を使います。既定では run-level を `useful=0 / false_positives=<findings_count> / unclear=0`、item-level を `watch_only / diagnostic_watch` として保存します。

`llreview import-github-reviews 42` は GitHub の inline PR review comments を取り込みます。Copilot / automated / human の comment を `external_items` に保存し、既存の local `review_items` と fingerprint、file、line、normalized text でゆるく照合して `item_links` を作ります。local review run がある場合だけ、link 済み external item に `covered_by_local`、unlinked external item に `missed_by_local` を外部側 verdict として保存します。local run candidate が無い場合、missed verdict は自動では書きません。

同じ GitHub comment id は update されるため、同じ PR を再 import しても row は増えません。再 import 時には、今回の GitHub comment snapshot に含まれない GitHub 由来の古い external item / link / importer verdict を削除します。API 取得結果を固定して再現確認したい場合は、GitHub `/pulls/comments` の JSON array を保存し、`--comments-json comments.json --repo owner/name` で通します。この場合は各 comment の GitHub `commit_id` を保持します。`--head-sha <sha>` は保存済み comment を特定の local run SHA に意図的に固定したい場合だけ使います。top-level PR conversation comments も保存済み JSON で取り込む場合は、GitHub `/issues/comments` の JSON array を別に保存し、`--include-issue-comments --issue-comments-json issue-comments.json` を併用します。

`llreview import-github-history --dry-run` は、過去の merged PR と local git 履歴を教材候補として preview します。remote GitHub PR は `--remote-*` limit で API 量を制限し、local git は `--local-*` limit で CPU/output 量だけを制限します。local scan は token 不要で、preferred GitHub remote の owner が `mt4110` ではない repository は `skipped_owner_not_mt4110` で block します。skip reason を DB の `github_backfill_queue` に残したい場合だけ `--refresh-queue` を付けます。queue から remote candidate を1件だけ進める場合は `--one` を付けます。`--one --dry-run` は選択PRと import/link 件数だけを確認し、実 import は既定で20分に1件までに制限します。`llreview backfill-pump` はこの queue を日常運用向けにまとめ、before/after、rate gate、次候補、external item delta を `out/review-history/backfill-pump/` に保存します。既定は report-only、`--import-one` で一件だけ進め、`--import-one --dry-run` は external item / link / verdict / queue state を書きません。`llreview report` / `llreview export-jsonl` は queue state と skip reason も出します。

`llreview learn-preview` は、DB に蓄積された local verdict、external verdict、backfill queue を集計し、次回 review に入る aggregate calibration を表示します。通常の `llreview` はこの集計を reviewer prompt に自動で追加しますが、raw comment や raw diff は入れません。対象は verdict reason、external verdict count、path class、queue state などの集計だけです。止めたい場合は `llreview --no-history-calibration` を使います。この feedback は review の疑い方と優先度を調整するためのもので、prompt/rule の自動書き換えは繰り返し evidence が揃った後に別途行います。

`llreview learn-candidates` は、aggregate evidence から `prompt_candidate` / `rule_candidate` / `needs_data` を導出します。candidate は evidence count、path class、reason/source、confidence、status、recommended action を持ちます。`proposed` は提案だけで、`active` は既に operator-approved DB calibration として次回 prompt に入る状態です。prompt や rule の source file は変更しません。一覧には短い ID と行番号が出ます。`llreview learn-candidates --inspect` は先頭候補、`--inspect 2` は2行目、`--inspect <candidate-id>` は candidate を支える sample を表示します。既定では本文全文を出さず body digest だけを表示し、短いローカル確認用 excerpt が必要な場合だけ `--show-text` を付けます。外部 item の sample は inspection 出力内の shortcut から `external-verdict --candidate <candidate-id> --sample <n>` で採点できます。削除済み repository や他 repository の queue も含めて見る場合は `--all-repos` を付けます。`llreview report` と `llreview export-jsonl` にも同じ candidate preview を含めます。

`llreview learn-review` は、まだハンコ待ちの候補だけを短く出す採点・承認用 command です。local review や app-developer teacher review は実行しません。先に `llreview daily`、新しい local review を必ず作る場合は `llreview daily --force-review` を実行します。teacher/external sample には `y` valid missed、`c` covered、`f` not actionable、`n` unsure、`s` skip、`q` quit で operator verdict を保存します。日本語でハンコを押したい場合は `llreview learn-review --language ja` または短縮形の `--ja` を使い、毎回固定する場合は `LLREVIEW_LEARN_REVIEW_LANGUAGE=ja` を設定します。この設定は対話表示を日本語化するだけで、DB の verdict / reason / export schema は安定した英語コードのままです。prompt/rule candidate は instruction preview と Calibration Risk Gate を見たうえで `y` を押すと active DB calibration として保存され、次回以降の review prompt に効きます。ハンコだけ押したい場合は `llreview learn-review --no-activate` を使います。既定では本文全文を出さず body digest も隠し、詳細を戻す場合だけ `--verbose` や `--include-active` を使います。実行前に流れだけ見る場合は `llreview learn-review --dry-run` を使います。一度 operator が押した external verdict は importer の `no_local_match` / `linked_by_importer` verdict で上書きされないため、同じ teacher gap が再 import でハンコ待ちに戻るのを防ぎます。

`llreview learn-propose --candidate <candidate-id>` は、candidate と supporting sample から deterministic な proposal markdown/json を `out/review-history/learning-proposals/` に書き出します。proposal は `applied=false` で、prompt や rule は変更しません。raw body は保存せず、sample id / body digest / title excerpt / guardrails / validation command だけを残します。既存 proposal を上書きする場合は `--force` を付けます。

`llreview learn-next` は、最高順位の `prompt_candidate` / `rule_candidate` を自動選択し、proposal を作成または再利用して、active calibration の dry-run preview まで表示します。DB は変更しません。納得した proposal だけ `llreview learn-next --candidate <candidate-id> --activate` で有効化します。daily にこの activation を組み込む場合は `llreview daily --auto-activate-learning` を使います。データ収集候補も眺めたい場合だけ `--include-needs-data` を付けます。この場合は `pending` を `deferred` より優先し、`needs_data` は preview-only として扱うため activate 案内は出しません。

`llreview learn-apply --proposal <proposal-id> --dry-run` は、proposal から作られる active calibration を preview します。`--activate` を付けた場合だけ DB の `learning_calibrations` に `status=active` として保存します。prompt/rule の source file は変更しません。通常の `llreview` は active calibration を repo/path class scope に沿って次回 review prompt に自動注入します。

`llreview learn-audit` は、active calibration 後の run 数、同じ path class の missed external item、local false positive を集計します。まだ軽量な事後監査ですが、効いていない calibration を `pause` / `retire` 候補にするための入口です。

finding 単位の false positive は、理由コードも残します。まずは `covered_by_existing_safeguard`、`intentional_behavior`、`environment_dependent`、`covered_by_tests`、`stale_or_already_fixed`、`diagnostic_watch` を使い、同じ理由が複数回出たものだけ prompt または local-rule update 候補として扱います。1回の空振りだけで suppress しないのが基本です。

`llreview report` は直近10件を benchmark markdown として出力し、run 単位の useful/false positive/unclear 率、remote review 差分、runtime、item verdict reason の集計をまとめます。

`llreview export-jsonl` は local review item、imported external item、backfill queue item、learning candidate、learning calibration を JSONL に出力します。local item には `prompt_hash`、`model_options_hash`、`diff_fingerprint`、`path_class` を含めます。run に trusted context や review-history calibration が使われた場合は、`context_digests` と `history_calibration_digests` に sha256 list も含めます。external item には GitHub comment id、source、link 先の local item id、外部側 verdict、`path_class` を含めます。learning candidate は `applied=false` の preview record として出力し、learning calibration は `learn-apply` で有効化された DB calibration として出力します。

デフォルトの DB path:

```text
out/review-history/local-ai-review.db
```

browser view:

```sh
make review-db-web
```

Datasette を Docker でバックグラウンド起動し、`http://127.0.0.1:8003` を開きます。Datasette のデフォルトは `8001` なので、この repository では `8003` に bind します。

停止:

```sh
make review-db-down
```

Datasette は read-only で立てるため、manual scoring は browser の `INSERT` ではなく CLI で行います。

```sh
make review-db-score \
  RUN=6 \
  USEFUL=0 \
  FALSE_POSITIVES=0 \
  UNCLEAR=1 \
  REMOTE_READY=yes \
  NOTE='Static-only looked clean enough for PR.'
```

desktop DB client を使う場合は、同じ DB file を DBeaver で開けます。

SQL 例:

```text
sql/review-history-example-queries.sql
```
