# 06. Operations Runbook

## Mac mini setup

### Install Ollama

```bash
brew install ollama
```

or install from the official Ollama site.

### Pull primary model

```bash
ollama pull qwen3-coder:30b-a3b-q4_K_M
```

Optional:

```bash
ollama pull devstral-small-2:24b-instruct-2512-q4_K_M
```

### Start Ollama

```bash
ollama serve
```

Confirm:

```bash
curl http://127.0.0.1:11434/api/tags
```

## GitHub runner setup

1. Create a dedicated macOS user.
2. Install GitHub Actions runner under that user.
3. Register runner with labels:

```text
self-hosted
macOS
local-ai
```

4. Do not store cloud credentials or SSH keys under this user.

## First workflow test

1. Copy `workflows/local-llm-review-diff-only.yml` to:

```text
.github/workflows/local-llm-review.yml
```

2. Create label:

```text
local-ai-review
```

3. Add label to a small PR.
4. Confirm PR comment appears.
5. Check runner logs.

## Model tuning

Initial:

```text
OLLAMA_MODEL=qwen3-coder:30b-a3b-q4_K_M
OLLAMA_NUM_CTX=65536
MAX_DIFF_BYTES=350000
```

If stable:

```text
OLLAMA_NUM_CTX=98304
MAX_DIFF_BYTES=500000
```

If still stable:

```text
OLLAMA_NUM_CTX=131072
MAX_DIFF_BYTES=600000
```

Do not jump straight to 256K context for daily review.

## Daily operation

- Normal PR: add `local-ai-review`.
- Risky PR: local review + Copilot/Gemini.
- Critical PR: local review + Copilot/Gemini + ChatGPT/Claude/Codex.
- Huge PR: split first.

## Troubleshooting

### No runner picked up the job

Check labels:

```text
self-hosted, macOS, local-ai
```

### Ollama timeout

Reduce:

```text
MAX_DIFF_BYTES
OLLAMA_NUM_CTX
MAX_FINDINGS
```

### Comment not posted

Check:

- `issues: write` permission
- `pull-requests: write` permission
- GitHub token availability

### Mac becomes slow

Reduce context and parallelism.
Avoid running Q8 models concurrently.

## Maintenance

Weekly:

- Review workflow logs.
- Clean runner working directory.
- Check Ollama model disk usage.
- Confirm no secrets are present in runner environment.

Monthly:

- Benchmark models.
- Review false positive / false negative rate.
- Decide whether to adjust prompt or model.
