import { expect, test, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync, rmSync } from "node:fs";
import path from "node:path";
import { expectLayoutIntegrity } from "./layout-integrity";

const origin = "http://127.0.0.1:8015";
const python = process.env.QA_E2E_PYTHON || "../.venv/bin/python";
const password = "pilot-synthetic-password";

type Control = {
  schema: string;
  data_dir: string;
  owner_id: string;
  source_hash: string;
  source_database: string;
  source_logical_hash: string;
  owner_conversation_id: string;
};

function operator(control: Control, module: string, ...arguments_: string[]) {
  return JSON.parse(
    execFileSync(python, ["-m", `qa_job_bot.${module}`, ...arguments_], {
      env: {
        ...process.env,
        DATABASE_URL: process.env.QA_TEST_DATABASE_URL,
        DATABASE_SCHEMA: control.schema,
        WEB_DATA_DIR: control.data_dir,
      },
      encoding: "utf8",
    }),
  );
}

async function headers(page: Page) {
  const session = await (await page.request.get("/api/session")).json();
  return { origin, "x-csrf-token": session.csrf_token };
}

async function mutate(
  page: Page,
  endpoint: string,
  data: unknown,
  method = "POST",
) {
  return page.request.fetch(endpoint, {
    method,
    headers: await headers(page),
    data,
  });
}

async function ready(page: Page, identifier: string) {
  await expect
    .poll(
      async () =>
        (await (await page.request.get(`/api/tasks/${identifier}`)).json())
          .status,
      { timeout: 30000 },
    )
    .toBe("ready");
  return (await page.request.get(`/api/tasks/${identifier}`)).json();
}

test("maintenance, verified import and two-account pilot preserve history and refuse destructive rollback", async ({
  page,
  browser,
}) => {
  test.setTimeout(120000);
  const control: Control = JSON.parse(
    readFileSync(path.resolve("../.runtime/e2e-cutover.json"), "utf8"),
  );
  expect(control.schema).toMatch(/^qa_test_[a-f0-9]+$/);
  expect(control.data_dir).toMatch(/qa-web-e2e-[^/]+\/restored$/);
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Плановое обслуживание" }),
  ).toBeVisible();
  expect(
    (await page.request.post("/api/search-runs", { data: {} })).status(),
  ).toBe(503);
  expect(
    operator(control, "cutover", "rollback-status").matches_import_baseline,
  ).toBe(true);
  for (const width of [320, 390, 768, 1024, 1440, 1920]) {
    await page.setViewportSize({ width, height: 900 });
    const card = await page.getByRole("region", { name: "Обслуживание сервиса" }).boundingBox();
    const title = await page.getByRole("heading", { name: "Плановое обслуживание" }).boundingBox();
    const description = await page.getByText("Мы обновляем сервис. Попробуйте зайти позже.").boundingBox();
    const retry = await page.getByRole("button", { name: "Проверить доступность" }).boundingBox();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    expect(card!.x).toBeGreaterThanOrEqual(24);
    expect(card!.x + card!.width).toBeLessThanOrEqual(width - 24);
    expect(Math.abs(card!.x + card!.width / 2 - width / 2)).toBeLessThan(1);
    expect(title!.y).toBeGreaterThan(card!.y + 24);
    expect(description!.y - title!.y - title!.height).toBeGreaterThanOrEqual(15);
    expect(retry!.y - description!.y - description!.height).toBeGreaterThanOrEqual(27);
    expect(retry!.height).toBeGreaterThanOrEqual(44);
    expect(retry!.y + retry!.height).toBeLessThanOrEqual(card!.y + card!.height - 24);
    await expectLayoutIntegrity(page);
    await page.screenshot({ path: `test-results/maintenance-${width}.png`, fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 1050 });
  await page.screenshot({
    path: "test-results/stage10-maintenance.png",
    fullPage: true,
  });
  operator(control, "maintenance", "off");
  await page.getByRole("button", { name: "Проверить доступность" }).click();
  await page.getByLabel("Логин", { exact: true }).fill("owner");
  await page
    .getByLabel("Пароль", { exact: true })
    .fill("legacy-synthetic-password");
  await page.getByRole("button", { name: "Войти", exact: true }).click();
  await expect(
    page.getByRole("article", { name: "Legacy Active QA", exact: true }),
  ).toBeVisible();
  const originalProfile = await (await page.request.get("/api/profile")).json();
  const originalMessages = await (
    await page.request.get(
      `/api/conversations/${control.owner_conversation_id}/messages`,
    )
  ).json();
  expect(originalMessages.items.length).toBeGreaterThan(0);
  const originalApplied = await (
    await page.request.get("/api/jobs?tab=applied")
  ).json();
  const originalArchive = await (
    await page.request.get("/api/jobs?tab=archive")
  ).json();
  expect(originalApplied.total).toBe(2);
  expect(originalArchive.total).toBe(1);
  const originalTasks = await (await page.request.get("/api/tasks")).json();
  const originalArtifact = originalTasks.items
    .flatMap(
      (item: {
        artifacts: { id: string; format: string }[];
        status: string;
      }) => (item.status === "ready" ? item.artifacts : []),
    )
    .find((item: { format: string }) => item.format === "pdf");
  expect(originalArtifact).toBeTruthy();
  expect(
    (
      await page.request.get(`/api/artifacts/${originalArtifact.id}/download`)
    ).status(),
  ).toBe(200);
  const invitation = await mutate(page, "/api/admin/invitations", {
    login: "pilot-designer",
    display_name: "Pilot Designer",
  });
  expect(invitation.status()).toBe(201);
  const issued = await invitation.json();
  expect(
    (
      await mutate(page, "/api/admin/invitations", {
        login: "premature-third",
        display_name: "Third",
      })
    ).status(),
  ).toBe(409);
  await page
    .getByRole("button", { name: "Аккаунт и доступ", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Состояние закрытого пилота" })
    .click();
  await expect(page.getByText("Зарезервировано мест: 2 из 2.")).toBeVisible();
  const context = await browser.newContext({ baseURL: origin });
  const evidenceFile = path.resolve("../.runtime/e2e-pair-evidence.json");
  try {
    const tester = await context.newPage();
    await tester.goto(`/#invite=${issued.token}`);
    await tester.getByLabel("Пароль", { exact: true }).fill(password);
    await tester
      .getByRole("button", { name: "Принять приглашение", exact: true })
      .click();
    await expect(tester.getByLabel("Текущий пользователь")).toHaveText(
      "Pilot Designer",
    );
    expect((await (await tester.request.get("/api/jobs")).json()).total).toBe(
      0,
    );
    expect(
      (await (await tester.request.get("/api/profile")).json()).active,
    ).toBeNull();
    expect(
      (
        await tester.request.get(
          `/api/templates/${originalProfile.active.id}/source`,
        )
      ).status(),
    ).toBe(404);
    expect(
      (
        await tester.request.get(
          `/api/artifacts/${originalArtifact.id}/download`,
        )
      ).status(),
    ).toBe(404);
    expect(
      (
        await tester.request.get(
          `/api/conversations/${control.owner_conversation_id}/messages`,
        )
      ).status(),
    ).toBe(404);
    const profileResponse = await mutate(
      tester,
      "/api/search-profile",
      {
        expected_version: 0,
        profile: {
          role_keywords: ["product designer"],
          allowed_locations: ["europe"],
        },
      },
      "PUT",
    );
    expect(profileResponse.status()).toBe(200);
    expect(
      (await mutate(tester, "/api/sources", { kind: "remotive" })).status(),
    ).toBe(201);
    const run = await mutate(tester, "/api/search-runs", {
      request_id: "pilot-designer-search",
    });
    expect(run.status()).toBe(202);
    await ready(tester, (await run.json()).id);
    const listing = await (await tester.request.get("/api/jobs")).json();
    expect(listing.total).toBe(1);
    const job = listing.items[0];
    expect(job.title).toBe("Product Designer");
    await tester.reload();
    await expect(
      tester.getByRole("article", { name: "Product Designer", exact: true }),
    ).toBeVisible();
    const bytes = execFileSync(python, [
      "-c",
      "import io,sys; from docx import Document; d=Document(); d.add_paragraph('Pilot Designer'); d.add_paragraph('Designs accessible interfaces in Figma.'); b=io.BytesIO(); d.save(b); sys.stdout.buffer.write(b.getvalue())",
    ]);
    const uploaded = await tester.request.post("/api/template-drafts", {
      headers: await headers(tester),
      multipart: {
        file: {
          name: "pilot-designer.docx",
          mimeType:
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          buffer: bytes,
        },
      },
    });
    expect(uploaded.status()).toBe(201);
    const draft = await uploaded.json();
    expect(
      (
        await mutate(tester, `/api/template-drafts/${draft.id}/activate`, {
          expected_version: null,
          confirmed: true,
        })
      ).status(),
    ).toBe(200);
    const conversation = await mutate(tester, "/api/conversations", {
      template_id: draft.id,
    });
    const conversationId = (await conversation.json()).id;
    const chat = await mutate(
      tester,
      `/api/conversations/${conversationId}/messages`,
      { text: "Emphasize accessible interfaces.", request_id: "pilot-chat" },
    );
    await ready(tester, (await chat.json()).id);
    const messages = await (
      await tester.request.get(`/api/conversations/${conversationId}/messages`)
    ).json();
    expect(
      messages.items.some(
        (item: { role: string }) => item.role === "assistant",
      ),
    ).toBe(true);
    expect(
      (
        await page.request.get(`/api/conversations/${conversationId}/messages`)
      ).status(),
    ).toBe(404);
    const generated = await mutate(tester, `/api/jobs/${job.id}/documents`, {
      kind: "cv",
      language: "en",
      request_id: "pilot-document",
    });
    expect(generated.status()).toBe(202);
    const document = await ready(tester, (await generated.json()).id);
    expect(document.artifacts).toHaveLength(2);
    for (const artifact of document.artifacts) {
      const response = await tester.request.get(
        `/api/artifacts/${artifact.id}/download`,
      );
      expect(response.status()).toBe(200);
      expect(
        (
          await page.request.get(`/api/artifacts/${artifact.id}/download`)
        ).status(),
      ).toBe(404);
      if (artifact.format === "pdf") {
        const content = execFileSync(
          python,
          [
            "-c",
            "import io,sys; from pypdf import PdfReader; print(' '.join(p.extract_text() for p in PdfReader(io.BytesIO(sys.stdin.buffer.read())).pages))",
          ],
          { input: await response.body(), encoding: "utf8" },
        );
        expect(content).toContain("Pilot Designer");
        expect(content).toContain("Figma");
      }
    }
    const ownerSearch = await mutate(page, "/api/search-runs", {
      request_id: "pilot-owner-search",
    });
    expect(ownerSearch.status()).toBe(202);
    await ready(page, (await ownerSearch.json()).id);
    const ownerJobs = await (await page.request.get("/api/jobs")).json();
    expect(ownerJobs.total).toBeGreaterThan(0);
    expect(
      ownerJobs.items.every((item: { title: string }) =>
        /qa/i.test(item.title),
      ),
    ).toBe(true);
    expect(
      (
        await mutate(
          page,
          `/api/jobs/${job.id}/state`,
          { applied: true },
          "PATCH",
        )
      ).status(),
    ).toBe(404);
    expect(
      (
        await mutate(
          tester,
          `/api/jobs/${job.id}/state`,
          { archived: true },
          "PATCH",
        )
      ).status(),
    ).toBe(200);
    expect(
      (await (await tester.request.get("/api/jobs?tab=archive")).json()).total,
    ).toBe(1);
    expect(
      (await (await page.request.get("/api/jobs?tab=archive")).json()).total,
    ).toBe(1);
    expect(
      (await (await page.request.get("/api/jobs?tab=applied")).json()).total,
    ).toBe(2);
    expect(
      (await (await page.request.get("/api/profile")).json()).active.id,
    ).toBe(originalProfile.active.id);
    expect(
      (
        await (
          await page.request.get(
            `/api/conversations/${control.owner_conversation_id}/messages`,
          )
        ).json()
      ).items,
    ).toEqual(originalMessages.items);
    const checks = [
      "owner_history",
      "search_professions",
      "personal_states",
      "candidate_templates",
      "conversations",
      "documents",
      "access_isolation",
    ];
    writeFileSync(
      evidenceFile,
      JSON.stringify({
        owner_id: control.owner_id,
        tester_id: issued.user_id,
        source_hash: control.source_hash,
        checks: Object.fromEntries(checks.map((name) => [name, true])),
      }),
      { mode: 0o600 },
    );
    const cohort = operator(
      control,
      "pilot",
      "expand",
      "--evidence",
      evidenceFile,
    );
    expect(cohort.phase).toBe("cohort");
    expect(cohort.limit).toBe(20);
    await page
      .getByRole("button", { name: "Обновить состояние пилота" })
      .click();
    await expect(
      page.getByText("Зарезервировано мест: 2 из 20."),
    ).toBeVisible();
    expect(
      (
        await mutate(page, "/api/admin/invitations", {
          login: "allowed-third",
          display_name: "Third",
        })
      ).status(),
    ).toBe(201);
    const sourceDigest = execFileSync(
      python,
      [
        "-c",
        "import hashlib,sqlite3,sys; from pathlib import Path; c=sqlite3.connect(Path(sys.argv[1]).resolve().as_uri()+'?mode=ro',uri=True); print(hashlib.sha256('\\n'.join(c.iterdump()).encode()).hexdigest()); c.close()",
        control.source_database,
      ],
      { encoding: "utf8" },
    ).trim();
    expect(sourceDigest).toBe(control.source_logical_hash);
    operator(control, "maintenance", "on");
    await expect(
      page.getByRole("heading", { name: "Плановое обслуживание" }),
    ).toBeVisible();
    await expect(
      tester.getByRole("heading", { name: "Плановое обслуживание" }),
    ).toBeVisible();
    const rollback = operator(control, "cutover", "rollback-status");
    expect(rollback.database_changed).toBe(true);
    expect(rollback.files_changed).toBe(true);
    expect(rollback.preserve_postgres_and_files).toBe(true);
    expect(rollback.legacy_restart_allowed).toBe(false);
  } finally {
    await context.close();
    rmSync(evidenceFile, { force: true });
    operator(control, "maintenance", "off");
  }
});
