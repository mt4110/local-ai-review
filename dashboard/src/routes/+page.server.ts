import { loadDashboardSnapshot } from '$lib/server/dashboard-data';

export async function load() {
  return {
    snapshot: await loadDashboardSnapshot()
  };
}
