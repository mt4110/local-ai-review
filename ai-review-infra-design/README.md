# AI Review Infrastructure Design Pack

作成日: 2026-04-24

このリポジトリは、個人開発者が 1日100回級のレビュー要求を処理するための、ローカルLLM中心のレビュー基盤設計パックです。

## 結論

レビュー主軸は OpenAI API / Codex Action ではなく、Mac mini 上の Local LLM に置く。
OpenAI API は最終監査・高リスクPRだけに限定する。

```text
L1: Local LLM on Mac mini
    大量レビュー、低コスト、一次フィルタ

L2: Copilot Pro+ / Gemini CLI
    GitHub統合、外部視点、無料/定額寄りの補助

L3: ChatGPT / Claude / Codex / OpenAI API
    深い最終監査、高リスクPRのみ
```

## 重要な設計変更

従来案の `pull_request + checkout` では、self-hosted runner 上で PR 側のコードやスクリプトを実行する危険が残る。
本設計では、原則として次の方式を採用する。

```text
pull_request_target
+ no checkout
+ GitHub API で PR diff だけ取得
+ diffをローカルLLMへ渡す
+ PRコメントを作成/更新
```

これにより、fork PR / 同一repo PR のどちらでも、PRコードを実行せずにレビューできる。
ただし `pull_request_target` は権限が強くなるため、PRコードを checkout / build / run しないことを絶対条件とする。

## ファイル構成

```text
.
├── README.md
├── AI_REVIEW_INFRA_DESIGN.md
├── docs/
│   ├── 00_project_brief.md
│   ├── 01_requirements.md
│   ├── 02_architecture_design.md
│   ├── 03_detailed_design.md
│   ├── 04_security_design.md
│   ├── 05_agent_manual.md
│   ├── 06_operations_runbook.md
│   ├── 07_rollout_plan.md
│   ├── 08_benchmark_plan.md
│   ├── 09_decision_log.md
│   └── 10_open_questions_wall.md
├── workflows/
│   └── local-llm-review-diff-only.yml
├── prompts/
│   └── local-llm-review.md
├── templates/
│   ├── AGENTS.md
│   └── copilot-instructions.md
└── scripts/
    └── rollout-local-llm-review.sh
```

## 最初の実装順

1. Mac mini に Ollama と Qwen3-Coder を入れる。
2. GitHub self-hosted runner を専用ユーザーで起動する。
3. runner に `self-hosted`, `macOS`, `local-ai` のラベルを付ける。
4. 1 repository に `workflows/local-llm-review-diff-only.yml` を導入する。
5. `local-ai-review` ラベルを付けた PR で動作確認する。
6. 10〜20 PRでベンチマークする。
7. 問題なければ rollout script で対象repoへ展開する。

## 参照

- GitHub self-hosted runner warning: https://docs.github.com/actions/hosting-your-own-runners/adding-self-hosted-runners
- GitHub pull_request_target warning: https://docs.github.com/actions/using-workflows/events-that-trigger-workflows
- GitHub concurrency: https://docs.github.com/actions/writing-workflows/choosing-what-your-workflow-does/control-the-concurrency-of-workflows-and-jobs
- GitHub Copilot plans: https://docs.github.com/en/copilot/concepts/billing/individual-plans
- Google run-gemini-cli: https://github.com/google-github-actions/run-gemini-cli
- Gemini Code Assist quotas: https://developers.google.com/gemini-code-assist/resources/quotas
- Ollama Qwen3-Coder: https://ollama.com/library/qwen3-coder/tags
- Mistral Devstral Small 2: https://docs.mistral.ai/models/devstral-small-2-25-12
