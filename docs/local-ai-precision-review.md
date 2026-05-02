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
llreview
llreview update
llreview score
llreview import-github-reviews 42
llreview report
llreview export-jsonl
```

`llreview update` は通常の更新入口です。既存の install path を置き換えたい場合は `llreview update --force` を使います。`llreview --update` は通常更新だけを行うショートカットです。

`llreview score` は直近の未採点 run を選び、run 単位の `useful` / `false_positive` / `unclear` count を保存します。TTY では続けて finding 単位の verdict も入力できます。local finding の verdict は `useful_fixed` / `false_positive` / `unclear` / `watch_only` に限定し、`missed` は外部・人間レビューで見つかった `external_items` 側にだけ付けます。

`llreview import-github-reviews 42` は GitHub の inline PR review comments を取り込みます。Copilot / automated / human の comment を `external_items` に保存し、既存の local `review_items` と fingerprint、file、line、normalized text でゆるく照合して `item_links` を作ります。local review run がある場合だけ、link 済み external item に `covered_by_local`、unlinked external item に `missed_by_local` を外部側 verdict として保存します。local run candidate が無い場合、missed verdict は自動では書きません。

同じ GitHub comment id は update されるため、同じ PR を再 import しても row は増えません。API 取得結果を固定して再現確認したい場合は、GitHub `/pulls/comments` の JSON array を保存し、`--comments-json comments.json --repo owner/name --head-sha <sha>` で同じ importer 経路に通せます。top-level PR conversation comments も保存済み JSON で取り込む場合は、GitHub `/issues/comments` の JSON array を別に保存し、`--include-issue-comments --issue-comments-json issue-comments.json` を併用します。

finding 単位の false positive は、理由コードも残します。まずは `covered_by_existing_safeguard`、`intentional_behavior`、`environment_dependent`、`covered_by_tests`、`stale_or_already_fixed`、`diagnostic_watch` を使い、同じ理由が複数回出たものだけ prompt または local-rule update 候補として扱います。1回の空振りだけで suppress しないのが基本です。

`llreview report` は直近10件を benchmark markdown として出力し、run 単位の useful/false positive/unclear 率、remote review 差分、runtime、item verdict reason の集計をまとめます。

`llreview export-jsonl` は local review item と imported external item を JSONL に出力します。local item には `prompt_hash`、`model_options_hash`、`diff_fingerprint` を含めます。run に trusted context が使われた場合は `context_digests` に sha256 list も含めます。external item には GitHub comment id、source、link 先の local item id、外部側 verdict を含めます。

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
