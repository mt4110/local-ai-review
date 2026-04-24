# 02. Architecture Design

## 全体アーキテクチャ

```text
GitHub Pull Request
  |
  | label: local-ai-review
  v
GitHub Actions pull_request_target
  |
  | no checkout
  | GitHub API: PR diff only
  v
Self-hosted runner on Mac mini
  |
  | local HTTP
  v
Ollama / Local LLM
  |
  | review markdown
  v
GitHub PR comment update
```

## レイヤー設計

### L1: Local LLM review

目的:

- 大量PRレビュー
- コストゼロ運用
- 一次フィルタ

推奨モデル:

```text
qwen3-coder:30b-a3b-q4_K_M
```

役割:

- 明らかなバグ候補
- テスト不足
- セキュリティ臭のある差分
- API破壊の疑い

### L2: Copilot Pro+ / Gemini CLI

目的:

- GitHub統合レビュー
- 定額/無料枠を利用した外部視点
- Local LLMの見落とし補助

### L3: ChatGPT / Claude / Codex / OpenAI API

目的:

- 本番前の高リスクPR
- 認可・認証・DB migration・決済・削除処理
- 設計判断が必要なPR

## 採用イベント

推奨:

```yaml
on:
  pull_request_target:
    types: [labeled, synchronize, reopened, ready_for_review]
```

理由:

- workflowはbase側のtrustedな定義で動く。
- PR側でworkflowやscriptを改ざんされにくい。
- checkoutしないため、PRコードを実行しない。

禁止:

```text
pull_request_target + checkout PR head
pull_request_target + build/test PR code
pull_request_target + repository script execution
```

## なぜ pull_request ではないか

`pull_request` + checkout で self-hosted runner を動かすと、PR側のworkflow/script/codeを実行する危険がある。
同一repo PRでも、PR作成者がtrustedでない場合や、誤って危険なscriptを混ぜた場合にrunnerが汚染される。

このプロジェクトではレビューに必要なのはdiff textだけなので、checkoutは不要。

## なぜ同一repo PR制限を主軸にしないか

同一repo制限は安全策として有効だが、運用上のブロッカーになりやすい。
本設計では、より根本的にPRコードを実行しない方式を採用する。

```text
安全性は「PR元が同一かどうか」ではなく、
「PRコードを実行するかどうか」で決まる。
```

## GitHubにするべきか

推奨: private GitHub repo にする。

理由:

- 設計書・workflow・prompt・runbookを履歴管理できる。
- 複数repoへの展開が楽になる。
- Issueで改善タスクを管理できる。
- rollbackが可能になる。
- 自分しか使わなくても、CI/CD基盤はGit管理した方が壊れにくい。

推奨repo名:

```text
review-infra
local-ai-review-infra
ai-review-infra
```

public repo化は非推奨。
理由は、runner構成、運用手順、攻撃面のヒントを公開する必要がないため。
