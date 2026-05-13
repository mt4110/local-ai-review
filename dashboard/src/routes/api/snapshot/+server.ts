import { json } from '@sveltejs/kit';
import { loadDashboardSnapshot } from '$lib/server/dashboard-data';

export async function GET() {
  return json(await loadDashboardSnapshot());
}
