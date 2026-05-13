<script lang="ts">
  import type { DashboardGrowthRow, DashboardSnapshot } from '$lib/types';

  export let data: { snapshot: DashboardSnapshot };

  let snapshot: DashboardSnapshot = data.snapshot;
  let refreshing = false;
  let refreshError = '';
  $: currentWorkspace = snapshot.workspace?.current;
  $: workspaceEligibility = snapshot.workspace?.eligibility;
  $: specbackfill = snapshot.workspace?.specbackfill;

  const numberFormatter = new Intl.NumberFormat('en-US');

  function fmt(value: unknown): string {
    const number = Number(value ?? 0);
    if (!Number.isFinite(number)) {
      return '0';
    }
    return numberFormatter.format(number);
  }

  function tableCount(key: string): string {
    const value = snapshot.tables?.[key];
    return value === null || value === undefined ? 'n/a' : fmt(value);
  }

  function maxGrowthRuns(): number {
    return Math.max(1, ...snapshot.growth.map((row: DashboardGrowthRow) => row.runs));
  }

  function barStyle(row: DashboardGrowthRow): string {
    const width = Math.max(4, Math.round((row.runs / maxGrowthRuns()) * 100));
    return `--bar-width: ${width}%`;
  }

  function backlogRows() {
    return [
      ['Unscored runs', snapshot.backlog.unscored_runs ?? 0],
      ['Stamp gate', snapshot.backlog.human_gate_external_examples ?? 0],
      ['Backfill pending', snapshot.backlog.backfill_pending ?? 0],
      ['Unlinked external', snapshot.backlog.unlinked_external_items ?? 0]
    ];
  }

  function readinessRows() {
    return [
      ['Training-ready', snapshot.learning_readiness.training_ready_external_examples ?? 0],
      ['Human gate', snapshot.learning_readiness.human_gate_external_examples ?? 0],
      ['Covered by local', snapshot.learning_readiness.covered_by_local ?? 0],
      ['Active calibration', snapshot.learning_readiness.active_calibrations ?? 0]
    ];
  }

  function gateText(status: string): string {
    if (status === 'pass') return 'OK';
    if (status === 'warn') return 'Watch';
    if (status === 'block') return 'Block';
    return 'Info';
  }

  function workspaceStatusText(status: string | undefined): string {
    if (status === 'ready') return 'Ready';
    if (status === 'manual_review_recommended') return 'Manual';
    if (status === 'blocked') return 'Blocked';
    if (status === 'idle') return 'Idle';
    if (status === 'up_to_date') return 'Current';
    return 'Unset';
  }

  async function refresh() {
    refreshing = true;
    refreshError = '';
    try {
      const response = await fetch('/api/snapshot', {
        headers: { accept: 'application/json' }
      });
      if (!response.ok) {
        throw new Error(`Snapshot request failed: ${response.status}`);
      }
      snapshot = (await response.json()) as DashboardSnapshot;
    } catch (error) {
      refreshError = error instanceof Error ? error.message : String(error);
    } finally {
      refreshing = false;
    }
  }
</script>

<svelte:head>
  <title>Review Dashboard</title>
</svelte:head>

<main class="page-shell">
  <header class="masthead">
    <div>
      <p class="eyebrow">{snapshot.loopback.host}:{snapshot.loopback.port} / {snapshot.db.open_mode}</p>
      <h1>Review Dashboard</h1>
    </div>
    <div class="masthead-actions">
      <span class="status-pill" class:muted={!snapshot.db.exists}>Read-only</span>
      <button type="button" on:click={refresh} disabled={refreshing}>{refreshing ? 'Refreshing' : 'Refresh'}</button>
    </div>
  </header>

  {#if refreshError}
    <p class="notice danger">{refreshError}</p>
  {/if}
  {#if snapshot.db.error}
    <p class="notice warn">{snapshot.db.error}</p>
  {/if}

  <section class="metric-strip" aria-label="Current aggregate state">
    <article>
      <span>Runs</span>
      <strong>{fmt(snapshot.runs.total)}</strong>
      <small>{fmt(snapshot.runs.unscored)} unscored</small>
    </article>
    <article>
      <span>Local output</span>
      <strong>{fmt(snapshot.runs.findings)}</strong>
      <small>{fmt(snapshot.runs.watch_items)} watch</small>
    </article>
    <article>
      <span>External items</span>
      <strong>{fmt(snapshot.external.total)}</strong>
      <small>{snapshot.external.link_rate} linked</small>
    </article>
    <article>
      <span>DB size</span>
      <strong>{snapshot.db.size_label}</strong>
      <small>{snapshot.db.backend || 'sqlite'}</small>
    </article>
  </section>

  <section class="split-section">
    <div>
      <h2>DB State</h2>
      <dl class="details">
        <div>
          <dt>Scope</dt>
          <dd>{snapshot.scope.repo}</dd>
        </div>
        <div>
          <dt>Path</dt>
          <dd>{snapshot.db.path || snapshot.db.target}</dd>
        </div>
        <div>
          <dt>Review items</dt>
          <dd>{tableCount('review_items')}</dd>
        </div>
        <div>
          <dt>Verdicts</dt>
          <dd>{tableCount('item_verdicts')}</dd>
        </div>
      </dl>
    </div>

    <div>
      <div class="section-heading compact">
        <h2>Workspace</h2>
        <span class="inline-state" class:warn={workspaceEligibility?.status === 'manual_review_recommended'} class:block={workspaceEligibility?.status === 'blocked'}>
          {workspaceStatusText(workspaceEligibility?.status)}
        </span>
      </div>
      {#if currentWorkspace?.configured}
        <div class="workspace-card">
          <div>
            <strong>{currentWorkspace.repo || snapshot.scope.repo}</strong>
            <span>{currentWorkspace.branch || 'detached'} / {currentWorkspace.head_sha || 'no head'}</span>
            <small>{currentWorkspace.path || currentWorkspace.requested_path}</small>
          </div>
          <dl>
            <div>
              <dt>Dirty</dt>
              <dd>{currentWorkspace.dirty ? 'yes' : 'no'}{currentWorkspace.untracked_count ? ` / ${fmt(currentWorkspace.untracked_count)} untracked` : ''}</dd>
            </div>
            <div>
              <dt>Ahead / behind</dt>
              <dd>{fmt(currentWorkspace.ahead)} / {fmt(currentWorkspace.behind)}{currentWorkspace.upstream ? ` vs ${currentWorkspace.upstream}` : ''}</dd>
            </div>
            <div>
              <dt>Diff</dt>
              <dd>{fmt(currentWorkspace.changed_files)} files / {currentWorkspace.diff_size_label}</dd>
            </div>
            <div>
              <dt>Fingerprint</dt>
              <dd>{currentWorkspace.diff_fingerprint_short || 'none'}</dd>
            </div>
          </dl>
        </div>
        <p class="workspace-summary">{workspaceEligibility?.summary}</p>
        <div class="gate-list workspace-gates">
          {#each workspaceEligibility?.gates ?? [] as gate}
            <article class:ready={gate.status === 'pass'} class:warn={gate.status === 'warn'} class:block={gate.status === 'block'}>
              <div>
                <span>{gate.label}</span>
                <small>{gate.detail}</small>
              </div>
              <strong>{gateText(gate.status)}</strong>
            </article>
          {/each}
        </div>
      {:else}
        <p class="empty">No workspace target is configured.</p>
      {/if}
      {#if snapshot.workspace?.recent?.length}
        <div class="workspace-list subdued">
          {#each snapshot.workspace.recent as row}
            <article>
              <strong>{row.repo}</strong>
              <span>{row.branch || row.head_ref || 'detached'} / {row.head_sha || 'no head'}</span>
              <small>{row.workspace_path}</small>
            </article>
          {/each}
        </div>
      {/if}
    </div>
  </section>

  <section class="section-band">
    <div class="section-heading">
      <h2>Review History Growth</h2>
      <span>{fmt(snapshot.runs.diff_bytes)} diff bytes recorded</span>
    </div>
    {#if snapshot.growth.length}
      <div class="growth-grid">
        {#each snapshot.growth as row}
          <div class="bar-row">
            <span>{row.month}</span>
            <div class="bar-track"><i style={barStyle(row)}></i></div>
            <strong>{fmt(row.runs)}</strong>
            <small>{fmt(row.findings)} findings / {fmt(row.watch_items)} watch</small>
          </div>
        {/each}
      </div>
    {:else}
      <p class="empty">No review history in this scope.</p>
    {/if}
  </section>

  <section class="split-section">
    <div>
      <div class="section-heading compact">
        <h2>Learning Readiness</h2>
        <span>{snapshot.learning_readiness.postgres_optional_backend}</span>
      </div>
      <div class="mini-grid">
        {#each readinessRows() as row}
          <article>
            <span>{row[0]}</span>
            <strong>{fmt(row[1])}</strong>
          </article>
        {/each}
      </div>
      <div class="specbackfill-line" class:ready={specbackfill?.available}>
        <div>
          <span>Specbackfill</span>
          <strong>{specbackfill?.available ? 'Available' : 'Missing'}</strong>
        </div>
        <p>{specbackfill?.summary}</p>
        {#if specbackfill?.db_items}
          <small>{fmt(specbackfill.db_items)} items / {fmt(specbackfill.db_runs)} runs / latest run {fmt(specbackfill.last_run_id)}</small>
        {/if}
      </div>
      <div class="gate-list">
        {#each snapshot.postgres_readiness as gate}
          <article class:ready={gate.ready}>
            <span>{gate.label}</span>
            <strong>{fmt(gate.current)} / {fmt(gate.threshold)}</strong>
          </article>
        {/each}
      </div>
    </div>

    <div>
      <h2>Scoring / Stamp Backlog</h2>
      <div class="mini-grid backlog">
        {#each backlogRows() as row}
          <article>
            <span>{row[0]}</span>
            <strong>{fmt(row[1])}</strong>
          </article>
        {/each}
      </div>
      <div class="queue-lines">
        {#each snapshot.backfill_queue.records as row}
          <p><strong>{row.source_kind}/{row.state}</strong> {row.reason} ({fmt(row.count)})</p>
        {/each}
      </div>
    </div>
  </section>

  <section class="section-band">
    <div class="section-heading">
      <h2>Next CLI</h2>
      <span>{snapshot.generated_at_utc}</span>
    </div>
    <div class="command-list">
      {#each snapshot.next_commands as command}
        <article>
          <div>
            <strong>{command.label}</strong>
            <span>{command.reason}</span>
          </div>
          <code>{command.command}</code>
        </article>
      {/each}
    </div>
  </section>
</main>
