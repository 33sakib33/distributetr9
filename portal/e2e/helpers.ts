import { expect, type ConsoleMessage, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";

export const STUDENT = "e2e-student@uni.edu";
export const ADMIN = "admin@uni.edu";

/** Sign in through the login page (the local mock login takes just an email). */
export async function login(page: Page, email: string) {
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("link", { name: "Submit" })).toBeVisible();
  await expect(page).toHaveURL(/\/$/);
}

/**
 * Submit a single-file job on the CPU option with the python-cpu environment
 * and return the job id from the page it lands on.
 */
export async function submitCpuJob(page: Page, source: string, name = "train.py"): Promise<string> {
  await page.goto("/submit");
  await page.getByLabel("Choose a .py, .zip or .tar.gz file").setInputFiles({
    name,
    mimeType: "text/x-python",
    buffer: Buffer.from(source),
  });
  await expect(page.locator(".dropzone-file")).toContainText(name);

  await page.getByRole("radio", { name: /CPU only/ }).check();

  const env = page.locator("select", { has: page.locator("option", { hasText: "python-cpu" }) });
  const value = await env.locator("option", { hasText: "python-cpu" }).first().getAttribute("value");
  await env.selectOption(value!);

  await page.getByRole("button", { name: "Queue job" }).click();
  await page.waitForURL(/\/jobs\/[0-9a-f-]{36}/, { timeout: 60_000 });
  return page.url().split("/jobs/")[1].split(/[?#]/)[0];
}

/** How long a test waits for a job to reach a state (CI sets E2E_JOB_TIMEOUT_MS higher). */
export const JOB_TIMEOUT_MS = Number(process.env.E2E_JOB_TIMEOUT_MS) || 3 * 60_000;

/** The job status chip on a job page. */
export function statusChip(page: Page) {
  return page.locator("p.muted .chip").first();
}

/** Node id of the agent the tests started (scripts/dev.sh writes .dev/agent-train.json). */
export function e2eNodeId(): string {
  const file = process.env.E2E_AGENT_STATE || path.resolve(__dirname, "../../.dev/agent-train.json");
  return JSON.parse(readFileSync(file, "utf8")).node_id as string;
}

/** Collects console errors and uncaught page errors while a test runs. */
export function watchErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (msg: ConsoleMessage) => {
    if (msg.type() === "error") errors.push(`console: ${msg.text()} (${msg.location().url})`);
  });
  page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
  return errors;
}
