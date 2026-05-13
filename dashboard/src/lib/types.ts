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
