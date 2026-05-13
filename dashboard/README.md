# Local dashboard scaffold

Read-only SvelteKit dashboard for the local review-history DB.

```sh
cd dashboard
npm install
npm run dev
```

Default URL:

```text
http://127.0.0.1:3069
```

Environment overrides:

```sh
LLREVIEW_DASHBOARD_DB=/absolute/path/to/local-ai-review.db npm run dev
LLREVIEW_DASHBOARD_REPO=owner/name npm run dev
LLREVIEW_DASHBOARD_WORKSPACE=/absolute/path/to/workspace npm run dev
```

The dashboard reads aggregate JSON from `scripts/dashboard_snapshot.py`. It does
not run reviews, post PR comments, write verdicts, activate calibration, or show
raw review bodies/diffs.

The snapshot includes read-only review health, stamp stock, and calibration
health aggregates: useful / false-positive / unclear rates, missed / covered
external examples, external and review-gap stamp inbox counts, unscored runs,
candidate activation inbox counts, and active calibration audit status.

When a workspace target is configured, the snapshot also shows read-only git
status: repo, branch, dirty/ahead-behind state, changed file count, diff byte
count, and a diff fingerprint. The diff text itself is not returned to the
browser.
