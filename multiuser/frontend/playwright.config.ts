import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60000,
  expect: { timeout: 15000 },
  reporter: [["list"], ["html", { open: "never" }]],
  projects: [
    {
      name: "cutover",
      testMatch: "cutover.spec.ts",
      use: { baseURL: "http://127.0.0.1:8015" },
    },
    {
      name: "accounts",
      testMatch: "accounts.spec.ts",
      use: { baseURL: "http://127.0.0.1:8014" },
    },
    { name: "workspace", testMatch: "workspace.spec.ts" },
    {
      name: "search-baseline",
      testMatch: "search-baseline.spec.ts",
      use: { baseURL: "http://127.0.0.1:8012" },
    },
    {
      name: "legacy-import",
      testMatch: "legacy-import.spec.ts",
      use: { baseURL: "http://127.0.0.1:8013" },
    },
  ],
  use: {
    baseURL: "http://127.0.0.1:8011",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    ...devices["Desktop Chrome"],
    viewport: { width: 1440, height: 1050 },
  },
  webServer: [
    {
      command: `cd .. && exec ${process.env.QA_E2E_PYTHON || ".venv/bin/python"} tests/e2e_server.py --cutover`,
      url: "http://127.0.0.1:8015/api/session",
      reuseExistingServer: false,
      timeout: 60000,
      gracefulShutdown: { signal: "SIGTERM", timeout: 10000 },
    },
    {
      command: `cd .. && exec ${process.env.QA_E2E_PYTHON || ".venv/bin/python"} tests/e2e_server.py`,
      url: "http://127.0.0.1:8011/api/session",
      reuseExistingServer: false,
      timeout: 60000,
      gracefulShutdown: { signal: "SIGTERM", timeout: 10000 },
    },
    {
      command: `cd .. && exec ${process.env.QA_E2E_PYTHON || ".venv/bin/python"} tests/e2e_server.py --baseline`,
      url: "http://127.0.0.1:8012/api/session",
      reuseExistingServer: false,
      timeout: 60000,
      gracefulShutdown: { signal: "SIGTERM", timeout: 10000 },
    },
    {
      command: `cd .. && exec ${process.env.QA_E2E_PYTHON || ".venv/bin/python"} tests/e2e_server.py --legacy-import`,
      url: "http://127.0.0.1:8013/api/session",
      reuseExistingServer: false,
      timeout: 60000,
      gracefulShutdown: { signal: "SIGTERM", timeout: 10000 },
    },
    {
      command: `cd .. && exec ${process.env.QA_E2E_PYTHON || ".venv/bin/python"} tests/e2e_server.py --accounts`,
      url: "http://127.0.0.1:8014/api/session",
      reuseExistingServer: false,
      timeout: 60000,
      gracefulShutdown: { signal: "SIGTERM", timeout: 10000 },
    },
  ],
});
