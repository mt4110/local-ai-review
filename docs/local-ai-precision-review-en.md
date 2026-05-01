# Local AI Precision Review

This runbook is for Copilot-style, diff-only local review calibration.

The first MVP workflow sends the whole PR diff to the model once. That is safe,
but it can miss small review comments that are easier to catch file by file. The
precision reviewer keeps the same safety contract while reviewing smaller diff
chunks.

## Safety Contract

- Do not checkout PR code.
- Do not run PR code.
- Do not run tests from the PR branch.
- Do not mutate labels, workflow files, or repository content.
- Fetch only the PR diff and review comments through the GitHub API.
- Send diff text only to local Ollama.

## Usage

```sh
python3 scripts/local-ai-precision-review.py \
  --repo mt4110/geo-line-ranker \
  --pr 23 \
  --output /tmp/geo-line-ranker-pr23-precision-review.md
```

Every run is also persisted into SQLite by default.

```sh
python3 scripts/local-ai-precision-review.py \
  --repo mt4110/geo-line-ranker \
  --pr 23 \
  --output out/reviews/geo-line-ranker-pr23.md \
  --db out/review-history/local-ai-review.db
```

To post or update a marker comment on the PR:

```sh
python3 scripts/local-ai-precision-review.py \
  --repo mt4110/geo-line-ranker \
  --pr 23 \
  --post-comment
```

For a fast static-only calibration pass:

```sh
python3 scripts/local-ai-precision-review.py \
  --repo mt4110/geo-line-ranker \
  --pr 23 \
  --max-model-files 0
```

Or use the bundled make targets:

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

For a pre-PR static-only pass:

```sh
make pre-pr-review-static \
  REPO=mt4110/geo-line-ranker \
  PROJECT_DIR=/absolute/path/to/geo-line-ranker \
  BASE=main
```

`pre-pr-review` builds a temporary diff from `BASE...HEAD` in the target
repository and, by default, appends the uncommitted working tree diff from
`git diff HEAD`. Set `INCLUDE_WORKING_TREE=0` if you want only committed
changes. If you prefer the remote default branch as the baseline, pass
`BASE=origin/main`.

Pre-PR runs are stored with `review_kind=pre_pr`. The DB also keeps `base_ref`,
`head_ref`, `head_sha`, and `working_tree_included`, so you can compare the
preflight findings, false positives, and manual score against the later remote
review.

## Calibration Rules

Past high-signal review comments in `mt4110/geo-line-ranker` show that useful
findings tend to be small and grounded:

- API/schema drift, especially public fields that are non-optional in code but
  optional in generated OpenAPI.
- Recoverable configuration or database setup failures becoming panics.
- Hard-coded local service URLs in tests/helpers.
- Shell strict-mode traps around command substitution and pipelines.
- Env/config mismatches between scripts, docs, and compose files.
- Runtime breakage from read-only containers, tmpfs, non-root users, and missing
  writable paths.
- Tests that mock the behavior they were supposed to verify.
- Documentation vocabulary drift for labels, statuses, and operating lanes.

Generic best-practice comments are intentionally filtered out or demoted to
watch items. Examples: fixed container UIDs, Docker `COPY` "missing error
handling", `/usr/local/bin` PATH concerns, and telemetry environment variables.

When `covered_by_existing_safeguard` repeats, update prompt/calibration before
adding suppression rules. Security findings such as path traversal, injection,
or unsafe file access must inspect downstream validation visible in the diff:
safe path helpers, absolute/parent path rejection, and artifact-root containment.
If a safeguard is already visible, demote the concern to a watch item for
negative tests or runtime verification instead of reporting a finding.

Artifact consistency manifests such as `checksums.txt` are not trust anchors
that must authenticate themselves. Do not report "known good checksum" or
self-integrity requirements unless the diff shows a concrete bypass after path
validation or a real security boundary that trusts the checksum file.

## Interpreting Output

`Findings` should be actionable enough to comment on a PR.

`Watch Items` are not findings. They are runtime or manual verification points,
such as container smoke tests after read-only filesystem hardening.

## SQLite History

The history DB is for measuring whether local review is actually useful before
remote review. It stores run metadata, pre-PR context, findings, watch items,
reviewed files, and an optional feedback row you can update later from the CLI.
For the v1.0 evidence loop, normalized local items are stored in `review_items`,
external or human-review items belong in `external_items`, and item-level
scoring is stored in `item_verdicts`. `missed` belongs to external/human items,
not to local findings.

Daily entrypoints:

```sh
./llreview install
llreview status
llreview
llreview update
llreview score
llreview report
llreview export-jsonl
```

`llreview update` is the canonical update entrypoint. Use
`llreview update --force` when the existing install path should be replaced.
`llreview --update` remains a normal-update shortcut.

`llreview score` selects the latest unscored run and records run-level counts.
In a TTY it also prompts for per-finding verdicts: `useful_fixed`,
`false_positive`, `unclear`, or `watch_only`. False positives keep a short
reason code such as `covered_by_existing_safeguard`, `intentional_behavior`,
`environment_dependent`, `covered_by_tests`, `stale_or_already_fixed`, or
`diagnostic_watch`. A single false positive is evidence, not an automatic
suppression rule; repeated reasons become prompt/local-rule candidates in
`llreview report`.

Default DB path:

```text
out/review-history/local-ai-review.db
```

Example browser view:

```sh
make review-db-web
```

This starts Datasette in Docker in the background, then opens
`http://127.0.0.1:8003` in the browser. Datasette defaults to `8001`, so this
repo binds `8003` to stay two ports above the default.

To stop it:

```sh
make review-db-down
```

Datasette is intentionally read-only here, so manual scoring is done through the
CLI instead of browser `INSERT` statements:

```sh
make review-db-score \
  RUN=6 \
  USEFUL=0 \
  FALSE_POSITIVES=0 \
  UNCLEAR=1 \
  REMOTE_READY=yes \
  NOTE='Static-only looked clean enough for PR.'
```

If you prefer a desktop DB client, open the same file in DBeaver.

Useful SQL examples live in:

```text
sql/review-history-example-queries.sql
```
