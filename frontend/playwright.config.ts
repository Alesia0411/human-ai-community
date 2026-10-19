// Synthetic E2E: dedicated ports, temporary backend data, never reuse an existing service.
import { defineConfig } from '@playwright/test'
export default defineConfig({
  testDir: './tests', workers: 1, timeout: 60000,
  use: { baseURL: 'http://127.0.0.1:15174', headless: true, channel: process.env.HAI_TEST_BROWSER_CHANNEL || undefined, screenshot: 'only-on-failure' },
  webServer: [
    { command: 'python ../evaluations/demo_server.py', url: 'http://127.0.0.1:18013/api/v1/health/live', reuseExistingServer: false, timeout: 60000 },
    { command: 'npm run dev:h5', env: { HAI_DEV_PORT: '15174', HAI_DEV_API: 'http://127.0.0.1:18013' }, url: 'http://127.0.0.1:15174', reuseExistingServer: false, timeout: 60000 },
  ],
})
