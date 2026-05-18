# Release Checklist

## Pre-release Verification

- [ ] All CI checks pass (`pytest`, `npm run build`).
- [ ] Version numbers are bumped in necessary files.
- [ ] `README.md` and `docs/` are up-to-date with current features.
- [ ] `LICENSE`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, and `COMMERCIAL_BOUNDARY.md` are present.

## Security & Privacy Verification

- [ ] `scripts/verify-workflow-policy.py` confirms no workflow safety regressions.
- [ ] GitHub workflow (`pull_request_target`) does not perform `actions/checkout` on the PR branch.
- [ ] No repository secrets are passed to the LLM or untrusted execution contexts.
- [ ] The core functionality respects the Commercial Boundary (no forced premium lock-in for basic local review).
