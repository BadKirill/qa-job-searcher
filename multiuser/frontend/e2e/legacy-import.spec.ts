import { expect, test } from "@playwright/test";

test("imported owner signs in with the old password and keeps history, checked files and refused unsafe documents", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByLabel("Код первоначальной настройки")).toHaveCount(0);
  await page.getByLabel("Пароль", { exact: false }).fill("legacy-synthetic-password");
  await page.getByRole("button", { name: "Войти", exact: true }).click();
  const active = page.getByRole("article", { name: "Legacy Active QA", exact: true });
  await expect(active).toBeVisible();
  await expect(active.getByText("Перенос: нужна повторная проверка документа.")).toBeVisible();
  await expect(active.getByText(/исход выполнения неизвестен/)).toBeVisible();
  await expect(active.getByRole("link", { name: "Скачать PDF", exact: true })).toHaveCount(1);
  await active.getByRole("button", { name: "Содержание и подтверждённые факты" }).click();
  await expect(active.getByRole("region", { name: "Проверенное содержание документа" })).toBeVisible();
  for (const format of ["PDF", "DOCX"]) {
    const downloadEvent = page.waitForEvent("download");
    await active.getByRole("link", { name: `Скачать ${format}`, exact: true }).click();
    const download = await downloadEvent;
    expect(await download.failure()).toBeNull();
    expect(download.suggestedFilename()).toMatch(new RegExp(`\\.${format.toLowerCase()}$`));
  }
  const tasks = await (await page.request.get("/api/tasks")).json();
  const blocked = tasks.items.find((task: { status: string }) => task.status === "blocked");
  expect(blocked.artifacts).toHaveLength(1);
  expect((await page.request.get(`/api/artifacts/${blocked.artifacts[0].id}/download`)).status()).toBe(409);
  expect((await page.request.get(`/api/tasks/${blocked.id}/document-preview`)).status()).toBe(404);
  await page.getByRole("button", { name: "Отклики", exact: true }).click();
  await expect(page.getByRole("article")).toHaveCount(2);
  const incomplete = page.getByRole("article", { name: "Legacy Incomplete QA", exact: true });
  await expect(incomplete.getByRole("alert")).toContainText("Историческая вакансия без описания");
  await expect(incomplete.getByRole("button", { name: "Сделать CV", exact: true })).toBeDisabled();
  await expect(incomplete.getByRole("button", { name: "Сделать кавер леттер", exact: true })).toBeDisabled();
  const applied = await (await page.request.get("/api/jobs?tab=applied")).json();
  const historical = applied.items.find((job: { needs_refresh?: boolean }) => job.needs_refresh);
  const session = await (await page.request.get("/api/session")).json();
  expect((await page.request.post(`/api/jobs/${historical.id}/documents`, {
    headers: { origin: "http://127.0.0.1:8013", "x-csrf-token": session.csrf_token },
    data: { kind: "cv", language: "en", request_id: "e2e-incomplete" },
  })).status()).toBe(409);
  const appliedCard = page.getByRole("article", { name: "Legacy Applied QA", exact: true });
  await expect(appliedCard.getByRole("link", { name: "Скачать PDF", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Архив", exact: true }).click();
  await expect(page.getByRole("article", { name: "Legacy Archived QA", exact: true })).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Архив", exact: true }).click();
  await expect(page.getByRole("article", { name: "Legacy Archived QA", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Профиль и шаблон", exact: true }).click();
  await expect(page.getByText("Активный эталон", { exact: true })).toBeVisible();
  const originalEvent = page.waitForEvent("download");
  await page.getByRole("link", { name: /Скачать исходный PDF/ }).click();
  const original = await originalEvent;
  expect(await original.failure()).toBeNull();
  expect(original.suggestedFilename()).toBe("candidate.pdf");
  for (const path of ["/api/templates/foreign-template/source", "/api/tasks/foreign-task/document-preview", "/api/artifacts/foreign-artifact/download"]) {
    expect((await page.request.get(path)).status()).toBe(404);
  }
  await page.getByRole("button", { name: "Выйти", exact: true }).click();
  await expect(page.getByRole("button", { name: "Войти", exact: true })).toBeVisible();
  expect((await page.request.get("/api/jobs")).status()).toBe(401);
});
