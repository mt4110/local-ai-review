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
  backfill_queue: DashboardBackfillQueue;
  calibrations: {
    active: number;
    recent: Array<Record<string, string | number>>;
  };
  learning_readiness: Record<string, string | number | Record<string, number>>;
  backlog: Record<string, number>;
  growth: DashboardGrowthRow[];
  postgres_readiness: DashboardReadinessGate[];
  next_commands: DashboardCommand[];
};
