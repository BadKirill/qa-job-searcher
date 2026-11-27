import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Diagnostics } from "../src/Diagnostics";

const snapshot = {
  queue: [{ kind: "cv", status: "blocked", count: 2 }],
  expired_leases: 1,
  oldest_wait_seconds: 42,
  usage_states: { unknown: 3 },
  unknown_usage: 3,
  cooldown_seconds: 60,
  source_errors: 4,
  services: {
    scheduler: 1,
    search: 2,
    chat: 0,
    document: 1,
    all: 0,
    telegram: 1,
  },
};

it("shows operational counts and refreshes without retaining stale values", async () => {
  const fetcher = vi
    .fn()
    .mockResolvedValueOnce(new Response(JSON.stringify(snapshot)))
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({ ...snapshot, queue: [], usage_states: {} }),
      ),
    );
  vi.stubGlobal("fetch", fetcher);
  render(<Diagnostics />);
  await screen.findByText("cv / blocked: 2");
  expect(screen.getByText(/Просроченные задания/)).toHaveTextContent(
    "1. Самое долгое ожидание: 42",
  );
  expect(screen.getByText(/Вызовы без точного учёта/)).toHaveTextContent(
    "3. Пауза OpenAI: 60",
  );
  expect(screen.getByText("Поиск: 2")).toBeInTheDocument();
  expect(screen.getByText("Telegram: 1")).toBeInTheDocument();
  expect(screen.getByText("unknown: 3")).toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("button", { name: "Обновить диагностику" }),
  );
  await screen.findByText("Заданий пока нет.");
  expect(screen.getByText("Вызовов пока нет.")).toBeInTheDocument();
  expect(screen.queryByText("cv / blocked: 2")).not.toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledTimes(2);
});

it("reports denied reads and permits retry", async () => {
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
      .mockResolvedValueOnce(new Response(JSON.stringify(snapshot))),
  );
  render(<Diagnostics />);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Нужны права администратора.",
  );
  await userEvent.click(
    screen.getByRole("button", { name: "Обновить диагностику" }),
  );
  await screen.findByText("cv / blocked: 2");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it.each([true, false])(
  "ignores a late result or error after leaving the page (%s)",
  async (success) => {
    let finish!: (value: Response) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            finish = resolve;
          }),
      ),
    );
    const view = render(<Diagnostics />);
    view.unmount();
    await act(async () =>
      finish(
        success
          ? new Response(JSON.stringify(snapshot))
          : new Response("{}", { status: 503 }),
      ),
    );
    expect(screen.queryByText("cv / blocked: 2")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  },
);
