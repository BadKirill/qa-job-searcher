import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../src/App";
import {
  emptySearch,
  SearchSettings,
  type Preferences,
  type Source,
} from "../src/SearchSettings";
import { server } from "./fixtures";

const examples = {
  developer: { ...emptySearch, role_keywords: ["backend engineer"] },
  designer: { ...emptySearch, role_keywords: ["product designer", "ux designer"], skill_keywords: ["figma"] },
  qa: { ...emptySearch, role_keywords: ["qa engineer"], qa_title_fallback: true },
};

function preferencesServer(initial?: Partial<Preferences>) {
  let state: Preferences = {
    search_profile: { configured: false, version: 0, profile: null },
    sources: { version: 0, items: [] },
    ...initial,
  };
  const base = server();
  const calls: { path: string; body: any }[] = [];
  const failures: string[] = [];
  const versions: NonNullable<Preferences["search_profile"]["profile"]>[] = [];
  let activeVersion = state.search_profile.version;
  const variantNames = new Map<number, string>();
  const deleted = new Set<number>();
  const validation: Record<string, string>[] = [];
  let check = {
    status: "ok",
    items: 2,
    stale: false,
    reason: null as string | null,
  };
  const fetcher = vi.fn(async (path: string, options: RequestInit = {}) => {
    if (
      !/search-settings|search-profile|search-presets|source-types|\/sources|\/places/.test(
        path,
      )
    )
      return base.fetcher(path, options);
    const body =
      typeof options.body === "string" ? JSON.parse(options.body) : undefined;
    calls.push({ path, body });
    if (failures[0] === path) {
      failures.shift();
      return new Response(
        '{"detail":"Настройки изменились в другой вкладке"}',
        { status: 409 },
      );
    }
    if (path === "/api/search-profile" && validation.length) {
      return new Response(JSON.stringify({ detail: "Проверьте поля запроса.", fields: validation.shift() }), { status: 422 });
    }
    let data: unknown = {};
    if (path === "/api/search-settings") data = state;
    else if (path === "/api/source-types")
      data = {
        items: [
          "remotive",
          "himalayas",
          "weworkremotely",
          "remote_com",
          "hirify",
          "lever",
          "greenhouse",
          "ashby",
          "telegram",
        ].map((kind) => ({
          kind,
          name: kind,
          limitations: "Окно выдачи ограничено",
        })),
      };
    else if (path === "/api/search-presets")
      data = { items: examples };
    else if (path === "/api/search-profile") {
      versions.push(body.profile);
      const version = state.search_profile.version + 1;
      const name = body.name ?? state.search_profile.name ?? "Версия 1";
      variantNames.set(versions.length, name);
      if (!body.create || !state.search_profile.configured) {
        activeVersion = versions.length;
        state.search_profile = {
          configured: true, version, editor_version: version, variant_id: body.variant_id ?? "1",
          name, profile: body.profile,
        };
      }
      data = body.create ? { ...state.search_profile, saved_variant_id: String(versions.length), saved_name: name } : state.search_profile;
    } else if (path.startsWith("/api/search-profile/history/")) {
      const version = Number(path.split("/").at(-1));
      if (body.action === "activate") {
        activeVersion = version;
        state.search_profile = {
          ...state.search_profile, version, profile: versions[version - 1], name: variantNames.get(version),
        };
      } else if (body.action === "rename") variantNames.set(version, body.name);
      else if (body.action === "delete") deleted.add(version);
      state.search_profile.editor_version = (state.search_profile.editor_version ?? 0) + 1;
      data = state.search_profile;
    } else if (path === "/api/search-profile/history") {
      data = { items: versions.map((profile, index) => ({
        profile, version: index + 1, variant_id: String(index + 1),
        name: variantNames.get(index + 1) ?? "Версия 1",
        created_at: "2026-11-26T10:00:00+00:00",
        active: index + 1 === activeVersion, deleted: deleted.has(index + 1),
      })) };
    } else if (path.startsWith("/api/places?")) {
      data = { items: [{ id: "region:mena", label: "MENA · Ближний Восток и Северная Африка" }], corrected: false };
    } else if (path.startsWith("/api/places/legacy?")) {
      data = {
        "region:mena": [{ id: "region:mena", label: "MENA · Ближний Восток и Северная Африка" }],
        "tbilisi": [{ id: "city:ge-tbilisi", label: "Тбилиси, Грузия" }],
        "city:ge-tbilisi": [{ id: "city:ge-tbilisi", label: "Тбилиси, Грузия" }],
        "serbia": [{ id: "country:RS", label: "Сербия" }],
        "country:RS": [{ id: "country:RS", label: "Сербия" }],
        "unknown": [],
      };
    } else if (path === "/api/sources/resolve") {
      const existing = state.sources.items.find((row) => row.kind === "telegram");
      data = { spec: { kind: "telegram", enabled: true, config: { handle: "public_jobs" } }, existing };
    } else if (path === "/api/sources/import/file") {
      data = { items: [
        { row: 2, status: "new", message: "Готов к добавлению.", name: "Board", spec: { kind: "lever", enabled: true, config: { token: "board" } } },
        { row: 3, status: "new", message: "Готов к добавлению.", name: "Other", spec: { kind: "ashby", enabled: true, config: { token: "other" } } },
        { row: 4, status: "duplicate", message: "Источник уже есть в списке." },
        { row: 5, status: "invalid", message: "Укажите карьерную страницу." },
      ] };
    } else if (path === "/api/sources/import") {
      data = { items: body.items };
    } else if (path === "/api/sources" && !body) data = state.sources;
    else if (path.endsWith("/check")) data = check;
    else {
      const id = path.split("/").at(-1)!;
      if (options.method === "DELETE")
        state.sources.items = state.sources.items.filter(
          (row) => row.id !== id,
        );
      else if (options.method === "PATCH")
        state.sources.items = state.sources.items.map((row) =>
          row.id === id ? { ...row, ...body, name: body.config?.name ?? row.name, version: row.version + 1 } : row,
        );
      else {
        state.sources.items.push({
          id: "source-" + calls.length,
          version: 1,
          enabled: true,
          valid: true,
          name: body.config?.name ?? body.config?.handle ?? body.kind,
          config: body.config ?? {},
          limitations: "Окно выдачи ограничено",
          ...body,
        });
      }
      state.sources.version += 1;
      if (options.method === "DELETE")
        return new Response(null, { status: 204 });
    }
    return new Response(JSON.stringify(data), { status: 200 });
  });
  vi.stubGlobal("fetch", fetcher);
  return {
    base,
    calls,
    failures,
    validation,
    get state() {
      return state;
    },
    setCheck(value: Partial<typeof check>) {
      check = { ...check, ...value };
    },
  };
}
async function ready() {
  await screen.findByRole("button", { name: /^Сохранить$/ });
}
function source(overrides: Partial<Source> = {}): Source {
  return {
    id: "mine",
    name: "My board",
    kind: "lever",
    config: { token: "acme", name: "My board", region: "eu", tags: ["saas"] },
    enabled: true,
    valid: true,
    version: 1,
    limitations: "Окно выдачи ограничено",
    ...overrides,
  };
}

test("fresh personal workspace has no inherited settings and saves a universal profile", async () => {
  const api = preferencesServer();
  const user = userEvent.setup();
  render(<SearchSettings />);
  await ready();
  expect(screen.getByText("Источники пока не добавлены.")).toBeVisible();
  expect(screen.getByLabelText("Названия ролей и синонимы")).toHaveValue("");
  expect(
    screen.getByLabelText("Исключать Junior и стажировки"),
  ).not.toBeChecked();
  await user.type(
    screen.getByLabelText("Названия ролей и синонимы"),
    "Backend Engineer, software engineer",
  );
  await user.click(screen.getByText("Дополнительные настройки"));
  await user.type(
    screen.getByLabelText("Запросы для сайтов вакансий"),
    "python, ",
  );
  expect(
    screen.queryByLabelText("Учитывать дополнительные QA-названия"),
  ).not.toBeInTheDocument();
  await user.selectOptions(screen.getByLabelText("Пример профиля"), "qa");
  for (const name of [
    "Гибрид",
    "В офисе",
    "Исключать Junior и стажировки",
    "Учитывать дополнительные QA-названия",
    "Показывать удалённые вакансии с неясной географией",
  ])
    await user.click(screen.getByLabelText(name));
  await user.click(screen.getByLabelText("Удалённо"));
  fireEvent.change(screen.getByLabelText("Порог совпадения с критериями"), {
    target: { value: "60" },
  });
  await user.click(
    screen.getByRole("button", { name: /^Сохранить$/ }),
  );
  expect(await screen.findByText(/^Сохранено ·/)).toBeVisible();
  expect(api.state.search_profile.profile).toMatchObject({
    role_keywords: ["Backend Engineer", "software engineer"],
    source_queries: ["python"],
    workplace_types: ["hybrid", "on-site"],
    min_score: 60,
    exclude_entry_level: true,
  });
  expect(
    api.calls.find((call) => call.path === "/api/search-profile")?.body
      .expected_version,
  ).toBe(0);
});

test("examples fill a draft and history loads a saved revision without writing", async () => {
  const api = preferencesServer();
  const user = userEvent.setup();
  const { unmount } = render(<SearchSettings />);
  await ready();
  expect(screen.getByRole("button", { name: "Подставить пример" })).toBeDisabled();
  await user.selectOptions(screen.getByLabelText("Пример профиля"), "designer");
  await user.click(screen.getByRole("button", { name: "Подставить пример" }));
  expect(screen.getByLabelText("Названия ролей и синонимы")).toHaveValue("product designer, ux designer");
  expect(screen.getByLabelText("Навыки")).toHaveValue("figma");
  expect(screen.getByText(/Есть несохранённые изменения/)).toBeVisible();
  expect(api.calls.some((call) => call.body)).toBe(false);
  expect(api.state.sources.items).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: /^Сохранить$/ }));
  await screen.findByText(/^Сохранено · Версия 1/);
  unmount();
  render(<SearchSettings />);
  await ready();
  expect(screen.getByLabelText("Пример профиля")).toHaveValue("");
  const roles = screen.getByLabelText("Названия ролей и синонимы");
  expect(roles).toHaveValue("product designer, ux designer");
  await user.clear(roles);
  await user.type(roles, "brand designer");
  await user.click(screen.getByRole("button", { name: /^Сохранить$/ }));
  await screen.findByText(/^Сохранено ·/);
  api.failures.push("/api/search-profile/history");
  await user.click(screen.getByText("История версий профиля"));
  await user.click(await screen.findByRole("button", { name: "Повторить загрузку версий" }));
  const previous = await screen.findByText(/ревизия 1/);
  await user.click(previous);
  await user.click(within(previous.closest("details")!).getByRole("button", { name: "Загрузить в форму" }));
  expect(roles).toHaveValue("product designer, ux designer");
  expect(api.calls.filter((call) => call.body?.profile)).toHaveLength(2);
});

test("working language choices save together and can be cleared", async () => {
  const api = preferencesServer({
    search_profile: { configured: true, version: 1, profile: examples.developer },
  });
  const user = userEvent.setup();
  const { unmount } = render(<SearchSettings />);
  await ready();
  const russian = () => screen.getByRole("checkbox", { name: "Русскоязычные" });
  const english = () => screen.getByRole("checkbox", { name: "Англоязычные" });
  const save = () => user.click(screen.getByRole("button", { name: /^Сохранить$/ }));
  expect(russian()).not.toBeChecked();
  expect(english()).not.toBeChecked();
  await user.click(russian());
  await user.click(english());
  await save();
  await screen.findByText(/^Сохранено ·/);
  expect(api.state.search_profile.profile?.working_languages).toEqual(["ru", "en"]);

  unmount();
  render(<SearchSettings />);
  await ready();
  expect(russian()).toBeChecked();
  expect(english()).toBeChecked();
  await user.click(russian());
  await user.click(english());
  await save();
  await screen.findByText(/^Сохранено ·/);
  expect(api.state.search_profile.profile?.working_languages).toEqual([]);
});

test("new accounts have an empty profile history", async () => {
  const api = preferencesServer();
  render(<SearchSettings />);
  await ready();
  expect(api.calls.some((call) => call.path.endsWith("/history"))).toBe(false);
  const history = screen.getByText("История версий профиля");
  expect(history.closest("summary")).toHaveAccessibleDescription("Здесь появятся ваши сохранённые критерии");
  await userEvent.click(history);
  expect(await screen.findByText("Сохранённых версий пока нет.")).toBeVisible();
});

test("duplicate link opens the existing connection and does not mutate it", async () => {
  const api = preferencesServer({
    sources: { version: 1, items: [
      source({ id: "agg", kind: "remotive", name: "Remotive", config: {}, enabled: false }),
      source({ id: "channel", kind: "telegram", name: "Telegram", config: { handle: "public_jobs" } }),
    ] },
  });
  const user = userEvent.setup();
  render(<SearchSettings />);
  await ready();
  expect(screen.getByRole("button", { name: "Включить remotive" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "Удалить Remotive" })).not.toBeInTheDocument();
  await user.type(screen.getByLabelText("Добавить по ссылке"), "https://t.me/public_jobs");
  await user.click(screen.getByRole("button", { name: "Определить источник" }));
  await screen.findByText("Уже подключён · Telegram");
  expect(api.state.sources.items).toHaveLength(2);
  expect(api.calls.some((call) => call.path === "/api/sources" && call.body)).toBe(false);
});

test("board and channel can be edited without discarding criteria", async () => {
  const api = preferencesServer();
  const user = userEvent.setup();
  render(<SearchSettings />);
  await ready();
  const criteria = screen.getByLabelText("Названия ролей и синонимы");
  await user.type(criteria, "unsaved designer");
  await user.click(screen.getByRole("button", { name: "Указать параметры вручную" }));
  let dialog = screen.getByRole("dialog", { name: "Добавить подключение" });
  await user.selectOptions(within(dialog).getByLabelText("Тип источника"), "lever");
  await user.type(within(dialog).getByLabelText("Идентификатор доски"), "company");
  await user.type(within(dialog).getByLabelText("Название компании"), "Design jobs");
  await user.type(within(dialog).getByLabelText("Теги через запятую"), "design, ux");
  await user.selectOptions(within(dialog).getByLabelText("Регион"), "eu");
  await user.click(within(dialog).getByRole("button", { name: "Сохранить" }));
  await screen.findByRole("heading", { name: "Design jobs" });
  expect(criteria).toHaveValue("unsaved designer");
  expect(api.state.sources.items[0].config).toEqual({
    token: "company", name: "Design jobs", region: "eu", tags: ["design", "ux"],
  });
  await user.click(screen.getByRole("button", { name: "Выключить Design jobs" }));
  await screen.findByRole("button", { name: "Включить Design jobs" });
  await user.click(screen.getByRole("button", { name: "Изменить Design jobs" }));
  dialog = screen.getByRole("dialog", { name: "Изменить подключение" });
  expect(within(dialog).getByLabelText("Регион")).toHaveValue("eu");
  await user.clear(within(dialog).getByLabelText("Название компании"));
  await user.type(within(dialog).getByLabelText("Название компании"), "Changed");
  await user.click(within(dialog).getByRole("button", { name: "Сохранить" }));
  await screen.findByRole("heading", { name: "Changed" });
  await user.click(screen.getByRole("button", { name: "Указать параметры вручную" }));
  dialog = screen.getByRole("dialog", { name: "Добавить подключение" });
  await user.selectOptions(within(dialog).getByLabelText("Тип источника"), "telegram");
  await user.type(within(dialog).getByLabelText("Канал Telegram"), "public_jobs");
  await user.click(within(dialog).getByRole("button", { name: "Сохранить" }));
  await screen.findByRole("heading", { name: "public_jobs" });
});

test("deleting a personal connection requires confirmation and invalid imports can be edited", async () => {
  const api = preferencesServer({
    sources: { version: 1, items: [
      source(), source({ id: "invalid", name: "Invalid", valid: false, kind: "greenhouse", config: { token: "broken" } }),
    ] },
  });
  const user = userEvent.setup();
  render(<SearchSettings />);
  await ready();
  expect(screen.getByText("Исправьте параметры подключения.")).toBeVisible();
  expect(screen.getByRole("button", { name: "Проверить Invalid" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Изменить Invalid" }));
  expect(screen.getByLabelText("Название компании")).toHaveValue("");
  await user.click(screen.getByRole("button", { name: "Отмена" }));
  await user.click(screen.getByRole("button", { name: "Удалить Invalid" }));
  let dialog = screen.getByRole("dialog", { name: "Удалить Invalid?" });
  await user.click(within(dialog).getByRole("button", { name: "Отмена" }));
  expect(screen.getByRole("heading", { name: "Invalid" })).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Удалить Invalid" }));
  dialog = screen.getByRole("dialog", { name: "Удалить Invalid?" });
  await user.click(within(dialog).getByRole("button", { name: "Удалить" }));
  await waitFor(() => expect(screen.queryByRole("heading", { name: "Invalid" })).not.toBeInTheDocument());
  expect(api.state.sources.items).toHaveLength(1);
});

test("import preview shows rejected rows and confirms only selected sources", async () => {
  const api = preferencesServer();
  const user = userEvent.setup();
  render(<SearchSettings section="sources" />);
  await user.click(await screen.findByText("Импортировать компании и источники"));
  const file = new File(["company,url"], "sources.csv", { type: "text/csv" });
  await user.upload(screen.getByLabelText("Файл CSV"), file);
  await user.click(screen.getByRole("button", { name: "Предпросмотр импорта" }));
  const list = await screen.findByRole("list", { name: "Строки импорта" });
  expect(within(list).getByRole("checkbox", { name: /Источник уже есть/ })).toBeDisabled();
  expect(within(list).getByRole("checkbox", { name: /Укажите карьерную страницу/ })).toBeDisabled();
  await user.click(within(list).getByRole("checkbox", { name: /Other/ }));
  await user.click(screen.getByRole("button", { name: "Добавить выбранные" }));
  await screen.findByText("Добавлено источников · 1");
  expect(api.calls.find((call) => call.path.endsWith("/import"))?.body.items).toHaveLength(1);
});

test("geography suggestions can be selected with the keyboard and removed", async () => {
  const api = preferencesServer();
  const user = userEvent.setup();
  render(<SearchSettings section="criteria" />);
  await ready();
  const input = screen.getByRole("combobox", { name: "Откуда могу работать удалённо" });
  await user.type(input, "ме");
  await screen.findByRole("option", { name: /MENA/ });
  await user.keyboard("{ArrowDown}{Enter}");
  expect(screen.getByRole("button", { name: /Убрать MENA/ })).toBeVisible();
  await user.click(screen.getByRole("button", { name: /Убрать MENA/ }));
  expect(screen.queryByRole("button", { name: /Убрать MENA/ })).not.toBeInTheDocument();
  expect(api.calls.some((call) => call.body)).toBe(false);
});

test.each([
  [{}, "Источник доступен. Объявлений"],
  [{ status: "empty", items: 0 }, "Источник ответил. Пробная выборка пуста."],
  [{ stale: true }, "Настройки изменились во время проверки"],
  [
    { status: "unavailable", reason: "redirect" },
    "Автоматический переход запрещён",
  ],
  [
    { status: "unavailable", reason: "unexpected" },
    "Не удалось проверить источник.",
  ],
  [{ status: "unavailable", reason: null }, "Не удалось проверить источник."],
])("source checks explain their outcome %j", async (result, message) => {
  const api = preferencesServer({ sources: { version: 1, items: [source()] } });
  api.setCheck(result);
  render(<SearchSettings />);
  await ready();
  await userEvent.click(
    screen.getByRole("button", { name: "Проверить My board" }),
  );
  expect(await screen.findByText(new RegExp(message))).toBeVisible();
  expect(api.calls.find((call) => call.path.endsWith("/check"))?.body).toEqual({
    expected_version: 1,
  });
});

test("conflicts preserve the draft until explicit reload and failed initial reads can retry", async () => {
  const api = preferencesServer();
  api.failures.push("/api/search-settings");
  const user = userEvent.setup();
  render(<SearchSettings />);
  await screen.findByText("Настройки изменились в другой вкладке");
  await user.click(
    screen.getByRole("button", { name: "Загрузить актуальные настройки" }),
  );
  await ready();
  await user.type(
    screen.getByLabelText("Названия ролей и синонимы"),
    "draft role",
  );
  api.failures.push("/api/search-profile");
  await user.click(
    screen.getByRole("button", { name: /^Сохранить$/ }),
  );
  await screen.findByText("Настройки изменились в другой вкладке");
  expect(screen.getByLabelText("Названия ролей и синонимы")).toHaveValue(
    "draft role",
  );
  await user.click(
    screen.getByRole("button", { name: "Загрузить актуальные настройки" }),
  );
  await waitFor(() =>
    expect(screen.getByLabelText("Названия ролей и синонимы")).toHaveValue("draft role"),
  );
});

test("shared workspace navigation opens personal settings", async () => {
  const api = preferencesServer();
  api.base.state.session.user = {
    id: "me",
    login: "me",
    display_name: "Me",
    role: "user",
  };
  render(<App />);
  await userEvent.click(
    await screen.findByRole("button", { name: "Настройки поиска" }),
  );
  expect(
    await screen.findByRole("heading", { name: "Настройки поиска" }),
  ).toBeVisible();
  await ready();
});

test("late requests after unmount do not update another account", async () => {
  preferencesServer();
  let resolve!: (response: Response) => void;
  vi.stubGlobal(
    "fetch",
    () =>
      new Promise<Response>((done) => {
        resolve = done;
      }),
  );
  const view = render(<SearchSettings />);
  view.unmount();
  await act(async () => {
    resolve(new Response('{"items":{}}'));
  });
  expect(screen.queryByRole("heading")).not.toBeInTheDocument();
});

test("named variants support activate, rename, confirmation and removal without restoring a stale draft", async () => {
  const api = preferencesServer();
  const user = userEvent.setup();
  render(<SearchSettings section="criteria" />);
  await ready();
  const roles = screen.getByLabelText("Названия ролей и синонимы");
  const save = () => screen.getByRole("button", { name: /^Сохранить$/ });
  await user.type(roles, "backend engineer");
  await user.click(save());
  await screen.findByText(/^Сохранено · Версия 1/);
  await user.clear(roles);
  await user.type(roles, "product designer");
  await user.click(screen.getByRole("button", { name: "Сохранить как новую версию" }));
  let dialog = screen.getByRole("dialog", { name: "Новая версия профиля" });
  await user.type(within(dialog).getByLabelText("Имя варианта"), "Дизайн");
  await user.click(within(dialog).getByRole("button", { name: /^Сохранить$/ }));
  await screen.findByText(/^Сохранено · Дизайн/);
  await user.click(screen.getByText("История версий профиля"));
  const row = () => screen.getByText(/ревизия 2/).closest("details")!;
  await user.click(await screen.findByText(/ревизия 2/));
  await user.click(within(row()).getByRole("button", { name: "Переименовать" }));
  dialog = screen.getByRole("dialog", { name: "Переименовать вариант" });
  const name = within(dialog).getByLabelText("Имя варианта");
  await user.clear(name);
  await user.type(name, "Продукт");
  await user.click(within(dialog).getByRole("button", { name: /^Сохранить$/ }));
  await screen.findByText("В редакторе · Продукт");
  await user.click(await screen.findByText(/ревизия 2/));
  await user.click(within(row()).getByRole("button", { name: "Использовать в поиске" }));
  await screen.findByText("Используется в поиске · Продукт");
  await user.click(await screen.findByText(/ревизия 1/));
  const former = screen.getByText(/ревизия 1/).closest("details")!;
  await user.click(within(former).getByRole("button", { name: "Удалить вариант" }));
  dialog = screen.getByRole("dialog", { name: "Удалить вариант из списка?" });
  await user.click(within(dialog).getByRole("button", { name: "Отмена" }));
  expect(screen.getByText(/ревизия 1/)).toBeVisible();
  await user.click(within(former).getByRole("button", { name: "Удалить вариант" }));
  await user.click(within(dialog).getByRole("button", { name: /^Удалить$/ }));
  await waitFor(() => expect(screen.queryByText(/ревизия 1/)).not.toBeInTheDocument());
  expect(api.calls.filter((call) => call.body?.action).map((call) => call.body.action)).toEqual(["rename", "activate", "delete"]);
});

test("legacy location review keeps the old values and field errors focus the invalid input", async () => {
  const api = preferencesServer({
    search_profile: {
      configured: true, version: 1, name: "Мой поиск", variant_id: "1", editor_version: 0,
      profile: { ...examples.developer, candidate_locations: ["tbilisi", "unknown"], allowed_locations: ["serbia"] },
    },
  });
  const user = userEvent.setup();
  render(<SearchSettings section="criteria" />);
  await ready();
  await user.click(screen.getByText(/География из прежнего профиля/));
  await user.click(screen.getByRole("button", { name: "Сверить прежние локации" }));
  await screen.findByText("unknown · Требует уточнения");
  await user.click(screen.getByRole("button", { name: "Подставить распознанные локации" }));
  expect(await screen.findByRole("button", { name: "Убрать Тбилиси, Грузия" })).toBeVisible();
  expect(screen.getByText(/Проверьте подставленные локации/)).toBeVisible();
  await user.click(screen.getByText("Часы доступности"));
  await user.click(screen.getByRole("checkbox", { name: "Учитывать часы доступности" }));
  const zone = screen.getByLabelText("Часовой пояс");
  fireEvent.change(zone, { target: { value: "Asia/Tbilisi" } });
  fireEvent.change(screen.getByLabelText("Доступен с"), { target: { value: "10:00" } });
  fireEvent.change(screen.getByLabelText("Доступен до"), { target: { value: "19:00" } });
  api.validation.push({ "availability.zone": "Выберите зону IANA." });
  await user.click(screen.getByRole("button", { name: /^Сохранить$/ }));
  await screen.findByText("Выберите зону IANA.");
  expect(zone).toHaveAttribute("aria-invalid", "true");
  await waitFor(() => expect(zone).toHaveFocus());
  await user.click(screen.getByRole("button", { name: /^Сохранить$/ }));
  expect(api.state.search_profile.profile?.availability).toEqual({ zone: "Asia/Tbilisi", start: "10:00", end: "19:00" });
  expect(api.state.search_profile.profile?.candidate_locations).toEqual(["tbilisi", "unknown"]);
  expect(api.state.search_profile.profile?.remote_place_ids).toEqual(["city:ge-tbilisi"]);
  await user.click(screen.getByRole("button", { name: "Убрать прежние значения" }));
  expect(screen.queryByText(/География из прежнего профиля/)).not.toBeInTheDocument();
});

test("a second example offers to keep the dirty editor and replaces it only after explicit confirmation", async () => {
  preferencesServer();
  const user = userEvent.setup();
  render(<SearchSettings />);
  await ready();
  const roles = screen.getByLabelText("Названия ролей и синонимы");
  await user.type(roles, "My draft");
  await user.selectOptions(screen.getByLabelText("Пример профиля"), "designer");
  const apply = screen.getByRole("button", { name: "Подставить пример" });
  await user.click(apply);
  const dialog = screen.getByRole("dialog", { name: "Заменить несохранённые поля?" });
  await user.click(within(dialog).getByRole("button", { name: "Оставить черновик" }));
  expect(roles).toHaveValue("My draft");
  await user.click(apply);
  await user.click(within(dialog).getByRole("button", { name: /^Заменить$/ }));
  expect(roles).toHaveValue("product designer, ux designer");
});
