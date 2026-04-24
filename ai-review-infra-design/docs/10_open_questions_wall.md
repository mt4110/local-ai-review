# 10. Open Questions / Wall Discussion

この設計はv0.1として実装に進める価値がある。
ただし、以下は実測と壁打ちで詰める。

## Q1. public repoのfork PRもLocal LLMで見るか？

推奨初期値:

```text
見る。ただしpull_request_target + no checkout + label triggerのみ。
```

検証:

- contributorがlabelを付けられないこと。
- runnerにsecretがないこと。
- workflowがcheckoutしないこと。

## Q2. MAX_DIFF_BYTESはいくつにするか？

初期:

```text
350000
```

候補:

```text
500000
600000
```

判断材料:

- runtime
- memory pressure
- review quality

## Q3. Qwen Q4で十分か？

初期はQ4。

比較:

- Qwen Q4 vs Qwen Q8
- Qwen Q4 vs Devstral Q4
- Qwen + Devstral two-pass

## Q4. Copilot Pro+は必要か？

Copilot Pro+はGitHub統合の外部レビューとして有用。
ただしLocal LLMが主軸になれば、Copilot reviewの回数は削減できる。

判断:

- Local LLMだけで十分なPRが何割か。
- Copilot reviewの有用指摘数。
- premium request消費量。

## Q5. Gemini CLIをどこに置くか？

候補:

```text
L2 external free/cloud lane
security review lane
issue triage lane
```

注意:

- quotaは変化しうる。
- cloud依存なのでLocal LLMの代替ではなく補助。

## Q6. review-infra repoはGitHubに置くか？

推奨:

```text
private GitHub repoに置く。
```

理由:

- docs/workflows/prompts/scriptsを履歴管理できる。
- rolloutしやすい。
- Issue化できる。

## Q7. まだ壁打ちすべきか？

はい。ただし抽象論ではなく、ベンチ結果を持って壁打ちする。

次の壁打ち材料:

```text
10 PRのLocal LLMレビュー結果
Copilotとの差分
Geminiとの差分
false positive数
runtime
Mac miniのメモリ/温度/queue状況
```
