# ローカル LLM 停止手順書

この手順書は、Mac の self-hosted runner 上で動いている Ollama / Qwen3-Coder を安全に止めたい場合の runbook です。

目的は 3 つです。

- GitHub Actions の review job が途中で壊れた状態にならないようにする。
- Ollama の model memory / server process を意図した粒度で止める。
- 復旧時に `local-ai-review` workflow がすぐ再開できる状態を確認する。

## 判断基準

まず、何を止めたいのかを決めます。

| やりたいこと | 推奨手順 |
|---|---|
| GPU / memory を空けたいだけ | model unload |
| local review を一時停止したい | PR label を外す、または runner を止める |
| Ollama server 自体を止めたい | Ollama app / service を停止 |
| model file をディスクから消したい | `ollama rm` |
| Mac ログイン時の自動起動も止めたい | Login Items から Ollama を無効化 |

普段は `model unload` または `local-ai-review` label を外すだけで十分です。Ollama server まで止めるのは、runner メンテナンス、Mac 再起動、メモリ圧迫、異常プロセス調査のときに限定します。

## 0. 事前確認

現在 Ollama が動いているかを確認します。

```sh
curl -fsS http://127.0.0.1:11434/api/tags >/dev/null \
  && echo "Ollama is running" \
  || echo "Ollama is not responding"
```

稼働中の model を確認します。

```sh
ollama ps
```

GitHub Actions の実行中 job を確認します。

```sh
gh run list --limit 10
```

特定 repository で確認する場合:

```sh
gh run list --repo OWNER/REPO --limit 10
```

## 1. 新規 review 起動を止める

一番安全なのは、先に workflow の入口を閉じることです。

対象 PR から trigger label を外します。

```sh
gh pr edit PR_NUMBER \
  --repo OWNER/REPO \
  --remove-label local-ai-review
```

既に走っている job がある場合は、完了を待つか cancel します。

```sh
gh run list --repo OWNER/REPO --limit 10
gh run cancel RUN_ID --repo OWNER/REPO
```

runner 全体を止める場合は、先に GitHub repository settings または runner host 側で self-hosted runner を停止します。runner を止めると、この runner に依存する他の workflow も止まる点に注意してください。

## 2. model だけ unload する

Ollama server は残し、読み込まれている model だけ memory から落とします。通常はこの手順で十分です。

```sh
ollama stop qwen3-coder:30b-a3b-q4_K_M
```

確認します。

```sh
ollama ps
```

`qwen3-coder:30b-a3b-q4_K_M` が表示されなければ unload 済みです。

`ollama stop` が使えない環境では、API で unload できます。

```sh
curl -fsS http://127.0.0.1:11434/api/generate \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen3-coder:30b-a3b-q4_K_M",
    "prompt": "",
    "keep_alive": 0
  }'
```

## 3. Ollama server を止める

Ollama の起動方法ごとに手順が違います。

### macOS app として起動している場合

GUI で止める場合:

1. macOS menu bar の Ollama icon を開く。
2. Quit Ollama を選ぶ。

terminal から止める場合:

```sh
osascript -e 'quit app "Ollama"'
```

確認します。

```sh
curl -fsS http://127.0.0.1:11434/api/tags
```

接続できなければ停止済みです。

### Homebrew service として起動している場合

```sh
brew services list | grep -i ollama
brew services stop ollama
```

確認します。

```sh
brew services list | grep -i ollama
curl -fsS http://127.0.0.1:11434/api/tags
```

### terminal で `ollama serve` している場合

起動している terminal で `Ctrl-C` を押します。

別 terminal から確認する場合:

```sh
pgrep -af ollama
lsof -nP -iTCP:11434 -sTCP:LISTEN
```

通常停止できない場合だけ、PID を確認して終了します。

```sh
pkill -TERM -f 'ollama serve'
```

`kill -9` は最後の手段です。まずは `TERM` で止めてください。

## 4. model file を削除する場合

memory unload ではなく、disk 上の model file も削除したい場合だけ実行します。

```sh
ollama rm qwen3-coder:30b-a3b-q4_K_M
```

削除後は、再利用前に再 pull が必要です。

```sh
ollama pull qwen3-coder:30b-a3b-q4_K_M
```

model file の保存場所は macOS では通常 `~/.ollama/models` です。

## 5. macOS ログイン時の自動起動も止める

Ollama がログイン時に自動起動する設定になっていると、server を止めても次回ログイン時に復活します。長めに止めたい場合は、自動起動も無効化します。

1. macOS Settings を開く。
2. `Login Items` を検索する。
3. `Allow in the Background` にある `Ollama` を無効化する。

一時停止だけなら、この手順は不要です。

## 6. 復旧手順

Ollama を起動します。

macOS app の場合:

```sh
open -a Ollama
```

Homebrew service の場合:

```sh
brew services start ollama
```

terminal 起動の場合:

```sh
ollama serve
```

別 terminal で疎通確認します。

```sh
curl -fsS http://127.0.0.1:11434/api/tags
ollama list | grep 'qwen3-coder:30b-a3b-q4_K_M'
```

GitHub Actions review を再開する場合は、対象 PR に label を戻します。

```sh
gh pr edit PR_NUMBER \
  --repo OWNER/REPO \
  --add-label local-ai-review
```

## 7. smoke test

復旧後に、小さな PR で 1 回だけ確認します。

1. `local-ai-review` label を付ける。
2. workflow が `self-hosted`, `macOS`, `local-ai` runner で起動することを確認する。
3. PR diff が `MAX_DIFF_BYTES` 以下なら review comment が作成または更新されることを確認する。
4. PR comment が増殖せず、`<!-- local-llm-review -->` marker の既存コメントが更新されることを確認する。

## トラブルシュート

### `curl` が成功するのに review job が失敗する

runner 環境から localhost に到達できているか確認します。Mac に複数ユーザーや service 起動が混在している場合、Ollama は動いていても runner user から想定通り見えていないことがあります。

```sh
whoami
curl -v http://127.0.0.1:11434/api/tags
```

### port 11434 が残っている

```sh
lsof -nP -iTCP:11434 -sTCP:LISTEN
```

表示された process が Ollama であれば、起動方式に合わせて停止します。

### model が memory に残る

```sh
ollama ps
ollama stop qwen3-coder:30b-a3b-q4_K_M
ollama ps
```

それでも残る場合は、Ollama server を再起動します。

## 参照

- [Ollama macOS documentation](https://docs.ollama.com/macos)
- [Ollama FAQ](https://docs.ollama.com/faq)
- [Ollama API documentation](https://docs.ollama.com/api)
