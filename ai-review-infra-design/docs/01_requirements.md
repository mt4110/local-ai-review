# 01. Requirements

## Functional requirements

### FR-001: Label-triggered review

PRに `local-ai-review` ラベルが付いたとき、Local LLMレビューを実行する。

### FR-002: Diff-only review

レビュー対象はPR diffのみとする。
Repository checkout はしない。
PRコードの実行もしない。

### FR-003: PR comment update

同じPRでは、毎回新規コメントを増やさず、既存のLocal LLMレビューコメントを更新する。

### FR-004: Diff size gate

diffが指定サイズを超える場合、LLMレビューをスキップして、PR分割またはdeep reviewを促す。

### FR-005: Model selection

初期モデルは以下を推奨する。

```text
qwen3-coder:30b-a3b-q4_K_M
```

将来的に以下を比較する。

```text
qwen3-coder:30b-a3b-q8_0
devstral-small-2:24b-instruct-2512-q4_K_M
devstral-small-2:24b-instruct-2512-q8_0
```

## Non-functional requirements

### NFR-001: Cost stability

Local LLMレビューはAPI課金を発生させない。
OpenAI APIは原則として高リスクPRだけに限定する。

### NFR-002: Security

self-hosted runner上で、PR由来のコード、workflow、script、binaryを実行しない。

### NFR-003: Reproducibility

workflow, prompt, security policy, rollout procedureをGitで管理する。

### NFR-004: Operability

モデル、context、diff上限、timeoutは環境変数で調整可能にする。

### NFR-005: Failure tolerance

LLMレビューが失敗しても、lint / typecheck / test / Semgrep などのdeterministic CIを妨げない。
AIレビューはadvisory扱いとする。
