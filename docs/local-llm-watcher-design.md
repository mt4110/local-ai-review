# ローカル LLM Watcher 設計

この文書は、ローカル AI review の起動状態を監視し、使用頻度が低いときは model / Ollama を落とし、必要なときだけ起こすための設計です。

重要な前提:

- MVP workflow には混ぜない。
- PR 由来のコードは引き続き実行しない。
- Mac 側に外部から入ってくる port を開けない。
- Discord webhook は通知専用として扱う。
- Discord から起動したい場合は、polling 型または IP allowlist 付き interactions endpoint に限定する。
- Discord interactions endpoint を使う場合も、IP allowlist だけに依存せず、Discord 署名検証を必須にする。

## 結論

`launchd` で常駐する watcher を 1 つ置きます。

watcher は定期的に以下を確認します。

- Ollama server が生きているか。
- `qwen3-coder:30b-a3b-q4_K_M` が memory に載っているか。
- GitHub PR に `local-ai-review` label があるか。
- 対象 workflow が queued / in_progress / failed になっていないか。
- 最後の review からどれくらい idle か。
- Discord command polling が有効なら、許可された channel / user からの command があるか。

通知は Discord webhook に投げます。

```text
Mac mini
  |
  | launchd
  v
local-ai-review-watcher
  |
  | GitHub API: PR labels / workflow runs
  | Ollama API: localhost only
  | optional Discord command intake
  v
Discord webhook notification
```

## なぜ crontab ではなく launchd か

crontab でも可能です。ただし Mac では `launchd` の方が運用に向いています。

| 方式 | 向いている用途 |
|---|---|
| crontab | 5分ごとの軽い定期確認 |
| launchd StartInterval | 常駐に近い定期 watcher |
| launchd KeepAlive | 落ちたら自動復帰させたい watcher |

最初は `launchd StartInterval=60` で十分です。常時プロセスとして loop し続けるより、1分ごとに短く起動して終了する方が壊れにくいです。

## 状態モデル

watcher は状態を小さく保ちます。

| State | 意味 |
|---|---|
| `offline` | Ollama server が応答しない |
| `ready` | Ollama server は応答するが model は未ロード |
| `loaded` | model が memory に載っている |
| `reviewing` | local review workflow が実行中 |
| `cooling_down` | review 後の猶予時間 |
| `stopped` | watcher が意図的に model / server を止めた |

状態は local file に保存します。

```text
~/.local/state/local-ai-review-watcher/state.json
```

保存する情報:

- `last_seen_at`
- `last_review_started_at`
- `last_review_finished_at`
- `last_command_id`
- `last_notified_state`
- `stopped_by_watcher`

## 停止ポリシー

まずは model unload までに留めます。

```text
review 実行中なら何もしない
queued workflow があるなら何もしない
local-ai-review label 付き open PR があるなら何もしない
最後の review から IDLE_UNLOAD_MINUTES を超えたら ollama stop
さらに IDLE_SERVER_STOP_MINUTES を超えたら Ollama server 停止
```

初期値:

| Name | Default | 意味 |
|---|---:|---|
| `WATCH_INTERVAL_SECONDS` | `60` | watcher の確認間隔 |
| `IDLE_UNLOAD_MINUTES` | `20` | model unload までの idle 時間 |
| `IDLE_SERVER_STOP_MINUTES` | `0` | server 停止は初期無効。`0` は無効 |
| `WATCH_REPOS` | empty | 監視対象 repo の comma-separated list |
| `WATCH_LABEL` | `local-ai-review` | 起動対象 label |
| `OLLAMA_MODEL` | `qwen3-coder:30b-a3b-q4_K_M` | unload 対象 model |

普段の推奨は `IDLE_SERVER_STOP_MINUTES=0` です。Ollama server は残し、model だけ落とす運用が一番なめらかです。

## 起動ポリシー

起動は 2 段階に分けます。

### 1. Ollama server wake

Ollama server が落ちていて、起動対象 PR がある場合だけ起こします。

macOS app の場合:

```sh
open -a Ollama
```

Homebrew service の場合:

```sh
brew services start ollama
```

起動後は API を確認します。

```sh
curl -fsS http://127.0.0.1:11434/api/tags
```

### 2. model preload

model を事前ロードしたい場合だけ実行します。

```sh
curl -fsS http://127.0.0.1:11434/api/generate \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen3-coder:30b-a3b-q4_K_M",
    "prompt": "",
    "keep_alive": "20m"
  }'
```

最初は preload なしでも構いません。workflow 側の review request で自動ロードされます。

## Discord 通知

Discord webhook は通知専用です。

通知する event:

- watcher started
- Ollama offline
- Ollama recovered
- model unloaded
- review queued
- review started
- review finished
- workflow failed
- command rejected

通知例:

```text
local-ai-review watcher
repo: mt4110/geo-line-ranker
pr: #20
state: reviewing
model: qwen3-coder:30b-a3b-q4_K_M
diff_bytes: 308372
```

Discord webhook URL は secret として扱います。repository に書かず、runner host の環境変数か local config に置きます。

```text
DISCORD_WEBHOOK_URL
```

## Discord から起動する場合

Discord webhook だけでは Discord から command を受け取れません。Discord から起動したい場合は、次のどちらかにします。

初期実装で許可する command は 2 つだけにします。

| Command | Action |
|---|---|
| `status` | watcher / Ollama / model / workflow の状態を返す |
| `wake-if-down` | Ollama server が落ちている場合だけ起動を試みる |

`wake-if-down` は「落ちていたら起こす」だけです。起動済みなら何もしません。PR checkout、PR code 実行、任意 shell 実行、workflow file 変更、model 削除はすべて禁止です。

最初は `unload`, `sleep`, `label add` も入れません。運用が安定してから opt-in で増やします。

### 固定気味の自宅グローバル IP の使いどころ

自宅回線のグローバル IP がほぼ固定なら、かなり便利です。ただし、何を IP で縛っているのかを分けます。

使ってよい場所:

- public command queue / small endpoint が、自宅 Mac watcher からの polling / ack だけを許可する。
- 管理 dashboard を自宅 IP からだけ見られるようにする。
- SSH / metrics / health endpoint を自宅 IP に限定する。
- GitHub webhook relay などを置く場合に、自宅 IP からの管理操作だけ許可する。

使ってはいけない判断:

- Discord interactions の送信元が自宅 IP になる、と考える。
- 自宅 IP 固定を Discord command の認証そのものにする。
- IP allowlist だけで command 実行を許可する。

Discord から interactions endpoint に届く request の source IP は Discord 側の infrastructure です。自宅 IP が固定気味でも、Discord request の認証にはなりません。

したがって、自宅 IP は「自宅 Mac / watcher / 管理者操作を絞る補助」に使い、Discord command には以下を必須にします。

- Discord request signature verification
- guild / channel / user allowlist
- command allowlist
- rate limit
- command queue 分離

この切り分けなら、自宅 IP の安定性を活かしつつ、Discord 側の操作口も安全にできます。

### 初期実装では review trigger を Discord command に含めない

初期実装の Discord command は `status` と `wake-if-down` だけにします。PR review の起動、つまり `local-ai-review` label の付与は GitHub UI または `gh` で人間が行います。

例:

```sh
gh pr edit 20 \
  --repo mt4110/geo-line-ranker \
  --add-label local-ai-review
```

この操作は Discord command からは実行しません。review trigger は GitHub の監査ログに残し、Discord 側は状態確認と Ollama 起動確認に閉じます。

### opt-in A: Discord bot polling

どうしても Discord から命令したい場合は、Mac 側 watcher が Discord API を polling します。Mac に inbound port は開けません。

command 例:

```text
!local-ai status
!local-ai wake-if-down
```

必須 guard:

- `DISCORD_BOT_TOKEN` は runner host の local secret に置く。
- 許可 channel ID を固定する。
- 許可 user ID を固定する。
- command prefix を固定する。
- 最後に処理した message ID を state に保存し、二重実行しない。
- `wake-if-down` は Ollama server 起動だけに限定する。
- 起動済みなら no-op にする。
- shell command を Discord から直接実行しない。
- command 引数を shell に渡さない。

この方式でも、PR code checkout / build / test は禁止のままです。

### opt-in B: Discord interactions endpoint

IP allowlist で縛れるなら、Discord slash command / button から起動する endpoint も使えます。

ただし、この endpoint は「任意 command 実行口」ではなく、「固定 command を command queue に積むだけ」の入口にします。

```text
Discord slash command
  |
  | HTTPS POST
  | source IP allowlist
  | Discord signature verification
  v
command intake endpoint
  |
  | append allowed command only
  v
command queue
  |
  | local watcher polls or reads queue
  v
Ollama status / wake-if-down notify
```

必須 guard:

- HTTPS のみ。
- source IP allowlist を設定する。
- Discord の request signature を必ず検証する。
- timestamp が古い request は拒否する。
- 許可 guild ID / channel ID / user ID を固定する。
- command は allowlist に限定する。
- command 引数を shell に渡さない。
- endpoint は Ollama に直接 request しない。
- endpoint は runner 上で PR code / repository script を実行しない。
- endpoint は command queue に `status` または `wake-if-down` だけを積む。
- watcher 側が queue を読んで実行する。

IP allowlist は有効ですが、それだけを認証にしてはいけません。IP range の変更、proxy、設定ミスに備えて、署名検証と user allowlist を必須にします。

command 例:

```text
/local-ai status
/local-ai wake-if-down
```

初期実装では `/local-ai wake-if-down` の実行内容を次に限定します。

1. Ollama server の health check を行う。
2. 応答がなければ、許可された起動方式で Ollama server を起こす。
3. 起動後に health check を再実行する。
4. 結果を Discord に通知する。

`wake-if-down` で workflow file を変更したり、PR branch を checkout したり、test を実行したり、GitHub label を変更したりしてはいけません。

#### endpoint を Mac に置くか

Mac に直接 endpoint を置くことは可能ですが、推奨は弱めです。

より安全な構成:

```text
Discord
  |
  v
small public endpoint
  |
  | command queue / GitHub issue comment / repository_dispatch
  v
Mac watcher polling
```

Mac 側は外へ polling するだけにすると、家庭内 network / runner host に inbound port を開けずに済みます。

Mac に直接置く場合は、router / firewall / reverse proxy のすべてで path と IP を絞り、endpoint process は runner user と分離してください。

自宅 IP がほぼ固定でも、Mac 直置き endpoint は「外から自宅に届く公開口」です。使うなら、少なくとも次を必須にします。

- router port forward は endpoint port のみにする。
- reverse proxy で `/discord/interactions` だけ通す。
- Ollama `11434` は絶対に外へ出さない。
- runner user と endpoint user を分ける。
- endpoint は command queue 書き込みだけにする。
- watcher が queue を読んで、固定 action だけ実行する。

## GitHub 側の watcher 対象

watcher は GitHub API で以下だけを見ます。

- open PR list
- PR labels
- workflow runs
- optional: issue comments の marker

見ないもの:

- PR source code
- repository checkout
- PR branch の script
- GitHub secrets

## 失敗時の挙動

| Failure | Behavior |
|---|---|
| GitHub API failure | Discord に warning、次 interval で retry |
| Discord webhook failure | local log に記録、review は止めない |
| Ollama wake failure | Discord に alert、workflow rerun はしない |
| command parse failure | command rejected として通知 |
| unauthorized command | 無視または rejected 通知 |

## launchd 例

将来の実装では、次のような plist を使います。

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>dev.local-ai-review.watcher</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string>
    <string>python3</string>
    <string>/Users/YOU/dev/local-ai-review/scripts/local-ai-review-watcher.py</string>
  </array>
  <key>StartInterval</key>
  <integer>60</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/local-ai-review-watcher.out.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/local-ai-review-watcher.err.log</string>
</dict>
</plist>
```

## 実装順

1. Discord webhook 通知だけ実装する。
2. GitHub PR label / workflow run の監視を実装する。
3. idle 時の `ollama stop` を実装する。
4. Ollama server wake を実装する。
5. 必要なら Discord bot polling または IP allowlist 付き interactions endpoint を opt-in で実装する。

Discord command は最後でいいです。先に通知と idle unload が動くだけで、運用価値は十分あります。
