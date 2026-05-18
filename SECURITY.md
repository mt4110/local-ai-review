# Security Policy

## Supported versions

Security fixes are provided for the latest released version of `local-ai-review`.

## Reporting a vulnerability

Please report vulnerabilities privately. Do not open a public issue containing exploit details, secrets, or private code.

Include:

- affected version or commit;
- affected command/workflow;
- reproduction steps using synthetic data;
- whether PR workflow safety, evidence encryption, pack verification, or dashboard exposure is involved.

## Security design summary

The local PR review workflow is designed around:

- diff-only review;
- no `actions/checkout` in the privileged workflow;
- no PR-controlled code execution;
- no repository secrets in the review job;
- local Ollama by default;
- encrypted local evidence by default once the Evidence Vault phase is implemented.

## Known limitations

- Self-hosted runners require careful hardening.
- Offline commercial pack DRM is deterrence, not perfect copy prevention.
- AI findings require human judgment.
- Export-safe learning signals must be explicitly enabled.
