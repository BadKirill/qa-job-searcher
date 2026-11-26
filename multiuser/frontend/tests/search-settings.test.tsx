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
  designer: { ...emptySearch, role_keywords: ["product designer", "ux designer"] },
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
  let check = {
    status: "ok",
    items: 2,
    stale: false,
    reason: null as string | null,
  };
  const fetcher = vi.fn(async (path: string, options: RequestInit = {}) => {
    if (
      !/search-settings|search-profile|search-presets|source-types|\/sources/.test(
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
    else if (path.includes("/apply")) {
      const id = path.split("/")[3] as keyof typeof examples;
      versions.push(examples[id]);
      state = {
        ...state,
        search_profile: {
          version: state.search_profile.version + 1,
          configured: true,
          profile: examples[id],
        },
      };
      data = state;
    } else if (path === "/api/search-profile") {
      versions.push(body.profile);
      state.search_profile = {
        configured: true,
        version: state.search_profile.version + 1,
        profile: body.profile,
      };
      data = state.search_profile;
    } else if (path === "/api/search-profile/history") {
      data = { items: versions.map((profile, index) => ({
        profile,
        version: index + 1,
        active: index === versions.length - 1,
      })) };
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
          row.id === id ? { ...row, ...body, version: row.version + 1 } : row,
        );
      else {
        state.sources.items.push({
          id: "source-" + calls.length,
          version: 1,
          enabled: true,
          valid: true,
          name: body.config.name ?? body.config.handle ?? body.kind,
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
    get state() {
      return state;
    },
    setCheck(value: Partial<typeof check>) {
      check = { ...check, ...value };
    },
  };
}
async function ready() {
  await screen.findByRole("button", { name: "Сохранить профиль поиска" });
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
  await user.type(
    screen.getByLabelText("Запросы к агрегаторам (до 8)"),
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
  fireEvent.change(screen.getByLabelText("Минимальная оценка"), {
    target: { value: "60" },
  });
  await user.click(
    screen.getByRole("button", { name: "Сохранить профиль поиска" }),
  );
  expect(await screen.findByText("Профиль поиска сохранён.")).toBeVisible();
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

test("presets require explicit application and load a versioned saved profile", async () => {
  const api = preferencesServer();
  const user = userEvent.setup();
  const { unmount } = render(<SearchSettings />);
  await ready();
  expect(api.calls.some((call) => call.path.includes("apply"))).toBe(false);
  expect(screen.getByLabelText("Пример профиля")).toHaveValue("");
  expect(screen.getByRole("button", { name: "Применить пример" })).toBeDisabled();
  expect(screen.getByLabelText("Запросы к агрегаторам (до 8)")).toHaveAccessibleDescription(
    "(если пусто, используются первые 8 названий ролей)",
  );
  await user.selectOptions(screen.getByLabelText("Пример профиля"), "designer");
  await user.click(screen.getByRole("button", { name: "Применить пример" }));
  expect(await screen.findByText(/Пример применён/)).toBeVisible();
  expect(screen.getByLabelText("Названия ролей и синонимы")).toHaveValue(
    "product designer, ux designer",
  );
  expect(api.calls.find((call) => call.path.includes("apply"))?.body).toEqual({
    expected_version: 0,
    expected_sources_version: 0,
  });
  unmount();
  render(<SearchSettings />);
  await ready();
  expect(screen.getByLabelText("Пример профиля")).toHaveValue("designer");
  const roles = screen.getByLabelText("Названия ролей и синонимы");
  await user.clear(roles);
  await user.type(roles, "brand designer");
  await user.click(screen.getByRole("button", { name: "Сохранить профиль поиска" }));
  await screen.findByText("Профиль поиска сохранён.");
  expect(screen.getByLabelText("Пример профиля")).toHaveValue("");

  const history = screen.getByText("История версий профиля");
  expect(history.closest("summary")).toHaveAccessibleDescription("В поиске используется версия 2");
  api.failures.push("/api/search-profile/history");
  await user.click(history);
  await user.click(await screen.findByRole("button", { name: "Повторить загрузку версий" }));
  expect(api.calls.filter((call) => call.path === "/api/search-profile")).toHaveLength(1);
  const current = await screen.findByText("Версия 2 · используется в поиске");
  const previous = screen.getByText("Версия 1");
  await user.click(previous);
  expect(within(previous.closest("details")!).getByText("product designer, ux designer")).toBeVisible();
  await user.click(current);
  expect(within(current.closest("details")!).getByText("brand designer")).toBeVisible();
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

test("already connected sources are flagged before sending a duplicate mutation", async () => {
  const api = preferencesServer({
    sources: {
      version: 1,
      items: [
        source(),
        source({ id: "agg", kind: "remotive", name: "Remotive", config: {}, enabled: false }),
        source({ id: "channel", kind: "telegram", name: "Telegram", config: { handle: "Jobs_Channel" } }),
      ],
    },
  });
  const user = userEvent.setup();
  render(<SearchSettings />);
  await ready();
  const type = screen.getByLabelText("Тип источника");
  const submit = screen.getByRole("button", { name: "Добавить источник" });
  expect(screen.getByRole("option", { name: "remotive · уже подключён" })).toBeDisabled();
  expect(submit).toBeDisabled();
  await user.selectOptions(type, "lever");
  const board = screen.getByLabelText("Идентификатор доски");
  await user.type(board, "ACME");
  expect(submit).toBeEnabled();
  await user.selectOptions(screen.getByLabelText("Регион API"), "eu");
  expect(submit).toBeDisabled();
  expect(screen.getByText(/Мои источники» как «My board»/)).toBeVisible();
  await user.selectOptions(type, "telegram");
  const channel = screen.getByLabelText("Handle публичного канала");
  await user.clear(channel);
  await user.type(channel, "@jobs_channel");
  expect(submit).toBeDisabled();
  expect(api.calls.some((call) => call.path === "/api/sources" && call.body)).toBe(false);
});
test("board, channel and aggregator connections are editable without discarding profile edits", async () => {
  const api = preferencesServer();
  const user = userEvent.setup();
  render(<SearchSettings />);
  await ready();
  await user.type(
    screen.getByLabelText("Названия ролей и синонимы"),
    "unsaved designer",
  );
  await user.selectOptions(screen.getByLabelText("Тип источника"), "lever");
  await user.type(screen.getByLabelText("Идентификатор доски"), "company");
  await user.type(screen.getByLabelText("Название подключения"), "Design jobs");
  await user.type(screen.getByLabelText("Теги источника"), "design, ux");
  await user.selectOptions(screen.getByLabelText("Регион API"), "eu");
  await user.click(screen.getByRole("button", { name: /^Добавить источник$/ }));
  await screen.findByRole("heading", { name: "Design jobs" });
  expect(screen.getByLabelText("Названия ролей и синонимы")).toHaveValue(
    "unsaved designer",
  );
  expect(api.state.sources.items[0].config).toEqual({
    token: "company",
    name: "Design jobs",
    region: "eu",
    tags: ["design", "ux"],
  });
  await user.click(screen.getByLabelText("Включить Design jobs"));
  await waitFor(() =>
    expect(screen.getByLabelText("Включить Design jobs")).not.toBeChecked(),
  );
  await user.click(
    screen.getByRole("button", { name: "Изменить Design jobs" }),
  );
  expect(screen.getByLabelText("Регион API")).toHaveValue("eu");
  await user.clear(screen.getByLabelText("Название подключения"));
  await user.type(screen.getByLabelText("Название подключения"), "Changed");
  await user.click(
    screen.getByRole("button", { name: "Сохранить подключение" }),
  );
  await screen.findByText("Источник сохранён.");
  expect(api.state.sources.items[0].config.name).toBe("Changed");
  await user.selectOptions(screen.getByLabelText("Тип источника"), "telegram");
  await user.type(
    screen.getByLabelText("Handle публичного канала"),
    "public_jobs",
  );
  await user.click(screen.getByRole("button", { name: /^Добавить источник$/ }));
  await screen.findByRole("heading", { name: "public_jobs" });
  await user.click(
    screen.getByRole("button", { name: "Изменить public_jobs" }),
  );
  expect(screen.getByLabelText("Handle публичного канала")).toHaveValue(
    "public_jobs",
  );
  await user.click(screen.getByRole("button", { name: "Отменить изменение" }));
  await user.selectOptions(screen.getByLabelText("Тип источника"), "remotive");
  await user.click(screen.getByRole("button", { name: /^Добавить источник$/ }));
  await screen.findByRole("heading", { name: "remotive" });
  expect(api.state.sources.items.at(-1)?.config).toEqual({});
});

test("deleting an edited connection resets the form and invalid imports require correction", async () => {
  const api = preferencesServer({
    sources: {
      version: 1,
      items: [
        source(),
        source({
          id: "invalid",
          name: "Invalid",
          valid: false,
          kind: "greenhouse",
          config: { token: "broken" },
        }),
      ],
    },
  });
  const user = userEvent.setup();
  render(<SearchSettings />);
  await ready();
  expect(screen.getByText("Исправьте параметры подключения.")).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Проверить Invalid" }),
  ).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Изменить Invalid" }));
  expect(screen.getByLabelText("Название подключения")).toHaveValue("");
  await user.click(screen.getByRole("button", { name: "Удалить Invalid" }));
  await waitFor(() =>
    expect(
      screen.queryByRole("heading", { name: "Invalid" }),
    ).not.toBeInTheDocument(),
  );
  expect(
    screen.getByRole("button", { name: /^Добавить источник$/ }),
  ).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Удалить My board" }));
  await screen.findByText("Источники пока не добавлены.");
  expect(api.state.sources.items).toHaveLength(0);
});

test.each([
  [{}, "Источник доступен. Объявлений"],
  [{ status: "empty", items: 0 }, "Источник доступен, проверенный ответ пуст."],
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
    screen.getByRole("button", { name: "Сохранить профиль поиска" }),
  );
  await screen.findByText("Настройки изменились в другой вкладке");
  expect(screen.getByLabelText("Названия ролей и синонимы")).toHaveValue(
    "draft role",
  );
  await user.click(
    screen.getByRole("button", { name: "Загрузить актуальные настройки" }),
  );
  await waitFor(() =>
    expect(screen.getByLabelText("Названия ролей и синонимы")).toHaveValue(""),
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
    await screen.findByRole("button", { name: "Поиск и источники" }),
  );
  expect(
    await screen.findByRole("heading", { name: "Поиск и источники" }),
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
