import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { JobCard, Jobs } from "../src/Jobs";
import { makeJob, makeTask, server } from "./fixtures";

it("shows sparse vacancies honestly and renders external descriptions as text", () => {
  const job = makeJob("sparse", {
    company: "",
    location: "",
    salary: null,
    description: "",
    url: "javascript:alert(1)",
    details: {
      qa_types: [],
      languages: [],
      programming_languages: [],
      responsibilities: [],
      requirements: [],
    },
  });
  const props = {
    tasks: [],
    hasTemplate: true,
    onProfile: vi.fn(),
    onState: vi.fn(),
    onTask: vi.fn(),
    revision: 0,
  };
  const { rerender } = render(<JobCard job={job} {...props} />);
  expect(screen.getByText("Компания не указана")).toBeInTheDocument();
  expect(screen.getByText("Рейтинг по фильтрам")).toBeInTheDocument();
  expect(screen.getByText("роль: qa engineer")).toBeInTheDocument();
  expect(screen.getByText("Ссылка недоступна")).toBeInTheDocument();
  expect(
    screen.getByText(/Полное описание пока недоступно/),
  ).toBeInTheDocument();
  expect(screen.queryByRole("link")).not.toBeInTheDocument();
  expect(screen.queryByText("Что понадобится")).not.toBeInTheDocument();
  rerender(
    <JobCard
      job={{ ...job, description: "<img src=x onerror=steal()>" }}
      {...props}
    />,
  );
  expect(screen.getByText("<img src=x onerror=steal()>")).toBeInTheDocument();
  expect(document.querySelector("img")).toBeNull();
});

it("shows stale search snapshots and handles missing freshness metadata", async () => {
  const backend = server();
  const props = {
    tab: "active" as const,
    tasks: [],
    hasTemplate: true,
    onProfile: vi.fn(),
    onTask: vi.fn(),
  };
  const { rerender } = render(<Jobs {...props} revision={0} />);
  expect(await screen.findByText("Последний поиск:", { exact: false })).toBeInTheDocument();
  backend.state.lastSearchAt = "2026-10-03T11:00:00Z";
  rerender(<Jobs {...props} revision={1} />);
  expect(await screen.findByText(/Просматривается сохранённый снимок/)).toBeInTheDocument();
  backend.state.lastSearchAt = null;
  rerender(<Jobs {...props} revision={2} />);
  expect(await screen.findByText("Поиск ещё не завершался")).toBeInTheDocument();
});

it("surfaces document failure and deduplicates task snapshots", async () => {
  const backend = server();
  const task = makeTask({ status: "ready" });
  const onTask = vi.fn();
  backend.failures.push({
    path: "/jobs/job-1/documents",
    status: 422,
    detail: "Описание неполное",
  });
  render(
    <JobCard
      job={makeJob("job-1", { documents: [task] })}
      tasks={[task]}
      hasTemplate
      onProfile={vi.fn()}
      onState={vi.fn()}
      onTask={onTask}
      revision={0}
    />,
  );
  expect(screen.getAllByText("Готово")).toHaveLength(1);
  await userEvent.click(screen.getByRole("button", { name: "Сделать CV" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Описание неполное",
  );
  expect(onTask).not.toHaveBeenCalled();
});

it("asks whether the user applied and changes state only after confirmation", async () => {
  const backend = server();
  const onState = vi.fn();
  render(
    <JobCard
      job={backend.state.jobs[0]}
      tasks={[]}
      hasTemplate
      onProfile={vi.fn()}
      onState={onState}
      onTask={vi.fn()}
      revision={0}
    />,
  );
  const link = screen.getByRole("link", {
    name: "Перейти на страницу вакансии",
  });

  await userEvent.click(link);
  const dialog = screen.getByRole("dialog", { name: "Ты откликнулся?" });
  expect(backend.state.jobs[0].applied_at).toBeNull();
  expect(within(dialog).getByRole("button", { name: "Нет" })).toHaveFocus();
  await userEvent.click(within(dialog).getByRole("button", { name: "Нет" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(onState).not.toHaveBeenCalled();

  await userEvent.click(link);
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  await userEvent.click(link);
  await userEvent.click(screen.getByRole("button", { name: "Да" }));
  await waitFor(() => expect(onState).toHaveBeenCalledOnce());
  expect(backend.state.jobs[0].applied_at).toBeTruthy();
});

it("keeps the application confirmation open when saving fails", async () => {
  const backend = server();
  backend.failures.push({
    path: "/jobs/job-1/state",
    status: 503,
    detail: "Статус не сохранён",
  });
  render(
    <JobCard
      job={backend.state.jobs[0]}
      tasks={[]}
      hasTemplate
      onProfile={vi.fn()}
      onState={vi.fn()}
      onTask={vi.fn()}
      revision={0}
    />,
  );

  await userEvent.click(
    screen.getByRole("link", { name: "Перейти на страницу вакансии" }),
  );
  await userEvent.click(screen.getByRole("button", { name: "Да" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Статус не сохранён");
  expect(screen.getByRole("dialog", { name: "Ты откликнулся?" })).toBeVisible();
  expect(backend.state.jobs[0].applied_at).toBeNull();
});

it("cancels a pending document, preserving errors when cancellation fails", async () => {
  const backend = server();
  const onTask = vi.fn();
  const task = makeTask();
  render(
    <JobCard
      job={makeJob("job-1", { documents: [task] })}
      tasks={[]}
      hasTemplate
      onProfile={vi.fn()}
      onState={vi.fn()}
      onTask={onTask}
      revision={0}
    />,
  );
  backend.failures.push({
    path: "/tasks/task-1/cancel",
    status: 503,
    detail: "Попробуйте отменить позже",
  });
  await userEvent.click(
    screen.getByRole("button", { name: "Отменить создание" }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Попробуйте отменить позже",
  );
  backend.fetcher.mockImplementationOnce(async () => new Response("{}"));
  await userEvent.click(
    screen.getByRole("button", { name: "Отменить создание" }),
  );
  await waitFor(() => expect(onTask).toHaveBeenCalledOnce());
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("deletes a finished document after confirmation and surfaces deletion failures", async () => {
  const backend = server();
  const onTask = vi.fn();
  const task = makeTask({ status: "failed" });
  render(
    <JobCard
      job={makeJob("job-1", { documents: [task] })}
      tasks={[]}
      hasTemplate
      onProfile={vi.fn()}
      onState={vi.fn()}
      onTask={onTask}
      revision={0}
    />,
  );
  backend.failures.push({
    path: "/tasks/task-1",
    status: 503,
    detail: "Не удалось удалить файлы",
  });

  await userEvent.click(screen.getByRole("button", { name: "Удалить" }));
  await userEvent.click(screen.getByRole("button", { name: "Удалить документ" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Не удалось удалить файлы");
  expect(onTask).not.toHaveBeenCalled();

  await userEvent.click(screen.getByRole("button", { name: "Удалить документ" }));
  await waitFor(() => expect(onTask).toHaveBeenCalledOnce());
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(backend.calls.at(-1)).toMatchObject({
    path: "/tasks/task-1",
    method: "DELETE",
  });
});

it("handles a jobs endpoint outage and reloads after explicit retry", async () => {
  const backend = server();
  backend.failures.push({
    path: "/jobs",
    status: 503,
    detail: "Поиск временно недоступен",
  });
  render(
    <Jobs
      tab="active"
      tasks={[]}
      hasTemplate
      revision={0}
      onProfile={vi.fn()}
      onTask={vi.fn()}
    />,
  );
  await screen.findByText("Поиск временно недоступен");
  await userEvent.click(screen.getByRole("button", { name: "Повторить" }));
  await screen.findByText(/11 вакансий/);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("compresses long pagination without hiding the first or last pages", async () => {
  const backend = server();
  backend.state.jobs = Array.from({ length: 101 }, (_, index) =>
    makeJob(`j-${index}`),
  );
  render(
    <Jobs
      tab="active"
      tasks={[]}
      hasTemplate
      revision={0}
      onProfile={vi.fn()}
      onTask={vi.fn()}
    />,
  );
  const last = await screen.findByRole("button", { name: "Страница 11" });
  expect(screen.getByText("…")).toBeInTheDocument();
  await userEvent.click(last);
  await screen.findByText("Страница 11 из 11");
  expect(
    screen.getByRole("button", { name: "Следующая страница" }),
  ).toBeDisabled();
  expect(screen.getByRole("button", { name: "Страница 1" })).toBeEnabled();
});
