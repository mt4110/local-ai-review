# 09. Decision Log

## ADR-001: OpenAI API is not the primary review lane

Decision:

OpenAI API / Codex Action is not used for daily high-volume review.

Reason:

1日100レビュー級では、token-based API課金が現実的でない。
APIは高リスクPRだけに限定する。

## ADR-002: Local LLM is the primary high-volume review lane

Decision:

Mac mini M4 Pro / 64GB上のLocal LLMをL1レビュー基盤にする。

Reason:

API課金がゼロで、レビュー回数が多いほど有利。

## ADR-003: Use Qwen3-Coder first

Decision:

初期モデルは `qwen3-coder:30b-a3b-q4_K_M`。

Reason:

30B A3BのMoEで効率が良く、coding / agentic workflow向け。
64GB unified memory上で実用可能なサイズ。

## ADR-004: Do not checkout PR code in self-hosted runner review

Decision:

Local LLMレビューworkflowではcheckoutしない。

Reason:

self-hosted runner上でPRコードを実行するリスクを避けるため。

## ADR-005: Use pull_request_target only in diff-only mode

Decision:

`pull_request_target` を使うが、PR code checkout/build/runは禁止。
GitHub APIからdiffのみ取得する。

Reason:

同一repo PR制限を避けつつ、PRコード実行をしないため。

## ADR-006: AI review is advisory

Decision:

AIレビューはmerge gateにしない。

Reason:

AIは非決定的であり、deterministicなCIの代替ではない。
merge gateはlint/typecheck/test/security scanを使う。
