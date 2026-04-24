# 00. Project Brief

## 背景

レビュー処理が手動AIレビューに依存しており、1日100回級のレビュー要求が発生する。
ChatGPT UI / Copilot review / Codex app は品質は高いが、量・速度・回数制限・コスト面でボトルネックになっている。

既存の課題:

- ChatGPT UIレビューが遅い。
- AIレビュー結果がGitHub上に安定して残らない。
- Copilot review は有用だが、出力数・速度・premium request が制約になる。
- OpenAI API / Codex Action を主軸にすると、1日100レビュー級では月額が現実的でない。
- self-hosted runner は安全に扱わないとPR経由の任意コード実行リスクがある。

## 目的

1日100レビュー級の開発活動を、月額コストを抑えつつ、GitHub PR上で継続運用できるレビュー基盤を作る。

## 非目的

- AIレビューをmerge必須条件にすること。
- Local LLMだけで最終品質保証を完結させること。
- 全commitごとにAIレビューを実行すること。
- self-hosted runnerでPRコードを実行すること。

## 基本方針

```text
大量レビューは Local LLM。
高精度レビューは外部AI。
merge gate は deterministic CI。
```

## 成功条件

- 1日100レビュー級でもAPI課金が増えない。
- PRに `local-ai-review` ラベルを付けるだけでレビューが走る。
- self-hosted runner 上でPRコードをcheckout/build/runしない。
- レビュー結果はPRコメントとして更新される。
- 重要PRだけ Copilot / Gemini / ChatGPT / Claude / Codex に昇格できる。
