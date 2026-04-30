-- Recent runs
SELECT
  id,
  created_at,
  review_kind,
  repo,
  pr_number,
  base_ref,
  head_ref,
  model,
  findings_count,
  watch_items_count,
  ROUND(elapsed_seconds, 1) AS elapsed_s
FROM review_run_summary
ORDER BY id DESC
LIMIT 20;

-- Finding mix by source
SELECT
  source,
  severity,
  COUNT(*) AS findings
FROM findings
GROUP BY source, severity
ORDER BY source, severity;

-- Repo and model level health
SELECT
  repo,
  review_kind,
  model,
  COUNT(*) AS runs,
  ROUND(AVG(elapsed_seconds), 1) AS avg_elapsed_s,
  ROUND(AVG(findings_count), 2) AS avg_findings,
  ROUND(AVG(watch_items_count), 2) AS avg_watch_items
FROM review_run_summary
GROUP BY repo, review_kind, model
ORDER BY runs DESC, repo, review_kind, model;

-- Pre-PR runs waiting for later manual scoring
SELECT
  id,
  created_at,
  repo,
  base_ref,
  head_ref,
  SUBSTR(head_sha, 1, 12) AS head_sha,
  working_tree_included,
  findings_count,
  watch_items_count
FROM review_run_summary
WHERE review_kind = 'pre_pr'
  AND useful_findings_fixed IS NULL
ORDER BY id DESC
LIMIT 20;

-- Record your own evaluation for a run after remote review lands
INSERT INTO run_feedback (
  run_id,
  useful_findings_fixed,
  false_positives,
  unclear_findings,
  would_request_remote_review_now,
  remote_findings_count,
  note,
  updated_at
) VALUES (
  1,
  2,
  0,
  1,
  1,
  3,
  'Caught two obvious issues before remote review.',
  CURRENT_TIMESTAMP
)
ON CONFLICT(run_id) DO UPDATE SET
  useful_findings_fixed = excluded.useful_findings_fixed,
  false_positives = excluded.false_positives,
  unclear_findings = excluded.unclear_findings,
  would_request_remote_review_now = excluded.would_request_remote_review_now,
  remote_findings_count = excluded.remote_findings_count,
  note = excluded.note,
  updated_at = CURRENT_TIMESTAMP;

-- Compare preflight value after manual scoring
SELECT
  repo,
  COUNT(*) AS scored_runs,
  ROUND(AVG(COALESCE(useful_findings_fixed, 0)), 2) AS avg_useful,
  ROUND(AVG(COALESCE(false_positives, 0)), 2) AS avg_false_positives,
  ROUND(AVG(COALESCE(remote_findings_count, 0)), 2) AS avg_remote_findings,
  ROUND(AVG(CASE WHEN would_request_remote_review_now = 1 THEN 1.0 ELSE 0.0 END), 2) AS remote_ready_rate
FROM review_run_summary
WHERE useful_findings_fixed IS NOT NULL
GROUP BY repo
ORDER BY scored_runs DESC, repo;
