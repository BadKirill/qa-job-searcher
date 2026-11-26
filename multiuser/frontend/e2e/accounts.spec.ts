import { expect, test, type Page, type Request } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import type { Task } from "../src/types";
import { expectReadableColors } from "./visual-accessibility";
import { expectLayoutIntegrity } from "./layout-integrity";

const origin = "http://127.0.0.1:8014";
const password = "accounts-synthetic-password";

test("administrator inspects live operational counts while members cannot open diagnostics", async ({
  page,
  browser,
}) => {
  const health = await page.request.get(`${origin}/health/ready`);
  expect(health.status()).toBe(200);
  expect(await health.json()).toEqual({ ready: true });
  await signIn(page);
  await page
    .getByRole("button", { name: "Аккаунт и доступ", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Расписание и лимиты", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Открыть диагностику", exact: true })
    .click();
  const panel = page.getByRole("region", {
    name: "Диагностика исполнения",
    exact: true,
  });
  await expect(panel.getByText(/Просроченные задания/)).toBeVisible();
  await expect(panel.getByText("Telegram: 0")).toBeVisible();
  await panel.getByRole("button", { name: "Обновить диагностику" }).click();
  await expect(
    panel.getByText(/Ошибки источников за всю историю/),
  ).toBeVisible();
  await page.screenshot({
    path: "test-results/stage9-diagnostics.png",
    fullPage: true,
  });
  const invitation = await invite(page, "stage9-diagnostics");
  const context = await browser.newContext();
  try {
    const member = await context.newPage();
    await accept(member, invitation.token);
    await member
      .getByRole("button", { name: "Аккаунт и доступ", exact: true })
      .click();
    await member
      .getByRole("button", { name: "Расписание и лимиты", exact: true })
      .click();
    await expect(
      member.getByRole("button", { name: "Открыть диагностику" }),
    ).toHaveCount(0);
    expect(
      (await member.request.get(`${origin}/api/admin/diagnostics`)).status(),
    ).toBe(403);
    await page.getByRole("button", { name: "Закрыть диагностику" }).click();
    await expect(
      page.getByRole("heading", { name: "Диагностика исполнения" }),
    ).toHaveCount(0);
  } finally {
    await context.close();
  }
});

async function signIn(page: Page, login = "admin", secret = password) {
  await page.goto(origin);
  const session = await (
    await page.request.get(`${origin}/api/session`)
  ).json();
  if (session.setup_required) {
    await page
      .getByLabel("Код первоначальной настройки")
      .fill("e2e-setup-token-for-tests");
  } else {
    await page.getByLabel("Логин", { exact: true }).fill(login);
  }
  await page.getByLabel("Пароль", { exact: false }).fill(secret);
  await page
    .getByRole("button", {
      name: session.setup_required ? "Создать рабочее пространство" : "Войти",
      exact: true,
    })
    .click();
  await expect(
    page.getByRole("button", { name: "Аккаунт и доступ", exact: true }),
  ).toBeVisible();
}

async function headers(page: Page) {
  const session = await (
    await page.request.get(`${origin}/api/session`)
  ).json();
  return { origin, "x-csrf-token": session.csrf_token };
}

async function invite(page: Page, login: string) {
  const response = await page.request.post(`${origin}/api/admin/invitations`, {
    headers: await headers(page),
    data: { login, display_name: login },
  });
  expect(response.status()).toBe(201);
  return response.json();
}

async function accept(page: Page, token: string) {
  await page.goto(`${origin}/#invite=${encodeURIComponent(token)}`);
  await expect(
    page.getByRole("heading", { name: "Принять приглашение" }),
  ).toBeVisible();
  expect(new URL(page.url()).hash).toBe("");
  await page.getByLabel("Пароль", { exact: true }).fill(password);
  await page
    .getByRole("button", { name: "Принять приглашение", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Аккаунт и доступ" }),
  ).toBeVisible();
}

test("a saved design example survives reload and previous criteria remain visible", async ({ page, browser }) => {
  await signIn(page);
  const invitation = await invite(page, "profile-version-designer");
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  try {
    const member = await context.newPage();
    await accept(member, invitation.token);
    const openCriteria = () => member.getByRole("button", { name: "Настроить критерии" }).click();
    await openCriteria();
    const examples = member.getByLabel("Пример профиля", { exact: true });
    const historyTitle = member.getByText("История версий профиля", { exact: true });
    const history = member.locator(".profile-history > summary");
    await expect(history).toBeInViewport();
    await expect(historyTitle).toHaveCSS("font-size", "20px");
    await expect(history).toHaveAccessibleDescription("Здесь появятся ваши сохранённые критерии");
    await expect(examples).toHaveValue("");
    await examples.selectOption("designer");
    await member.getByRole("button", { name: "Применить пример" }).click();
    await expect(member.getByText("Пример применён. Критерии сохранены в вашем аккаунте.")).toBeVisible();
    const startupReads = ["/api/profile", "/api/jobs", "/api/tasks", "/api/workspace"];
    const startupPaths = new Set([...startupReads, "/api/events"]);
    const startupRequests = new Set<Request>();
    const failures: string[] = [];
    let reloaded = false;
    const markReload = () => {
      reloaded = true;
    };
    const recordRequest = (request: Request) => {
      if (reloaded && startupPaths.has(new URL(request.url()).pathname)) {
        startupRequests.add(request);
      }
    };
    const recordFailure = (request: Request) => {
      if (startupRequests.has(request)) failures.push(new URL(request.url()).pathname);
    };
    member.once("framenavigated", markReload);
    member.on("request", recordRequest);
    member.on("requestfailed", recordFailure);
    const startupResponses = [...startupPaths].map((path) =>
      member.waitForResponse((response) =>
        startupRequests.has(response.request()) &&
        new URL(response.url()).pathname === path && response.ok(),
      ),
    );
    await member.reload();
    await Promise.all(startupResponses);
    expect(failures).toEqual([]);
    member.off("request", recordRequest);
    member.off("requestfailed", recordFailure);
    await expect(member.getByText("Критерии поиска сохранены")).toBeVisible();
    await openCriteria();
    await expect(examples).toHaveValue("designer");
    await expect(examples).toHaveCSS("appearance", "none");
    await expect(examples).toHaveCSS("padding-right", "40px");
    const roles = member.getByLabel("Названия ролей и синонимы");
    await expect(roles).toHaveValue("product designer, ux designer");
    const queries = member.getByLabel("Запросы к агрегаторам (до 8)");
    await expect(queries).toHaveValue("");
    await expect(queries).toHaveAccessibleDescription("(если пусто, используются первые 8 названий ролей)");
    await roles.fill("brand designer");
    await member.getByRole("button", { name: "Сохранить профиль поиска" }).click();
    await expect(member.getByText("Профиль поиска сохранён.")).toBeVisible();
    await expect(examples).toHaveValue("");
    await expect(history).toHaveAccessibleDescription("В поиске используется версия 2");
    await history.focus();
    await member.keyboard.press("Enter");
    await member.getByText("Версия 1", { exact: true }).click();
    await expect(member.getByText("product designer, ux designer", { exact: true })).toBeVisible();
    await member.getByText("Версия 2 · используется в поиске", { exact: true }).click();
    await expect(member.getByText("brand designer", { exact: true })).toBeVisible();
    await member.getByRole("button", { name: "Источники", exact: true }).click();
    await expect(history).toHaveCount(0);
    await expect(member.getByRole("option", { name: "Remotive · уже подключён" })).toBeDisabled();
    await expect(member.getByRole("button", { name: "Добавить источник", exact: true })).toBeDisabled();
  } finally {
    await context.close();
  }
});

function pythonBytes(script: string, input?: Buffer) {
  return execFileSync(
    process.env.QA_E2E_PYTHON || "../.venv/bin/python",
    ["-c", script],
    { input },
  );
}

async function activatePersonalCV(
  page: Page,
  name: string,
  experience: string,
  filename = `${name}.docx`,
) {
  await page
    .getByRole("button", { name: "Профиль и шаблон", exact: true })
    .click();
  const buffer = pythonBytes(
    `import io,sys\nfrom docx import Document\nd=Document()\nd.add_paragraph(${JSON.stringify(name)})\nd.add_paragraph(${JSON.stringify(experience)})\nb=io.BytesIO()\nd.save(b)\nsys.stdout.buffer.write(b.getvalue())`,
  );
  await page.getByLabel("Файл эталонного CV").setInputFiles({
    name: filename,
    mimeType:
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    buffer,
  });
  await page
    .getByRole("checkbox", {
      name: "Подтверждаю достоверность указанных фактов.",
    })
    .check();
  await page
    .getByRole("button", { name: "Сохранить эталон", exact: true })
    .click();
  await expect(
    page.getByText("Активный эталон", { exact: true }),
  ).toBeVisible();
  return (await (await page.request.get(`${origin}/api/profile`)).json())
    .active;
}

test("two professions generate independent documents, revise their own chats and delete only their own files", async ({
  page,
  browser,
}) => {
  await signIn(page);
  const invitations = [
    await invite(page, "stage6-designer"),
    await invite(page, "stage6-developer"),
  ];
  const contexts = [await browser.newContext(), await browser.newContext()];
  try {
    const people = [
      {
        page: await contexts[0].newPage(),
        name: "Alice Example",
        experience:
          "Product Designer building accessible interfaces with Figma.",
        preset: "designer",
        title: "Product Designer",
      },
      {
        page: await contexts[1].newPage(),
        name: "Bob Example",
        experience:
          "Backend Engineer building Python services with PostgreSQL.",
        preset: "developer",
        title: "Backend Engineer",
      },
    ];
    for (const [index, person] of people.entries()) {
      await accept(person.page, invitations[index].token);
      await person.page
        .getByRole("button", { name: "Поиск и источники" })
        .click();
      await person.page
        .getByLabel("Пример профиля")
        .selectOption(person.preset);
      await person.page
        .getByRole("button", { name: "Применить пример" })
        .click();
      await expect(
        person.page.getByText(
          "Пример применён. Критерии сохранены в вашем аккаунте.",
        ),
      ).toBeVisible();
      await activatePersonalCV(person.page, person.name, person.experience);
      await person.page
        .getByRole("button", { name: /Вакансии/ })
        .first()
        .click();
      await launchSearch(person.page);
      await person.page
        .getByRole("button", { name: /Показать свежую выдачу/ })
        .click();
      await expect(
        person.page.getByRole("article", { name: person.title, exact: true }),
      ).toBeVisible();
    }
    await Promise.all(
      people.map(async (person) => {
        const card = person.page.getByRole("article", {
          name: person.title,
          exact: true,
        });
        await card
          .getByRole("button", { name: "Сделать CV", exact: true })
          .click();
        await expect(
          card.getByRole("link", { name: "Скачать PDF", exact: true }),
        ).toBeVisible();
      }),
    );
    const documents: Task[] = [];
    for (const [index, person] of people.entries()) {
      const other = people[1 - index];
      const jobs = await (
        await person.page.request.get(`${origin}/api/jobs`)
      ).json();
      const task = jobs.items[0].documents[0];
      expect(task.input_summary.template_filename).toBe(`${person.name}.docx`);
      documents.push(task);
      const card = person.page.getByRole("article", {
        name: person.title,
        exact: true,
      });
      await card
        .getByRole("button", { name: "Содержание и подтверждённые факты" })
        .click();
      await expect(
        card
          .getByRole("paragraph")
          .filter({ hasText: new RegExp(`^${person.name}$`) }),
      ).toBeVisible();
      await expect(card.getByText(other.name, { exact: true })).toHaveCount(0);
      for (const artifact of task.artifacts) {
        const route = `${origin}/api/artifacts/${artifact.id}/download`;
        expect((await other.page.request.get(route)).status()).toBe(404);
        const response = await person.page.request.get(route);
        expect(response.status()).toBe(200);
        const script =
          artifact.format === "pdf"
            ? "import io,sys\nfrom pypdf import PdfReader\nprint(' '.join(p.extract_text() for p in PdfReader(io.BytesIO(sys.stdin.buffer.read())).pages))"
            : "import io,sys\nfrom docx import Document\nprint(' '.join(p.text for p in Document(io.BytesIO(sys.stdin.buffer.read())).paragraphs))";
        const text = pythonBytes(script, await response.body()).toString();
        expect(text).toContain(person.name);
        expect(text).toContain(person.experience);
        expect(text).not.toContain(other.name);
      }
      await card.getByRole("button", { name: /доработать/ }).click();
      await expect(
        card.getByRole("textbox", { name: "Сообщение агенту" }),
      ).toBeEnabled();
      await card
        .getByRole("textbox", { name: "Сообщение агенту" })
        .fill(`Keep ${person.name}'s confirmed experience.`);
      await card
        .getByRole("button", { name: "Отправить", exact: true })
        .click();
      await expect(
        card.getByText(
          "Я могу помочь с шаблоном. Изменения фактов подтвердите в форме эталона.",
        ),
      ).toBeVisible();
      await expect(
        other.page.getByText(`Keep ${person.name}'s confirmed experience.`),
      ).toHaveCount(0);
      await card.getByRole("button", { name: "Сгенерировать заново" }).click();
      await expect
        .poll(
          async () =>
            (
              await (await person.page.request.get(`${origin}/api/jobs`)).json()
            ).items[0].documents.filter(
              (item: { status: string }) => item.status === "ready",
            ).length,
        )
        .toBe(2);
    }
    const firstCard = people[0].page.getByRole("article", {
      name: people[0].title,
      exact: true,
    });
    const originalDocument = firstCard.locator(".task-status").filter({
      has: people[0].page.locator(
        `a[href="/api/artifacts/${documents[0].artifacts[0].id}/download"]`,
      ),
    });
    await originalDocument
      .getByRole("button", { name: "Удалить", exact: true })
      .click();
    await originalDocument
      .getByRole("button", { name: "Удалить документ", exact: true })
      .click();
    await expect
      .poll(async () =>
        (
          await people[0].page.request.get(
            `${origin}/api/tasks/${documents[0].id}`,
          )
        ).status(),
      )
      .toBe(404);
    expect(
      (
        await people[1].page.request.get(
          `${origin}/api/artifacts/${documents[1].artifacts[0].id}/download`,
        )
      ).status(),
    ).toBe(200);
  } finally {
    await Promise.allSettled(contexts.map((context) => context.close()));
  }
});

test("two invited accounts keep independent workspaces, files and sessions on one server", async ({
  page,
  browser,
}) => {
  await signIn(page);
  await page.getByRole("button", { name: "Аккаунт и доступ" }).click();
  await page.getByLabel("Логин нового пользователя").fill("browser-designer");
  await page
    .getByLabel("Имя пользователя", { exact: true })
    .fill("Browser Designer");
  await page.getByRole("button", { name: "Создать приглашение" }).click();
  const link = await page.getByLabel("Одноразовая ссылка").inputValue();
  const token = new URLSearchParams(new URL(link).hash.slice(1)).get("invite")!;
  const other = await invite(page, "browser-developer");
  const firstContext = await browser.newContext();
  const secondContext = await browser.newContext();
  try {
    const designer = await firstContext.newPage();
    const developer = await secondContext.newPage();
    await accept(designer, token);
    await accept(developer, other.token);
    expect(
      (await designer.request.get(`${origin}/api/admin/users`)).status(),
    ).toBe(403);
    await expect(designer.getByRole("article")).toHaveCount(0);
    await expect(developer.getByRole("article")).toHaveCount(0);
    for (const [memberPage, preset] of [
      [designer, "designer"],
      [developer, "developer"],
    ] as const) {
      await memberPage
        .getByRole("button", { name: "Поиск и источники" })
        .click();
      await memberPage.getByLabel("Пример профиля").selectOption(preset);
      await memberPage
        .getByRole("button", { name: "Применить пример" })
        .click();
      await expect(
        memberPage.getByText(
          "Пример применён. Критерии сохранены в вашем аккаунте.",
        ),
      ).toBeVisible();
      await memberPage
        .getByRole("button", { name: /Вакансии/ })
        .first()
        .click();
    }
    await designer
      .getByRole("button", { name: "Обновить поиск", exact: true })
      .click();
    const card = designer.getByRole("article", {
      name: "Product Designer",
      exact: true,
    });
    await expect
      .poll(
        async () =>
          (await (await designer.request.get(`${origin}/api/jobs`)).json())
            .total,
      )
      .toBe(1);
    await designer
      .getByRole("button", { name: /Показать свежую выдачу/ })
      .click();
    await expect(card).toBeVisible();
    await developer
      .getByRole("button", { name: "Обновить поиск", exact: true })
      .click();
    await expect
      .poll(
        async () =>
          (await (await developer.request.get(`${origin}/api/jobs`)).json())
            .total,
      )
      .toBe(1);
    await developer
      .getByRole("button", { name: /Показать свежую выдачу/ })
      .click();
    await expect(
      developer.getByRole("article", { name: "Backend Engineer", exact: true }),
    ).toBeVisible();
    await expect(
      developer.getByRole("article", { name: "Product Designer", exact: true }),
    ).toHaveCount(0);
    await card.getByRole("button", { name: "Сделать CV", exact: true }).click();
    await designer
      .getByLabel("Файл эталонного CV")
      .setInputFiles(path.resolve("../tests/fixtures/candidate.pdf"));
    await designer.getByRole("checkbox").check();
    await designer
      .getByRole("button", { name: "Сохранить эталон", exact: true })
      .click();
    await expect(
      designer.getByText("Активный эталон", { exact: true }),
    ).toBeVisible();
    await designer
      .getByRole("button", { name: /Вакансии/ })
      .first()
      .click();
    await card.getByRole("button", { name: "Сделать CV", exact: true }).click();
    const download = card.getByRole("link", {
      name: "Скачать PDF",
      exact: true,
    });
    await expect(download).toBeVisible();
    const event = designer.waitForEvent("download");
    await download.click();
    expect(await (await event).failure()).toBeNull();
    const profile = await (
      await designer.request.get(`${origin}/api/profile`)
    ).json();
    const jobs = await (
      await designer.request.get(`${origin}/api/jobs`)
    ).json();
    const task = jobs.items[0].documents[0];
    const foreign = [
      `/api/templates/${profile.active.id}/source`,
      `/api/templates/${profile.active.id}/pages/1`,
      `/api/tasks/${task.id}`,
      `/api/tasks/${task.id}/review`,
      `/api/tasks/${task.id}/document-preview`,
      `/api/jobs?snapshot_id=${jobs.snapshot_id}`,
      (await download.getAttribute("href")) ?? "",
    ];
    for (const route of foreign) {
      expect(
        (await developer.request.get(origin + route)).status(),
        route,
      ).toBe(404);
      expect((await page.request.get(origin + route)).status(), route).toBe(
        404,
      );
    }
    await designer.reload();
    await expect(card).toBeVisible();
    await designer.getByRole("button", { name: "Выйти", exact: true }).click();
    await designer
      .getByLabel("Логин", { exact: true })
      .fill("browser-developer");
    await designer.getByLabel("Пароль", { exact: true }).fill(password);
    await designer.getByRole("button", { name: "Войти", exact: true }).click();
    await expect(
      designer.getByRole("article", { name: "Backend Engineer", exact: true }),
    ).toBeVisible();
    await expect(
      designer.getByRole("article", { name: "Product Designer", exact: true }),
    ).toHaveCount(0);
    expect(
      (await (await designer.request.get(`${origin}/api/profile`)).json())
        .active,
    ).toBeNull();
  } finally {
    await Promise.allSettled([firstContext.close(), secondContext.close()]);
  }
});

test("password changes, reset links and administrator disable terminate live sessions", async ({
  page,
  browser,
}) => {
  await signIn(page);
  const issued = await invite(page, "browser-recovery");
  const firstContext = await browser.newContext();
  const secondContext = await browser.newContext();
  try {
    const member = await firstContext.newPage();
    const second = await secondContext.newPage();
    await accept(member, issued.token);
    await signIn(second, "browser-recovery");
    await member.getByRole("button", { name: "Аккаунт и доступ" }).click();
    await member.getByLabel("Текущий пароль").fill(password);
    await member.getByLabel("Новый пароль").fill(password + "-new");
    await member
      .getByRole("button", { name: "Изменить пароль", exact: true })
      .click();
    await expect(
      member.getByText("Пароль изменён. Остальные сессии завершены."),
    ).toBeVisible();
    await expect(second.getByLabel("Логин", { exact: true })).toBeVisible();
    expect((await second.request.get(`${origin}/api/jobs`)).status()).toBe(401);
    await page.getByRole("button", { name: "Аккаунт и доступ" }).click();
    const memberRow = page
      .getByRole("listitem")
      .filter({ hasText: "browser-recovery" });
    await memberRow
      .getByRole("button", { name: "Ссылка восстановления" })
      .click();
    const firstLink = await page.getByLabel("Одноразовая ссылка").inputValue();
    const revokedToken = new URLSearchParams(
      new URL(firstLink).hash.slice(1),
    ).get("reset")!;
    await page
      .getByRole("button", { name: "Отозвать ссылку восстановления" })
      .click();
    await expect(
      page.getByText("Ссылка восстановления отозвана."),
    ).toBeVisible();
    expect(
      (
        await second.request.post(`${origin}/api/password-resets/accept`, {
          headers: { origin },
          data: { token: revokedToken, password },
        })
      ).status(),
    ).toBe(403);
    await memberRow
      .getByRole("button", { name: "Ссылка восстановления" })
      .click();
    const nextLink = await page.getByLabel("Одноразовая ссылка").inputValue();
    const access = {
      token: new URLSearchParams(new URL(nextLink).hash.slice(1)).get("reset")!,
    };
    await second.goto(`${origin}/#reset=${access.token}`);
    await second.getByLabel("Пароль", { exact: true }).fill(password);
    await second
      .getByRole("button", { name: "Сохранить новый пароль" })
      .click();
    await expect(
      second.getByRole("button", { name: "Войти", exact: true }),
    ).toBeVisible();
    await expect(member.getByLabel("Логин", { exact: true })).toBeVisible();
    await signIn(member, "browser-recovery");
    const disabled = await page.request.patch(
      `${origin}/api/admin/users/${issued.user_id}`,
      { headers: await headers(page), data: { disabled: true } },
    );
    expect(disabled.status()).toBe(200);
    await expect(member.getByLabel("Логин", { exact: true })).toBeVisible();
    expect((await member.request.get(`${origin}/api/tasks`)).status()).toBe(
      401,
    );
    expect(
      (
        await member.request.post(`${origin}/api/login`, {
          headers: { origin },
          data: { login: "browser-recovery", password },
        })
      ).status(),
    ).toBe(401);
    expect(
      (
        await page.request.patch(
          `${origin}/api/admin/users/${issued.user_id}`,
          { headers: await headers(page), data: { disabled: false } },
        )
      ).status(),
    ).toBe(200);
    await signIn(member, "browser-recovery");
    await member.getByRole("button", { name: "Аккаунт и доступ" }).click();
    await member.getByRole("button", { name: "Завершить все сессии" }).click();
    await expect(member.getByLabel("Логин", { exact: true })).toBeVisible();
  } finally {
    await Promise.allSettled([firstContext.close(), secondContext.close()]);
  }
});

test("revoked, expired and reused invitations never open a workspace", async ({
  page,
  browser,
}) => {
  await signIn(page);
  const revoked = await invite(page, "browser-revoked");
  expect(
    (
      await page.request.delete(
        `${origin}/api/admin/invitations/${revoked.id}`,
        { headers: await headers(page) },
      )
    ).status(),
  ).toBe(204);
  const used = await invite(page, "browser-once");
  const isolated = await browser.newContext();
  try {
    const visitor = await isolated.newPage();
    await accept(visitor, used.token);
    await visitor.getByRole("button", { name: "Выйти", exact: true }).click();
    await expect(visitor.getByLabel("Логин", { exact: true })).toBeVisible();
    for (const token of [
      revoked.token,
      used.token,
      "expired-synthetic-token",
    ]) {
      await visitor.goto(`${origin}/#invite=${token}`);
      await expect.poll(() => new URL(visitor.url()).hash).toBe("");
      await expect(
        visitor.getByRole("heading", { name: "Принять приглашение" }),
      ).toBeVisible();
      await visitor.getByLabel("Пароль", { exact: true }).fill(password);
      await visitor
        .getByRole("button", { name: "Принять приглашение", exact: true })
        .click();
      await expect(visitor.getByRole("alert")).toHaveText(
        /Ссылка недействительна/,
      );
      expect((await visitor.request.get(`${origin}/api/jobs`)).status()).toBe(
        401,
      );
      await expect(visitor.getByRole("article")).toHaveCount(0);
    }
  } finally {
    await Promise.allSettled([isolated.close()]);
  }
});

test("personal settings drive real source adapters and matching, persist versions and reject stale edits", async ({
  page,
  browser,
}) => {
  await signIn(page);
  const first = await invite(page, "settings-junior");
  const second = await invite(page, "settings-office");
  const contexts = [await browser.newContext(), await browser.newContext()];
  try {
    const junior = await contexts[0].newPage();
    const office = await contexts[1].newPage();
    await accept(junior, first.token);
    await accept(office, second.token);
    for (const member of [junior, office]) {
      await member.getByRole("button", { name: "Поиск и источники" }).click();
      await expect(
        member.getByText("Источники пока не добавлены."),
      ).toBeVisible();
      await expect(member.getByLabel("Названия ролей и синонимы")).toHaveValue(
        "",
      );
      await member.getByLabel("Тип источника").selectOption("lever");
      await member.getByLabel("Идентификатор доски").fill("synthetic_company");
      await member.getByLabel("Название подключения").fill("Личная доска");
      await member.getByLabel("Регион API").selectOption("eu");
      await member
        .getByRole("button", { name: "Добавить источник", exact: true })
        .click();
      await expect(
        member.getByRole("heading", { name: "Личная доска" }),
      ).toBeVisible();
    }
    await junior
      .getByLabel("Названия ролей и синонимы")
      .fill("backend engineer");
    await office
      .getByLabel("Названия ролей и синонимы")
      .fill("product designer");
    for (const member of [junior, office]) {
      await member.getByLabel("Удалённо", { exact: true }).uncheck();
      await member
        .getByLabel("Подходящие страны и регионы")
        .fill("germany, berlin");
      await member.getByLabel("Где вы можете работать").fill("germany");
    }
    await junior.getByLabel("Гибрид", { exact: true }).check();
    await junior.getByLabel("Города для гибридной работы").fill("berlin");
    await office.getByLabel("В офисе", { exact: true }).check();
    for (const member of [junior, office]) {
      await member
        .getByRole("button", { name: "Сохранить профиль поиска" })
        .click();
      await expect(member.getByText("Профиль поиска сохранён.")).toBeVisible();
      await member
        .getByRole("button", { name: "Проверить Личная доска" })
        .click();
      await expect(
        member.getByText(
          /Источник доступен. Объявлений в проверенном ответе: 3/,
        ),
      ).toBeVisible();
      expect(
        (await (await member.request.get(`${origin}/api/jobs`)).json()).total,
      ).toBe(0);
    }
    const stale = await contexts[0].newPage();
    await stale.goto(origin);
    await stale.getByRole("button", { name: "Поиск и источники" }).click();
    await expect(stale.getByLabel("Названия ролей и синонимы")).toHaveValue(
      "backend engineer",
    );
    await junior.getByLabel("Навыки").fill("python");
    await junior
      .getByRole("button", { name: "Сохранить профиль поиска" })
      .click();
    await expect(junior.getByText("Профиль поиска сохранён.")).toBeVisible();
    await stale.getByLabel("Навыки").fill("draft stale");
    await stale
      .getByRole("button", { name: "Сохранить профиль поиска" })
      .click();
    await expect(stale.getByRole("alert")).toContainText(
      "Настройки изменились в другой вкладке",
    );
    await expect(stale.getByLabel("Навыки")).toHaveValue("draft stale");
    await stale
      .getByRole("button", { name: "Загрузить актуальные настройки" })
      .click();
    await expect(stale.getByLabel("Навыки")).toHaveValue("python");
    const sources = (
      await (await junior.request.get(`${origin}/api/sources`)).json()
    ).items;
    expect(
      (
        await office.request.get(
          `${origin}/api/sources/${sources[0].id}/history`,
        )
      ).status(),
    ).toBe(404);
    for (const [member, title] of [
      [junior, "Junior Backend Engineer"],
      [office, "Product Designer"],
    ] as const) {
      await member
        .getByRole("button", { name: /Вакансии/ })
        .first()
        .click();
      await member
        .getByRole("button", { name: "Обновить поиск", exact: true })
        .click();
      await expect
        .poll(
          async () =>
            (await (await member.request.get(`${origin}/api/jobs`)).json())
              .total,
        )
        .toBe(1);
      await member
        .getByRole("button", { name: /Показать свежую выдачу/ })
        .click();
      await expect(
        member.getByRole("article", { name: title, exact: true }),
      ).toBeVisible();
      await expect(member.getByRole("article")).toHaveCount(1);
      await member.reload();
      await expect(
        member.getByRole("article", { name: title, exact: true }),
      ).toBeVisible();
    }
    await junior.getByRole("button", { name: "Поиск и источники" }).click();
    await junior.getByRole("button", { name: "Изменить Личная доска" }).click();
    await junior.getByLabel("Идентификатор доски").fill("../private");
    await junior.getByRole("button", { name: "Сохранить подключение" }).click();
    await expect(junior.getByRole("alert")).toContainText(
      "Нужен идентификатор доски, а не URL",
    );
    await junior.getByLabel("Идентификатор доски").fill("synthetic_updated");
    await junior.getByRole("button", { name: "Сохранить подключение" }).click();
    await expect(junior.getByText("Источник сохранён.")).toBeVisible();
    await junior.getByLabel("Включить Личная доска").click();
    await expect(junior.getByLabel("Включить Личная доска")).not.toBeChecked();
    expect(
      (
        await junior.request.post(`${origin}/api/search-runs`, {
          headers: await headers(junior),
        })
      ).status(),
    ).toBe(409);
    await junior.getByRole("button", { name: "Удалить Личная доска" }).click();
    await expect(
      junior.getByText("Источники пока не добавлены."),
    ).toBeVisible();
    expect(
      (
        await (
          await junior.request.get(
            `${origin}/api/sources/${sources[0].id}/history`,
          )
        ).json()
      ).items.at(-1).deleted,
    ).toBe(true);
    expect(
      (await (await office.request.get(`${origin}/api/sources`)).json()).items,
    ).toHaveLength(1);
    await junior.getByLabel("Тип источника").selectOption("telegram");
    await junior
      .getByLabel("Handle публичного канала")
      .fill("https://127.0.0.1/private");
    await junior
      .getByRole("button", { name: "Добавить источник", exact: true })
      .click();
    await expect(junior.getByRole("alert")).toContainText(
      "Нужен handle канала, а не URL",
    );
    await junior.getByLabel("Handle публичного канала").fill("public_jobs");
    await junior
      .getByRole("button", { name: "Добавить источник", exact: true })
      .click();
    await junior
      .getByRole("button", { name: "Проверить Telegram · @public_jobs" })
      .click();
    await expect(
      junior.getByText("Источник доступен, проверенный ответ пуст."),
    ).toBeVisible();
    await junior.reload();
    await junior.getByRole("button", { name: "Поиск и источники" }).click();
    await expect(
      junior.getByRole("heading", { name: "Telegram · @public_jobs" }),
    ).toBeVisible();
  } finally {
    await Promise.allSettled(contexts.map((context) => context.close()));
  }
});

async function configureSearch(page: Page, token: string) {
  await page.getByRole("button", { name: "Поиск и источники" }).click();
  await page.getByLabel("Названия ролей и синонимы").fill("backend engineer");
  await page.getByRole("button", { name: "Сохранить профиль поиска" }).click();
  await expect(page.getByText("Профиль поиска сохранён.")).toBeVisible();
  await page.getByLabel("Тип источника").selectOption("lever");
  await page.getByLabel("Идентификатор доски").fill(token);
  await page
    .getByRole("button", { name: "Добавить источник", exact: true })
    .click();
  await expect(page.getByText("Источник сохранён.")).toBeVisible();
  await page
    .getByRole("button", { name: /Вакансии/ })
    .first()
    .click();
}
async function launchSearch(page: Page, wait = true) {
  const response = page.waitForResponse(
    (response) =>
      response.url() === `${origin}/api/search-runs` &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Обновить поиск", exact: true })
    .click();
  const task = await (await response).json();
  if (wait)
    await expect
      .poll(
        async () =>
          (
            await (
              await page.request.get(`${origin}/api/tasks/${task.id}`)
            ).json()
          ).status,
      )
      .toBe("ready");
  return task.id as string;
}

test("working language choices persist and filter searches by declared or inferred language", async ({
  page,
  browser,
}) => {
  await signIn(page);
  const invitation = await invite(page, "working-languages");
  const context = await browser.newContext();
  try {
    const member = await context.newPage();
    await accept(member, invitation.token);
    await configureSearch(member, "working-languages");
    const jobCards = member.getByRole("article");
    const russian = member.getByRole("checkbox", { name: "Русскоязычные" });
    const english = member.getByRole("checkbox", { name: "Англоязычные" });
    const openCriteria = () => member.getByRole("button", { name: "Поиск и источники" }).click();
    const search = async () => {
      await launchSearch(member);
      await member.getByRole("button", { name: /Показать свежую выдачу/ }).click();
    };
    const save = async () => {
      await member.getByRole("button", { name: "Сохранить профиль поиска" }).click();
      await expect(member.getByText("Профиль поиска сохранён.")).toBeVisible();
    };
    const showMatches = async (labels: string[]) => {
      await expect(jobCards).toHaveCount(labels.length);
      for (const label of labels) {
        await expect(member.getByRole("article", {
          name: `Backend Engineer · ${label}`,
          exact: true,
        })).toBeVisible();
      }
    };
    await search();
    await expect(jobCards).toHaveCount(9);
    await expect(member.getByText("Компания из России", { exact: true })).toHaveCount(2);
    await openCriteria();
    await russian.check();
    await save();
    await member.reload();
    await openCriteria();
    await expect(russian).toBeChecked();
    await expect(english).not.toBeChecked();
    await member.getByText("История версий профиля", { exact: true }).click();
    await member.getByText("Версия 2 · используется в поиске", { exact: true }).click();
    await expect(member.locator(".profile-versions").getByText("Русскоязычные", { exact: true })).toBeVisible();
    await member.getByRole("button", { name: /Вакансии/ }).first().click();
    await search();
    await showMatches(["Russian post", "Russian team", "Bilingual team", "Russian company"]);
    await expect(member.getByText("Компания из России", { exact: true })).toHaveCount(1);

    await openCriteria();
    await russian.uncheck();
    await english.check();
    await save();
    await member.getByRole("button", { name: /Вакансии/ }).first().click();
    await search();
    await showMatches(["English post", "English team", "Bilingual team", "English at Russian company", "Russian customers"]);
    await expect(member.getByText("Компания из России", { exact: true })).toHaveCount(1);
  } finally {
    await context.close();
  }
});

test("personal schedules and administrator quotas persist while exhausted quotas block document calls", async ({
  page,
  browser,
}) => {
  await signIn(page);
  const issued = await invite(page, "stage7-quota-user");
  const context = await browser.newContext();
  try {
    const member = await context.newPage();
    await accept(member, issued.token);
    await activatePersonalCV(
      member,
      "Quota Example",
      "Backend Engineer with confirmed Python experience.",
    );
    await configureSearch(member, "stage7-quota");
    await launchSearch(member);
    await member
      .getByRole("button", { name: /Показать свежую выдачу/ })
      .click();
    const card = member.getByRole("article", {
      name: "Backend Engineer 00",
      exact: true,
    });
    await expect(card).toBeVisible();
    await page
      .getByRole("button", { name: "Аккаунт и доступ", exact: true })
      .click();
    await page
      .getByRole("button", { name: "Расписание и лимиты", exact: true })
      .click();
    const quota = page.getByLabel("Суточный лимит stage7-quota-user");
    await expect(quota).toHaveValue("60");
    await quota.fill("0");
    await page
      .getByRole("button", { name: "Сохранить лимит stage7-quota-user" })
      .click();
    await expect
      .poll(
        async () =>
          (await (await member.request.get(`${origin}/api/usage`)).json())
            .daily_calls,
      )
      .toBe(0);
    await card.getByRole("button", { name: "Сделать CV", exact: true }).click();
    await expect(
      card.getByText(/Ваш суточный лимит OpenAI исчерпан/),
    ).toBeVisible();
    await expect(
      card.getByRole("link", { name: "Скачать PDF", exact: true }),
    ).toHaveCount(0);
    expect(
      (await (await member.request.get(`${origin}/api/usage`)).json())
        .used_calls,
    ).toBe(0);
    await quota.fill("3");
    await page
      .getByRole("button", { name: "Сохранить лимит stage7-quota-user" })
      .click();
    await expect
      .poll(
        async () =>
          (await (await member.request.get(`${origin}/api/usage`)).json())
            .daily_calls,
      )
      .toBe(3);
    await card.getByRole("button", { name: "Сделать CV", exact: true }).click();
    await expect(
      card.getByRole("link", { name: "Скачать PDF", exact: true }),
    ).toBeVisible();
    expect(
      (await (await member.request.get(`${origin}/api/usage`)).json())
        .used_calls,
    ).toBe(3);
    await member
      .getByRole("button", { name: "Аккаунт и доступ", exact: true })
      .click();
    await member
      .getByRole("button", { name: "Расписание и лимиты", exact: true })
      .click();
    await expect(member.getByText(/Вызовы за .*: 3 из 3/)).toBeVisible();
    await expect(
      member.getByText("Общие ограничения исполнения", { exact: true }),
    ).toHaveCount(0);
    await member.getByLabel("Автоматический поиск").uncheck();
    await member.getByLabel("Интервал поиска в минутах").fill("60");
    await member.getByRole("button", { name: "Сохранить расписание" }).click();
    await expect(
      member.getByText("Настройки исполнения сохранены."),
    ).toBeVisible();
    await member.reload();
    await member
      .getByRole("button", { name: "Аккаунт и доступ", exact: true })
      .click();
    await member
      .getByRole("button", { name: "Расписание и лимиты", exact: true })
      .click();
    await expect(member.getByLabel("Автоматический поиск")).not.toBeChecked();
    await expect(member.getByLabel("Интервал поиска в минутах")).toHaveValue(
      "60",
    );
    expect(
      (await member.request.get(`${origin}/api/admin/runtime`)).status(),
    ).toBe(403);
    expect(
      (await (await page.request.get(`${origin}/api/schedule`)).json()).enabled,
    ).toBe(true);
  } finally {
    await context.close();
  }
});

test("frozen pages, partial source failures, missing feeds and personal run history survive reload", async ({
  page,
  browser,
}) => {
  await signIn(page);
  const invitation = await invite(page, "stage5-browser-history");
  const context = await browser.newContext();
  try {
    const member = await context.newPage();
    await accept(member, invitation.token);
    const originalTemplate = await activatePersonalCV(
      member,
      "Historical Example",
      "Backend Engineer with confirmed Python experience.",
    );
    await configureSearch(member, "stage5-history");
    const first = await launchSearch(member);
    await member
      .getByRole("button", { name: /Показать свежую выдачу/ })
      .click();
    await expect(member.getByRole("article")).toHaveCount(10);
    await expect(member.locator(".job-list")).toHaveAttribute(
      "aria-busy",
      "false",
    );
    const pagination = await member.getByRole("navigation", { name: "Пагинация вакансий" }).boundingBox();
    const history = await member.locator(".search-history").boundingBox();
    const sources = await member.getByRole("region", { name: "Результат поискового запуска" }).boundingBox();
    expect(history!.y).toBeGreaterThanOrEqual(pagination!.y + pagination!.height);
    expect(sources!.y).toBeGreaterThanOrEqual(history!.y + history!.height);
    await member
      .getByRole("button", { name: "Страница 2", exact: true })
      .click();
    await expect(member.getByText("Страница 2 из 3")).toBeVisible();
    const firstPage = await (
      await member.request.get(`${origin}/api/jobs?run_id=${first}&page=2`)
    ).json();
    const titles = firstPage.items.map((item: { title: string }) => item.title);
    await expect(
      member.getByRole("article").getByRole("heading", { level: 2 }),
    ).toHaveText(titles);
    const firstFetched = firstPage.items[0].fetched_at;
    const second = await launchSearch(member);
    await expect(member.getByText("Страница 2 из 3")).toBeVisible();
    await expect(
      member.getByRole("article").getByRole("heading", { level: 2 }),
    ).toHaveText(titles);
    await expect(
      member.getByText(/Сохранённая вакансия: источник недоступен/),
    ).toHaveCount(0);
    await member
      .getByRole("button", { name: /Показать свежую выдачу/ })
      .click();
    await expect(
      member.getByText(/Сохранённая вакансия: источник недоступен/),
    ).toHaveCount(10);
    const failedSummary = member.getByRole("region", { name: "Результат поискового запуска" });
    await failedSummary.getByText("Проблемы источников: 1").click();
    await expect(failedSummary.getByText(/на стороне источника произошёл сбой/)).toBeVisible();
    expect(
      (await (await member.request.get(`${origin}/api/jobs`)).json()).items[0]
        .fetched_at,
    ).toBe(firstFetched);
    const third = await launchSearch(member);
    await member
      .getByRole("button", { name: /Показать свежую выдачу/ })
      .click();
    await expect(member.getByText(/закрытие не установлено/)).toHaveCount(10);
    const fourth = await launchSearch(member);
    await member
      .getByRole("button", { name: /Показать свежую выдачу/ })
      .click();
    await expect(
      member
        .getByRole("article")
        .first()
        .getByText("Changed requirements.", { exact: true }),
    ).toHaveText("Changed requirements.");
    await member.getByText("История поиска", { exact: true }).click();
    await member
      .getByRole("button", {
        name: `Открыть выдачу запуска ${first.slice(0, 8)}`,
      })
      .click();
    await expect(
      member
        .getByRole("article")
        .first()
        .getByText("Original Python requirements.", { exact: true }),
    ).toHaveText("Original Python requirements.");
    const historicalCard = member.getByRole("article").first();
    const generationResponse = member.waitForResponse(
      (response) =>
        response.url().endsWith("/documents") &&
        response.request().method() === "POST",
    );
    await historicalCard
      .getByRole("button", { name: "Сделать CV", exact: true })
      .click();
    const queuedHistoricalTask = await (await generationResponse).json();
    await expect(
      historicalCard.getByRole("link", { name: "Скачать PDF", exact: true }),
    ).toBeVisible();
    const historicalListing = await (
      await member.request.get(
        `${origin}/api/jobs?snapshot_id=${queuedHistoricalTask.input_summary.job_version.snapshot_id}`,
      )
    ).json();
    const historicalTask = historicalListing.items[0].documents[0];
    expect(historicalTask.input_summary.job_version.run_id).toBe(first);
    expect(historicalTask.input_summary.job_version.snapshot_id).toBe(
      historicalListing.snapshot_id,
    );
    expect(historicalTask.input_summary.template_id).toBe(originalTemplate.id);
    await expect(
      historicalCard.getByText(/Эталон: Historical Example.docx/),
    ).toBeVisible();
    expect(
      (
        await (
          await member.request.get(`${origin}/api/search-runs/${second}`)
        ).json()
      ).errors,
    ).toHaveLength(1);
    for (const run of [first, second, third, fourth]) {
      expect(
        (await page.request.get(`${origin}/api/search-runs/${run}`)).status(),
      ).toBe(404);
    }
    await member
      .getByRole("article")
      .first()
      .getByRole("button", { name: "Не подходит", exact: true })
      .click();
    await member.getByRole("button", { name: "Архив", exact: true }).click();
    await expect(member.getByRole("article")).toHaveCount(1);
    await member.reload();
    await member.getByRole("button", { name: "Архив", exact: true }).click();
    await expect(member.getByRole("article")).toHaveCount(1);
  } finally {
    await context.close();
  }
});

test("a run with changed settings is saved as superseded without publishing its old matches", async ({
  page,
  browser,
}) => {
  await signIn(page);
  const invitation = await invite(page, "stage5-browser-stale");
  const context = await browser.newContext();
  try {
    const member = await context.newPage();
    await accept(member, invitation.token);
    await configureSearch(member, "stage5-slow");
    const id = await launchSearch(member, false);
    await expect
      .poll(
        async () =>
          (await (await member.request.get(`${origin}/api/tasks/${id}`)).json())
            .status,
      )
      .toBe("running");
    await member.getByRole("button", { name: "Поиск и источники" }).click();
    await member
      .getByLabel("Названия ролей и синонимы")
      .fill("product designer");
    await member
      .getByRole("button", { name: "Сохранить профиль поиска" })
      .click();
    await expect(member.getByText("Профиль поиска сохранён.")).toBeVisible();
    await expect
      .poll(
        async () =>
          (
            await (
              await member.request.get(`${origin}/api/search-runs/${id}`)
            ).json()
          ).state,
      )
      .toBe("superseded");
    expect(
      (await (await member.request.get(`${origin}/api/jobs`)).json()).total,
    ).toBe(0);
    await member
      .getByRole("button", { name: /Вакансии/ })
      .first()
      .click();
    await member.getByText("История поиска", { exact: true }).click();
    await expect(
      member.getByText(/Настройки изменились: сохранён в истории/),
    ).toBeVisible();
    await member
      .getByRole("button", { name: `Открыть выдачу запуска ${id.slice(0, 8)}` })
      .click();
    await expect(
      member.getByRole("article", { name: "Backend Engineer 00", exact: true }),
    ).toBeVisible();
    await expect(member.getByText(/текущая выдача не заменена/)).toBeVisible();
    await member
      .getByRole("button", { name: "Показать текущую выдачу" })
      .click();
    await expect(member.getByRole("article")).toHaveCount(0);
  } finally {
    await context.close();
  }
});

test("a new mobile member configures separate pages with keyboard, gets a first document and updates their own identity", async ({
  page,
  browser,
}) => {
  await signIn(page);
  const invitation = await invite(page, "stage8-mobile-designer");
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
  });
  try {
    const member = await context.newPage();
    await accept(member, invitation.token);
    await expect(member.getByLabel("Текущий пользователь")).toHaveText(
      "stage8-mobile-designer",
    );
    const setup = member.getByRole("region", {
      name: "Состояние личного кабинета",
    });
    await expect(
      setup.getByRole("heading", { name: "Подготовьте личный кабинет" }),
    ).toBeVisible();
    const criteria = setup.getByRole("button", { name: "Настроить критерии" });
    await criteria.focus();
    await member.keyboard.press("Enter");
    await expect(
      member.getByRole("heading", { name: "Что ищу", exact: true }),
    ).toBeFocused();
    await expect(member.getByLabel("Названия ролей и синонимы")).toBeVisible();
    await expect(
      member.getByLabel("Учитывать дополнительные QA-названия"),
    ).toHaveCount(0);
    await member
      .getByLabel("Названия ролей и синонимы")
      .fill("product designer");
    await member.getByLabel("Подходящие страны и регионы").fill("Europe");
    await member.getByLabel("Где вы можете работать").fill("Serbia");
    await member
      .getByRole("button", { name: "Сохранить профиль поиска" })
      .click();
    await expect(member.getByText("Профиль поиска сохранён.")).toBeVisible();
    const sections = member.getByRole("navigation", { name: "Разделы поиска" });
    await sections
      .getByRole("button", { name: "Источники", exact: true })
      .click();
    await expect(
      member.getByRole("heading", { name: "Источники", exact: true }),
    ).toBeFocused();
    await expect(
      member.getByText("Источники пока не добавлены."),
    ).toBeVisible();
    await member
      .getByRole("button", { name: "Добавить источник", exact: true })
      .click();
    await expect(
      member.getByRole("heading", { name: "Remotive", exact: true }),
    ).toBeVisible();
    const longName = "PersonalDesignerBoard".repeat(4);
    await member.getByLabel("Тип источника").selectOption("lever");
    await member.getByLabel("Идентификатор доски").fill("synthetic_company");
    await member.getByLabel("Название подключения").fill(longName);
    await member
      .getByRole("button", { name: "Добавить источник", exact: true })
      .click();
    await expect(
      member.getByRole("heading", { name: longName, exact: true }),
    ).toBeVisible();
    await member.screenshot({
      path: "test-results/stage8-mobile-sources.png",
      fullPage: true,
    });
    expect(
      await member.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await sections
      .getByRole("button", { name: "Что ищу", exact: true })
      .click();
    await member
      .getByLabel("Названия ролей и синонимы")
      .fill("product designer, UX designer");
    await sections
      .getByRole("button", { name: "Источники", exact: true })
      .click();
    await sections
      .getByRole("button", { name: "Что ищу", exact: true })
      .click();
    await expect(member.getByLabel("Названия ролей и синонимы")).toHaveValue(
      "product designer, UX designer",
    );
    await member
      .getByRole("button", { name: "Сохранить профиль поиска" })
      .click();
    await expect(member.getByText("Профиль поиска сохранён.")).toBeVisible();
    await member
      .getByRole("button", { name: /Вакансии/ })
      .first()
      .click();
    await launchSearch(member);
    await member
      .getByRole("button", { name: /Показать свежую выдачу/ })
      .click();
    const card = member.getByRole("article", {
      name: "Product Designer",
      exact: true,
    });
    await expect(card).toBeVisible();
    await expect(card).not.toContainText("Manual");
    await member.getByRole("button", { name: "Открыть эталон CV" }).click();
    await activatePersonalCV(
      member,
      "Designer First",
      "Product Designer building accessible interfaces with Figma.",
    );
    await member
      .getByRole("button", { name: /Вакансии/ })
      .first()
      .click();
    await expect(
      setup.getByRole("heading", { name: "Подготовьте личный кабинет" }),
    ).toHaveCount(0);
    await card.getByRole("button", { name: "Сделать CV", exact: true }).click();
    await expect(
      card.getByRole("link", { name: "Скачать PDF", exact: true }),
    ).toBeVisible();
    await member
      .getByRole("button", { name: "Аккаунт и доступ", exact: true })
      .click();
    const displayName = "Александра" + "ОченьДлинноеИмя".repeat(5);
    await member.getByLabel("Отображаемое имя").fill(displayName);
    await member.getByRole("button", { name: "Сохранить имя" }).click();
    await expect(member.getByLabel("Текущий пользователь")).toHaveText(
      displayName,
    );
    await member.reload();
    await expect(member.getByLabel("Текущий пользователь")).toHaveText(
      displayName,
    );
    expect(
      await member.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await member
      .getByRole("button", { name: "OpenAI за сутки: 3 из 60" })
      .click();
    await expect(
      member.getByRole("heading", { name: "Использование OpenAI" }),
    ).toBeVisible();
    await member.screenshot({
      path: "test-results/stage8-mobile-account.png",
      fullPage: true,
    });
    await member.locator(".skip-link").focus();
    await member.keyboard.press("Enter");
    await expect(member.locator("#main-content")).toBeFocused();
    await member
      .getByRole("button", { name: "Выйти из аккаунта", exact: true })
      .click();
    await expect(member.getByLabel("Логин", { exact: true })).toBeVisible();
    await expect(member.getByLabel("Текущий пользователь")).toHaveCount(0);
    await expect(member.getByRole("article")).toHaveCount(0);
  } finally {
    await context.close();
  }
});

test("access links leave URLs before slow startup, can be cancelled and recover a password without leaking tokens", async ({
  page,
  browser,
}) => {
  await signIn(page);
  const invitation = await invite(page, "stage8-access");
  const context = await browser.newContext();
  try {
    const member = await context.newPage();
    const requests: string[] = [];
    member.on("request", (request) => requests.push(request.url()));
    let resume!: () => void;
    const held = new Promise<void>((resolve) => {
      resume = resolve;
    });
    await member.route("**/api/session", async (route) => {
      await held;
      await route.continue();
    });
    await member.goto(`${origin}/#invite=${invitation.token}`, {
      waitUntil: "domcontentloaded",
    });
    await expect.poll(() => new URL(member.url()).hash).toBe("");
    await expect(
      member.getByRole("heading", { name: "Принять приглашение" }),
    ).toHaveCount(0);
    resume();
    await expect(
      member.getByRole("button", { name: "Вернуться ко входу" }),
    ).toBeVisible();
    await member.unroute("**/api/session");
    await member.getByRole("button", { name: "Вернуться ко входу" }).click();
    await expect(member.getByLabel("Логин", { exact: true })).toBeVisible();
    expect(requests.some((url) => url.includes(invitation.token))).toBe(false);
    await accept(member, invitation.token);
    await member.getByRole("button", { name: "Выйти", exact: true }).click();
    const issued = await page.request.post(
      `${origin}/api/admin/users/${invitation.user_id}/password-resets`,
      { headers: await headers(page) },
    );
    expect(issued.status()).toBe(201);
    const recovery = await issued.json();
    await member.goto(`${origin}/#reset=${recovery.token}`);
    await expect.poll(() => new URL(member.url()).hash).toBe("");
    await member
      .getByLabel("Пароль", { exact: true })
      .fill(password + "-recovered");
    await member
      .getByRole("button", { name: "Сохранить новый пароль", exact: true })
      .click();
    await expect(
      member.getByText("Пароль обновлён. Войдите с новым паролем."),
    ).toBeVisible();
    await member.getByLabel("Логин", { exact: true }).fill("stage8-access");
    await member.getByLabel("Пароль", { exact: true }).fill(password);
    await member.getByRole("button", { name: "Войти", exact: true }).click();
    await expect(member.getByLabel("Пароль", { exact: true })).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    await member
      .getByLabel("Пароль", { exact: true })
      .fill(password + "-recovered");
    await member.getByLabel("Пароль", { exact: true }).press("Enter");
    await expect(member.getByLabel("Текущий пользователь")).toHaveText(
      "stage8-access",
    );
    expect(requests.some((url) => url.includes(recovery.token))).toBe(false);
    const summary = await member.request.get(`${origin}/api/workspace`);
    expect(summary.status()).toBe(200);
    expect(summary.headers()["cache-control"]).toBe("no-store");
    expect(summary.headers()["referrer-policy"]).toBe("no-referrer");
  } finally {
    await context.close();
  }
});

test("large board and partial Telegram search publish together with compact source diagnostics", async ({ page, browser }) => {
  await signIn(page);
  const issued = await invite(page, "stage-search-fix");
  const context = await browser.newContext();
  try {
    const member = await context.newPage();
    await accept(member, issued.token);
    await configureSearch(member, "stage-search-fix");
    for (const source of [
      { kind: "greenhouse", config: { token: "stage-search-fix" } },
      { kind: "telegram", config: { handle: "partial_jobs" } },
    ]) {
      const added = await member.request.post(`${origin}/api/sources`, {
        headers: await headers(member), data: source,
      });
      expect(added.status()).toBe(201);
    }
    await member.reload();
    const runId = await launchSearch(member);
    const result = await (await member.request.get(`${origin}/api/search-runs/${runId}`)).json();
    expect(result.sources.find((source: {kind: string}) => source.kind === "greenhouse").count).toBe(300);
    const telegram = result.sources.find((source: {kind: string}) => source.kind === "telegram");
    expect(telegram.count).toBe(1);
    expect(telegram.error).toBe("HTTP_429");
    await member.getByRole("button", { name: /Показать свежую выдачу/ }).click();
    const summary = member.getByRole("region", { name: "Результат поискового запуска" });
    await expect(summary.getByText("Проблемы источников: 1")).toBeVisible();
    const reason = summary.getByText(/Telegram · @partial_jobs: источник временно/);
    await expect(reason).not.toBeVisible();
    await summary.getByText("Проблемы источников: 1").click();
    await expect(reason).toBeVisible();
    await summary.getByText("Источники поиска: 3").click();
    await expect(summary.getByText(/Telegram · @partial_jobs · получено частично · объявлений: 1/)).toBeVisible();
    await member.screenshot({ path: "test-results/search-source-diagnostics.png", fullPage: true });
    await member.reload();
    await expect(member.getByRole("article")).toHaveCount(10);
  } finally {
    await context.close();
  }
});

test("layout stays aligned across workspace pages and viewport sizes", async ({ page, browser }) => {
  test.setTimeout(180000);
  await signIn(page);
  const issued = await invite(page, "design-layout");
  const context = await browser.newContext();
  const guest = await browser.newContext();
  try {
    const member = await context.newPage();
    const login = await guest.newPage();
    await accept(member, issued.token);
    await member.request.patch(`${origin}/api/account`, {
      headers: await headers(member), data: { display_name: "LayoutParticipant".repeat(6) },
    });
    await member.reload();
    await context.route("https://example.com/**", (route) => route.fulfill({ body: "Synthetic vacancy" }));
    await login.goto(origin);
    await expect(login).toHaveTitle("getCareer · Поиск работы");
    await expect(member).toHaveTitle("getCareer · Поиск работы");
    await expect(member.locator(".main-footer").getByText("getCareer", { exact: true })).toBeVisible();
    const memberBrand = member.getByRole("link", { name: "getCareer", exact: true });
    const loginBrand = login.getByRole("link", { name: "getCareer", exact: true });
    const widths = [320, 360, 390, 620, 621, 768, 800, 801, 900, 1024, 1150, 1151, 1440, 1920];
    async function contained(target: Page) {
      await expectReadableColors(target);
      await expectLayoutIntegrity(target);
      const sizes = await target.evaluate(() => ({
        width: document.documentElement.clientWidth,
        content: document.documentElement.scrollWidth,
      }));
      expect(sizes.content).toBeLessThanOrEqual(sizes.width + 1);
      for (const panel of await target.locator(".search-settings fieldset").all()) {
        if (!(await panel.isVisible())) continue;
        const panelBox = await panel.boundingBox();
        const titleBox = await panel.locator("legend").boundingBox();
        expect(titleBox!.y - panelBox!.y).toBeGreaterThanOrEqual(16);
        expect(titleBox!.x - panelBox!.x).toBeGreaterThanOrEqual(16);
        expect(titleBox!.x + titleBox!.width).toBeLessThanOrEqual(panelBox!.x + panelBox!.width - 16);
      }
      for (const element of await target.locator("input:not([type=hidden]):not([type=file]), select, textarea").all()) {
        if (!(await element.isVisible())) continue;
        const bounds = await element.boundingBox();
        expect(bounds!.x).toBeGreaterThanOrEqual(0);
        expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(sizes.width + 1);
      }
    }
    for (const width of widths) {
      await member.setViewportSize({ width, height: 960 });
      await login.setViewportSize({ width, height: 960 });
      await expect(member.getByRole("heading", { name: "Подготовьте личный кабинет" })).toBeVisible();
      await expect(memberBrand).toBeVisible();
      await expect(loginBrand).toBeVisible();
      if (width <= 620) {
        const brand = await memberBrand.boundingBox();
        const account = await member.locator(".topbar-account").boundingBox();
        expect(brand!.x + brand!.width).toBeLessThan(account!.x);
      }
      await contained(member);
      await contained(login);
      const passwordField = login.getByLabel("Пароль", { exact: true });
      await passwordField.focus();
      await passwordField.press("Shift+Tab");
      await login.keyboard.press("Tab");
      await expect(passwordField).toBeFocused();
      const focus = await passwordField.evaluate((element) => {
        const style = getComputedStyle(element);
        return { visible: element.matches(":focus-visible"), width: parseFloat(style.outlineWidth), style: style.outlineStyle };
      });
      expect(focus.visible).toBe(true);
      expect(focus.width).toBeGreaterThanOrEqual(3);
      expect(focus.style).toBe("solid");
      const submit = login.locator(".primary");
      await login.mouse.move(0, 0);
      const restingFill = await submit.evaluate((element) => getComputedStyle(element).backgroundColor);
      await submit.hover();
      await expect.poll(() => submit.evaluate((element) => getComputedStyle(element).backgroundColor)).not.toBe(restingFill);
      await expectReadableColors(login);
      await passwordField.hover();
      const actions = member.locator(".setup-guide .button");
      expect(await actions.count()).toBe(3);
      const boxes = await Promise.all((await actions.all()).map((action) => action.boundingBox()));
      for (const box of boxes) expect(box!.height).toBeGreaterThanOrEqual(44);
      if (width > 1024) {
        expect(Math.max(...boxes.map((box) => box!.y)) - Math.min(...boxes.map((box) => box!.y))).toBeLessThan(2);
        expect(Math.max(...boxes.map((box) => box!.width)) - Math.min(...boxes.map((box) => box!.width))).toBeLessThan(2);
      } else {
        expect(Math.max(...boxes.map((box) => box!.x)) - Math.min(...boxes.map((box) => box!.x))).toBeLessThan(2);
        expect(Math.max(...boxes.map((box) => box!.width)) - Math.min(...boxes.map((box) => box!.width))).toBeLessThan(2);
        for (let index = 1; index < boxes.length; index++) {
          expect(boxes[index]!.y).toBeGreaterThan(boxes[index - 1]!.y + boxes[index - 1]!.height);
        }
      }
      await member.screenshot({ path: `test-results/design-setup-${width}.png`, fullPage: true });
      await login.screenshot({ path: `test-results/design-login-${width}.png`, fullPage: true });
    }
    await member.setViewportSize({ width: 1440, height: 1050 });
    await activatePersonalCV(member, "Layout Example", "Backend Engineer with confirmed Python experience.", `${"DetailedCandidateReference_".repeat(4)}LayoutExample.docx`);
    await configureSearch(member, "stage-design-layout");
    const sourceList = (await (await member.request.get(`${origin}/api/sources`)).json()).items;
    await member.request.patch(`${origin}/api/sources/${sourceList[0].id}`, {
      headers: await headers(member),
      data: { expected_version: sourceList[0].version, config: { ...sourceList[0].config, name: "VeryLongSourceName".repeat(6) } },
    });
    await member.reload();
    await launchSearch(member);
    await member.getByRole("button", { name: /Показать свежую выдачу/ }).click();
    await member.getByRole("article").first().getByRole("button", { name: "Сделать CV", exact: true }).click();
    await expect(member.getByRole("link", { name: "Скачать PDF", exact: true })).toBeVisible();
    await member.getByRole("button", { name: "Профиль и шаблон", exact: true }).click();
    await member.getByLabel("Файл эталонного CV").setInputFiles({
      name: `${"DetailedCandidateDraft_".repeat(4)}LayoutExample.docx`,
      mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      buffer: readFileSync(path.resolve("../tests/fixtures/candidate.docx")),
    });
    await expect(member.getByRole("heading", { name: "Факты из вашего CV" })).toBeVisible();
    await member.getByRole("textbox", { name: "Сообщение агенту", exact: true }).fill(`Сохраните подтверждённые сведения. Обсудим пример: https://example.com/${"long-reference-".repeat(12)}`);
    await member.getByRole("button", { name: "Отправить", exact: true }).click();
    await expect(member.locator(".message-assistant")).toBeVisible();
    for (const width of widths) {
      await member.setViewportSize({ width, height: 1050 });
      for (const [button, heading, image] of [
        ["Вакансии", "Подходящие вакансии.", "jobs"],
        ["Applied", "Applied.", "applied"],
        ["Архив", "Архив вакансий.", "archive"],
        ["Профиль и шаблон", "Профиль и шаблон", "profile"],
        ["Поиск и источники", "Поиск и источники", "criteria"],
        ["Аккаунт и доступ", "Аккаунт и доступ", "account"],
      ]) {
        await member.getByRole("navigation", { name: "Основная навигация" }).getByRole("button", { name: new RegExp(button) }).click();
        await expect(member.getByRole("heading", { level: 1, name: heading, exact: true })).toBeVisible();
        await expect(member.getByRole("status").filter({ hasText: /Загружаем/ })).toHaveCount(0);
        await contained(member);
        if (image === "profile") {
          await member.locator(".draft-item").first().click();
          await expect(member.getByRole("heading", { name: "Факты из вашего CV" })).toBeVisible();
          await contained(member);
        }
        if (image === "jobs") {
          const results = await member.locator(".job-list").boundingBox();
          const history = await member.locator(".search-history").boundingBox();
          const summary = await member.locator(".search-run-summary").boundingBox();
          expect(history!.y).toBeGreaterThanOrEqual(results!.y + results!.height);
          expect(summary!.y).toBeGreaterThanOrEqual(history!.y + history!.height);
          const pagination = member.getByRole("navigation", { name: "Пагинация вакансий" });
          if (await pagination.count()) {
            const bounds = await pagination.boundingBox();
            expect(history!.y).toBeGreaterThanOrEqual(bounds!.y + bounds!.height);
          }
          const popupWait = member.waitForEvent("popup");
          await member.getByRole("link", { name: "Перейти на страницу вакансии" }).click();
          await (await popupWait).close();
          const dialog = member.getByRole("dialog", { name: "Ты откликнулся?" });
          await expect(dialog).toBeVisible();
          await expectLayoutIntegrity(member);
          const bounds = await dialog.boundingBox();
          expect(bounds!.x).toBeGreaterThanOrEqual(0);
          expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width + 1);
          await member.screenshot({ path: `test-results/design-dialog-${width}.png` });
          await member.getByRole("button", { name: "Нет", exact: true }).click();
          await expect(member.locator(".job-list")).toHaveAttribute("aria-busy", "false");
          await expect(member.locator(".job-list")).toHaveCSS("opacity", "1");
          await contained(member);
        }
        if (image === "criteria") {
          await member.getByRole("button", { name: "Источники", exact: true }).click();
          const checkbox = member.locator(".source-list .checkbox-row").first();
          const labelBox = await checkbox.boundingBox();
          const inputBox = await checkbox.getByRole("checkbox").boundingBox();
          expect(inputBox!.x).toBeLessThan(labelBox!.x + 2);
          expect(inputBox!.width).toBeLessThanOrEqual(20);
          await contained(member);
          await member.screenshot({ path: `test-results/design-sources-${width}.png`, fullPage: true });
          await member.getByRole("button", { name: "Что ищу", exact: true }).click();
          await contained(member);
        }
        if (image === "account") {
          await member.getByRole("button", { name: "Расписание и лимиты", exact: true }).click();
          await expect(member.getByRole("heading", { name: "Ваше расписание поиска" })).toBeVisible();
          await contained(member);
          await member.getByLabel("Интервал поиска в минутах").fill("1");
          await member.getByRole("button", { name: "Сохранить расписание", exact: true }).click();
          expect(await member.getByLabel("Интервал поиска в минутах").evaluate((input: HTMLInputElement) => input.validity.rangeUnderflow)).toBe(true);
          await member.getByLabel("Интервал поиска в минутах").fill("180");
        }
        if (width >= 1024) {
          const mainBox = await member.locator("main").boundingBox();
          const headingBox = await member.getByRole("heading", { level: 1 }).boundingBox();
          expect(headingBox!.x).toBeGreaterThan(mainBox!.x);
        }
        await member.evaluate(() => window.scrollTo(0, 0));
        await member.screenshot({ path: `test-results/design-${image}-${width}.png`, fullPage: true });
      }
    }
    await page.getByRole("button", { name: "Аккаунт и доступ", exact: true }).click();
    await page.getByRole("button", { name: "Расписание и лимиты", exact: true }).click();
    await page.getByRole("button", { name: "Открыть диагностику", exact: true }).click();
    for (const width of [390, 1440]) {
      await page.setViewportSize({ width, height: 1050 });
      await contained(page);
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: `test-results/design-admin-${width}.png`, fullPage: true });
    }
    const renamed = await member.request.patch(`${origin}/api/account`, {
      headers: await headers(member), data: { display_name: "Алексей Смирнов" },
    });
    expect(renamed.ok()).toBe(true);
    await member.reload();
    await expect(member.getByRole("heading", { name: "Подходящие вакансии.", exact: true })).toBeVisible();
    await expect(member.getByRole("status").filter({ hasText: /Загружаем|Подбираем/ })).toHaveCount(0);
    await expect(member.getByRole("article").first()).toBeVisible();
    await member.locator(".list-meta").click();
    for (const width of [390, 1440]) {
      await member.setViewportSize({ width, height: 1050 });
      await contained(member);
      await member.screenshot({ path: `test-results/palette-overview-${width}.png`, fullPage: true });
    }
  } finally {
    await Promise.all([context.close(), guest.close()]);
  }
});
