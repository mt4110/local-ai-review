import { execFile } from 'node:child_process';
import { resolve } from 'node:path';
import { promisify } from 'node:util';
import type { DashboardSnapshot } from '$lib/types';

const execFileAsync = promisify(execFile);

function repoRoot(): string {
  return process.env.LLREVIEW_ROOT ? resolve(process.env.LLREVIEW_ROOT) : resolve(process.cwd(), '..');
}

function resolveFromRoot(root: string, value: string): string {
  if (value.startsWith('/')) {
    return value;
  }
  return resolve(root, value);
}

function fallbackSnapshot(error: unknown): DashboardSnapshot {
  const message = error instanceof Error ? error.message : String(error);
  return {
    schema_name: 'local_ai_review.dashboard_snapshot',
    schema_version: 1,
    generated_at_utc: new Date().toISOString(),
    loopback: {
      host: '127.0.0.1',
      port: Number(process.env.LLREVIEW_DASHBOARD_PORT ?? 3069),
      browser_actions_enabled: false
    },
    policy: {
      read_only: true,
      review_execution_enabled: false,
      pr_comment_posting_enabled: false,
      verdict_writes_enabled: false,
      calibration_activation_enabled: false,
      raw_private_rows_included: false,
      raw_bodies_included: false,
      raw_diffs_included: false
    },
    db: {
      backend: 'sqlite',
      target: process.env.LLREVIEW_DASHBOARD_DB ?? 'out/review-history/local-ai-review.db',
      path: '',
      exists: false,
      size_bytes: 0,
      size_label: '0 B',
      open_mode: 'read-only',
      error: message
    },
    scope: {
      repo: process.env.LLREVIEW_DASHBOARD_REPO || 'global',
      requested_workspace: process.env.LLREVIEW_DASHBOARD_WORKSPACE || '',
      source: process.env.LLREVIEW_DASHBOARD_REPO ? 'environment' : 'global'
    },
    workspace: { saved_target: null, recent: [] },
    tables: {},
    runs: {
      total: 0,
      unscored: 0,
      zero_finding_runs: 0,
      findings: 0,
      watch_items: 0,
      diff_bytes: 0,
      average_elapsed_seconds: 0
    },
    external: { total: 0, linked: 0, unlinked: 0, link_rate: 'n/a', verdict_rows: [] },
    backfill_queue: { total: 0, signal: 0, by_state: {}, by_source_state: {}, records: [] },
    calibrations: { active: 0, recent: [] },
    learning_readiness: {
      active_calibrations: 0,
      training_ready_external_examples: 0,
      human_gate_external_examples: 0,
      postgres_optional_backend: 'optional'
    },
    backlog: {
      unscored_runs: 0,
      human_gate_external_examples: 0,
      backfill_pending: 0,
      unlinked_external_items: 0,
      unlabeled_external_items: 0
    },
    growth: [],
    postgres_readiness: [],
    next_commands: [
      {
        label: 'Check workspace target',
        command: 'llreview status',
        reason: 'Dashboard snapshot failed before reading aggregate state.'
      }
    ]
  };
}

export async function loadDashboardSnapshot(): Promise<DashboardSnapshot> {
  const root = repoRoot();
  const python = process.env.PYTHON ?? 'python3';
  const dbPath = resolveFromRoot(root, process.env.LLREVIEW_DASHBOARD_DB ?? 'out/review-history/local-ai-review.db');
  const args = [
    resolve(root, 'scripts/dashboard_snapshot.py'),
    '--db',
    dbPath,
    '--port',
    String(process.env.LLREVIEW_DASHBOARD_PORT ?? 3069)
  ];
  if (process.env.LLREVIEW_DASHBOARD_REPO) {
    args.push('--repo', process.env.LLREVIEW_DASHBOARD_REPO);
  }
  if (process.env.LLREVIEW_DASHBOARD_WORKSPACE) {
    args.push('--workspace', process.env.LLREVIEW_DASHBOARD_WORKSPACE);
  }
  try {
    const { stdout } = await execFileAsync(python, args, {
      cwd: root,
      maxBuffer: 1024 * 1024
    });
    return JSON.parse(stdout) as DashboardSnapshot;
  } catch (error) {
    return fallbackSnapshot(error);
  }
}
