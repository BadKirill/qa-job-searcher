import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App, Workspace } from "../src/App";
import { Login } from "../src/Login";
import { server, TestEvents, makeTask } from "./fixtures";

it("loads ten cards, paginates, persists applied/archive and restores a vacancy", async () => {
  const backend = server();
  render(<App />);
  await screen.findByText(/11 вакансий/);
  await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(10));
  await userEvent.click(
    screen.getByRole("button", { name: "Следующая страница" }),
  );
  await screen.findByRole("heading", { name: "Senior QA Engineer job-11" });
  expect(screen.getAllByRole("article")).toHaveLength(1);
  await userEvent.click(
    screen.getByRole("button", { name: "Предыдущая страница" }),
  );
  const card = await screen.findByRole("article", {
    name: "Senior QA Engineer job-1",
  });
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "Предыдущая страница" }),
    ).toBeDisabled(),
  );
  await userEvent.click(
    within(card).getByRole("link", { name: /Перейти на страницу вакансии/ }),
  );
  await within(card).findByText("applied");
  expect(card).toHaveClass("job-applied");
  expect(backend.state.jobs[0].applied_at).toBeTruthy();
  await userEvent.click(
    within(card).getByRole("button", { name: "Отменить applied" }),
  );
  await waitFor(() =>
    expect(within(card).queryByText("applied")).not.toBeInTheDocument(),
  );
  await userEvent.click(
    within(card).getByRole("button", { name: "Не подходит" }),
  );
  await waitFor(() =>
    expect(
      screen.queryByRole("article", { name: "Senior QA Engineer job-1" }),
    ).not.toBeInTheDocument(),
  );
  await userEvent.click(
    screen.getByRole("button", { name: "Закрыть уведомление" }),
  );
  await userEvent.click(screen.getByRole("button", { name: "Архив" }));
  const archived = await screen.findByRole("article", {
    name: "Senior QA Engineer job-1",
  });
  await userEvent.click(
    within(archived).getByRole("button", { name: "Вернуть из архива" }),
  );
  await screen.findByText("В архиве пока пусто");
  await userEvent.click(screen.getByRole("button", { name: /Вакансии/ }));
  await screen.findByRole("article", { name: "Senior QA Engineer job-1" });
  expect(
    backend.calls.some(
      (call) => call.path === "/jobs" && call.method === "GET",
    ),
  ).toBe(true);
});

it("shows an API error without falsely applying a failed action and permits retry", async () => {
  const backend = server();
  backend.state.jobs = [backend.state.jobs[0]];
  render(<App />);
  const card = await screen.findByRole("article");
  backend.failures.push({
    path: "/jobs/job-1/state",
    status: 500,
    detail: "Сохранение недоступно",
  });
  await userEvent.click(
    within(card).getByRole("button", { name: "Не подходит" }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Сохранение недоступно",
  );
  expect(backend.state.jobs[0].archived_at).toBeNull();
  await userEvent.click(
    within(card).getByRole("button", { name: "Не подходит" }),
  );
  await screen.findByText("Новые возможности впереди");
});

it("removes the last item on page two and returns to the last existing page", async () => {
  server();
  render(<App />);
  await screen.findByText(/11 вакансий/);
  await userEvent.click(screen.getByRole("button", { name: "Страница 2" }));
  const card = await screen.findByRole("article", {
    name: "Senior QA Engineer job-11",
  });
  await userEvent.click(
    within(card).getByRole("button", { name: "Не подходит" }),
  );
  await screen.findByText(/10 вакансий/);
  expect(screen.getAllByRole("article")).toHaveLength(10);
  expect(
    screen.queryByRole("navigation", { name: "Пагинация вакансий" }),
  ).not.toBeInTheDocument();
});

it("handles empty search, starts a search and refreshes results while retaining search errors", async () => {
  const backend = server();
  backend.state.jobs = [];
  render(<App />);
  await screen.findByText("Новые возможности впереди");
  backend.failures.push({
    path: "/search-runs",
    status: 503,
    detail: "Источник недоступен",
  });
  await userEvent.click(screen.getByRole("button", { name: "Найти вакансии" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Источник недоступен",
  );
  await userEvent.click(screen.getByRole("button", { name: "Обновить поиск" }));
  await screen.findByText(/Поиск запущен/);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Обновляем…" })).toBeDisabled(),
  );
  await userEvent.click(
    screen.getByRole("button", { name: /Показать свежую выдачу/ }),
  );
  await waitFor(() =>
    expect(screen.queryByText(/Поиск запущен/)).not.toBeInTheDocument(),
  );
  expect(screen.getByText("Обновление поиска")).toBeInTheDocument();
});

it("generates CV in English and letters in vacancy language then shows checked downloads", async () => {
  const backend = server();
  backend.state.jobs = [backend.state.jobs[0]];
  render(<App />);
  const card = await screen.findByRole("article");
  await userEvent.click(
    within(card).getByRole("button", { name: "Сделать CV" }),
  );
  expect(
    backend.calls.find((call) => call.path.endsWith("/documents"))?.body,
  ).toMatchObject({ kind: "cv", language: "en" });
  await waitFor(() =>
    expect(
      within(card).getByRole("button", { name: "Сделать CV" }),
    ).toBeDisabled(),
  );
  await userEvent.click(
    within(card).getByRole("button", { name: "Сделать кавер леттер" }),
  );
  expect(
    backend.calls.filter((call) => call.path.endsWith("/documents"))[1].body,
  ).toMatchObject({ kind: "cover_letter", language: "auto" });
  backend.state.tasks[0].status = "ready";
  backend.state.tasks[0].artifacts = [
    { id: "pdf-1", format: "pdf", filename: "CV.pdf" },
    { id: "docx-1", format: "docx", filename: "CV.docx" },
  ];
  act(() => TestEvents.latest.change?.());
  await screen.findByRole("link", { name: "Скачать PDF" });
  expect(screen.getByRole("link", { name: "Скачать DOCX" })).toHaveAttribute(
    "download",
    "CV.docx",
  );
  await userEvent.click(
    screen.getByRole("button", { name: /Обсудить и доработать/ }),
  );
  await screen.findByRole("region", { name: "Диалог с агентом" });
  await userEvent.click(screen.getByRole("button", { name: "Закрыть диалог" }));
  expect(
    screen.queryByRole("region", { name: "Диалог с агентом" }),
  ).not.toBeInTheDocument();
});

it("routes generation without an active template into web profile setup", async () => {
  const backend = server();
  backend.state.profile.active = null;
  render(<App />);
  await screen.findByText(/11 вакансий/);
  await userEvent.click(
    screen.getAllByRole("button", { name: "Сделать CV" })[0],
  );
  expect(
    await screen.findByRole("heading", { name: "Профиль и шаблон" }),
  ).toBeInTheDocument();
  expect(
    backend.calls.filter((call) => call.path.endsWith("/documents")),
  ).toHaveLength(0);
});

it("reports event disconnection and resynchronizes on reconnect, messages and fallback timer", async () => {
  const backend = server();
  const { unmount } = render(<App />);
  await screen.findByText(/11 вакансий/);
  act(() => TestEvents.latest.onerror?.());
  expect(screen.getByRole("status")).toHaveTextContent(
    "Связь с обновлениями потеряна",
  );
  const before = backend.calls.length;
  act(() => TestEvents.latest.onopen?.());
  await waitFor(() => expect(backend.calls.length).toBeGreaterThan(before));
  expect(
    screen.queryByText(/Связь с обновлениями потеряна/),
  ).not.toBeInTheDocument();
  act(() => TestEvents.latest.onmessage?.());
  vi.useFakeTimers();
  unmount();
  expect(TestEvents.latest.close).toHaveBeenCalledOnce();
  vi.useRealTimers();
});

it("shows profile failures, retries and supports logout failure then success", async () => {
  const backend = server();
  backend.failures.push({
    path: "/profile",
    status: 500,
    detail: "Профиль недоступен",
  });
  render(<App />);
  await screen.findByText("Профиль недоступен");
  await userEvent.click(screen.getByRole("button", { name: "Повторить" }));
  await waitFor(() =>
    expect(screen.queryByText("Профиль недоступен")).not.toBeInTheDocument(),
  );
  backend.failures.push({
    path: "/logout",
    status: 500,
    detail: "Выход не выполнен",
  });
  await userEvent.click(screen.getByRole("button", { name: "Выйти" }));
  await screen.findByText("Выход не выполнен");
  await userEvent.click(screen.getByRole("button", { name: "Выйти" }));
  await screen.findByRole("heading", { name: "С возвращением" });
  await userEvent.type(screen.getByLabelText("Пароль"), "correct-password");
  await userEvent.click(screen.getByRole("button", { name: "Войти" }));
  await screen.findByText(/11 вакансий/);
});

it("bootstraps a private workspace, rejects a failed login and expires an active session", async () => {
  const backend = server();
  backend.state.session = { authenticated: false, setup_required: true };
  render(<App />);
  await screen.findByRole("heading", { name: "Добро пожаловать" });
  await userEvent.type(
    screen.getByLabelText(/Код первоначальной настройки/),
    "local-setup-token",
  );
  await userEvent.type(screen.getByLabelText(/Пароль/), "long-enough-password");
  backend.failures.push({
    path: "/setup",
    status: 403,
    detail: "Неверный код",
  });
  await userEvent.click(
    screen.getByRole("button", { name: "Создать рабочее пространство" }),
  );
  await screen.findByText("Неверный код");
  await userEvent.click(
    screen.getByRole("button", { name: "Создать рабочее пространство" }),
  );
  await screen.findByText(/11 вакансий/);
  act(() => window.dispatchEvent(new Event("session-expired")));
  await screen.findByRole("heading", { name: "С возвращением" });
});

it("recovers from initial session failure", async () => {
  const backend = server();
  backend.failures.push({
    path: "/session",
    status: 502,
    detail: "Сервер запускается",
  });
  render(<App />);
  await screen.findByText("Сервер запускается");
  await userEvent.click(screen.getByRole("button", { name: "Повторить" }));
  await screen.findByText(/11 вакансий/);
});

it("disables duplicate form submissions until authentication completes", async () => {
  let resolve!: (value: Response) => void;
  const fetcher = vi.fn(
    () =>
      new Promise<Response>((done) => {
        resolve = done;
      }),
  );
  vi.stubGlobal("fetch", fetcher);
  const loggedIn = vi.fn();
  render(
    <Login
      session={{ authenticated: false, setup_required: false }}
      onLogin={loggedIn}
    />,
  );
  fireEvent.change(screen.getByLabelText("Пароль"), {
    target: { value: "valid-password" },
  });
  fireEvent.submit(
    screen.getByRole("button", { name: "Войти" }).closest("form")!,
  );
  expect(screen.getByRole("button", { name: "Подождите…" })).toBeDisabled();
  fireEvent.submit(
    screen.getByRole("button", { name: "Подождите…" }).closest("form")!,
  );
  expect(fetcher).toHaveBeenCalledOnce();
  await act(async () =>
    resolve(new Response('{"authenticated":true,"setup_required":false}')),
  );
  expect(loggedIn).toHaveBeenCalledOnce();
});

it("shows search failure details and navigates directly to profile from sidebar", async () => {
  const backend = server();
  backend.state.tasks = [
    makeTask({ kind: "search", status: "failed", error: "Search timeout" }),
  ];
  render(<Workspace onLogout={vi.fn()} />);
  await screen.findByText("Search timeout");
  await userEvent.click(
    screen.getByRole("button", { name: "Изменить эталонный шаблон" }),
  );
  await screen.findByRole("heading", { name: "Профиль и шаблон" });
  await userEvent.click(
    screen.getByRole("button", { name: "Профиль и шаблон" }),
  );
  expect(screen.getByText("Candidate.docx")).toBeInTheDocument();
});

it("waits for the existing profile before allowing template changes", async () => {
  const backend = server();
  const original = backend.fetcher.getMockImplementation()!;
  let finish!: (value: Response) => void;
  backend.fetcher.mockImplementation((url, init) =>
    url === "/api/profile"
      ? new Promise<Response>((resolve) => {
          finish = resolve;
        })
      : original(url, init),
  );
  render(<App />);
  await screen.findByText(/11 вакансий/);
  await userEvent.click(
    screen.getByRole("button", { name: "Профиль и шаблон" }),
  );
  expect(screen.getByRole("status")).toHaveTextContent("Загружаем профиль");
  expect(screen.queryByLabelText("Файл эталонного CV")).not.toBeInTheDocument();
  await act(async () =>
    finish(new Response(JSON.stringify(backend.state.profile))),
  );
  expect(await screen.findByText("Активный эталон")).toBeInTheDocument();
});
