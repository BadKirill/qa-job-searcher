import { expect, test, type Page } from "@playwright/test";
import path from "node:path";
import { createHash } from "node:crypto";

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

test("PDF references preserve paragraphs, show original pages and require confirmation after rereading", async ({
  page,
  browser,
}) => {
  await signIn(page);
  const before = await (await page.request.get("/api/profile")).json();
  await page
    .getByRole("button", { name: "Профиль и шаблон", exact: true })
    .click();
  const upload = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/template-drafts") &&
      response.request().method() === "POST",
  );
  await page
    .getByLabel("Файл эталонного CV")
    .setInputFiles(path.resolve("../tests/fixtures/candidate.pdf"));
  const draft = await (await upload).json();
  expect(draft.reference.page_count).toBe(2);
  const summaryIndex = draft.facts.findIndex((fact: { text: string }) =>
    fact.text.startsWith("QA Engineer with experience in Python"),
  );
  expect(summaryIndex).toBeGreaterThanOrEqual(0);
  expect(draft.facts[summaryIndex].text).toContain("investigate failures.");
  const wrappedBullet = draft.facts.find((fact: { text: string }) =>
    fact.text.includes("Built automated API tests with Python"),
  );
  expect(wrappedBullet.text).toContain(
    "across releases while collaborating with the engineering team.",
  );
  expect(wrappedBullet.text).not.toContain("(cid:");
  const image = page.getByRole("img", { name: "Исходный PDF, страница 1" });
  await expect(image).toBeVisible();
  await expect
    .poll(() =>
      image.evaluate((element) => (element as HTMLImageElement).naturalWidth),
    )
    .toBeGreaterThan(0);
  await page
    .getByRole("button", { name: /^Показать источник: страница 2, блок/ })
    .first()
    .click();
  const secondPage = page.getByRole("img", {
    name: "Исходный PDF, страница 2",
  });
  await expect(secondPage).toBeVisible();
  await expect
    .poll(() =>
      secondPage.evaluate(
        (element) => (element as HTMLImageElement).naturalWidth,
      ),
    )
    .toBeGreaterThan(0);
  await page.getByRole("button", { name: "Увеличить", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Уместить по ширине" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Уместить по ширине" }).click();
  const source = await page.request.get(`/api/templates/${draft.id}/source`);
  expect(source.status()).toBe(200);
  const pdfBytes = await source.body();
  expect(pdfBytes.subarray(0, 4).toString()).toBe("%PDF");
  expect(createHash("sha256").update(pdfBytes).digest("hex")).toBe(
    draft.reference.sha256,
  );
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("link", { name: /Скачать исходный PDF/ }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("candidate.pdf");
  expect(await download.failure()).toBeNull();
  const anonymous = await browser.newContext({
    baseURL: "http://127.0.0.1:8011",
  });
  expect(
    (
      await anonymous.request.get(`/api/templates/${draft.id}/pages/1`)
    ).status(),
  ).toBe(401);
  expect(
    (await anonymous.request.get(`/api/templates/${draft.id}/source`)).status(),
  ).toBe(401);
  expect(
    (
      await anonymous.request.post(`/api/templates/${draft.id}/reparse`, {
        data: {},
        headers: { Origin: "http://127.0.0.1:8011" },
      })
    ).status(),
  ).toBe(401);
  await anonymous.close();
  expect(
    (
      await page.request.post(`/api/templates/${draft.id}/reparse`, {
        data: {},
        headers: { Origin: "http://127.0.0.1:8011" },
      })
    ).status(),
  ).toBe(403);
  expect(
    (await page.request.get("/api/templates/unknown/pages/1")).status(),
  ).toBe(404);
  expect(
    (await page.request.get(`/api/templates/${draft.id}/pages/3`)).status(),
  ).toBe(404);
  await page.getByRole("checkbox").check();
  await page
    .getByRole("textbox", { name: `Факт ${summaryIndex + 1}`, exact: true })
    .fill(`${draft.facts[summaryIndex].text} Comfortable working remotely.`);
  await expect(page.getByRole("checkbox")).not.toBeChecked();
  await expect(
    page.getByRole("button", { name: "Сохранить эталон", exact: true }),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "Сохранить черновик", exact: true })
    .click();
  await expect(
    page.getByText("Черновик сохранён.", { exact: false }),
  ).toBeVisible();
  const saved = await (await page.request.get("/api/profile")).json();
  expect(saved.active.id).toBe(before.active.id);
  const savedDraft = saved.drafts.find(
    (item: { id: string }) => item.id === draft.id,
  );
  expect(savedDraft.facts[summaryIndex].source).toBe(
    `user_confirmed; original:${draft.facts[summaryIndex].source}`,
  );
  expect(savedDraft.facts[summaryIndex].kind).toBe(
    draft.facts[summaryIndex].kind,
  );
  await page.getByRole("checkbox").check();
  await page
    .getByRole("button", { name: "Сохранить эталон", exact: true })
    .click();
  await expect(
    page.getByText("Эталон сохранён.", { exact: false }),
  ).toBeVisible();
  const reparseResponse = page.waitForResponse((response) =>
    response.url().endsWith(`/api/templates/${draft.id}/reparse`),
  );
  await page
    .getByRole("button", { name: "Перечитать PDF", exact: true })
    .click();
  const reparsed = await (await reparseResponse).json();
  expect(reparsed.id).not.toBe(draft.id);
  expect(reparsed.reference.sha256).toBe(draft.reference.sha256);
  await expect(
    page.getByText("PDF прочитан заново.", { exact: false }),
  ).toBeVisible();
  await expect(page.getByRole("checkbox")).not.toBeChecked();
  await expect(
    page.getByRole("button", { name: "Сохранить эталон", exact: true }),
  ).toBeDisabled();
  expect(
    (await (await page.request.get("/api/profile")).json()).active.id,
  ).toBe(draft.id);
  await expect(
    page.getByRole("img", { name: "Исходный PDF, страница 1" }),
  ).toBeVisible();
  await page
    .getByRole("heading", { name: "Факты из вашего CV" })
    .scrollIntoViewIfNeeded();
  await page.screenshot({
    path: "test-results/pdf-reference-desktop.png",
    fullPage: false,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page
    .getByRole("heading", { name: "Исходный PDF", exact: true })
    .scrollIntoViewIfNeeded();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/pdf-reference-mobile.png",
    fullPage: false,
  });
  await page.setViewportSize({ width: 1440, height: 1050 });
  await page
    .getByRole("button", { name: "Вакансии", exact: false })
    .first()
    .click();
  const accepted = page.getByRole("article", {
    name: "Senior QA Engineer 05",
    exact: true,
  });
  const acceptedResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith("/documents") &&
      response.request().method() === "POST",
  );
  await accepted
    .getByRole("button", { name: "Сделать CV", exact: true })
    .click();
  const acceptedTask = await (await acceptedResponse).json();
  const acceptedPdf = accepted.getByRole("link", {
    name: "Скачать PDF",
    exact: true,
  });
  await expect(acceptedPdf).toBeVisible();
  const review = await (
    await page.request.get(`/api/tasks/${acceptedTask.id}/review`)
  ).json();
  expect(review.review.verdict).toBe("pass");
  expect(review.visual_review.verdict).toBe("pass");
  expect(review.visual_review.template_sha256).toBe(draft.reference.sha256);
  expect(review.visual_review.draft_hash).toBe(review.draft_hash);
  const renderedPdfResponse = await page.request.get(
    (await acceptedPdf.getAttribute("href"))!,
  );
  expect(renderedPdfResponse.status()).toBe(200);
  const renderedPdf = await renderedPdfResponse.body();
  expect(renderedPdf.subarray(0, 4).toString()).toBe("%PDF");
  expect(createHash("sha256").update(renderedPdf).digest("hex")).toBe(
    review.visual_review.rendered_sha256,
  );
  const renderedDownload = page.waitForEvent("download");
  await acceptedPdf.click();
  expect(await (await renderedDownload).failure()).toBeNull();
  const renderedDocx = await page.request.get(
    (await accepted
      .getByRole("link", { name: "Скачать DOCX", exact: true })
      .getAttribute("href"))!,
  );
  expect(renderedDocx.status()).toBe(200);
  expect((await renderedDocx.body()).subarray(0, 2).toString()).toBe("PK");
  const rejected = page.getByRole("article", {
    name: "Senior QA Engineer 06",
    exact: true,
  });
  const rejectedResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith("/documents") &&
      response.request().method() === "POST",
  );
  await rejected
    .getByRole("button", { name: "Сделать CV", exact: true })
    .click();
  const rejectedTask = await (await rejectedResponse).json();
  await expect(
    rejected.getByText("Проверка оформления не пройдена:", { exact: false }),
  ).toBeVisible();
  await expect(rejected.getByRole("link", { name: /Скачать/ })).toHaveCount(0);
  const rejectedResult = await (
    await page.request.get(`/api/tasks/${rejectedTask.id}`)
  ).json();
  expect(rejectedResult.status).toBe("blocked");
  expect(rejectedResult.artifacts).toHaveLength(0);
  expect(
    (
      await page.request.get(`/api/artifacts/${rejectedTask.id}/download`)
    ).status(),
  ).toBe(404);
});
