import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60000,
  expect: { timeout: 15000 },
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: "http://127.0.0.1:8011",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    ...devices["Desktop Chrome"],
    viewport: { width: 1440, height: 1050 },
  },
  webServer: {
    command: `cd .. && ${process.env.QA_E2E_PYTHON || ".venv/bin/python"} tests/e2e_server.py`,
    url: "http://127.0.0.1:8011/api/session",
    reuseExistingServer: false,
    timeout: 60000,
  },
});
