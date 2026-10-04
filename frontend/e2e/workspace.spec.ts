import { expect, test, type Page } from "@playwright/test";
import path from "node:path";

const password = "e2e-owner-password-2026";
const cvPath = path.resolve("../tests/fixtures/candidate.docx");

async function signIn(page: Page) {
  await page.goto("/");
  const session = await (await page.request.get("/api/session")).json();
  if (session.authenticated) return;
  if (session.setup_required) {
    await page
      .getByLabel("Код первоначальной настройки")
      .fill("e2e-setup-token-for-tests");
    await page.getByLabel("Пароль", { exact: false }).fill(password);
    await page
      .getByRole("button", { name: "Создать рабочее пространство" })
      .click();
  } else {
    await page.getByLabel("Пароль", { exact: true }).fill(password);
    await page.getByRole("button", { name: "Войти", exact: true }).click();
  }
  await expect(
    page.getByRole("heading", { name: "Подходящие вакансии." }),
  ).toBeVisible();
}

test("private access, pagination, applied and archive survive browser reload", async ({
  page,
}) => {
  expect((await page.request.get("/api/jobs")).status()).toBe(401);
  expect(
    (await page.request.get("/api/artifacts/guessed/download")).status(),
  ).toBe(401);
  await signIn(page);
  await expect(page.getByRole("article")).toHaveCount(10);
  await expect(page.getByText("21 вакансий", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: "Страница 3", exact: true }).click();
  await expect(page.getByRole("article")).toHaveCount(1);
  await expect(
    page.getByRole("heading", { name: "Senior QA Engineer 21" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Не подходит", exact: true }).click();
  await expect(page.getByRole("article")).toHaveCount(10);
  await expect(page.getByText("Страница 2 из 2")).toBeVisible();
  await page.getByRole("button", { name: "Архив", exact: true }).click();
  await expect(page.getByRole("article")).toHaveCount(1);
  await page.getByRole("button", { name: "Вернуть из архива" }).click();
  await expect(page.getByText("В архиве пока пусто")).toBeVisible();
  await page
    .getByRole("button", { name: "Вакансии", exact: false })
    .first()
    .click();
  const card = page.getByRole("article", {
    name: "Senior QA Engineer 03",
    exact: true,
  });
  const popupPromise = page.waitForEvent("popup");
  await card
    .getByRole("link", { name: "Перейти на страницу вакансии" })
    .click();
  const popup = await popupPromise;
  await popup.close();
  await expect(card.getByText("applied", { exact: true })).toBeVisible();
  await page.reload();
  await expect(card).toHaveClass(/job-applied/);
  await card.getByRole("button", { name: "Не подходит" }).click();
  await page.getByRole("button", { name: "Архив", exact: true }).click();
  await expect(card.getByText("applied", { exact: true })).toBeVisible();
  await card.getByRole("button", { name: "Вернуть из архива" }).click();
  await page
    .getByRole("button", { name: "Вакансии", exact: false })
    .first()
    .click();
  await card.getByRole("button", { name: "Отменить applied" }).click();
  await expect(card.getByText("applied", { exact: true })).toHaveCount(0);
  const csrfFailure = await page.request.patch(`/api/jobs/unknown/state`, {
    data: { applied: true },
  });
  expect(csrfFailure.status()).toBe(403);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: "test-results/jobs-desktop.png",
    fullPage: false,
  });
});

test("template setup, persistent web conversation, confirmed facts and downloadable checked documents", async ({
  page,
}) => {
  await signIn(page);
  const card = page.getByRole("article", {
    name: "Senior QA Engineer 03",
    exact: true,
  });
  await card.getByRole("button", { name: "Сделать CV", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Профиль и шаблон" }),
  ).toBeVisible();
  await page.getByLabel("Файл эталонного CV").setInputFiles({
    name: "unsafe.html",
    mimeType: "text/html",
    buffer: Buffer.from("<script>attack()</script>"),
  });
  await expect(page.getByRole("alert")).toContainText(
    "Выберите файл PDF или DOCX",
  );
  await page.getByLabel("Файл эталонного CV").setInputFiles({
    name: "broken.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("Not a PDF"),
  });
  await expect(page.getByRole("alert")).toBeVisible();
  await page.getByLabel("Файл эталонного CV").setInputFiles(cvPath);
  await expect(
    page.getByRole("heading", { name: "Факты из вашего CV" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Сохранить эталон", exact: true }),
  ).toBeDisabled();
  await page
    .getByRole("textbox", { name: "Сообщение агенту" })
    .fill("Please keep dates and company names unchanged.");
  await page.getByRole("button", { name: "Отправить", exact: true }).click();
  await expect(
    page.getByText("Я могу помочь с шаблоном.", { exact: false }),
  ).toBeVisible();
  await page.reload();
  await page
    .getByRole("button", { name: "Профиль и шаблон", exact: true })
    .click();
  await page
    .getByRole("button", { name: /candidate.docx.*Продолжить/ })
    .click();
  await expect(
    page.getByText("Please keep dates and company names unchanged."),
  ).toBeVisible();
  await page.getByRole("checkbox").check();
  await page
    .getByRole("button", { name: "Сохранить эталон", exact: true })
    .click();
  await expect(page.getByText("Активный эталон")).toBeVisible();
  await expect(
    page
      .getByRole("main")
      .getByRole("button", { name: "Изменить эталонный шаблон" }),
  ).toBeVisible();
  await page.getByLabel("Файл эталонного CV").setInputFiles(cvPath);
  await expect(
    page.getByText("До сохранения нового эталона используется текущая версия."),
  ).toBeVisible();
  await page.getByRole("button", { name: "Закрыть черновик" }).click();
  await expect(page.getByText("Активный эталон")).toBeVisible();
  await page
    .getByRole("button", { name: "Вакансии", exact: false })
    .first()
    .click();
  await card.getByRole("button", { name: "Сделать CV", exact: true }).click();
  await expect(card.getByRole("link", { name: "Скачать PDF" })).toBeVisible();
  const pdfDownload = page.waitForEvent("download");
  await card.getByRole("link", { name: "Скачать PDF" }).click();
  const pdf = await pdfDownload;
  expect(pdf.suggestedFilename()).toMatch(/\.pdf$/);
  expect(await pdf.failure()).toBeNull();
  const docxDownload = page.waitForEvent("download");
  await card.getByRole("link", { name: "Скачать DOCX" }).click();
  const docx = await docxDownload;
  expect(docx.suggestedFilename()).toMatch(/\.docx$/);
  expect(await docx.failure()).toBeNull();
  await card
    .getByRole("button", { name: "Сделать кавер леттер", exact: true })
    .click();
  await expect(card.getByRole("link", { name: "Скачать PDF" })).toHaveCount(2);
  await page.reload();
  await expect(card.getByRole("link", { name: "Скачать PDF" })).toHaveCount(2);
  await card
    .getByRole("button", { name: /Обсудить и доработать/ })
    .first()
    .click();
  await expect(
    card.getByRole("region", { name: "Диалог с агентом" }),
  ).toBeVisible();
});

test("review rejection and provider failure never expose downloads", async ({
  page,
}) => {
  await signIn(page);
  const blocked = page.getByRole("article", {
    name: "Blocked QA Engineer",
    exact: true,
  });
  await blocked
    .getByRole("button", { name: "Сделать CV", exact: true })
    .click();
  await expect(blocked.getByText("Проверка не пройдена")).toBeVisible();
  await expect(blocked.getByRole("link", { name: /Скачать/ })).toHaveCount(0);
  const tasks = await (await page.request.get("/api/tasks")).json();
  const blockedTask = tasks.items.find(
    (task: { status: string }) => task.status === "blocked",
  );
  expect(blockedTask).toBeTruthy();
  expect(
    (
      await page.request.get(`/api/artifacts/${blockedTask.id}/download`)
    ).status(),
  ).toBe(404);
  await blocked.getByRole("button", { name: /Обсудить с агентом/ }).click();
  await expect(
    blocked.getByRole("region", { name: "Диалог с агентом" }),
  ).toBeVisible();
  const failed = page.getByRole("article", {
    name: "Unavailable QA Engineer",
    exact: true,
  });
  await failed.getByRole("button", { name: "Сделать CV", exact: true }).click();
  await expect(failed.getByText("Ошибка", { exact: true })).toBeVisible();
  await expect(failed.getByRole("link", { name: /Скачать/ })).toHaveCount(0);
});

test("conflicting template activation preserves the newest confirmed version", async ({
  page,
  context,
}) => {
  await signIn(page);
  await page
    .getByRole("button", { name: "Профиль и шаблон", exact: true })
    .click();
  await page.getByLabel("Файл эталонного CV").setInputFiles(cvPath);
  await page.getByRole("checkbox").check();
  const other = await context.newPage();
  await other.goto("/");
  await other
    .getByRole("button", { name: "Профиль и шаблон", exact: true })
    .click();
  await other.getByLabel("Файл эталонного CV").setInputFiles(cvPath);
  await other.getByRole("checkbox").check();
  await other
    .getByRole("button", { name: "Сохранить эталон", exact: true })
    .click();
  await expect(
    other.getByText("Эталон сохранён.", { exact: false }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Сохранить эталон", exact: true })
    .click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.getByText("Активный эталон")).toBeVisible();
  await other.close();
});

test("cancellation survives a late worker response and refresh preserves state", async ({
  page,
}) => {
  await signIn(page);
  const card = page.getByRole("article", {
    name: "Senior QA Engineer 04",
    exact: true,
  });
  await card.getByRole("button", { name: "Сделать CV", exact: true }).click();
  await card.getByRole("button", { name: "Отменить создание" }).click();
  await expect(card.getByText("Отменено", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Обновить поиск" }).click();
  await expect(
    page.getByText("Поиск запущен.", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Обновить поиск" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: /Показать свежую выдачу/ }).click();
  await page.reload();
  await expect(card.getByText("Отменено", { exact: true })).toBeVisible();
  await expect(card.getByRole("link", { name: /Скачать/ })).toHaveCount(0);
});

test("mobile layout stays within viewport and logout closes access", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page);
  await expect(page.getByRole("article")).toHaveCount(10);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/jobs-mobile.png",
    fullPage: false,
  });
  await page
    .getByRole("button", { name: "Выйти из аккаунта", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "С возвращением" }),
  ).toBeVisible();
  expect((await page.request.get("/api/tasks")).status()).toBe(401);
  await page.getByLabel("Пароль", { exact: true }).fill("incorrect-password");
  await page.getByRole("button", { name: "Войти", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "С возвращением" }),
  ).toBeVisible();
});
