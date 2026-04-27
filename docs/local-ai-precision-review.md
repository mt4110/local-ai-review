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

## Interpreting Output

`Findings` should be actionable enough to comment on a PR.

`Watch Items` are not findings. They are runtime or manual verification points,
such as container smoke tests after read-only filesystem hardening.
