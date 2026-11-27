import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RuntimeSettings } from "../src/RuntimeSettings";

function backend() {
  const usage = {
    day: "2026-11-12",
    daily_calls: 60,
    used_calls: 4,
    unknown_calls: 1,
    tokens: 1234,
    cooldown_until: Date.now() / 1000 + 100,
  };
  let schedule = {
    enabled: true,
    interval_seconds: 10800,
    next_run_at: Date.now() / 1000 + 3600,
  };
  let configuration = {
    limits: {
      search_slots: 2,
      chat_slots: 2,
      document_slots: 2,
      per_user_documents: 1,
      api_slots: 2,
      daily_calls: 600,
      user_daily_calls: 60,
    },
    users: [{ ...usage, id: "member/1", login: "member" }],
  };
  const calls: { path: string; body: any }[] = [];
  const failures: string[] = [];
  const fetcher = vi.fn(async (path: string, options: RequestInit = {}) => {
    const body = options.body ? JSON.parse(String(options.body)) : null;
    calls.push({ path, body });
    if (path === "/api/admin/diagnostics")
      return new Response(
        JSON.stringify({
          queue: [],
          usage_states: {},
          services: {},
          expired_leases: 0,
          oldest_wait_seconds: 0,
          unknown_usage: 0,
          cooldown_seconds: 0,
          source_errors: 0,
        }),
      );
    if (failures[0] === path) {
      failures.shift();
      return new Response(
        JSON.stringify({ detail: "Не удалось сохранить лимиты" }),
        { status: 409 },
      );
    }
    if (options.method === "PUT") {
      if (path === "/api/schedule") schedule = { ...schedule, ...body };
      if (path === "/api/admin/runtime")
        configuration = { ...configuration, limits: body };
      if (path.endsWith("/limits"))
        configuration = {
          ...configuration,
          users: [{ ...configuration.users[0], daily_calls: body.daily_calls }],
        };
      return new Response("{}");
    }
    const value =
      path === "/api/usage"
        ? usage
        : path === "/api/schedule"
          ? schedule
          : configuration;
    return new Response(JSON.stringify(value));
  });
  vi.stubGlobal("fetch", fetcher);
  return { calls, failures, fetcher, usage };
}

it("shows personal usage without treating unknown usage as zero and persists a schedule", async () => {
  const server = backend();
  render(<RuntimeSettings admin={false} />);
  await screen.findByText(/Вызовы за 2026-11-12/);
  expect(screen.getByText(/Токены с известным расходом/)).toHaveTextContent(
    "1234. Вызовы без точного учёта: 1",
  );
  expect(screen.getByText(/OpenAI запросил паузу/)).toBeInTheDocument();
  expect(server.calls.some((call) => call.path.includes("admin"))).toBe(false);
  await userEvent.click(screen.getByLabelText("Автоматический поиск"));
  expect(screen.getByText(/Автоматический поиск выключен/)).toBeVisible();
  await userEvent.clear(screen.getByLabelText("Интервал поиска в минутах"));
  await userEvent.type(
    screen.getByLabelText("Интервал поиска в минутах"),
    "60",
  );
  await userEvent.click(
    screen.getByRole("button", { name: "Сохранить расписание" }),
  );
  await screen.findByText("Настройки исполнения сохранены.");
  expect(server.calls.find((call) => call.body)?.body).toEqual({
    enabled: false,
    interval_seconds: 3600,
  });
  await waitFor(() =>
    expect(screen.getByLabelText("Автоматический поиск")).not.toBeChecked(),
  );
});

it("lets the administrator change global concurrency and an individual zero-call quota", async () => {
  const server = backend();
  render(<RuntimeSettings admin />);
  const slots = await screen.findByLabelText("Одновременные вызовы OpenAI");
  await userEvent.clear(slots);
  await userEvent.type(slots, "1");
  await userEvent.click(
    screen.getByRole("button", { name: "Сохранить общие лимиты" }),
  );
  await screen.findByText("Настройки исполнения сохранены.");
  expect(
    server.calls.find((call) => call.path === "/api/admin/runtime" && call.body)
      ?.body.api_slots,
  ).toBe(1);
  const personal = screen.getByLabelText("Суточный лимит member");
  await userEvent.clear(personal);
  await userEvent.type(personal, "0");
  await userEvent.click(
    screen.getByRole("button", { name: "Сохранить лимит member" }),
  );
  await waitFor(() =>
    expect(
      server.calls.find((call) => call.path.endsWith("/member%2F1/limits"))
        ?.body,
    ).toEqual({ daily_calls: 0 }),
  );
  await waitFor(() =>
    expect(screen.getByLabelText("Суточный лимит member")).toHaveValue(0),
  );
  await userEvent.click(
    screen.getByRole("button", { name: "Открыть диагностику" }),
  );
  await screen.findByText("Заданий пока нет.");
  await userEvent.click(
    screen.getByRole("button", { name: "Закрыть диагностику" }),
  );
  expect(
    screen.queryByRole("heading", { name: "Диагностика исполнения" }),
  ).not.toBeInTheDocument();
});

it("preserves unsaved settings after save failure and permits retry", async () => {
  const server = backend();
  render(<RuntimeSettings admin={false} />);
  const interval = await screen.findByLabelText("Интервал поиска в минутах");
  await userEvent.clear(interval);
  await userEvent.type(interval, "30");
  server.failures.push("/api/schedule");
  await userEvent.click(
    screen.getByRole("button", { name: "Сохранить расписание" }),
  );
  await screen.findByText("Не удалось сохранить лимиты");
  expect(interval).toHaveValue(30);
  await userEvent.click(
    screen.getByRole("button", { name: "Сохранить расписание" }),
  );
  await screen.findByText("Настройки исполнения сохранены.");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("reports a failed initial read without offering incomplete settings", async () => {
  const server = backend();
  server.failures.push("/api/usage");
  render(<RuntimeSettings admin />);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Не удалось сохранить лимиты",
  );
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

it("ignores late reads after unmount and prevents duplicate pending saves", async () => {
  const server = backend();
  const mounted = render(<RuntimeSettings admin={false} />);
  await screen.findByText(/Вызовы за/);
  let finish!: (value: Response) => void;
  server.fetcher.mockImplementationOnce(
    () =>
      new Promise<Response>((resolve) => {
        finish = resolve;
      }),
  );
  const form = screen
    .getByRole("button", { name: "Сохранить расписание" })
    .closest("form")!;
  fireEvent.submit(form);
  fireEvent.submit(form);
  expect(
    screen.getByRole("button", { name: "Сохранить расписание" }),
  ).toBeDisabled();
  mounted.unmount();
  await act(async () => finish(new Response("{}")));
  expect(
    screen.queryByText("Настройки исполнения сохранены."),
  ).not.toBeInTheDocument();
  server.usage.cooldown_until = 0;
  let read!: (value: Response) => void;
  server.fetcher.mockImplementationOnce(
    () =>
      new Promise<Response>((resolve) => {
        read = resolve;
      }),
  );
  const next = render(<RuntimeSettings admin={false} />);
  next.unmount();
  await act(async () => read(new Response(JSON.stringify(server.usage))));
  expect(screen.queryByText(/Вызовы за/)).not.toBeInTheDocument();
});
