# Contributing

Thank you for contributing to `local-ai-review`.

## Development rules

Before sending a PR, run:

```sh
python3 -m py_compile scripts/*.py
python3 scripts/verify-workflow-policy.py
python3 -m pytest -q
cd dashboard && npm ci && npm run check && npm run build && npm audit --omit=dev
```

## Security rules

Do not add to the privileged local review workflow:

- `actions/checkout`;
- external actions;
- package installs;
- build/test commands;
- repository secrets;
- PR code execution.

## Commercial boundary

OSS Core must remain useful and transparent. Commercial pack code must not hide network calls, weaken local privacy, or make OSS review intentionally poor.
