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

## Output の読み方

`Findings` は PR comment にできる程度に actionable なものです。

`Watch Items` は findings ではありません。read-only filesystem hardening 後の container smoke test など、runtime または manual verification の確認点です。

## SQLite History

history DB は、remote review の前に local review が実際に役立ったかを測るためのものです。run metadata、pre-PR context、findings、watch items、reviewed files、後から更新できる feedback row を保存します。

v1.0 evidence loop 用に、従来の `findings` / `watch_items` に加えて `review_items` へ item 単位でも保存します。外部・人間レビューで見つかった指摘は `external_items`、採点は `item_verdicts` に分けて入れる設計です。`missed` は local finding の verdict ではなく、外部・人間側の item verdict として扱います。

日常利用の入口は `llreview` です。

```sh
./llreview install
llreview status
llreview
llreview score
llreview report
llreview export-jsonl
```

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
