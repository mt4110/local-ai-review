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
- `scripts/backfill-pump-scheduler.py`: one-shot launchd-safe wrapper for `llreview backfill-pump`.
- `config/local-ai-review-watcher.env.example`: example local env file for the watcher.
- `config/local-ai-review-backfill-pump.env.example`: example env file for the backfill pump scheduler.
- `launchd/dev.local-ai-review.watcher.discord.plist.example`: launchd example for the Discord interactions endpoint.
- `launchd/dev.local-ai-review.backfill-pump.plist.example`: 20-minute launchd example for the backfill pump.
- `docs/local-llm-shutdown-runbook-en.md`: detailed runbook for stopping the local LLM.
- `docs/local-llm-watcher-design-en.md`: watcher, Discord notification, and idle unload design.
- `docs/local-llm-watcher-runtime-ops-en.md`: env file, Discord App, live status, and launchd operations.
- `docs/local-ai-precision-review-en.md`: file-by-file precision diff-only review workflow.
- `docs/local-ai-precision-review.md`: Japanese version of the precision review runbook.
- `sql/review-history-example-queries.sql`: example SQL for evaluating saved review runs.
- `docker-compose.review-db.yml`: Datasette compose file for browsing review history in a browser.
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

## Precision Review History (SQLite)

This section is for locally-run precision review history, not the label-triggered workflow. The precision reviewer uses this PR comment marker:

```text
<!-- local-ai-precision-review -->
```

To keep evaluation data in SQLite, use:

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

`pre-pr-review` stores branch diffs before PR creation as `review_kind=pre_pr`.
The DB also records the `BASE...HEAD` base/head, head SHA, and whether
uncommitted changes were included, which makes the run easier to compare with
the later remote review.

In pre-PR mode, `llreview` auto-loads Markdown files from the target workspace's
`.private_docs/` directory as compact trusted design context when that directory
exists. Context helps interpret visible diff evidence; it is not evidence by
itself. The run stores context document path and sha256 records in
`artifacts(kind='context_digest')`. Use `llreview --no-trusted-context` to
disable this, or `llreview --trusted-context-dir /path/to/.private_docs` to pass
an explicit trusted context directory.

Use `llreview target set --project-dir /path/to/repo --repo owner/name` to save
a local default target in `out/review-history/llreview-target.json`. After that,
short commands such as `llreview status`, `llreview`, `llreview learn-preview`,
and `llreview learn-candidates` automatically use that workspace/repository when
they are run from the tool repository. Explicit `--project-dir` and `--repo`
still win. Use `llreview target clear` to remove the saved target.

Use `llreview daily` as the normal daily loop. It prints `status`, runs the
normal review only when there is no previous run, the head SHA changed, or the
working tree is dirty, then writes a lightweight artifact-only calibration
report and prints `learn-preview` and `learn-candidates`. Use `--force-review`
to run anyway, `--no-review` to inspect only status and learning output, and
`--no-calibration` to skip the calibration artifact. After daily output, use
`llreview learn-review` when you want to quickly stamp only the items that need
human judgment. If you do not want to
manually activate learning candidates every time, explicit opt-in with
`llreview daily --auto-activate-learning` or
`LLREVIEW_DAILY_AUTO_ACTIVATE_LEARNING=1` activates at most one highest-ranked
proposed prompt/rule candidate as an active DB calibration. Use
`llreview learn-pump` to import completed teacher artifacts and collect the
unscored-run, external-stamp, link-diagnostic, and queue inboxes into one
operator report. To include that inbox in daily, pass
`llreview daily --learning-pump` or set `LLREVIEW_DAILY_LEARNING_PUMP=1`. The
unscored-run scoring inbox can also be included with
`llreview daily --scoring-pump` or `LLREVIEW_DAILY_SCORING_PUMP=1`. The
human-gate review-gap stamping inbox can be included with
`llreview daily --review-gap-stamp-pump` or
`LLREVIEW_DAILY_REVIEW_GAP_STAMP_PUMP=1`. Missed-by-local recall clustering can
be included with `llreview daily --recall-pattern-miner` or
`LLREVIEW_DAILY_RECALL_PATTERN_MINER=1`. Watch/finding boundary review can be
included with `llreview daily --watch-sharpener` or
`LLREVIEW_DAILY_WATCH_SHARPENER=1`. Activation risk review can be included with
`llreview daily --calibration-risk-gate` or
`LLREVIEW_DAILY_CALIBRATION_RISK_GATE=1`. Post-activation regression audit can
be included with `llreview daily --prompt-regression-audit` or
`LLREVIEW_DAILY_PROMPT_REGRESSION_AUDIT=1`. Historical review evidence fuel can
be included as a queue report with `llreview daily --backfill-pump` or
`LLREVIEW_DAILY_BACKFILL_PUMP=1`; to advance one rate-limited remote row, use
`llreview daily --backfill-pump-import-one` or
`LLREVIEW_DAILY_BACKFILL_PUMP_IMPORT_ONE=1`. Link-failure explanation can be
included with `llreview daily --matcher-explain` or
`LLREVIEW_DAILY_MATCHER_EXPLAIN=1`. Safe train/val/test export of
training-ready examples can be included with
`llreview daily --training-export-splitter` or
`LLREVIEW_DAILY_TRAINING_EXPORT_SPLITTER=1`. Deterministic rule-candidate
extraction can be included with `llreview daily --rule-candidate-extractor` or
`LLREVIEW_DAILY_RULE_CANDIDATE_EXTRACTOR=1`. The read-only learning scoreboard
can be included with `llreview daily --learning-scoreboard` or
`LLREVIEW_DAILY_LEARNING_SCOREBOARD=1`. The
heavy pass is not included by default; use `llreview daily --second-opinion` to
wait for it, or `llreview daily --async-second-opinion` to launch it as a
background job. When the terminal output is easy to miss, pass
`llreview daily --notify` to send a macOS local notification on completion,
failure, or interruption. Set `LLREVIEW_DAILY_NOTIFY=1` to enable it by default,
and use `--notify-sound Glass` or `LLREVIEW_NOTIFY_SOUND=Glass` when you want a
sound.
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

Use `llreview learn-pump` when you want the learning loop to keep moving without
manually stitching commands together. It imports completed app-developer jobs,
refreshes lightweight calibration artifacts, lists learning candidates,
surfaces external items needing an operator stamp, shows recent unscored runs,
and reports link health/diagnostics under
`out/review-history/learning-pump/`. It also writes
`latest-review-gap-examples.jsonl`, a feature dataset that records the distance
between teacher/external items and local finding/watch candidates, operator
verdicts, label quality, and the next learning target. Single teacher gaps that
have not reached the learning-candidate threshold still appear in the `Review
Gap Stamp Inbox` with ready-to-run `external-verdict` commands. This is the
bridge toward future review-specialized ML, but records are not training-ready
without the human gate. If a background teacher job is still running,
`llreview learn-pump --wait-app-developer-review 600` waits briefly and then
imports anything that finished. The command does not call another model, post PR
comments, check out or execute PR code, or fetch remote review data.

Use `llreview scoring-pump` to turn the unscored-run backlog into a small
operator inbox. It separates `quick_drain_zero_findings` runs from
`manual_finding_score` runs, writes ready-to-run `llreview score` commands, and
stores Markdown/JSON artifacts under `out/review-history/scoring-pump/`. The
default mode is read-only. Use `llreview scoring-pump --apply-zero-findings`
only when you want to write run-level feedback for zero-finding runs; runs with
findings should still be scored with `llreview score --run <id> --items` or
reviewed before using `--demote-findings`.

Use `llreview review-gap-stamp-pump` to turn human-gate review-gap examples
into a focused stamping inbox. It shows short rationale, deterministic stamp
assistance, local finding/watch distance, and ready-to-run `external-verdict`
commands under
`out/review-history/review-gap-stamp-pump/`. The default mode is read-only. Use
`llreview review-gap-stamp-pump --stamp` in a TTY for a continuous
`y` valid, `f` not actionable, `c` covered, `n` unsure, `s` skip, `q` quit flow.
Use `y` only when the item is diff-local and actionable.

Use `llreview recall-pattern-miner` to cluster `missed_by_local` review gaps by
path class, learning target, path bucket, and title-token similarity. It writes
weakness-pattern Markdown/JSON artifacts under
`out/review-history/recall-pattern-miner/`. This is prioritization evidence,
not a prompt or rule update by itself; use the training-ready/human-gate split
to choose what to stamp or investigate next.

Use `llreview watch-sharpener` to inspect review gaps where local watch items
existed but did not become concrete findings. It writes Markdown/JSON artifacts
under `out/review-history/watch-sharpener/`, shows the best or representative
watch item, and separates true watch/finding boundary rows from unrelated-watch
recall gaps. It never promotes watch items automatically.

Use `llreview matcher-explain` to explain why unlinked external items did not
match local findings. It writes Markdown/JSON artifacts under
`out/review-history/matcher-explain/` with comparable-run presence,
finding/watch candidate counts, path status, line distance, title/body
similarity, token overlap, and threshold margin. The report classifies gaps such
as `no_comparable_local_run`, `watch_only_no_finding`, `path_mismatch`, and
`near_below_threshold`. It is read-only and uses body digests by default; use
`--show-text` only for a local-only excerpt view.

Use `llreview training-export-splitter` to export only `training_ready=true`
review gap examples that no longer require a human gate. It writes train/val/test
JSONL artifacts under `out/review-history/training-export/`. By default it does
not include raw diff/code/body text, raw paths, `.private_docs` context, or
generated/snapshot paths; path information is reduced to `path_class` and
`path_digest`. Pass `--include-paths` or `--include-title-excerpts` only for an
explicit local training run that needs those fields.

Use `llreview rule-candidate-extractor` to group training-ready
`missed_by_local` examples by path class, title tokens, and known mechanical
families. It writes Markdown/JSON artifacts under
`out/review-history/rule-candidate-extractor/`. By default it excludes
human-gate rows and raw body/diff text. Families such as `path_containment`,
`shell_quoting`, `state_normalization`, and `reserved_config` can become
`proposed_rule_candidate`; weaker groups remain prompt/watch evidence. The
command does not edit rule code or prompt source.

Use `llreview learning-scoreboard` to view a single read-only screen across the
learning pump, scoring pump, review-gap stamp pump, recall miner, watch
sharpener, risk gate, regression audit, backfill pump, matcher explain,
training export, and rule extractor. It reads DB aggregates and latest
artifacts only; it does not run reviews, import teacher artifacts, activate
calibrations, or export raw private text.

Use `llreview db-plan` to open the SQLite review-history DB read-only and
dry-run PostgreSQL optional backend readiness. It records required table/view
presence, row counts, training-ready external examples, the PostgreSQL schema
draft digest, and optional backend gates under `out/review-history/db-plan/` as
Markdown/JSON artifacts. The default run does not copy rows, mutate the DB, or
change the default backend. `llreview db-plan --docker-parity` applies the schema to a
temporary PostgreSQL container, imports SQLite rows through temporary CSV files,
and verifies table-count parity. Raw CSV files are deleted by default; keeping
them requires explicit `--keep-parity-workdir`.

Use `llreview calibration-risk-gate` before activating prompt/rule candidates.
It writes Markdown/JSON artifacts under
`out/review-history/calibration-risk-gate/` with training-ready support,
human-gate backlog, false-positive counter-evidence, and missed counter-evidence.
The same gate runs immediately before `learn-next --activate`,
`learn-apply --activate`, and `learn-review` activation. Blocked candidates are
not activated unless the operator passes `--force-risk`; daily auto activation
skips blocked candidates.

Use `llreview prompt-regression-audit` after calibrations have been active for
a few runs. It writes Markdown/JSON artifacts under
`out/review-history/prompt-regression-audit/` and checks whether same-scope
missed external items or local false positives decreased after activation.
Ineffective calibrations are reported as `stale_candidate`; the command does
not pause or retire them automatically.

After import, record the operator judgment with commands such as
`llreview external-verdict <external_item_id> --verdict teacher_false_positive`
or `--verdict missed_by_local`. When you are scoring from a candidate
inspection, you can use the sample number instead:
`llreview external-verdict --candidate <candidate-id> --sample 1 --verdict missed_by_local ...`.

Use `llreview calibration` to write a file-first calibration artifact from the
existing SQLite history for the latest workspace run, or pass `--run <id>`.
It writes manifest, normalized item JSONL, alignment JSONL, verdict candidate
JSONL, and Markdown/JSON reports under `out/calibration/runs/<calibration-run-id>/`.
This step does not call another model, post PR comments, check out or execute PR
code, or fetch remote review data. In `daily`, it runs by default and stores
only artifact digests in `artifacts(kind='calibration_*')`.

Use `llreview backup` to save timestamped learning snapshots to iCloud or
another folder while keeping the live SQLite DB on local SSD. The command uses
SQLite's backup API, refreshes `export-jsonl`, copies
`review-items.<timestamp>.jsonl`, and copies `benchmark-report.md` when it
exists. Use `llreview backup --latest` to also update stable latest files, or
`llreview backup --dry-run` to inspect the planned paths. `llreview daily
--offer-backup` offers an interactive backup only when that daily run changed
learning rows.

Use `llreview second-opinion` for the heavy review-killer pass, not as the
default daily reviewer. It runs `qwen3-coder-next:q4_K_M` with a smaller context
and two model-reviewed files by default, checks estimated macOS physical memory
use against `--max-memory-percent` (90 by default), and skips the run unless the
budget fits. Use `--force` only when the machine is intentionally idle. The
command stops the heavy model after the run unless `--keep-loaded` is passed.
When launched through `daily`, the primary reviewer model is stopped before the
second-opinion gate so a still-loaded daily model does not unnecessarily block
the background pass.

Use `llreview daily --async-second-opinion` to pass the same memory gate and
start `second-opinion` as a background process. Daily returns the job id, PID,
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

Use `llreview score --run <id> --items` for per-finding scoring. When every
finding in a run should be treated as non-blocking watch/calibration evidence,
use `llreview score --run <id> --demote-findings` instead of stepping through
the interactive prompts. By default this saves run-level
`useful=0 / false_positives=<findings_count> / unclear=0` and item-level
`watch_only / diagnostic_watch`. Use explicit values such as
`--demote-verdict false_positive --demote-reason insufficient_context` when the
bulk verdict should be stricter.

Use `llreview import-github-reviews 42` to import GitHub inline PR review
comments into `external_items`. The importer classifies Copilot, automated,
and human reviewer comments, then links them to local `review_items` through a
loose match on fingerprint, file, line, and normalized text. Re-importing the
same PR updates rows by comment id instead of duplicating them, and removes
stale GitHub-derived external items that are no longer in the current comment
snapshot. Add `--include-issue-comments` only when top-level PR conversation
comments should also become learning items. For reproducible JSON imports, pass
saved issue comments separately with `--issue-comments-json`; use `--head-sha`
only when intentionally pinning the import to a specific local run SHA.

Use `llreview import-github-history --dry-run` to preview historical merged PRs
and local git history as possible learning candidates. Remote GitHub scanning is
limited with `--remote-repo-limit`, `--remote-pr-limit`, and
`--remote-per-repo-pr-limit`; local git scanning is limited separately with
`--local-repo-limit`, `--local-pr-limit`, and `--local-per-repo-pr-limit`.
Local scans do not require a GitHub API token, and repositories whose preferred
GitHub remote is not owned by `mt4110` are blocked with
`skipped_owner_not_mt4110`. Pass `--refresh-queue` only when you want to store
skip reasons in `github_backfill_queue`. Use `llreview import-github-history
--one` to import exactly one queued remote candidate. `--one --dry-run` shows the
selected PR and import/link counts without writing external items, and real
imports keep the default 20-minute limiter.
Use `llreview backfill-pump` as the daily-friendly wrapper: it writes before/after,
rate-gate, next-candidate, and external-item delta artifacts to
`out/review-history/backfill-pump/`. It is report-only by default; add
`--import-one` to process one eligible `remote_github` row, and add `--dry-run`
to fetch/match without writing external items, links, verdicts, or queue state.
`llreview report` and `llreview export-jsonl` also include queue state and skip
reasons, so skipped/deferred candidates remain part of the learning ledger.

Use `llreview specbackfill-overlap --run <run-id>` to preview deterministic
alignment between saved `review_items(source='specbackfill')` rows and existing
local model `review_items` / imported `external_items` in the same scope. Pass
`--specbackfill-json` only when you want JSON findings to override the saved DB
input. The report surfaces external-missed-by-local, external-covered-by-specbackfill,
model/specbackfill overlap, and specbackfill false-positive
verdict signals. External items already judged false-positive or not-actionable
are excluded from missed/covered signals. The command performs no DB writes,
GitHub API calls, PR checkout/code execution, PR comments, or raw
body/evidence/diff output.

Use `llreview specbackfill-import-preview --specbackfill-json specbackfill.json
--run <run-id>` to normalize `specbackfill check --format json --fail-on off`
findings into would-be `review_items(source='specbackfill')` rows without DB
writes. It reports the required run anchor, append ordinals, rule ids,
fingerprints, and evidence digests while hiding raw body text, raw evidence, and
raw diff text.

Use `llreview specbackfill-import-apply --specbackfill-json specbackfill.json
--run <run-id>` only when you intentionally want to store those deterministic
findings. The apply path reuses the preview candidates, inserts only
`would_insert` rows, skips existing fingerprints and duplicate input
fingerprints, and writes Markdown/JSON artifacts after the import. `--dry-run`
checks the apply plan without writing rows or artifacts.

Use `llreview learn-preview` to inspect the aggregate calibration that will feed
the next review. Normal `llreview` runs now add only aggregate review-history
signals to the reviewer prompt: verdict reasons, external verdict counts, path
classes, and queue state. Raw comments and raw diffs are not injected. Pass
`--no-history-calibration` to disable this feedback path. This is deliberately a
small learning loop: it adjusts review skepticism and priority from repeated
evidence, but it does not rewrite prompts or rules from one-off anecdotes.

Use `llreview learn-candidates` to derive `prompt_candidate`,
`rule_candidate`, and `needs_data` rows from that aggregate evidence. Candidates
carry evidence count, path class, reason/source, confidence, status, and a
recommended action. `proposed` rows are previews; `active` rows are already
operator-approved DB calibrations for future prompts. Prompt and rule source
files are not edited. The list shows both a short ID and a row number. Use
`llreview learn-candidates --inspect` for the top candidate, `--inspect 2` for
row 2, or `--inspect <candidate-id>` for a specific candidate. By default it
hides full body text and shows a body digest; add `--show-text` only when you
want short local-only excerpts. External item samples include shortcut commands
for `external-verdict --candidate <candidate-id> --sample <n>`. Add
`--all-repos` to include deleted-repository or cross-repository queue signals.
`llreview report` and `llreview export-jsonl` include the same candidate preview.

Use `llreview learn-review` for the stamp-and-approve flow. It does not run the
local reviewer or app-developer teacher review; run `llreview daily` first, or
`llreview daily --force-review` when you want a fresh local review. By default it
shows candidates still waiting for a stamp plus human-gate review gaps, while
keeping the output compact. For teacher/external samples, press `y` for a valid missed item,
`c` when it was covered locally, `f` when it is not actionable, `n` when unsure,
`s` to skip, or `q` to quit. The interactive prompt shows deterministic stamp
assistance by default: whether the item is already operator-stamped, how much
the same repo/source/path-class bucket has learned, the local finding/watch link
diagnostics, and a recommended stamp with a reason. This is guidance, not
truth; teacher/external output still requires human judgment. Use
`--no-assist` to hide it, `--no-review-gap-stamps` to keep review-gap stamping
in its separate inbox, or `llreview stamp-assist <external_item_id>` for a standalone
check. For prompt/rule candidates, pressing `y` after the instruction preview
and Calibration Risk Gate writes an active DB calibration that affects future
review prompts; press `v` to view the preview, or `s` to skip. Use
`llreview learn-review --no-activate` for a stamp-only pass. It hides full body
text and body digests by default; use `--verbose` or `--include-active` only
when you want the longer audit view. Use `llreview learn-review --dry-run` to
preview the queue.

Use `llreview learn-propose --candidate <candidate-id>` to write deterministic
proposal markdown/json under `out/review-history/learning-proposals/`. A
proposal has `applied=false` and does not edit prompts or rules. It stores
candidate metadata, sample ids, body digests, title excerpts, guardrails, and
validation commands, but not raw body text. Pass `--force` to overwrite an
existing proposal.

Use `llreview learn-next` to select the highest-ranked `prompt_candidate` or
`rule_candidate`, write or reuse its proposal, and show the active-calibration
dry-run preview without changing the DB. Activate only after review with
`llreview learn-next --candidate <candidate-id> --activate`. To fold that
activation into the daily loop, use `llreview daily --auto-activate-learning`.
Add `--include-needs-data` only when you want to preview data-collection
candidates. In that mode, `pending` data candidates are preferred over
`deferred` ones, and `needs_data` remains preview-only with no activation prompt.

Use `llreview learn-apply --proposal <proposal-id> --dry-run` to preview the
active calibration that would be created from a proposal. Only `--activate`
writes `status=active` into `learning_calibrations`. Prompt/rule source files
are not changed. Normal `llreview` runs automatically include active
calibrations in the next review prompt by repo/path-class scope.

Use `llreview learn-audit` to summarize runs, missed external items, and local
false positives after an active calibration was created. This is a lightweight
post-activation check and the entrypoint for later `pause` / `retire`
recommendations.

`make review-db-web` starts Datasette in Docker in the background and opens the DB at `http://127.0.0.1:8003`. Datasette defaults to `8001`, so this repo binds `8003` to stay two ports above the default. Stop it with `make review-db-down`. Datasette is intentionally read-only here, so manual scoring goes through `make review-db-score ...`. If you prefer a desktop client, open `out/review-history/local-ai-review.db` in DBeaver.

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
