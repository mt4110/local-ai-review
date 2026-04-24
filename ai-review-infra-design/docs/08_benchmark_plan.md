# 08. Benchmark Plan

## Goal

Local LLMがレビュー基盤として実用レベルか、感覚ではなくデータで判断する。

## Test set

最低10 PR。
できれば20 PR。

分類:

- small PR: 1〜300 changed lines
- medium PR: 300〜2,000 changed lines
- large PR: 2,000〜5,000 changed lines
- risky PR: auth/db/delete/payment/session系

## Models to compare

Initial:

```text
qwen3-coder:30b-a3b-q4_K_M
```

Optional:

```text
qwen3-coder:30b-a3b-q8_0
devstral-small-2:24b-instruct-2512-q4_K_M
devstral-small-2:24b-instruct-2512-q8_0
```

External baseline:

```text
Copilot review
Gemini CLI
ChatGPT / Claude for selected cases
```

## Metrics

| Metric | Description |
|---|---|
| useful_findings | 実際に修正した指摘数 |
| false_positives | 間違い・ノイズ |
| missed_critical | 他のAIや人間が見つけた重要見落とし |
| runtime_seconds | レビュー時間 |
| diff_bytes | diff size |
| changed_lines | changed lines |
| model | model tag |
| num_ctx | context setting |

## Decision rule

Local LLMを主軸にできる条件:

```text
useful_findings >= false_positives
and runtime is acceptable
and no security policy violation
```

Local LLMは最終監査ではないため、完璧さは求めない。
一次フィルタとして価値があるかを見る。

## Benchmark sheet template

```text
PR:
Diff bytes:
Changed lines:
Model:
num_ctx:
Runtime:
Useful findings:
False positives:
Missed issues:
Would I keep this review lane? yes/no
Notes:
```
