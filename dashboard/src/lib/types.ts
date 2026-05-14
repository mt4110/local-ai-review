export type CountMap = Record<string, number | null>;

export type DashboardCommand = {
  label: string;
  command: string;
  reason: string;
};

export type DashboardRunCounts = {
  total: number;
  unscored: number;
  zero_finding_runs: number;
  findings: number;
  watch_items: number;
  diff_bytes: number;
  average_elapsed_seconds: number;
};

export type DashboardExternalCounts = {
  total: number;
  linked: number;
  unlinked: number;
  link_rate: string;
  verdict_rows: Array<Record<string, string | number>>;
};

export type DashboardReviewHealth = {
  status: string;
  summary: string;
  local_findings: number;
  scored_local_findings: number;
  useful: number;
  false_positive: number;
  unclear: number;
  watch_only: number;
  missed: number;
  covered: number;
  useful_rate: string;
  false_positive_rate: string;
  unclear_rate: string;
  missed_to_covered_ratio: string;
  local_item_verdicts: Record<string, number>;
  top_local_reasons: Array<Record<string, string | number>>;
};

export type DashboardStampStock = {
  external_stamp_inbox: number;
  review_gap_stamp_inbox: number;
  unscored_runs: number;
  candidate_activation_inbox: number;
  candidate_needs_data: number;
  backfill_pending: number;
  total: number;
};

export type DashboardBackfillQueue = {
  total: number;
  signal: number;
  by_state: Record<string, number>;
  by_source_state: Record<string, number>;
  records: Array<Record<string, string | number>>;
};

export type DashboardGrowthRow = {
  month: string;
  runs: number;
  findings: number;
  watch_items: number;
  diff_bytes: number;
};

export type DashboardWorkspaceRow = {
  workspace_path: string;
  repo: string;
  branch: string;
  pr_number: number;
  base_ref: string;
  head_ref: string;
  head_sha: string;
  last_run_id: number;
  updated_at: string;
};

export type DashboardWorkspaceGate = {
  key: string;
  label: string;
  status: 'pass' | 'warn' | 'block' | 'info' | string;
  ok: boolean;
  detail: string;
};

export type DashboardCurrentWorkspace = {
  configured: boolean;
  requested_path: string;
  path: string;
  exists: boolean;
  is_git_repo: boolean;
  repo: string;
  branch: string;
  head_sha: string;
  base_ref: string;
  upstream: string;
  ahead: number;
  behind: number;
  dirty: boolean;
  tracked_dirty: boolean;
  untracked_count: number;
  untracked_examples: string[];
  changed_files: number;
  changed_file_examples: string[];
  diff_bytes: number;
  diff_size_label: string;
  diff_fingerprint: string;
  diff_fingerprint_short: string;
  diff_error: string;
  last_run: Record<string, string | number> | null;
  diff_changed_since_last_run: boolean;
  ollama_endpoint: {
    endpoint: string;
    loopback: boolean;
  };
  error: string;
};

export type DashboardWorkspaceEligibility = {
  status: string;
  summary: string;
  review_recommended: boolean;
  suggested_command: string;
  limits: Record<string, number>;
  gates: DashboardWorkspaceGate[];
};

export type DashboardSpecbackfillStatus = {
  available: boolean;
  path: string;
  db_items: number;
  db_runs: number;
  last_seen_at: string;
  last_run_id: number;
  status: string;
  summary: string;
};

export type DashboardReadinessGate = {
  key: string;
  label: string;
  current: number;
  threshold: number;
  ready: boolean;
};

export type DashboardLearningCandidates = {
  threshold: number;
  total: number;
  proposed: number;
  active: number;
  paused: number;
  retired: number;
  needs_more_data: number;
  activation_inbox: number;
  by_signal: Record<string, number>;
};

export type DashboardCalibrationHealth = {
  status: string;
  summary: string;
  active: number;
  supported: number;
  promising: number;
  insufficient_recent_runs: number;
  thin_evidence: number;
  watch_missed: number;
  watch_false_positives: number;
  needs_audit: number;
  with_recent_runs: number;
  recent: Array<Record<string, string | number>>;
};

export type DashboardSnapshot = {
  schema_name: string;
  schema_version: number;
  generated_at_utc: string;
  loopback: {
    host: string;
    port: number;
    browser_actions_enabled: boolean;
  };
  policy: Record<string, boolean>;
  db: {
    backend: string;
    target: string;
    path: string;
    exists: boolean;
    size_bytes: number;
    size_label: string;
    open_mode: string;
    error: string;
  };
  scope: {
    repo: string;
    requested_workspace: string;
    source: string;
  };
  workspace?: {
    saved_target: Record<string, string> | null;
    recent: DashboardWorkspaceRow[];
    current: DashboardCurrentWorkspace;
    eligibility: DashboardWorkspaceEligibility;
    specbackfill: DashboardSpecbackfillStatus;
  };
  tables?: CountMap;
  runs: DashboardRunCounts;
  external: DashboardExternalCounts;
  review_health: DashboardReviewHealth;
  stamp_stock: DashboardStampStock;
  backfill_queue: DashboardBackfillQueue;
  calibrations: {
    active: number;
    recent: Array<Record<string, string | number>>;
  };
  calibration_health: DashboardCalibrationHealth;
  learning_candidates: DashboardLearningCandidates;
  learning_readiness: Record<string, string | number | Record<string, number>>;
  backlog: Record<string, number>;
  growth: DashboardGrowthRow[];
  postgres_readiness: DashboardReadinessGate[];
  next_commands: DashboardCommand[];
};
