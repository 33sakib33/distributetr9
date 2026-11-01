import { expect, test } from "@playwright/test";
import { JOB_TIMEOUT_MS, STUDENT, login, statusChip, submitCpuJob } from "./helpers";

const TRAIN = `import os
print("hello from e2e train.py", flush=True)
out = os.environ["OUTPUT_DIR"]
os.makedirs(out, exist_ok=True)
with open(os.path.join(out, "result.txt"), "w") as f:
    f.write("done\\n")
`;

const SLEEPER = `import time
print("sleeping for a while", flush=True)
time.sleep(120)
`;

test("a student submits a CPU job and sees it complete", async ({ page }) => {
  await login(page, STUDENT);
  await submitCpuJob(page, TRAIN);

  await expect(statusChip(page)).toHaveText("completed", { timeout: JOB_TIMEOUT_MS });
  await expect(page.locator(".log")).toContainText("hello from e2e train.py");
  await expect(page.getByRole("link", { name: "Download all outputs" })).toBeVisible();
});

test("a running job can be cancelled", async ({ page }) => {
  await login(page, STUDENT);
  await submitCpuJob(page, SLEEPER);

  await expect(statusChip(page)).toHaveText("running", { timeout: JOB_TIMEOUT_MS });
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(statusChip(page)).toHaveText("cancelled", { timeout: 2 * 60_000 });
});
