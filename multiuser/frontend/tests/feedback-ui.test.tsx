import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { JobCard, Jobs } from "../src/Jobs";
import { ManualJob } from "../src/ManualJob";
import { PlacePicker } from "../src/PlacePicker";
import { makeJob, makeTask } from "./fixtures";

const text = "Apply Python and PostgreSQL experience to build reliable APIs.";
const original = { title: "Backend Engineer", company: "Example", location: "Worldwide", url: "https://example.test/job", description: text };
const checkedAt = "2026-11-26T08:00:00+00:00";
const posting = { status: "open" as const, checked_at: checkedAt, evidence: "Подтверждено ATS", url: original.url };
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });

test("manual job takes an ATS preview, preserves user edits and uses the same request after an uncertain save", async () => {
  const fetcher = vi.fn()
    .mockResolvedValueOnce(json({ posting, job: original }))
    .mockRejectedValueOnce(new Error("Connection lost"))
    .mockResolvedValueOnce(json({ id: "manual", existing: false }, 201));
  vi.stubGlobal("fetch", fetcher);
  const onSave = vi.fn();
  const user = userEvent.setup();
  render(<ManualJob onSave={onSave} onClose={vi.fn()} />);
  const dialog = screen.getByRole("dialog", { name: "Добавить вакансию" });
  const get = (label: string) => within(dialog).getByLabelText(label);
  expect(within(dialog).getByRole("button", { name: "Получить предпросмотр" })).toBeDisabled();
  await user.type(get("Ссылка на вакансию"), original.url);
  await user.type(get("Компания"), "Название от пользователя");
  await user.click(within(dialog).getByRole("button", { name: "Получить предпросмотр" }));
  await screen.findByText("Проверьте данные объявления перед сохранением.");
  expect(get("Название вакансии")).toHaveValue(original.title);
  expect(get("Локация")).toHaveValue(original.location);
  expect(get("Компания")).toHaveValue("Название от пользователя");
  expect(get("Описание вакансии")).toHaveValue(original.description);
  const save = within(dialog).getByRole("button", { name: "Сохранить вакансию" });
  await user.click(save);
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("Нет связи с сервером");
  expect(get("Название вакансии")).toHaveValue(original.title);
  await user.click(save);
  expect(onSave).toHaveBeenCalledWith("manual", false);
  const payloads = fetcher.mock.calls.slice(1).map((call) => JSON.parse(call[1].body));
  expect(payloads[0]).toEqual(payloads[1]);
});

test("manual editing keeps fields when preview fails and sends the expected revision", async () => {
  const fetcher = vi.fn()
    .mockResolvedValueOnce(json({ job: null, posting: { ...posting, evidence: "Оригинал недоступен" } }))
    .mockResolvedValueOnce(json({ detail: "Изменено в другой вкладке" }, 409))
    .mockResolvedValueOnce(json({}));
  vi.stubGlobal("fetch", fetcher);
  const onSave = vi.fn();
  const onClose = vi.fn();
  const user = userEvent.setup();
  render(<ManualJob job={makeJob("owned", { ...original, manual_version: 3 })} onSave={onSave} onClose={onClose} />);
  const dialog = screen.getByRole("dialog", { name: "Изменить вакансию" });
  await user.click(within(dialog).getByRole("button", { name: "Получить предпросмотр" }));
  await screen.findByText(/Оригинал недоступен Вставьте текст вакансии/);
  const location = within(dialog).getByLabelText("Локация");
  await user.clear(location);
  await user.type(location, "Тбилиси");
  const save = within(dialog).getByRole("button", { name: "Сохранить вакансию" });
  await user.click(save);
  await within(dialog).findByRole("alert");
  expect(location).toHaveValue("Тбилиси");
  await user.click(save);
  expect(onSave).toHaveBeenCalledWith("owned", false);
  expect(JSON.parse(fetcher.mock.calls[2][1].body)).toMatchObject({ expected_version: 3, location: "Тбилиси" });
  fireEvent(dialog, new Event("cancel", { cancelable: true, bubbles: true }));
  expect(onClose).toHaveBeenCalled();
});

test("manual preview recovers from a temporary error and a partial original preserves typed text", async () => {
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(json({ detail: "Временная ошибка" }, 503))
    .mockResolvedValueOnce(json({ posting, job: { ...original, description: "" } })));
  const user = userEvent.setup();
  const onClose = vi.fn();
  render(<ManualJob onSave={vi.fn()} onClose={onClose} />);
  await user.type(screen.getByLabelText("Ссылка на вакансию"), original.url);
  await user.type(screen.getByLabelText("Описание вакансии"), text);
  const preview = screen.getByRole("button", { name: "Получить предпросмотр" });
  await user.click(preview);
  await screen.findByText("Временная ошибка");
  await user.click(preview);
  await screen.findByText("Добавьте недостающее описание.");
  expect(screen.getByLabelText("Описание вакансии")).toHaveValue(text);
  await user.click(screen.getByRole("button", { name: "Отмена" }));
  expect(onClose).toHaveBeenCalledOnce();
});

const cardProps = () => ({
  personal: true,
  hasTemplate: true,
  onProfile: vi.fn(),
  onState: vi.fn(),
  onTask: vi.fn(),
  onContentChange: vi.fn(),
  revision: 0,
});

test("posting evidence, hours and languages retain their separate sources and queue state", async () => {
  const job = makeJob("posting", {
    description_quality: "fragment",
    language_evidence: [
      { kind: "team", languages: ["ru"], evidence: "working language Russian" },
      { kind: "required", languages: ["en", "ru"], evidence: "Russian and English", alternatives: [["en", "ru"], ["de"]] },
      { kind: "inferred", languages: ["en"], evidence: "" },
      { kind: "company_inferred", languages: ["ru"], evidence: "" },
    ],
    summary: [
      { field: "industry", value: "Education", evidence: "Industry: Education", url: original.url },
      { field: "custom", value: "Unsafe link is shown as text", evidence: "", url: "javascript:alert(1)" },
    ],
    work_time: {
      evidence: "Required working hours", zone: "America/New_York", start: "09:00", end: "17:00", mandatory: true,
      needs_review: false, local_start: "2026-11-26T15:00:00Z", local_end: "2026-11-26T23:00:00Z",
    },
  });
  const props = cardProps();
  const task = makeTask({ kind: "posting_check", status: "queued", job_id: job.id });
  const fetcher = vi.fn().mockResolvedValueOnce(json({ task }));
  vi.stubGlobal("fetch", fetcher);
  const { rerender } = render(<JobCard {...props} job={job} tasks={[]} />);
  expect(screen.getByText(/Обязательные часы · 09:00–17:00/)).toHaveTextContent("ваше время 15:00–23:00");
  expect(screen.getByText(/Требуются · английский и русский или de/)).toBeVisible();
  expect(screen.getByText("Язык команды · русский")).toBeVisible();
  expect(screen.getByText("Язык публикации, предположение · английский")).toBeVisible();
  expect(screen.getByText("Предположение по компании · русский")).toBeVisible();
  expect(within(screen.getByRole("list", { name: "Кратко о вакансии" })).getAllByRole("link")).toHaveLength(1);
  await userEvent.click(screen.getByRole("button", { name: "Найти полное описание" }));
  await screen.findByText("Проверка объявления в очереди");
  rerender(<JobCard {...props} job={job} tasks={[{ ...task, status: "running" }]} />);
  expect(screen.getByRole("status")).toHaveTextContent("Проверяем оригинал");
  rerender(<JobCard {...props} job={{ ...job, posting, description_quality: "full" }} tasks={[{ ...task, status: "ready" }]} />);
  expect(screen.getByRole("status")).toHaveTextContent("Вакансия открыта");
  expect(screen.getByText("Полное описание")).toBeVisible();
  rerender(<JobCard {...props} job={job} tasks={[{ ...task, status: "failed", error: "Фоновая проверка прервалась" }]} />);
  expect(screen.getByRole("status")).toHaveTextContent("Фоновая проверка прервалась");
});

test("a personal card offers its profile link, edits a saved description and reports a cached check", async () => {
  const props = cardProps();
  const fetcher = vi.fn()
    .mockResolvedValueOnce(json({ posting: { ...posting, status: "unconfirmed" } }))
    .mockResolvedValueOnce(json({}));
  vi.stubGlobal("fetch", fetcher);
  const user = userEvent.setup();
  const job = makeJob("manual", { ...original, manual_version: 1, description_quality: "provided" });
  render(<JobCard {...props} job={job} tasks={[]} hasTemplate={false} />);
  expect(screen.queryByText("Рейтинг по фильтрам")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Сделать CV" }));
  await user.click(screen.getByRole("button", { name: "Открыть профиль" }));
  expect(props.onProfile).toHaveBeenCalledOnce();
  await user.click(screen.getByRole("button", { name: "Проверить объявление" }));
  await screen.findByText("Не удалось подтвердить оригинал");
  await user.click(screen.getByRole("button", { name: "Изменить вакансию" }));
  const dialog = screen.getByRole("dialog", { name: "Изменить вакансию" });
  await user.click(within(dialog).getByRole("button", { name: "Сохранить вакансию" }));
  expect(props.onContentChange).toHaveBeenCalledOnce();
});

test("a failed posting queue request permits another check, and uncertain work hours keep their evidence", async () => {
  const props = cardProps();
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ detail: "Проверка недоступна" }, 503)));
  const job = makeJob("short", {
    description: "", description_quality: "missing", language_evidence: [],
    work_time: { evidence: "You can start at 3 PM", zone: null, start: null, end: null, mandatory: false, needs_review: true },
  });
  render(<JobCard {...props} job={job} tasks={[]} />);
  expect(screen.getByText(/Проверьте рабочие часы · You can start at 3 PM/)).toBeVisible();
  const button = screen.getByRole("button", { name: "Найти полное описание" });
  await userEvent.click(button);
  expect(await screen.findByRole("alert")).toHaveTextContent("Проверка недоступна");
  expect(button).toBeEnabled();
});

test("adding from an empty manual page refreshes the current list with no search settings", async () => {
  const job = makeJob("manual", { ...original, manual_version: 1, description_quality: "provided" });
  const post = vi.fn().mockResolvedValue(json({ existing: false, id: job.id }, 201));
  let items: typeof job[] = [];
  vi.stubGlobal("fetch", vi.fn(async (path: string) => {
    if (path === "/api/manual-jobs") {
      items = [job];
      return post();
    }
    return json({ items, page: 1, pages: 1, total: items.length, snapshot_id: "current" });
  }));
  const user = userEvent.setup();
  render(<Jobs personal tab="manual" hasTemplate={false} onProfile={vi.fn()} onTask={vi.fn()} revision={1} tasks={[]} />);
  await screen.findByText("Добавленных вручную вакансий пока нет");
  await user.click(screen.getByRole("button", { name: "Добавить вакансию" }));
  await user.type(screen.getByLabelText("Название вакансии"), original.title);
  await user.type(screen.getByLabelText("Компания"), original.company);
  await user.type(screen.getByLabelText("Описание вакансии"), original.description);
  await user.click(screen.getByRole("button", { name: "Сохранить вакансию" }));
  await screen.findByText("Вакансия сохранена.");
  expect(await screen.findByRole("article", { name: original.title })).toBeVisible();
});

test("place lookup handles no results, suggestion correction, errors and Escape", async () => {
  const source = { id: "city:ge-tbilisi", label: "Тбилиси, Грузия" };
  vi.stubGlobal("fetch", vi.fn(async (path: string) => {
    const query = new URL(path, "https://example.test").searchParams.get("q");
    if (query === "sf") return json({ detail: "Справочник недоступен" }, 503);
    return json(query === "tb" ? { items: [source], corrected: true } : { items: [], corrected: false });
  }));
  const change = vi.fn();
  const user = userEvent.setup();
  render(<PlacePicker label="Город" kinds="city" values={[]} onChange={change} />);
  const input = screen.getByRole("combobox", { name: "Город" });
  await user.type(input, "bad");
  await screen.findByText("Локация не найдена");
  await user.clear(input);
  await user.type(input, "tb");
  await screen.findByText("Возможно, вы имели в виду");
  await user.keyboard("{ArrowUp}{Escape}");
  expect(input).toHaveAttribute("aria-expanded", "false");
  expect(change).not.toHaveBeenCalled();
  await user.clear(input);
  await user.type(input, "sf");
  expect(await screen.findByText("Справочник недоступен")).toBeVisible();
  await user.clear(input);
  await user.type(input, "tb");
  await user.click(await screen.findByRole("option", { name: source.label }));
  expect(change).toHaveBeenCalledWith([source.id]);
});

test("a check keeps its idempotency key across a failed network response", async () => {
  const props = cardProps();
  const fetcher = vi.fn().mockRejectedValueOnce(new Error("network"))
    .mockResolvedValueOnce(json({ posting }));
  vi.stubGlobal("fetch", fetcher);
  const job = makeJob("existing", { description_quality: "full" });
  render(<JobCard {...props} job={job} tasks={[]} />);
  const check = screen.getByRole("button", { name: "Проверить объявление" });
  await userEvent.click(check);
  await waitFor(() => expect(check).toBeEnabled());
  await userEvent.click(check);
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual(JSON.parse(fetcher.mock.calls[1][1].body));
});

test("a reloaded search shows a real queue phase, supports cancellation and dismisses the outcome", async () => {
  const task = makeTask({ kind: "search", status: "queued", id: "search-1" });
  const stopped = { ...task, status: "cancelled" };
  let refuse = true;
  vi.stubGlobal("fetch", vi.fn(async (path: string) => {
    if (path === "/api/tasks/search-1/cancel") {
      if (refuse) {
        refuse = false;
        return json({ detail: "Не удалось отменить поиск" }, 503);
      }
      return json(stopped);
    }
    return json({ items: [], page: 1, pages: 1, total: 0, snapshot_id: "current" });
  }));
  const user = userEvent.setup();
  const props = { tab: "active" as const, personal: true, revision: 0, hasTemplate: false, onProfile: vi.fn(), onTask: vi.fn() };
  const { rerender } = render(<Jobs {...props} tasks={[task]} />);
  expect(screen.getByRole("button", { name: "Поиск в очереди" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Отменить поиск" }));
  await screen.findByText("Не удалось отменить поиск");
  await user.click(screen.getByRole("button", { name: "Отменить поиск" }));
  expect(props.onTask).toHaveBeenCalledOnce();
  rerender(<Jobs {...props} tasks={[stopped]} />);
  expect(screen.getByText("Поиск отменён.")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Закрыть уведомление" }));
  expect(screen.queryByText("Поиск отменён.")).not.toBeInTheDocument();
});

test("a failed search stays visible after reopening the list", async () => {
  const task = makeTask({ kind: "search", status: "failed", error: "Источник недоступен" });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ items: [], total: 0, page: 1, pages: 1, snapshot_id: "current" })));
  render(<Jobs tab="active" personal revision={0} hasTemplate={false} onProfile={vi.fn()} onTask={vi.fn()} tasks={[task]} />);
  expect(await screen.findByText("Источник недоступен")).toBeVisible();
});
