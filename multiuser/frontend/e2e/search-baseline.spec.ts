import { expect, test } from "@playwright/test";
import baseline from "../../tests/fixtures/search-baseline.json" with { type: "json" };

test("synthetic professions survive real matching, API, browser reload and state changes", async ({ page }) => {
  const accepted = baseline.cases.filter((item) => item.baseline.accepted);
  expect((await page.request.get("/api/jobs")).status()).toBe(401);
  await page.goto("/");
  await page.getByLabel("Код первоначальной настройки").fill("e2e-setup-token-for-tests");
  await page.getByLabel("Пароль", { exact: false }).fill("baseline-synthetic-password");
  await page.getByRole("button", { name: "Создать рабочее пространство" }).click();
  await expect(page.getByRole("article")).toHaveCount(accepted.length);
  const result = await page.request.get("/api/jobs");
  expect(result.status()).toBe(200);
  const data = await result.json();
  expect(data.items.map((item: { source_id: string }) => item.source_id).sort())
    .toEqual(accepted.map((item) => item.id).sort());
  for (const item of accepted) {
    await expect(page.getByRole("article", { name: item.job.title, exact: true }).first())
      .toBeVisible();
  }
  for (const item of baseline.cases.filter((entry) => !entry.baseline.accepted)) {
    expect(data.items.some((entry: { source_id: string }) => entry.source_id === item.id))
      .toBe(false);
  }
  const designer = page.getByRole("article", { name: "Senior Product Designer", exact: true });
  await designer.getByText("Полное описание", { exact: false }).click();
  await expect(designer.getByText("Design accessible products in Figma.")).toBeVisible();
  await designer.getByRole("button", { name: "Не подходит", exact: true }).click();
  await page.reload();
  await page.getByRole("button", { name: "Архив", exact: true }).click();
  await expect(designer).toBeVisible();
  await designer.getByRole("button", { name: "Вернуть из архива" }).click();
  await expect(designer).toHaveCount(0);
  await page.getByRole("button", { name: "Вакансии", exact: false }).first().click();
  await expect(designer).toBeVisible();
  await page.getByRole("button", { name: "Выйти", exact: true }).click();
  await expect(page.getByRole("button", { name: "Войти", exact: true })).toBeVisible();
  expect((await page.request.get("/api/jobs")).status()).toBe(401);
});

test("browser session cannot read or mutate another owner's objects in the same PostgreSQL schema", async ({ page }) => {
  await page.goto("/");
  const initial = await (await page.request.get("/api/session")).json();
  if (initial.setup_required) {
    await page.getByLabel("Код первоначальной настройки").fill("e2e-setup-token-for-tests");
  }
  await page.getByLabel("Пароль", { exact: false }).fill("baseline-synthetic-password");
  await page.getByRole("button", {
    name: initial.setup_required ? "Создать рабочее пространство" : "Войти", exact: true,
  }).click();
  await expect(page.getByRole("article")).toHaveCount(6);
  const session = await (await page.request.get("/api/session")).json();
  const headers = { origin: new URL(page.url()).origin, "x-csrf-token": session.csrf_token };
  const routes = [
    "/api/jobs?snapshot_id=foreign-snapshot",
    "/api/templates/foreign-template/source",
    "/api/templates/foreign-template/pages/1",
    "/api/tasks/foreign-task",
    "/api/tasks/foreign-task/review",
    "/api/tasks/foreign-task/document-preview",
    "/api/conversations/foreign-conversation/messages",
    "/api/artifacts/foreign-artifact/download",
    "/api/artifacts/foreign-artifact/preview",
  ];
  for (const route of routes) expect((await page.request.get(route)).status(), route).toBe(404);
  expect((await page.request.post("/api/tasks/foreign-task/cancel", { headers })).status()).toBe(404);
  expect((await page.request.delete("/api/tasks/foreign-task", { headers })).status()).toBe(404);
  expect((await page.request.post("/api/conversations/foreign-conversation/messages", {
    headers, data: { text: "forged", request_id: "foreign-message" },
  })).status()).toBe(404);
  await page.getByRole("button", { name: "Профиль и шаблон", exact: true }).click();
  await expect(page.getByText("Foreign CV.pdf")).toHaveCount(0);
  await expect(page.getByText("Foreign private fact")).toHaveCount(0);
  expect(await (await page.request.get("/api/profile")).json()).toEqual({ active: null, drafts: [] });
});
