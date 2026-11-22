import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../src/App";
import { Account } from "../src/Account";
import { PilotStatus } from "../src/PilotStatus";
import { request } from "../src/api";
import { server, TestEvents } from "./fixtures";

it("shows maintenance before login and resumes the existing session after an operator reopens service", async () => {
  const backend = server();
  backend.state.session.maintenance = true;
  render(<App />);
  await screen.findByRole("heading", { name: "Плановое обслуживание" });
  expect(screen.queryByRole("article")).not.toBeInTheDocument();
  expect(backend.calls.some((call) => call.path === "/profile")).toBe(false);
  backend.state.session.maintenance = false;
  await userEvent.click(
    screen.getByRole("button", { name: "Проверить доступность" }),
  );
  await screen.findByRole("article", { name: "Senior QA Engineer job-1" });
});

it("drops the private workspace and closes its stream when maintenance starts through SSE", async () => {
  server();
  render(<App />);
  await screen.findByRole("article", { name: "Senior QA Engineer job-1" });
  const events = TestEvents.latest;
  act(() => events.listeners.maintenance());
  await screen.findByRole("heading", { name: "Плановое обслуживание" });
  expect(screen.queryByRole("article")).not.toBeInTheDocument();
  expect(events.close).toHaveBeenCalledOnce();
  act(() => events.listeners.maintenance());
  expect(
    screen.getByRole("heading", { name: "Плановое обслуживание" }),
  ).toBeInTheDocument();
});

it("reacts to a recognized maintenance response and ignores unrecognized or aborted errors", async () => {
  const backend = server();
  render(<App />);
  await screen.findByRole("article", { name: "Senior QA Engineer job-1" });
  const original = globalThis.fetch;
  let payload: unknown = { detail: "Unavailable", maintenance: false };
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(payload), { status: 503 })),
  );
  await expect(request("/jobs")).rejects.toThrow("Unavailable");
  expect(
    screen.queryByRole("heading", { name: "Плановое обслуживание" }),
  ).not.toBeInTheDocument();
  payload = { detail: "Unavailable", maintenance: true };
  const controller = new AbortController();
  controller.abort();
  await expect(
    request("/jobs", { signal: controller.signal }),
  ).rejects.toThrow();
  expect(
    screen.queryByRole("heading", { name: "Плановое обслуживание" }),
  ).not.toBeInTheDocument();
  await act(async () => {
    await expect(request("/jobs")).rejects.toThrow();
  });
  await screen.findByRole("heading", { name: "Плановое обслуживание" });
  vi.stubGlobal("fetch", original);
  expect(backend.state.session.authenticated).toBe(true);
});

it("does not let an older bootstrap response replace the maintenance screen", async () => {
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
  act(() => window.dispatchEvent(new Event("maintenance-started")));
  await act(async () =>
    finish(
      new Response(
        JSON.stringify({
          authenticated: true,
          setup_required: false,
          csrf_token: "old",
        }),
      ),
    ),
  );
  expect(
    screen.getByRole("heading", { name: "Плановое обслуживание" }),
  ).toBeInTheDocument();
});

it.each(["not_started", "pair", "cohort"])(
  "shows the actual pilot phase %s without offering a verification shortcut",
  async (phase) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            phase,
            limit: phase === "not_started" ? null : phase === "pair" ? 2 : 20,
            reserved: 2,
            pair_verified: phase === "cohort",
          }),
        ),
      ),
    );
    render(<PilotStatus />);
    if (phase === "not_started")
      await screen.findByText("Закрытый пилот ещё не подготовлен.");
    else {
      await screen.findByText(
        `Зарезервировано мест: 2 из ${phase === "pair" ? 2 : 20}.`,
      );
      expect(
        screen.getByText(
          phase === "pair"
            ? /Проверяются первые/
            : /Проверка двух аккаунтов завершена/,
        ),
      ).toBeInTheDocument();
    }
    expect(screen.getAllByRole("button")).toHaveLength(1);
  },
);

it("reports a denied pilot read and refreshes after retry", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ detail: "Нужны права администратора." }),
          { status: 403 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            phase: "pair",
            limit: 2,
            reserved: 1,
            pair_verified: false,
          }),
        ),
      ),
  );
  render(<PilotStatus />);
  await screen.findByRole("alert");
  await userEvent.click(
    screen.getByRole("button", { name: "Обновить состояние пилота" }),
  );
  await screen.findByText("Зарезервировано мест: 1 из 2.");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it.each([
  null,
  {},
  { phase: "unexpected", limit: 20, reserved: 2, pair_verified: true },
  { phase: "pair", limit: 2, reserved: -1, pair_verified: false },
  { phase: "pair", limit: 20, reserved: 2, pair_verified: false },
  { phase: "cohort", limit: 20, reserved: 2, pair_verified: false },
  { phase: "cohort", limit: 2, reserved: 2, pair_verified: true },
  { phase: "not_started", limit: 2, reserved: 2, pair_verified: false },
  { phase: "pair", limit: 2, reserved: 2, pair_verified: "true" },
])(
  "does not display malformed pilot status as a completed verification (%s)",
  async (value) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify(value))),
    );
    render(<PilotStatus />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Не удалось получить состояние пилота.",
    );
    expect(
      screen.queryByText(/Проверка двух аккаунтов завершена/),
    ).not.toBeInTheDocument();
  },
);

it.each([true, false])(
  "ignores late pilot reads after unmount (%s)",
  async (success) => {
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
    const view = render(<PilotStatus />);
    view.unmount();
    await act(async () =>
      finish(
        new Response(success ? JSON.stringify({ phase: "pair" }) : "{}", {
          status: success ? 200 : 503,
        }),
      ),
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  },
);

it("only an administrator can open the pilot summary", async () => {
  server();
  const user = {
    id: "member",
    login: "member",
    display_name: "Member",
    role: "user" as const,
  };
  const view = render(<Account user={user} onLogout={() => {}} />);
  expect(
    screen.queryByRole("button", { name: "Состояние закрытого пилота" }),
  ).not.toBeInTheDocument();
  view.rerender(
    <Account user={{ ...user, role: "admin" }} onLogout={() => {}} />,
  );
  await userEvent.click(
    screen.getByRole("button", { name: "Состояние закрытого пилота" }),
  );
  expect(
    screen.getByRole("region", { name: "Закрытый пилот" }),
  ).toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("button", { name: "Состояние закрытого пилота" }),
  );
  await waitFor(() =>
    expect(
      screen.queryByRole("region", { name: "Закрытый пилот" }),
    ).not.toBeInTheDocument(),
  );
});
