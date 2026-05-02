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
  prompt_version,
  SUBSTR(diff_fingerprint, 1, 16) AS diff_fp,
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

-- Normalized review items for future item-level scoring
SELECT
  runs.id AS run_id,
  items.item_type,
  items.source,
  items.path,
  items.line,
  items.title,
  items.fingerprint
FROM review_items AS items
JOIN review_runs AS runs
ON runs.id = items.run_id
ORDER BY runs.id DESC, items.item_type, items.ordinal
LIMIT 50;

-- Trusted context documents recorded for a run
SELECT
  runs.id AS run_id,
  runs.repo,
  runs.review_kind,
  artifacts.path,
  artifacts.sha256
FROM artifacts
JOIN review_runs AS runs
ON runs.id = artifacts.run_id
WHERE artifacts.kind = 'context_digest'
ORDER BY runs.id DESC, artifacts.path
LIMIT 50;

-- Imported external review items and local-link coverage
SELECT
  external_items.repo,
  external_items.pr_number,
  external_items.source,
  COUNT(DISTINCT external_items.id) AS external_items,
  COUNT(DISTINCT item_links.external_item_id) AS linked_external_items,
  COUNT(DISTINCT external_items.id) - COUNT(DISTINCT item_links.external_item_id) AS unlinked_external_items
FROM external_items
LEFT JOIN item_links
ON item_links.external_item_id = external_items.id
GROUP BY external_items.repo, external_items.pr_number, external_items.source
ORDER BY external_items.repo, external_items.pr_number DESC, external_items.source;

-- External items that may represent local misses
SELECT
  external_items.repo,
  external_items.pr_number,
  external_items.source,
  external_items.path,
  external_items.line,
  external_items.title,
  verdicts.verdict,
  verdicts.reason
FROM external_items
LEFT JOIN item_links
ON item_links.external_item_id = external_items.id
LEFT JOIN (
  SELECT item_verdicts.*
  FROM item_verdicts
  JOIN (
    SELECT target_kind, target_id, MAX(id) AS id
    FROM item_verdicts
    GROUP BY target_kind, target_id
  ) AS latest
  ON latest.id = item_verdicts.id
) AS verdicts
ON verdicts.target_kind = 'external_item'
AND verdicts.target_id = external_items.id
WHERE item_links.id IS NULL
ORDER BY external_items.created_at DESC
LIMIT 50;

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
