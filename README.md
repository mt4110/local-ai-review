# local-ai-review

**Local Data Gravity × Global Bug Immune Network × OSS-grade Trust**

GitHub Actions の self-hosted runner で動かす、diff-only のローカル PR レビュー基盤です。自社コードを外に出さず、ローカルで安全にレビューを実行します。

英語版: [README_EN.md](README_EN.md)

## Current Status

**Implemented:**
- local diff-only review
- SQLite Review DB
- calibration/scoring/backfill basics
- dashboard scaffold

**Beta:**
- local learning loop
- team memory ingestion
- PostgreSQL planning

**Planned:**
- encrypted SQLite by default
- installed llreview workflow
- Evidence Preview
- signed .ipack packs
- PostgreSQL Team Backend

## Why local-first

クラウドAIに機密なソースコードを送信することなく、ローカルLLM（Ollama + Qwen3-Coder等）を用いて安全にレビューを行います。
これにより、以下のメリットが得られます：
- **Data Privacy**: PRのコードが外部に流出しない。
- **Zero Cost**: 外部APIの課金が発生しない。
- **Local Data Gravity**: 組織内の暗黙知や過去のレビュー履歴をローカルの SQLite (Review DB) に蓄積し、独自ルールの学習（Bug Immune Network）に繋げる。

## Quickstart

### 1. インストール
ローカルの PATH が通ったディレクトリに `llreview` をインストールします。
```sh
./llreview install
```

### 2. 環境の診断
`llreview doctor` コマンドで、実行環境（Python, Ollama, Git, DB等）が正しくセットアップされているか確認します。
```sh
llreview doctor
```

### 3. Ollama とモデルの準備
```sh
ollama pull qwen3-coder:30b-a3b-q4_K_M
```

## Safety (セキュリティ契約)

本基盤は安全性を最優先に設計されています。
- `pull_request_target` でのみ実行する。
- PR に `local-ai-review` ラベルが付いている場合だけ実行する。
- `self-hosted`, `macOS`, `local-ai` ラベルを持つ self-hosted runner で実行する。
- `actions/checkout` を使わない。外部 Action を使わない。
- PR 由来のコード、repository script、build、test、package install を実行しない。
- repository secrets を job に渡さない。
- Ollama に送るのは PR diff text のみとする。
- model prompt では diff を未信頼 text として扱う。
- 再実行のたびにコメントを増やさず、marker 付きコメントを更新する。
- watcher は MVP workflow と分離し、Discord からは `status` / `wake-if-down` だけを受け付ける。

## Daily use

日常的なレビューワークフローは `llreview daily` に集約されています。

```sh
llreview daily
```
このコマンドは以下を行います：
- 変更の自動検知とレビュー実行
- 軽量なキャリブレーションと学習候補の提示
- ターミナル上で80行以内に収まる簡潔な出力

手動で採点や学習を行う場合は、受信箱を確認します。
```sh
llreview scoring-pump
llreview score --run <run-id>
```

PR上でレビューを行う場合は、DraftではないPRに `local-ai-review` ラベルを付けます。GitHub Actionsが自動で動作し、PRにコメントを投稿します。

## Commercial boundary

**OSS Core (本リポジトリ)**
- ローカルLLMを用いたPRレビュー基盤
- SQLite を用いたレビュー履歴の蓄積とローカル学習ループ
- 基本的なプロンプトやルール生成
- 永久に無料でフル機能が利用可能

**Review Immune Pro (Enterprise / Commercial Pack)**
- 世界中のバグ・レビュー知見から抽象化された商用パックの同期
- オフラインでのライセンス運用とエアギャップ環境でのアップデート
- エンタープライズ向けの監査ログエクスポートとロックダウンモード
*(※ Pro Pack の機能は現在設計・ベータ段階です)*

## Troubleshooting

**Q: `llreview doctor` で Ollama が FAIL になる**
A: Ollama が起動しているか、`OLLAMA_BASE_URL` (デフォルト: `http://127.0.0.1:11434`) が正しいか確認してください。

**Q: `MAX_DIFF_BYTES` を超えてレビューがスキップされた**
A: 巨大なPRはローカルLLMのコンテキスト長を圧迫するためスキップされます。PRを分割するか、対象PRのみ上限を引き上げてください。

**Q: ローカルLLMを完全に停止したい**
A: [docs/local-llm-shutdown-runbook.md](docs/local-llm-shutdown-runbook.md) を参照し、`ollama stop` やランナーの停止を行ってください。

---

## Advanced Usage & Reference

### ファイル
- `.github/workflows/local-llm-review.yml`: MVP の GitHub Actions workflow。
- `scripts/verify-workflow-policy.py`: workflow の静的セキュリティチェック。
- `scripts/local-ai-review-watcher.py`: `status` / `wake-if-down` だけを実行する watcher。
- `scripts/llreview.py`: local CLI core.

### Workflow 設定
初期値は安定性を優先して控えめにしています。

| Name | Default |
|---|---:|
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` |
| `OLLAMA_MODEL` | `qwen3-coder:30b-a3b-q4_K_M` |
| `OLLAMA_NUM_CTX` | `65536` |
| `OLLAMA_TEMPERATURE` | `0.1` |
| `OLLAMA_TIMEOUT_SECONDS` | `1800` |
| `MAX_DIFF_BYTES` | `350000` |
| `MAX_FINDINGS` | `8` |

### 豊富な CLI コマンド
`llreview` は高精度なローカルレビューのためのコマンドを多数備えています。
`llreview status`, `llreview target set`, `llreview backup`, `llreview score`, `llreview import-github-history` などの詳細は [docs/local-ai-precision-review.md](docs/local-ai-precision-review.md) を参照してください。

### Watcher
Ollama が落ちている場合だけ起こす:
```sh
python3 scripts/local-ai-review-watcher.py wake-if-down
```
詳細は [docs/local-llm-watcher-runtime-ops.md](docs/local-llm-watcher-runtime-ops.md) を参照。
