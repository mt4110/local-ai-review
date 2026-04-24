# 07. Rollout Plan

## Phase 0: Design freeze candidate

Duration: 0.5 day

Tasks:

- Create private `review-infra` GitHub repo.
- Commit this design pack.
- Create Issues for open questions.
- Confirm runner security policy.

Exit criteria:

- No one-off local notes remain outside Git.

## Phase 1: Single repository pilot

Duration: 1 day

Tasks:

- Install Ollama.
- Pull Qwen3-Coder Q4.
- Configure self-hosted runner.
- Add workflow to one repository.
- Run on 5 small PRs.

Exit criteria:

- PR comment is posted/updated.
- No checkout occurs.
- No secrets are used.
- Average review time is acceptable.

## Phase 2: Benchmark

Duration: 3〜7 days

Tasks:

- Run 10〜20 PRs through Local LLM.
- Compare with Copilot/Gemini/ChatGPT for selected PRs.
- Record:
  - useful findings
  - false positives
  - missed issues
  - runtime
  - memory pressure

Exit criteria:

- Primary model chosen.
- MAX_DIFF_BYTES and OLLAMA_NUM_CTX set.

## Phase 3: Controlled rollout

Duration: 1〜2 days

Tasks:

- Roll out to 3 repositories.
- Keep manual labels only.
- Monitor runner load.

Exit criteria:

- No runner queue explosion.
- No comment spam.

## Phase 4: Wider rollout

Duration: 1 week

Tasks:

- Use rollout script for more repositories.
- Add standard labels.
- Add AGENTS.md / Copilot instructions.

Exit criteria:

- All target repos have local review lane.
- Critical repos have escalation policy.

## Phase 5: Advanced automation

Future tasks:

- Risk-based auto-labeling.
- Devstral second-pass review.
- Gemini CLI lane.
- Copilot Pro+ automatic review once per PR.
- OpenAI API only for `deep-ai-review` label.
