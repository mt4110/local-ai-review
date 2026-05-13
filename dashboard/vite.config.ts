import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

const dashboardPort = Number(process.env.LLREVIEW_DASHBOARD_PORT ?? 3069);

export default defineConfig({
  plugins: [sveltekit()],
  server: {
    host: '127.0.0.1',
    port: dashboardPort,
    strictPort: true
  },
  preview: {
    host: '127.0.0.1',
    port: dashboardPort,
    strictPort: true
  }
});
