# 05. Agent Manual

## Agent roles

### Local LLM agent

Primary model:

```text
qwen3-coder:30b-a3b-q4_K_M
```

Role:

- fast first-pass review
- high-confidence bug detection
- missing-test detection
- security smell detection

Do not use it as final authority.

### Copilot agent

Role:

- GitHub-native review
- IDE補助
- PR ready時の外部レビュー

Use once per PR when possible.
Avoid every-push review.

### Gemini CLI agent

Role:

- free/large quota cloud review lane
- issue triage
- security review extension候補

Use as L2, not as hard merge gate.

### ChatGPT / Claude / Codex

Role:

- high-risk review
- architecture decision
- auth/authz review
- DB migration review
- production incident risk review

Use manually or label-triggered with strict budget.

## Escalation matrix

| Case | L1 Local | L2 Copilot/Gemini | L3 ChatGPT/Claude/Codex |
|---|---:|---:|---:|
| small UI change | yes | optional | no |
| test-only PR | yes | no | no |
| auth/authz | yes | yes | yes |
| DB migration | yes | yes | yes |
| payment | yes | yes | yes |
| delete/destructive operation | yes | yes | yes |
| huge refactor | yes | yes | optional |
| release branch | yes | yes | yes |

## Agent output policy

Good output:

```text
File: src/auth/session.ts
Risk: token refresh path does not validate session owner.
Why it matters: user A may refresh user B's session if session ID is reused.
Fix direction: verify session.userId against authenticated user before refresh.
```

Bad output:

```text
This code looks complicated. Consider refactoring.
```

## Comment budget

Default:

```text
Max findings: 8
```

If more than 8 findings appear, the PR is probably too large.
Split the PR instead of asking the model for 30 comments.
