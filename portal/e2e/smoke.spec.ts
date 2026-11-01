import { expect, test } from "@playwright/test";
import { ADMIN, STUDENT, login, watchErrors } from "./helpers";

const ADMIN_PAGES = [
  "/admin",
  "/admin/nodes",
  "/admin/jobs",
  "/admin/alerts",
  "/admin/usage",
  "/admin/schedules",
  "/admin/users",
  "/admin/catalog",
  "/admin/rollout",
  "/admin/audit",
];
const STUDENT_PAGES = ["/", "/submit", "/usage", "/projects", "/settings"];

async function visitAll(page: import("@playwright/test").Page, paths: string[]) {
  const errors = watchErrors(page);
  for (const path of paths) {
    const before = errors.length;
    const res = await page.goto(path);
    expect(res?.status(), path).toBeLessThan(400);
    await expect(page.locator("h1").first(), path).toBeVisible();
    await page.waitForLoadState("networkidle");
    await expect(page.getByTestId("error-boundary"), path).toHaveCount(0);
    await expect(page.getByText("Something went wrong"), path).toHaveCount(0);
    expect(errors.slice(before), `console errors on ${path}`).toEqual([]);
  }
}

test("every student page renders without errors", async ({ page }) => {
  await login(page, STUDENT);
  await visitAll(page, STUDENT_PAGES);
});

test("every admin page renders without errors", async ({ page }) => {
  await login(page, ADMIN);
  await visitAll(page, ADMIN_PAGES);
});

test("an uncaught browser error is reported to the server", async ({ page }) => {
  await login(page, STUDENT);
  await page.goto("/");
  const report = page.waitForResponse((r) => r.url().endsWith("/api/client-errors") && r.request().method() === "POST");
  await page.evaluate(() => {
    setTimeout(() => {
      throw new Error("e2e deliberate error");
    }, 0);
  });
  const res = await report;
  expect(res.status()).toBe(202);
  expect(res.request().postDataJSON()).toMatchObject({ message: "e2e deliberate error" });
  expect(res.headers()["x-request-id"]).toBeTruthy();
});

test("an API error shows its request ID", async ({ page }) => {
  await login(page, STUDENT);
  await page.goto("/admin/nodes");
  const box = page.locator(".states-error");
  await expect(box).toBeVisible();
  await expect(box.locator(".states-error-id")).toContainText(/Request ID: \S{8,}/);
});
