-- PostgreSQL schema draft for the review-history DB.
--
-- This is an optional local backend target. SQLite remains the default until
-- migration dry-runs and operator pressure justify switching.

BEGIN;

CREATE TABLE IF NOT EXISTS review_runs (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    review_kind TEXT NOT NULL DEFAULT 'precision',
    repo TEXT NOT NULL,
    pr_number INTEGER,
    diff_source TEXT NOT NULL,
    base_ref TEXT NOT NULL DEFAULT '',
    head_ref TEXT NOT NULL DEFAULT '',
    head_sha TEXT NOT NULL DEFAULT '',
    working_tree_included INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL,
    ollama_base_url TEXT NOT NULL,
    diff_bytes INTEGER NOT NULL,
    changed_files INTEGER NOT NULL,
    reviewed_files_count INTEGER NOT NULL,
    findings_count INTEGER NOT NULL,
    watch_items_count INTEGER NOT NULL,
    static_findings_count INTEGER NOT NULL,
    model_findings_count INTEGER NOT NULL,
    static_watch_items_count INTEGER NOT NULL,
    model_watch_items_count INTEGER NOT NULL,
    existing_review_comments_count INTEGER NOT NULL,
    elapsed_seconds DOUBLE PRECISION NOT NULL,
    output_path TEXT,
    post_comment_requested INTEGER NOT NULL DEFAULT 0,
    prompt_family TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    prompt_hash TEXT NOT NULL DEFAULT '',
    model_options_hash TEXT NOT NULL DEFAULT '',
    diff_fingerprint TEXT NOT NULL DEFAULT '',
    context_docs_count INTEGER NOT NULL DEFAULT 0,
    context_summary_bytes INTEGER NOT NULL DEFAULT 0,
    report_markdown TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviewed_files (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS findings (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    source TEXT NOT NULL,
    severity TEXT NOT NULL,
    confidence TEXT NOT NULL,
    path TEXT NOT NULL,
    line INTEGER,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    fix TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watch_items (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    source TEXT NOT NULL,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    verification TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_feedback (
    run_id BIGINT PRIMARY KEY REFERENCES review_runs(id) ON DELETE CASCADE,
    useful_findings_fixed INTEGER,
    false_positives INTEGER,
    unclear_findings INTEGER,
    would_request_remote_review_now INTEGER,
    remote_findings_count INTEGER,
    note TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS review_items (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
    item_type TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    source TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL,
    line INTEGER,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    fix TEXT NOT NULL DEFAULT '',
    verification TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS review_items_run_type_ordinal_idx
ON review_items(run_id, item_type, ordinal);

CREATE INDEX IF NOT EXISTS review_items_fingerprint_idx
ON review_items(fingerprint);

CREATE TABLE IF NOT EXISTS external_items (
    id BIGSERIAL PRIMARY KEY,
    repo TEXT NOT NULL,
    pr_number INTEGER,
    head_sha TEXT NOT NULL DEFAULT '',
    import_head_sha TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    path TEXT NOT NULL DEFAULT '',
    line INTEGER,
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    github_comment_id TEXT NOT NULL DEFAULT '',
    github_thread_id TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS external_items_lookup_idx
ON external_items(repo, pr_number, head_sha, source);

CREATE INDEX IF NOT EXISTS external_items_fingerprint_idx
ON external_items(fingerprint);

CREATE UNIQUE INDEX IF NOT EXISTS external_items_github_comment_idx
ON external_items(repo, pr_number, github_comment_id)
WHERE github_comment_id <> '';

CREATE INDEX IF NOT EXISTS external_items_import_scope_idx
ON external_items(repo, pr_number, import_head_sha);

CREATE TABLE IF NOT EXISTS item_verdicts (
    id BIGSERIAL PRIMARY KEY,
    target_kind TEXT NOT NULL,
    target_id BIGINT NOT NULL,
    verdict TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    scorer TEXT NOT NULL DEFAULT '',
    scored_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS item_verdicts_target_idx
ON item_verdicts(target_kind, target_id);

CREATE TABLE IF NOT EXISTS item_links (
    id BIGSERIAL PRIMARY KEY,
    review_item_id BIGINT NOT NULL REFERENCES review_items(id) ON DELETE CASCADE,
    external_item_id BIGINT NOT NULL REFERENCES external_items(id) ON DELETE CASCADE,
    relation TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS item_links_pair_idx
ON item_links(review_item_id, external_item_id, relation);

CREATE INDEX IF NOT EXISTS item_links_external_idx
ON item_links(external_item_id);

CREATE TABLE IF NOT EXISTS rule_updates (
    id BIGSERIAL PRIMARY KEY,
    verdict_id BIGINT REFERENCES item_verdicts(id) ON DELETE SET NULL,
    change_type TEXT NOT NULL,
    status TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    artifact_path TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS runtime_metrics (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
    elapsed_seconds DOUBLE PRECISION NOT NULL,
    reviewed_files_count INTEGER NOT NULL,
    findings_count INTEGER NOT NULL,
    watch_items_count INTEGER NOT NULL,
    queue_depth INTEGER,
    memory_pressure TEXT NOT NULL DEFAULT '',
    ollama_status TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS artifacts (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT REFERENCES review_runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS workspace_state (
    workspace_path TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    branch TEXT NOT NULL DEFAULT '',
    pr_number INTEGER,
    base_ref TEXT NOT NULL DEFAULT '',
    head_ref TEXT NOT NULL DEFAULT '',
    head_sha TEXT NOT NULL DEFAULT '',
    last_run_id BIGINT REFERENCES review_runs(id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS github_backfill_queue (
    id BIGSERIAL PRIMARY KEY,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL DEFAULT 0,
    source_kind TEXT NOT NULL,
    remote_state TEXT NOT NULL DEFAULT 'unknown',
    state TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    updated_at_github TEXT NOT NULL DEFAULT '',
    merged_at TEXT NOT NULL DEFAULT '',
    head_sha TEXT NOT NULL DEFAULT '',
    doc_ratio DOUBLE PRECISION NOT NULL DEFAULT 0,
    generated_ratio DOUBLE PRECISION NOT NULL DEFAULT 0,
    changed_files INTEGER NOT NULL DEFAULT 0,
    changed_lines INTEGER NOT NULL DEFAULT 0,
    diff_fingerprint TEXT NOT NULL DEFAULT '',
    actionable_external_comments INTEGER NOT NULL DEFAULT 0,
    skip_reason TEXT NOT NULL DEFAULT '',
    last_attempt_at TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS github_backfill_queue_identity_idx
ON github_backfill_queue(repo, pr_number, source_kind, head_sha);

CREATE INDEX IF NOT EXISTS github_backfill_queue_state_idx
ON github_backfill_queue(state, priority, next_attempt_at);

CREATE TABLE IF NOT EXISTS learning_calibrations (
    id BIGSERIAL PRIMARY KEY,
    calibration_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    scope_repo TEXT NOT NULL DEFAULT '',
    path_class TEXT NOT NULL DEFAULT '',
    signal_kind TEXT NOT NULL DEFAULT '',
    instruction TEXT NOT NULL,
    guardrails_json TEXT NOT NULL DEFAULT '[]',
    evidence_count INTEGER NOT NULL DEFAULT 0,
    confidence TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    source_path TEXT NOT NULL DEFAULT '',
    support_digest TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS learning_calibrations_id_idx
ON learning_calibrations(calibration_id);

CREATE INDEX IF NOT EXISTS learning_calibrations_active_idx
ON learning_calibrations(status, scope_repo, path_class);

CREATE OR REPLACE VIEW review_run_summary AS
SELECT
    runs.id,
    runs.created_at,
    runs.review_kind,
    runs.repo,
    runs.pr_number,
    runs.diff_source,
    runs.base_ref,
    runs.head_ref,
    runs.head_sha,
    runs.working_tree_included,
    runs.model,
    runs.diff_bytes,
    runs.changed_files,
    runs.reviewed_files_count,
    runs.findings_count,
    runs.watch_items_count,
    runs.static_findings_count,
    runs.model_findings_count,
    runs.static_watch_items_count,
    runs.model_watch_items_count,
    runs.existing_review_comments_count,
    runs.elapsed_seconds,
    runs.output_path,
    runs.post_comment_requested,
    runs.prompt_family,
    runs.prompt_version,
    runs.prompt_hash,
    runs.model_options_hash,
    runs.diff_fingerprint,
    runs.context_docs_count,
    runs.context_summary_bytes,
    feedback.useful_findings_fixed,
    feedback.false_positives,
    feedback.unclear_findings,
    feedback.would_request_remote_review_now,
    feedback.remote_findings_count,
    feedback.note,
    feedback.updated_at
FROM review_runs AS runs
LEFT JOIN run_feedback AS feedback
ON feedback.run_id = runs.id;

COMMIT;
