# Local LLM Shutdown Runbook

This runbook explains how to safely stop Ollama / Qwen3-Coder on a Mac self-hosted runner.

Use the smallest shutdown scope that solves the problem:

| Goal | Recommended action |
|---|---|
| Free memory only | Unload the model |
| Pause local reviews | Remove the `local-ai-review` PR label or stop the runner |
| Stop the local server | Quit the Ollama app or service |
| Delete model files | Run `ollama rm` |
| Stop login auto-start | Disable Ollama in Login Items |

## 1. Stop New Review Runs

Remove the trigger label from the target PR:

```sh
gh pr edit PR_NUMBER \
  --repo OWNER/REPO \
  --remove-label local-ai-review
```

Cancel an already running workflow if needed:

```sh
gh run list --repo OWNER/REPO --limit 10
gh run cancel RUN_ID --repo OWNER/REPO
```

## 2. Unload Only the Model

Keep Ollama running, but unload the model from memory:

```sh
ollama stop qwen3-coder:30b-a3b-q4_K_M
ollama ps
```

If needed, unload through the API:

```sh
curl -fsS http://127.0.0.1:11434/api/generate \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen3-coder:30b-a3b-q4_K_M",
    "prompt": "",
    "keep_alive": 0
  }'
```

## 3. Stop Ollama

If Ollama is running as a macOS app:

```sh
osascript -e 'quit app "Ollama"'
```

If Ollama is running through Homebrew services:

```sh
brew services list | grep -i ollama
brew services stop ollama
```

If Ollama was started with `ollama serve`, stop it with `Ctrl-C` in that terminal. If needed:

```sh
pgrep -af ollama
lsof -nP -iTCP:11434 -sTCP:LISTEN
pkill -TERM -f 'ollama serve'
```

Use `kill -9` only as a last resort.

## 4. Delete Model Files

Only run this if you want to remove the model from disk:

```sh
ollama rm qwen3-coder:30b-a3b-q4_K_M
```

Pull it again before the next review:

```sh
ollama pull qwen3-coder:30b-a3b-q4_K_M
```

## 5. Disable Login Auto-Start

If Ollama is configured as a login item, it may start again the next time the Mac user logs in.

1. Open macOS Settings.
2. Search for `Login Items`.
3. Disable `Ollama` under `Allow in the Background`.

Skip this for a short maintenance pause.

## 6. Restart

macOS app:

```sh
open -a Ollama
```

Homebrew service:

```sh
brew services start ollama
```

Manual server:

```sh
ollama serve
```

Verify:

```sh
curl -fsS http://127.0.0.1:11434/api/tags
ollama list | grep 'qwen3-coder:30b-a3b-q4_K_M'
```

Re-enable review on a PR:

```sh
gh pr edit PR_NUMBER \
  --repo OWNER/REPO \
  --add-label local-ai-review
```

## References

- [Ollama macOS documentation](https://docs.ollama.com/macos)
- [Ollama FAQ](https://docs.ollama.com/faq)
- [Ollama API documentation](https://docs.ollama.com/api)
