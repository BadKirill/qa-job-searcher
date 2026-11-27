import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App, Workspace } from "../src/App";
import { JobCard } from "../src/Jobs";
import { Account } from "../src/Account";
import { Login } from "../src/Login";
import { SearchSettings, emptySearch } from "../src/SearchSettings";
import { WorkspaceStatus, type WorkspaceSummary } from "../src/WorkspaceStatus";
import { makeJob, server, TestEvents } from "./fixtures";

const member = {
  id: "member",
  login: "member",
  display_name: "Member Example",
  role: "user" as const,
};
function summary(overrides: Partial<WorkspaceSummary> = {}): WorkspaceSummary {
  return {
    setup: {
      search_configured: false,
      profile_version: 0,
      enabled_sources: 0,
      valid_sources: 0,
      sources_version: 0,
      template_ready: false,
      template_name: null,
    },
    queue: { queued: 2, active: 1, attention: 1 },
    usage: {
      daily_calls: 0,
      used_calls: 0,
      unknown_calls: 1,
      cooldown_until: Date.now() / 1000 + 100,
    },
    ...overrides,
  };
}
afterEach(() => window.history.replaceState(null, "", "/"));

it("guides a new account through personal criteria, sources and CV while showing real queue counts", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(summary()))),
  );
  const navigate = vi.fn();
  render(<WorkspaceStatus revision={0} onNavigate={navigate} />);
  await screen.findByRole("heading", { name: "Подготовьте личный кабинет" });
  expect(screen.getByLabelText("Личная очередь")).toHaveTextContent(
    "Заданий в очереди: 2Заданий выполняется: 1Требуют внимания: 1",
  );
  expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  expect(screen.getByText(/Личный лимит OpenAI исчерпан/)).toBeInTheDocument();
  expect(screen.getByText(/OpenAI запросил паузу/)).toBeInTheDocument();
  for (const [label, target] of [
    ["Настроить критерии", "criteria"],
    ["Подключить источники", "sources"],
    ["Открыть эталон CV", "profile"],
    ["OpenAI за сутки: 0 из 0", "usage"],
  ]) {
    await userEvent.click(screen.getByRole("button", { name: label }));
    expect(navigate).toHaveBeenLastCalledWith(target);
  }
});

it("shows invalid sources and confirmed CV until setup is complete, then removes the setup guide", async () => {
  let data = summary();
  data.setup = {
    ...data.setup,
    search_configured: true,
    profile_version: 4,
    enabled_sources: 2,
    valid_sources: 1,
    template_ready: true,
    template_name: "Own CV.docx",
  };
  data.usage = { ...data.usage, daily_calls: 60, cooldown_until: 0 };
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(data))),
  );
  const { rerender } = render(
    <WorkspaceStatus revision={0} onNavigate={vi.fn()} />,
  );
  await screen.findByText("Исправьте параметры включённых источников");
  expect(
    screen.getByText("Эталон подтверждён: Own CV.docx"),
  ).toBeInTheDocument();
  expect(screen.getByText("Критерии поиска сохранены")).toBeInTheDocument();
  data = { ...data, setup: { ...data.setup, valid_sources: 2 } };
  rerender(<WorkspaceStatus revision={1} onNavigate={vi.fn()} />);
  await waitFor(() =>
    expect(
      screen.queryByRole("heading", { name: "Подготовьте личный кабинет" }),
    ).not.toBeInTheDocument(),
  );
});

it("reports load failure and retries without fabricating readiness", async () => {
  const fetcher = vi
    .fn()
    .mockResolvedValueOnce(
      new Response('{"detail":"Cabinet unavailable"}', { status: 503 }),
    )
    .mockImplementation(async () => new Response(JSON.stringify(summary())));
  vi.stubGlobal("fetch", fetcher);
  render(<WorkspaceStatus revision={0} onNavigate={vi.fn()} />);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Cabinet unavailable",
  );
  expect(screen.queryByLabelText("Личная очередь")).not.toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("button", { name: "Повторить загрузку кабинета" }),
  );
  await screen.findByLabelText("Личная очередь");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("loads the new readiness revision after the pending response completes", async () => {
  let finish!: (response: Response) => void;
  const fetcher = vi
    .fn()
    .mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          finish = resolve;
        }),
    )
    .mockImplementation(async () => new Response(JSON.stringify(summary())));
  vi.stubGlobal("fetch", fetcher);
  const { rerender } = render(
    <WorkspaceStatus revision={0} onNavigate={vi.fn()} />,
  );
  rerender(<WorkspaceStatus revision={1} onNavigate={vi.fn()} />);
  expect(fetcher).toHaveBeenCalledTimes(1);
  await act(async () =>
    finish(
      new Response(
        JSON.stringify(
          summary({ queue: { queued: 999, active: 999, attention: 0 } }),
        ),
      ),
    ),
  );
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(screen.getByLabelText("Личная очередь")).not.toHaveTextContent("999");
});

it("keeps unsaved search criteria while switching separate pages and only shows QA options for QA context", async () => {
  const profile = { ...emptySearch, role_keywords: ["backend engineer"] };
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async (path: string) =>
        new Response(
          JSON.stringify(
            path === "/api/search-settings"
              ? {
                  search_profile: { configured: true, version: 2, profile },
                  sources: { version: 3, items: [] },
                }
              : path === "/api/source-types"
                ? {
                    items: [
                      {
                        kind: "remotive",
                        name: "Remotive",
                        limitations: "Limited feed",
                      },
                    ],
                  }
                : { items: { developer: profile, qa: profile } },
          ),
        ),
    ),
  );
  render(<SearchSettings section="criteria" />);
  await screen.findByRole("button", { name: /^Сохранить$/ });
  expect(
    screen.queryByLabelText("Учитывать дополнительные QA-названия"),
  ).not.toBeInTheDocument();
  await userEvent.type(
    screen.getByLabelText("Названия ролей и синонимы"),
    ", architect",
  );
  await userEvent.click(screen.getByRole("button", { name: "Источники" }));
  expect(
    screen.getByRole("heading", { name: "Источники" }),
  ).toBeInTheDocument();
  expect(screen.getByText("Мои источники")).toBeInTheDocument();
  expect(
    screen.queryByLabelText("Названия ролей и синонимы"),
  ).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Критерии" }));
  expect(screen.getByLabelText("Названия ролей и синонимы")).toHaveValue(
    "backend engineer, architect",
  );
  await userEvent.selectOptions(screen.getByLabelText("Пример профиля"), "qa");
  expect(
    screen.getByLabelText("Учитывать дополнительные QA-названия"),
  ).toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("button", { name: "Все настройки поиска" }),
  );
  expect(
    screen.getByRole("heading", { name: "Настройки поиска" }),
  ).toBeInTheDocument();
});

it("removes an invitation fragment before session loading finishes and allows returning to login", async () => {
  window.history.replaceState({ private: true }, "", "/#invite=private-token");
  let finish!: (response: Response) => void;
  vi.stubGlobal(
    "fetch",
    vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          finish = resolve;
        }),
    ),
  );
  render(<App />);
  expect(window.location.hash).toBe("");
  expect(window.history.state).toBeNull();
  expect(document.body).not.toHaveTextContent("private-token");
  await act(async () =>
    finish(
      new Response(
        JSON.stringify({
          authenticated: false,
          setup_required: false,
          account_mode: true,
        }),
      ),
    ),
  );
  await screen.findByRole("heading", { name: "Принять приглашение" });
  await userEvent.click(
    screen.getByRole("button", { name: "Вернуться ко входу" }),
  );
  expect(screen.getByLabelText("Логин", { exact: true })).toBeInTheDocument();
});

it("handles an incomplete access link without submitting its empty token", async () => {
  const fetcher = vi.fn();
  vi.stubGlobal("fetch", fetcher);
  render(
    <Login
      session={{
        authenticated: false,
        setup_required: false,
        account_mode: true,
      }}
      accessLink="#reset="
      onLogin={vi.fn()}
    />,
  );
  expect(screen.getByRole("alert")).toHaveTextContent(
    "Одноразовая ссылка неполная",
  );
  expect(
    screen.getByRole("button", { name: "Сохранить новый пароль" }),
  ).toBeDisabled();
  await userEvent.click(
    screen.getByRole("button", { name: "Вернуться ко входу" }),
  );
  expect(screen.getByLabelText("Логин", { exact: true })).toBeInTheDocument();
  expect(fetcher).not.toHaveBeenCalled();
});

it("explains successful password recovery at the subsequent login", async () => {
  window.history.replaceState(null, "", "/#reset=one-time-token");
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            authenticated: false,
            account_mode: true,
            setup_required: false,
          }),
        ),
    ),
  );
  render(<App />);
  await screen.findByLabelText("Пароль", { exact: true });
  await userEvent.type(
    screen.getByLabelText("Пароль", { exact: true }),
    "new-synthetic-password",
  );
  await userEvent.click(
    screen.getByRole("button", { name: "Сохранить новый пароль" }),
  );
  expect(await screen.findByRole("status")).toHaveTextContent(
    "Пароль обновлён. Войдите с новым паролем.",
  );
});

it("updates the displayed user name without changing the account and preserves a failed edit", async () => {
  const backend = server();
  backend.state.session = {
    authenticated: true,
    setup_required: false,
    account_mode: true,
    user: member,
  };
  const original = globalThis.fetch;
  let fail = true;
  vi.stubGlobal("fetch", async (path: string, options: RequestInit = {}) => {
    if (path === "/api/account") {
      if (fail)
        return new Response('{"detail":"Имя не сохранено"}', { status: 409 });
      const name = JSON.parse(String(options.body)).display_name;
      backend.state.session.user = { ...member, display_name: name };
      return new Response(JSON.stringify(backend.state.session.user));
    }
    return original(path, options);
  });
  render(<App />);
  await screen.findByRole("button", { name: "Аккаунт и доступ" });
  await userEvent.click(
    screen.getByRole("button", { name: "Аккаунт и доступ" }),
  );
  const input = screen.getByLabelText("Отображаемое имя");
  await userEvent.clear(input);
  await userEvent.type(input, "Designer Example");
  await userEvent.click(screen.getByRole("button", { name: "Сохранить имя" }));
  await screen.findByText("Имя не сохранено");
  expect(input).toHaveValue("Designer Example");
  fail = false;
  await userEvent.click(screen.getByRole("button", { name: "Сохранить имя" }));
  await waitFor(() =>
    expect(screen.getByLabelText("Текущий пользователь")).toHaveTextContent(
      "Designer Example",
    ),
  );
  expect(screen.getByLabelText("Личный аккаунт")).toHaveTextContent("D");
});

it("coalesces live updates while a profile response is pending and ignores closed SSE callbacks", async () => {
  const backend = server();
  const original = globalThis.fetch;
  let finish!: (response: Response) => void;
  let firstSignal: AbortSignal | null | undefined;
  let first = true;
  vi.stubGlobal("fetch", (path: string, options: RequestInit) => {
    if (path === "/api/profile" && first) {
      first = false;
      firstSignal = options.signal;
      return new Promise<Response>((resolve) => {
        finish = resolve;
      });
    }
    return original(path, options);
  });
  const mounted = render(<Workspace user={member} onLogout={vi.fn()} />);
  await screen.findByRole("button", { name: "Аккаунт и доступ" });
  const events = TestEvents.latest;
  const countBefore = backend.calls.length;
  const before = backend.calls.filter((call) => call.path === "/profile").length;
  await act(async () => events.onopen?.());
  expect(backend.calls).toHaveLength(countBefore);
  backend.state.profile.active!.filename = "Confirmed new CV.docx";
  await act(async () => events.change?.());
  await act(async () => events.change?.());
  expect(firstSignal?.aborted).toBe(false);
  expect(backend.calls.filter((call) => call.path === "/profile")).toHaveLength(before);
  await userEvent.click(
    screen.getByRole("button", { name: "Профиль и шаблон" }),
  );
  const old = structuredClone(backend.state.profile);
  old.active!.filename = "Stale private CV.docx";
  await act(async () => finish(new Response(JSON.stringify(old))));
  await screen.findByText("Confirmed new CV.docx");
  expect(screen.queryByText("Stale private CV.docx")).not.toBeInTheDocument();
  expect(backend.calls.filter((call) => call.path === "/profile")).toHaveLength(before + 1);
  mounted.unmount();
  const count = backend.calls.length;
  await act(async () => {
    events.onopen?.();
    events.onerror?.();
    events.onmessage?.();
    events.change?.();
    events.listeners["session-ended"]();
  });
  expect(backend.calls).toHaveLength(count);
});

it("retries failed administrator reads and shows the empty invitation state", async () => {
  let fail = true;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string) =>
      fail
        ? new Response('{"detail":"Admin unavailable"}', { status: 503 })
        : new Response(
            JSON.stringify(
              path.endsWith("/users") ? { items: [member] } : { items: [] },
            ),
          ),
    ),
  );
  render(<Account user={{ ...member, role: "admin" }} onLogout={vi.fn()} />);
  await screen.findByText("Список пользователей временно недоступен.");
  fail = false;
  await userEvent.click(
    screen.getByRole("button", {
      name: "Повторить загрузку администрирования",
    }),
  );
  await screen.findByText("Нет действующих приглашений.");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("does not label another profession as QA just because testing is mentioned in the vacancy", () => {
  const job = makeJob("designer", {
    title: "Product Designer",
    description: "Work with QA colleagues on manual testing.",
  });
  const props = {
    tasks: [],
    hasTemplate: true,
    revision: 0,
    onProfile: vi.fn(),
    onState: vi.fn(),
    onTask: vi.fn(),
  };
  const { rerender } = render(<JobCard job={job} {...props} />);
  expect(screen.queryByText("Manual", { exact: true })).not.toBeInTheDocument();
  expect(screen.getByText("Python", { exact: true })).toBeInTheDocument();
  rerender(<JobCard job={{ ...job, title: "QA Engineer" }} {...props} />);
  expect(screen.getByText("Manual", { exact: true })).toBeInTheDocument();
});
