import { expect, test } from "@playwright/test";
import { ADMIN, e2eNodeId, login } from "./helpers";

test("an admin drains and releases the agent's node", async ({ page }) => {
  const nodeId = e2eNodeId();
  await login(page, ADMIN);
  await page.goto("/admin/nodes");

  const row = page.locator("tr", { has: page.locator(`a[href="/admin/nodes/${nodeId}"]`) });
  await expect(row).toBeVisible();
  const chip = row.locator("td .chip");

  await row.getByRole("button", { name: "Drain" }).click();
  await expect(chip).toHaveText("draining");

  await row.getByRole("button", { name: "Release" }).click();
  await expect(chip).not.toHaveText("draining");
  await expect(row.getByRole("button", { name: "Release" })).toHaveCount(0);
});
