import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { JobCard, Jobs } from "../src/Jobs";
import {
  SearchHistory,
  SearchRunSummary,
  sourceErrorMessage,
} from "../src/SearchHistory";
import type { SearchRun } from "../src/types";
import { makeJob, server } from "./fixtures";

const run: SearchRun = {
  id: "run-first-12345",
  state: "published",
  published: true,
  task_status: "ready",
  profile_version: "2",
  created_at: "2026-11-01T12:00:00Z",
  completed_at: "2026-11-01T12:05:00Z",
  count: 11,
  errors: [],
  sources: [
    {
      id: "source",
      name: "My board",
      fetched_at: "2026-11-01T12:04:00Z",
      error: null,
      cached: false,
      limited: true,
    },
  ],
};
const props = {
  tasks: [],
  hasTemplate: false,
  onProfile: vi.fn(),
  onState: vi.fn(),
  onTask: vi.fn(),
  revision: 0,
};

function historyServer(items: SearchRun[] = [run]) {
  const base = server();
  const calls: string[] = [];
  const data = { items, has_more: false };
  const failures: string[] = [];
  vi.stubGlobal("fetch", async (url: string, options: RequestInit = {}) => {
    calls.push(url);
    if (url.includes("/search-runs?")) {
      if (failures.shift())
        return new Response('{"detail":"History unavailable"}', {
          status: 503,
        });
      return new Response(JSON.stringify(data));
    }
    return base.fetcher(url, options);
  });
  return { base, calls, data, failures };
}
async function openHistory() {
  await userEvent.click(screen.getByText("История поиска", { exact: true }));
}

test("history loads only when opened, shows personal versions and opens the selected run", async () => {
  const api = historyServer();
  const select = vi.fn();
  render(<SearchHistory revision={0} onSelect={select} />);
  expect(api.calls).toHaveLength(0);
  await openHistory();
  expect(await screen.findByText(/профиль v2/)).toBeVisible();
  expect(screen.getByText(/вакансий: 11/)).toBeVisible();
  await userEvent.click(
    screen.getByRole("button", { name: /Открыть выдачу запуска/ }),
  );
  expect(select).toHaveBeenCalledWith(run.id);
  expect(
    screen.getByRole("button", { name: "Предыдущие запуски" }),
  ).toBeDisabled();
  expect(
    screen.getByRole("button", { name: "Следующие запуски" }),
  ).toBeDisabled();
});

test("empty history, paged history and errors remain distinct", async () => {
  const api = historyServer([]);
  const { rerender } = render(
    <SearchHistory revision={0} onSelect={vi.fn()} />,
  );
  await openHistory();
  expect(await screen.findByText("Поисковых запусков пока нет.")).toBeVisible();
  api.data.items = [
    {
      ...run,
      state: "superseded",
      errors: ["Remotive: ReadTimeout"],
      task_error: "Synthetic task error",
    },
    {
      ...run,
      id: "pending",
      state: undefined,
      completed_at: null,
      profile_version: null,
      task_status: "queued",
    },
    {
      ...run,
      id: "unknown",
      state: "unknown",
      task_status: "unrecognized",
      count: undefined,
    },
  ];
  api.data.has_more = true;
  rerender(<SearchHistory revision={1} onSelect={vi.fn()} />);
  expect(
    await screen.findByText(/Настройки изменились: сохранён в истории/),
  ).toBeVisible();
  await userEvent.click(screen.getByText("Проблемы источников: 1"));
  expect(
    screen.getByText(/Remotive: источник не ответил вовремя/),
  ).toBeVisible();
  expect(screen.getByText("Synthetic task error")).toBeVisible();
  await userEvent.click(
    screen.getByRole("button", { name: "Следующие запуски" }),
  );
  expect(await screen.findByText("Страница истории 2")).toBeVisible();
  expect(api.calls.some((path) => path.endsWith("page=2"))).toBe(true);
  api.failures.push("fail");
  rerender(<SearchHistory revision={2} onSelect={vi.fn()} />);
  expect(await screen.findByText("History unavailable")).toBeVisible();
  await userEvent.click(
    screen.getByRole("button", { name: "Повторить загрузку истории" }),
  );
  await waitFor(() =>
    expect(screen.queryByText("History unavailable")).not.toBeInTheDocument(),
  );
  await userEvent.click(
    screen.getByRole("button", { name: "Предыдущие запуски" }),
  );
  expect(await screen.findByText("Страница истории 1")).toBeVisible();
});

test("source summary preserves actual fetch dates and explains partial and cached results", async () => {
  const { rerender } = render(<SearchRunSummary run={run} />);
  expect(screen.getByText(/ограниченное окно объявлений/)).not.toBeVisible();
  await userEvent.click(screen.getByText("Источники поиска: 1"));
  expect(screen.getByText(/ограниченное окно объявлений/)).toBeVisible();
  rerender(
    <SearchRunSummary
      run={{
        ...run,
        published: false,
        profile_version: null,
        errors: ["Board: ReadTimeout"],
        sources: [
          {
            ...run.sources![0],
            error: "ReadTimeout",
            cached: true,
            limited: false,
          },
        ],
      }}
    />,
  );
  expect(screen.getByText(/текущая выдача не заменена/)).toBeVisible();
  expect(screen.getByRole("alert")).toHaveTextContent(
    "My board: источник не ответил вовремя",
  );
  expect(screen.getByText(/ошибка получения.*из кэша/)).toBeVisible();
  rerender(
    <SearchRunSummary
      run={{ ...run, sources: undefined, errors: undefined }}
    />,
  );
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

test.each([
  ["fresh", "Источник · My board"],
  ["retained", "источник недоступен"],
  ["unconfirmed", "закрытие не установлено"],
])("card shows freshness separately from matching: %s", (freshness, label) => {
  render(
    <JobCard
      {...props}
      job={makeJob("fresh", {
        source: "My board",
        freshness,
        fetched_at: run.sources![0].fetched_at,
        location_status: "unknown",
      })}
    />,
  );
  expect(screen.getByText(new RegExp(label))).toBeVisible();
  expect(screen.getByText(/Географию нужно проверить/)).toBeVisible();
});

test("historical run selection changes only this listing and can return to current results", async () => {
  const api = historyServer();
  const savedFetch = fetch;
  const queries: string[] = [];
  api.base.state.jobs = [makeJob()];
  vi.stubGlobal("fetch", async (url: string, options: RequestInit = {}) => {
    if (url.includes("/jobs?")) {
      queries.push(url);
      const response = await api.base.fetcher(url, options);
      const body = await response.json();
      return new Response(
        JSON.stringify({ ...body, search_run: run, latest_run_id: "newer" }),
      );
    }
    return savedFetch(url, options);
  });
  render(<Jobs personal tab="active" {...props} />);
  const article = await screen.findByRole("article");
  const history = screen.getByText("История поиска", { exact: true });
  const summary = screen.getByText("Профиль поиска v2");
  expect(article.compareDocumentPosition(history) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(history.compareDocumentPosition(summary) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  await openHistory();
  await userEvent.click(
    await screen.findByRole("button", { name: /Открыть выдачу запуска/ }),
  );
  await waitFor(() =>
    expect(queries.some((url) => url.includes("run_id=run-first-12345"))).toBe(
      true,
    ),
  );
  expect(screen.getByText("Просматривается сохранённый запуск.")).toBeVisible();
  await userEvent.click(
    screen.getByRole("button", { name: "Показать текущую выдачу" }),
  );
  expect(screen.getByRole("article")).toBeVisible();
});

test("aborted history reads do not replace a newly selected account", async () => {
  let done!: (response: Response) => void;
  vi.stubGlobal(
    "fetch",
    () =>
      new Promise<Response>((resolve) => {
        done = resolve;
      }),
  );
  const view = render(<SearchHistory revision={0} onSelect={vi.fn()} />);
  await openHistory();
  view.unmount();
  await act(async () => {
    done(new Response('{"items":[],"has_more":false}'));
  });
  expect(
    screen.queryByText("Поисковых запусков пока нет."),
  ).not.toBeInTheDocument();
});

test("a late aborted listing response cannot replace the current snapshot", async () => {
  const api = server();
  api.state.jobs = [makeJob("new", { title: "New personal result" })];
  let finish!: (response: Response) => void;
  let first = true;
  vi.stubGlobal("fetch", (url: string, options: RequestInit = {}) => {
    if (url.includes("/jobs?") && first) {
      first = false;
      return new Promise<Response>((resolve) => {
        finish = resolve;
      });
    }
    return api.fetcher(url, options);
  });
  const view = render(<Jobs tab="archive" {...props} />);
  view.rerender(<Jobs tab="active" {...props} />);
  expect(
    await screen.findByRole("article", { name: "New personal result" }),
  ).toBeVisible();
  await act(async () => {
    finish(
      new Response(
        JSON.stringify({
          items: [makeJob("old", { title: "Stale result" })],
          total: 1,
          page: 1,
          pages: 1,
          snapshot_id: "old-snapshot",
        }),
      ),
    );
  });
  expect(
    screen.getByRole("article", { name: "New personal result" }),
  ).toBeVisible();
  expect(
    screen.queryByRole("article", { name: "Stale result" }),
  ).not.toBeInTheDocument();
});

test("source failures explain an action without showing internal exception classes", () => {
  expect(sourceErrorMessage("Board: HTTPStatusError")).toContain(
    "источник вернул ошибку",
  );
  expect(sourceErrorMessage("Board: UnexpectedError")).toContain(
    "не удалось получить вакансии",
  );
  expect(sourceErrorMessage("Board unavailable")).toContain(
    "Повторите поиск позже",
  );
});

test.each([
  ["request_limit", "лимит запросов к источнику"],
  ["run_request_limit", "лимит запросов этого поиска"],
  ["response_limit", "превышает допустимый размер"],
  ["run_response_limit", "объём данных"],
  ["blocked_resolution", "проверку безопасности"],
  ["HTTP_429", "частоту запросов"],
  ["HTTP_403", "отказал в доступе"],
  ["HTTP_404", "проверьте настройки"],
  ["HTTP_503", "на стороне источника"],
])("source failure %s gives a specific safe explanation", (code, message) => {
  expect(sourceErrorMessage(`Telegram · @jobs: ${code}`)).toContain(message);
});

test("many source errors are collapsed and partial counts remain inspectable", async () => {
  render(<SearchRunSummary run={{...run, sources: Array.from({length: 11}, (_, index) => ({
    ...run.sources![0], id: String(index), name: `Telegram · @jobs_${index}`,
    error: "HTTP_429", count: index === 0 ? 5 : 0,
  }))}} />);
  const reason = screen.getByText(/Telegram · @jobs_0: источник временно/);
  expect(reason).not.toBeVisible();
  await userEvent.click(screen.getByText("Проблемы источников: 11"));
  expect(reason).toBeVisible();
  await userEvent.click(screen.getByText("Источники поиска: 11"));
  expect(screen.getByText(/Telegram · @jobs_0 · получено частично · объявлений: 5/)).toBeVisible();
});
