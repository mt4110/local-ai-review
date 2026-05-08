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

In pre-PR mode, if the target workspace contains `.private_docs/`, `llreview`
summarizes its Markdown files as compact trusted design context for the model
prompt. This context helps interpret visible diff evidence; it is not evidence
by itself. The run stores only each context document path and sha256 in
`artifacts(kind='context_digest')`. Use `llreview --no-trusted-context` to
disable the auto-load path, or `llreview --trusted-context-dir /path/to/.private_docs`
to pass a trusted context directory explicitly. Direct
`scripts/local-ai-precision-review.py` runs can also use `--trusted-context-dir`.

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

Do not treat `cdn.example.com` or `blob:` strings in fixtures/tests as real URL
dependencies when the code only checks value-shaping and never fetches them.

Persistable value guards such as `toPersistableImageValue()` may intentionally
accept relative paths, CDN URLs, or durable references. Do not require `src` to
be an absolute valid URL unless that is the public contract. Do not require
strict `mimeType` syntax validation unless the diff shows the guard is the
upload/content-type trust boundary.

Do not emit generic watch items asking someone to verify new schema/docs/README
entries against implementation when the diff already shows the implementation,
focused tests, and no concrete mismatch. CLI default workspace ids, timeout
seconds, and example verification commands are not issues when overrides and
invalid-value tests are visible.

A verification command parsed with `shlex.split()` and executed through
`subprocess.run(..., shell=False)` is not shell injection by itself. Report it
only when the diff shows a shell boundary, untrusted command construction, or
`shell=True`.

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
Run metadata includes `prompt_family`, `prompt_version`, `prompt_hash`,
`model_options_hash`, `diff_fingerprint`, and trusted-context document count /
summary bytes. These fields make later calibration and learning exports
reconstructable without storing raw private rows.
For the v1.0 evidence loop, normalized local items are stored in `review_items`,
external or human-review items belong in `external_items`, and item-level
scoring is stored in `item_verdicts`. `missed` belongs to external/human items,
not to local findings.

Daily entrypoints:

```sh
./llreview install
llreview status
llreview target set --project-dir /absolute/path/to/repo --repo owner/name
llreview daily
llreview
llreview second-opinion
llreview async-status
llreview calibration
llreview update
llreview score
llreview import-github-reviews 42
llreview training-export-splitter
llreview learning-scoreboard
llreview report
llreview export-jsonl
```

`llreview update` is the canonical update entrypoint. Use
`llreview update --force` when the existing install path should be replaced.
`llreview --update` remains a normal-update shortcut.

`llreview target set --project-dir /path/to/repo --repo owner/name` saves the
frequent review target in `out/review-history/llreview-target.json`. When short
commands are run from the tool repository, `llreview status`, `llreview`,
`llreview learn-preview`, and `llreview learn-candidates` reuse that target.
Explicit `--project-dir` / `--repo` values always override it. Use `llreview
target clear` to remove the saved target.

`llreview daily` is the normal daily loop. It prints `status`, runs the normal
review only when there is no previous run, the head SHA changed, or the working
tree is dirty, then writes a lightweight artifact-only calibration report and
prints `learn-preview` and `learn-candidates`. Use `--force-review` to run
anyway, `--no-review` to inspect only status and learning output, and
`--no-calibration` to skip the calibration artifact. After daily output, use
`llreview learn-review` when you want to quickly stamp only the items that need
human judgment. If you do not want to
manually activate learning candidates every time, explicit opt-in with
`llreview daily --auto-activate-learning` or
`LLREVIEW_DAILY_AUTO_ACTIVATE_LEARNING=1` activates at most one highest-ranked
proposed prompt/rule candidate as an active DB calibration. Include the
completed-teacher import, unscored runs, external item stamp queue, and link
diagnostics in one inbox with `llreview learn-pump`, or include it in daily with
`llreview daily --learning-pump` or `LLREVIEW_DAILY_LEARNING_PUMP=1`. Include the
unscored-run scoring inbox with `llreview daily --scoring-pump` or
`LLREVIEW_DAILY_SCORING_PUMP=1`. Include the human-gate review-gap stamp inbox
with `llreview daily --review-gap-stamp-pump` or
`LLREVIEW_DAILY_REVIEW_GAP_STAMP_PUMP=1`. Include missed-by-local recall
clustering with `llreview daily --recall-pattern-miner` or
`LLREVIEW_DAILY_RECALL_PATTERN_MINER=1`. Include watch/finding boundary review
with `llreview daily --watch-sharpener` or
`LLREVIEW_DAILY_WATCH_SHARPENER=1`. Include activation risk review with
`llreview daily --calibration-risk-gate` or
`LLREVIEW_DAILY_CALIBRATION_RISK_GATE=1`. Include post-activation regression
audit with `llreview daily --prompt-regression-audit` or
`LLREVIEW_DAILY_PROMPT_REGRESSION_AUDIT=1`. Include historical review evidence
fuel as a queue report with `llreview daily --backfill-pump` or
`LLREVIEW_DAILY_BACKFILL_PUMP=1`; to advance one rate-limited remote row, use
`llreview daily --backfill-pump-import-one` or
`LLREVIEW_DAILY_BACKFILL_PUMP_IMPORT_ONE=1`. Include link-failure explanation
with `llreview daily --matcher-explain` or
`LLREVIEW_DAILY_MATCHER_EXPLAIN=1`. Include safe train/val/test export of
training-ready examples with `llreview daily --training-export-splitter` or
`LLREVIEW_DAILY_TRAINING_EXPORT_SPLITTER=1`. Include deterministic
rule-candidate extraction with `llreview daily --rule-candidate-extractor` or
`LLREVIEW_DAILY_RULE_CANDIDATE_EXTRACTOR=1`. Include the read-only learning
scoreboard with `llreview daily --learning-scoreboard` or
`LLREVIEW_DAILY_LEARNING_SCOREBOARD=1`. The heavy pass is
opt-in with `llreview daily --second-opinion`, or it can be launched as a
background job with `llreview daily --async-second-opinion`. When the terminal output is
easy to miss, pass `llreview daily --notify` to send a macOS local notification
on completion, failure, or interruption. Set
`LLREVIEW_DAILY_NOTIFY=1` to enable it by default, and use `--notify-sound Glass`
or `LLREVIEW_NOTIFY_SOUND=Glass` when you want a sound.
Use `llreview notify-test` to test only the local macOS notification path.

For the usual daily workflow, put the daily defaults in your shell rc. When you
want the app-developer teacher review harness to start in the background on
every daily run for an experiment, set `LLREVIEW_DAILY_APP_DEVELOPER_REVIEW=1`.
Start the heavy local second-opinion lane per run with
`llreview daily --async-second-opinion` when the machine is idle. Modern macOS
shells normally use zsh, so add the block to `~/.zshrc`. If you use bash, add
the same block to `~/.bashrc`.

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

For `daily`, the second-opinion memory gate now stops the primary reviewer model first (`LLREVIEW_PRIMARY_REVIEW_MODEL`, or `OLLAMA_MODEL` when unset) before estimating the heavy model load. Use `llreview daily --no-stop-primary-before-second-opinion` when you want to keep it loaded. Persistent tuning can live in your shell rc through `LLREVIEW_SECOND_OPINION_MODEL`, `LLREVIEW_SECOND_OPINION_NUM_CTX`, `LLREVIEW_SECOND_OPINION_MAX_MODEL_FILES`, `LLREVIEW_SECOND_OPINION_MODEL_MEMORY_GB`, and `LLREVIEW_SECOND_OPINION_MAX_MEMORY_PERCENT`. On 64GB-class machines, the default `qwen3-coder-next:q4_K_M` may not pass the memory gate. Keep it out of the everyday loop; switch to a lighter model for that run, or use `--force-second-opinion` only during intentional idle time.

After that, these short commands run the daily review/calibration/learning
activation and preview loop, app-developer teacher artifact, notifications, and
background status checks:

```sh
llreview daily
llreview async-status
```

You can shorten typing further with aliases and a small scoring helper:

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

Use `llreview daily --no-async-second-opinion` to temporarily skip only the
background second-opinion job, `llreview daily --no-auto-activate-learning` to
skip learning activation, or `llreview daily --no-notify` to suppress the
notification for one run.

Use `llreview daily --no-app-developer-review` to temporarily skip only the
app-developer teacher review harness. The harness embeds the pre-PR diff captured
by `llreview` into a prompt artifact, then a background client starts
`codex app-server` and sends `initialize`, `initialized`, `thread/start`, and
`turn/start` over JSONL. It writes the diff, prompt, review output, events
JSONL, stdout/stderr, and manifest under
`out/app-developer-review/runs/<job-id>/`. The default model is `gpt-5.4` for the
current app-server compatibility. It does not post PR comments, check out or
execute PR code, or import remote reviews.

On later runs, `llreview daily` imports completed app-developer jobs by
normalizing `review.md` into structured items and writing only high-confidence
findings to `external_items(source='teacher_model')`. It links those teacher
items against local primary `review_items` with the deterministic matcher, then
writes `comparison-report.md` and refreshes the calibration report. Use
`llreview app-developer-review-status --import-completed` to inspect or import
jobs manually. Treat teacher output as calibration evidence for comparison with
the local primary review, not as truth.

After import, record the operator judgment with commands such as
`llreview external-verdict <external_item_id> --verdict teacher_false_positive`
or `--verdict missed_by_local`. When you are scoring from a candidate
inspection, you can use the sample number instead:
`llreview external-verdict --candidate <candidate-id> --sample 1 --verdict missed_by_local ...`.

`llreview calibration` writes a file-first calibration artifact from the
existing SQLite history for the latest workspace run, or for `--run <id>`.
It writes manifest, normalized item JSONL, alignment JSONL, verdict candidate
JSONL, and Markdown/JSON reports under `out/calibration/runs/<calibration-run-id>/`.
This step does not call another model, post PR comments, check out or execute PR
code, or fetch remote review data. In `daily`, it runs by default and stores
only artifact digests in `artifacts(kind='calibration_*')`.

`llreview backup` saves timestamped learning snapshots to iCloud or another
folder while keeping the live SQLite DB on local SSD. The command uses SQLite's
backup API, refreshes `export-jsonl`, copies `review-items.<timestamp>.jsonl`,
and copies `benchmark-report.md` when it exists. Use `llreview backup --latest`
to also update stable latest files, or `llreview backup --dry-run` to inspect
the planned paths. `llreview daily --offer-backup` offers an interactive backup
only when that daily run changed learning rows.

`llreview second-opinion` is the heavy review-killer pass, not the default daily
reviewer. By default it runs `qwen3-coder-next:q4_K_M` with
`OLLAMA_NUM_CTX=12288` and `--max-model-files 2`, estimates macOS physical
memory usage, and skips the run when the post-load estimate would exceed
`--max-memory-percent` (90 by default). Use `--force` only for intentional idle
machine runs. The command stops the model after the run unless `--keep-loaded`
is passed. When launched through `daily`, the primary reviewer model is stopped
before the second-opinion gate so a still-loaded daily model does not
unnecessarily block the background pass.

`llreview daily --async-second-opinion` passes the same memory gate and starts
`second-opinion` as a background process. Daily returns the job id, PID,
manifest path, log paths, and output path immediately. Job manifests live under
`out/async-review/runs/<job-id>/manifest.json`, and `llreview async-status`
shows recent background jobs. The async launcher uses `subprocess.Popen` without
a shell and does not post PR comments, check out or execute PR code, or fetch
remote review data.

In a TTY, `llreview` renders one lightweight spinner line with the current
phase, elapsed time, model file count, and finding/watch totals. Use `--plain`
for line-oriented logs in CI or saved output. When the spinner is not active,
`llreview` prints a still-running heartbeat every 10 seconds by default; pass
`--progress-heartbeat-seconds 0` to disable it.

`llreview scoring-pump` turns the unscored-run backlog into a focused scoring
inbox under `out/review-history/scoring-pump/`. It separates
`quick_drain_zero_findings` runs from `manual_finding_score` runs and prints
ready-to-run `llreview score` commands. The default mode is read-only. Use
`llreview scoring-pump --apply-zero-findings` only to write run-level feedback
for zero-finding runs; runs with findings should still be reviewed with
`llreview score --run <id> --items` or checked before `--demote-findings`.

`llreview review-gap-stamp-pump` turns human-gate review gaps into a focused
stamp inbox under `out/review-history/review-gap-stamp-pump/`. It shows short
rationale, local finding/watch distance, and ready-to-run `external-verdict`
commands. The default mode is read-only. Use
`llreview review-gap-stamp-pump --stamp` in a TTY for a continuous `y` valid,
`f` not actionable, `c` covered, `n` unsure, `s` skip, `q` quit flow.

`llreview recall-pattern-miner` clusters `missed_by_local` review gaps by path
class, learning target, path bucket, and title-token similarity. It writes
weakness-pattern Markdown/JSON artifacts under
`out/review-history/recall-pattern-miner/`. This is prioritization evidence,
not a prompt or rule update by itself.

`llreview watch-sharpener` inspects review gaps where local watch items existed
but did not become concrete findings. It writes Markdown/JSON artifacts under
`out/review-history/watch-sharpener/`. When a near watch exists, it suggests
the condition that would have made the item finding-worthy; when no near watch
exists, it treats the case as a recall gap rather than a matcher failure. It is
boundary evidence for prompt calibration and review policy, not automatic
promotion.

`llreview matcher-explain` explains why unlinked external items did not match
local findings. It writes Markdown/JSON artifacts under
`out/review-history/matcher-explain/` with comparable-run presence,
finding/watch candidate counts, path status, line distance, title/body
similarity, token overlap, and threshold margin. It classifies rows such as
`no_comparable_local_run`, `watch_only_no_finding`, `path_mismatch`, and
`near_below_threshold`. It is read-only and uses body digests by default; use
`--show-text` only for a local-only excerpt view.

`llreview training-export-splitter` exports only `training_ready=true` review
gap examples that no longer require a human gate. It writes train/val/test JSONL
artifacts under `out/review-history/training-export/`. By default it does not
include raw diff/code/body text, raw paths, `.private_docs` context, or
generated/snapshot paths; path information is reduced to `path_class` and
`path_digest`. Use `--include-paths` or `--include-title-excerpts` only for an
explicit local training run that needs those fields.

`llreview rule-candidate-extractor` groups training-ready `missed_by_local`
examples by path class, title tokens, and known mechanical families. It writes
Markdown/JSON artifacts under `out/review-history/rule-candidate-extractor/`.
By default it excludes human-gate rows and raw body/diff text. Families such as
`path_containment`, `shell_quoting`, `state_normalization`, and
`reserved_config` can become `proposed_rule_candidate`; weaker groups remain
prompt/watch evidence. The command does not edit rule code or prompt source.

`llreview learning-scoreboard` shows one read-only screen across the learning
pump, scoring pump, review-gap stamp pump, recall miner, watch sharpener, risk
gate, regression audit, backfill pump, matcher explain, training export, and
rule extractor. It reads DB aggregates and latest artifacts only; it does not
run reviews, import teacher artifacts, activate calibrations, or export raw
private text.

`llreview calibration-risk-gate` checks proposed prompt/rule candidates before
they become active DB calibrations. It writes Markdown/JSON artifacts under
`out/review-history/calibration-risk-gate/` with training-ready support,
human-gate backlog, false-positive counter-evidence, and missed
counter-evidence. The same gate appears immediately before
`learn-next --activate`, `learn-apply --activate`, and `learn-review`
activation. Blocked candidates are not activated unless the operator passes
`--force-risk`; daily auto activation skips blocked candidates.

`llreview prompt-regression-audit` checks active calibrations after they have
been used in later runs. It writes Markdown/JSON artifacts under
`out/review-history/prompt-regression-audit/` and checks whether same-scope
missed external items or local false positives decreased after activation.
Ineffective calibrations are reported as `stale_candidate`; the command does
not pause or retire them automatically.

`llreview score` selects the latest unscored run and records run-level counts.
In a TTY it also prompts for per-finding verdicts: `useful_fixed`,
`false_positive`, `unclear`, or `watch_only`. False positives keep a short
reason code such as `covered_by_existing_safeguard`, `intentional_behavior`,
`environment_dependent`, `covered_by_tests`, `stale_or_already_fixed`, or
`diagnostic_watch`. A single false positive is evidence, not an automatic
suppression rule; repeated reasons become prompt/local-rule candidates in
`llreview report`. When every finding in a run should be treated as non-blocking
watch/calibration evidence, use `llreview score --run <id> --demote-findings`
instead of stepping through every prompt. By default this saves run-level
`useful=0 / false_positives=<findings_count> / unclear=0` and item-level
`watch_only / diagnostic_watch`.

`llreview import-github-reviews 42` imports GitHub inline PR review comments
into `external_items`. It classifies Copilot, automated, and human reviewer
comments, then links them to local `review_items` with a loose match over
fingerprint, file, line, and normalized text. When local review run candidates
exist, linked external items get a `covered_by_local` external-side verdict and
unlinked external items get `missed_by_local`. If there is no local run
candidate, the importer does not invent missed verdicts.

Re-importing the same PR updates by GitHub comment id instead of duplicating
rows. It also removes stale GitHub-derived external items, importer links, and
importer verdicts that are no longer present in the current comment snapshot.
For reproducible checks, save a GitHub `/pulls/comments` JSON array and pass it
with `--comments-json comments.json --repo owner/name`; this preserves each
comment's GitHub `commit_id`. Use `--head-sha <sha>` only when intentionally
pinning the saved comments to a specific local run SHA. Add
`--include-issue-comments` only when top-level PR conversation comments should
also become learning items. In reproducible JSON mode, pass a saved GitHub
`/issues/comments` array with `--issue-comments-json issue-comments.json`.

`llreview import-github-history --dry-run` previews historical merged PRs and
local git history as possible learning candidates. Remote GitHub scanning uses
`--remote-*` limits for API volume, while local git scanning uses separate
`--local-*` limits for local CPU/output volume. Local scans do not require a
token, and repositories whose preferred GitHub remote is not owned by `mt4110`
are blocked with `skipped_owner_not_mt4110`. Add `--refresh-queue` only when you
want to persist skip reasons in `github_backfill_queue`. Add `--one` to process
exactly one queued remote candidate; `--one --dry-run` shows the selected PR and
import/link counts without writing external items, and real imports keep the
default 20-minute limiter.
`llreview backfill-pump` is the daily-friendly wrapper for that queue. It writes
before/after, rate-gate, next-candidate, and external-item delta artifacts to
`out/review-history/backfill-pump/`. It is report-only by default; add
`--import-one` to process one eligible `remote_github` row, and add `--dry-run`
to fetch/match without writing external items, links, verdicts, or queue state.
`llreview report` and `llreview export-jsonl` also include queue state and skip
reasons so skipped/deferred candidates stay visible to the learning loop.

`llreview learn-preview` shows the aggregate calibration that will be fed into
the next review. Normal `llreview` runs automatically add that calibration to
the reviewer prompt, but only as aggregate counts: verdict reasons, external
verdicts, path classes, and queue state. Raw comments and raw diffs are not
injected. Use `llreview --no-history-calibration` to disable this feedback path.
The loop tunes skepticism and priority from repeated evidence; it does not
rewrite prompts or rules from one-off anecdotes.

`llreview learn-candidates` derives `prompt_candidate`, `rule_candidate`, and
`needs_data` rows from aggregate evidence. Candidates include evidence count,
path class, reason/source, confidence, status, and a recommended action. They
use `proposed` for previews and `active` for already operator-approved DB
calibrations that future prompts can use. Prompt and rule source files are not
edited. The list shows both a short ID and a row number. `llreview
learn-candidates --inspect` shows the top candidate, `--inspect 2` shows row 2,
and `--inspect <candidate-id>` shows a specific candidate. By default it hides
full body text and shows a body digest; add `--show-text` only for short
local-only excerpts. External item samples include shortcut commands for
`external-verdict --candidate <candidate-id> --sample <n>`. Add `--all-repos` to
include deleted-repository or cross-repository queue signals. `llreview report`
and `llreview export-jsonl` include the same candidate preview.

`llreview learn-review` is the stamp-and-approve flow. It does not run the local
reviewer or app-developer teacher review; run `llreview daily` first, or
`llreview daily --force-review` when you want a fresh local review. By default it
shows only candidates still waiting for a stamp and keeps the output compact. For
teacher/external samples, press `y` for a valid missed item, `c` when it was
covered locally, `f` when it is not actionable, `n` when unsure, `s` to skip, or
`q` to quit. For prompt/rule candidates, pressing `y` after the instruction
preview and Calibration Risk Gate writes an active DB calibration that affects
future review prompts; press `v` to view the preview, or `s` to skip. Use
`llreview learn-review --no-activate` for a stamp-only pass. It hides full body
text and body digests by default; use `--verbose` or `--include-active` only
when you want the longer audit view. Use `llreview learn-review --dry-run` to
preview the queue.

`llreview learn-propose --candidate <candidate-id>` writes deterministic
proposal markdown/json under `out/review-history/learning-proposals/`. A
proposal has `applied=false` and does not edit prompts or rules. It stores
candidate metadata, sample ids, body digests, title excerpts, guardrails, and
validation commands, but not raw body text. Pass `--force` to overwrite an
existing proposal.

`llreview learn-next` selects the highest-ranked `prompt_candidate` or
`rule_candidate`, writes or reuses its proposal, and shows the
active-calibration dry-run preview without changing the DB. Activate only after
review with `llreview learn-next --candidate <candidate-id> --activate`. To fold
that activation into the daily loop, use `llreview daily --auto-activate-learning`.
Add `--include-needs-data` only when previewing data-collection candidates. In
that mode, `pending` data candidates are preferred over `deferred` ones, and
`needs_data` remains preview-only with no activation prompt.

`llreview learn-apply --proposal <proposal-id> --dry-run` previews the active
calibration that would be created from a proposal. Only `--activate` writes
`status=active` into `learning_calibrations`. Prompt/rule source files are not
changed. Normal `llreview` runs automatically include active calibrations in the
next review prompt by repo/path-class scope.

`llreview learn-audit` summarizes runs, missed external items, and local false
positives after an active calibration was created. This is a lightweight
post-activation check and the entrypoint for later `pause` / `retire`
recommendations.

`llreview export-jsonl` writes local review items, imported external items,
backfill queue items, learning candidates, and learning calibrations. Local
records include `prompt_hash`, `model_options_hash`, `diff_fingerprint`, and
`path_class`. If trusted context or review-history calibration was used for the
run, each local record also includes `context_digests` and
`history_calibration_digests` sha256 lists. External records include the GitHub
comment id, source, linked local item ids, external-side verdict, and
`path_class`. Learning candidates are exported as preview records with
`applied=false`; learning calibrations are DB records activated by
`learn-apply`.

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
