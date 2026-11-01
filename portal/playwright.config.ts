import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.E2E_BASE_URL || "http://127.0.0.1:3000";
const jobTimeout = Number(process.env.E2E_JOB_TIMEOUT_MS) || 3 * 60_000;

// The tests drive a running stack (scripts/dev.sh): portal, control API and a
// local train agent. They run one at a time because they share that one agent.
export default defineConfig({
  testDir: "./e2e",
  outputDir: "./test-results",
  timeout: Math.max(5 * 60_000, jobTimeout + 2 * 60_000),
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
