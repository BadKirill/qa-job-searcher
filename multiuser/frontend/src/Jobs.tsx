import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage, forgetOperation, isPending, operationKey, request, safeJobUrl, taskLabel } from "./api";
import { SearchHistory, SearchRunSummary } from "./SearchHistory";
import { Chat } from "./Chat";
import { ManualJob } from "./ManualJob";
import {
  formatCreatedAt,
  Icon,
  Loading,
  Notice,
  TaskStatus,
} from "./components";
import type { Job, JobPage, PostingStatus, Task } from "./types";
import { useRefreshEffect } from "./useRefreshEffect";

const summaryLabels: Record<string, string> = {
  company_focus: "О компании", industry: "Отрасль", company_size: "Размер компании",
  founded: "Год основания", role_focus: "Фокус роли", experience: "Опыт",
  responsibilities: "Задачи", requirements: "Требования", stack: "Стек", benefits: "Условия",
};
const languageNames: Record<string, string> = { ru: "русский", en: "английский" };
const postingLabels = { open: "Открыта", closed: "Закрыта", unconfirmed: "Не удалось подтвердить" };

export function JobCard({
  snapshotId,
  personal = false,
  job,
  tasks,
  hasTemplate,
  onProfile,
  onState,
  onTask,
  onContentChange,
  revision,
}: {
  snapshotId?: string;
  personal?: boolean;
  job: Job;
  tasks: Task[];
  hasTemplate: boolean;
  onProfile: () => void;
  onState: () => void;
  onTask: () => void;
  onContentChange?: () => void;
  revision: number;
}) {
  const operations = useRef(new Map<string, string>());
  const [busy, setBusy] = useState("");
  const [editingJob, setEditingJob] = useState(false);
  const [checking, setChecking] = useState(false);
  const [postingNotice, setPostingNotice] = useState<PostingStatus | null>(null);
  const [queuedPosting, setQueuedPosting] = useState<Task | null>(null);
  const [needsTemplate, setNeedsTemplate] = useState(false);
  const [deletingTask, setDeletingTask] = useState("");
  const [error, setError] = useState("");
  const [confirmingApplied, setConfirmingApplied] = useState(false);
  const [chatOpen, setChatOpen] = useState(false);
  const declineApplied = useRef<HTMLButtonElement>(null);
  const [chatDocumentKind, setChatDocumentKind] = useState<
    "cv" | "cover_letter"
  >("cv");
  const documents = [
    ...job.documents.filter(
      (document) => !tasks.some((task) => task.id === document.id),
    ),
    ...tasks.filter(
      (task) =>
        task.job_id === job.id && ["cv", "cover_letter"].includes(task.kind),
    ),
  ];
  const url = safeJobUrl(job.url);
  const qaTypes =
    /\bqa\b|\bquality\b|\btest(?:er|ing)?\b|\bsdet\b|тестиров/i.test(job.title)
      ? job.details.qa_types
      : [];
  useEffect(() => {
    if (!confirmingApplied) return;
    declineApplied.current?.focus();
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) setConfirmingApplied(false);
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [busy, confirmingApplied]);
  async function state(value: { applied?: boolean; archived?: boolean }) {
    if (busy) return false;
    setBusy("state");
    setError("");
    try {
      await request(`/jobs/${encodeURIComponent(job.id)}/state`, {
        method: "PATCH",
        body: value,
      });
      onState();
      return true;
    } catch (failure) {
      setError(errorMessage(failure));
      return false;
    } finally {
      setBusy("");
    }
  }
  async function generate(kind: "cv" | "cover_letter") {
    if (!hasTemplate) {
      if (job.manual_version) setNeedsTemplate(true);
      else onProfile();
      return;
    }
    if (busy) return;
    setBusy(kind);
    setError("");
    try {
      await request(`/jobs/${encodeURIComponent(job.id)}/documents`, {
        method: "POST",
        body: {
          kind,
          language: kind === "cv" ? "en" : "auto",
          request_id: operationKey(operations.current, kind),
          ...(snapshotId ? { snapshot_id: snapshotId } : {}),
        },
      });
      forgetOperation(operations.current, kind);
      onTask();
    } catch (failure) {
      forgetOperation(operations.current, kind, failure);
      setError(errorMessage(failure));
    } finally {
      setBusy("");
    }
  }
  async function cancel(taskId: string) {
    setError("");
    try {
      await request(`/tasks/${encodeURIComponent(taskId)}/cancel`, {
        method: "POST",
        body: {},
      });
      onTask();
    } catch (failure) {
      setError(errorMessage(failure));
    }
  }
  async function remove(taskId: string) {
    if (deletingTask) return;
    setDeletingTask(taskId);
    setError("");
    try {
      await request(`/tasks/${encodeURIComponent(taskId)}`, {
        method: "DELETE",
      });
      onTask();
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setDeletingTask("");
    }
  }
  async function checkOriginal() {
    setChecking(true);
    setError("");
    try {
      const data = await request<{ posting?: PostingStatus; task?: Task }>(`/jobs/${encodeURIComponent(job.id)}/check-posting`, {
        method: "POST", body: { request_id: operationKey(operations.current, "posting") },
      });
      forgetOperation(operations.current, "posting");
      if (data.posting) setPostingNotice(data.posting);
      if (data.task) setQueuedPosting(data.task);
      onTask();
    } catch (failure) {
      forgetOperation(operations.current, "posting", failure);
      setError(errorMessage(failure));
    } finally {
      setChecking(false);
    }
  }
  const postingTask = tasks.find((task) => task.kind === "posting_check" && task.job_id === job.id) ?? queuedPosting;
  const postingPending = postingTask ? isPending(postingTask.status) : false;
  const posting = job.posting ?? postingNotice;
  const workTime = job.work_time;
  return (
    <article
      className={`job-card${job.applied_at ? " job-applied" : ""}`}
      aria-label={job.title}
    >
      <div className="job-topline">
        <div className="company-line">
          <span className="company-avatar">
            {job.company.slice(0, 1).toUpperCase() || "Q"}
          </span>
          <div className="company-details">
            <span>{job.company || "Компания не указана"}</span>
            {job.location && <span className="job-location">{job.location}</span>}
            {job.russian_company && <span className="job-location">Компания из России</span>}
          </div>
        </div>
        {job.applied_at && (
          <span className="applied-badge">
            <Icon name="check" size={14} />
            Отклик отправлен
          </span>
        )}
      </div>
      <div className="job-title-row">
        <h2>{job.title}</h2>
        {job.salary && <span className="salary">{job.salary}</span>}
      </div>
      {!job.manual_version && <div className="job-match" aria-label="Причины подбора">
        <strong>{job.score}/100</strong>
        <span>Рейтинг по фильтрам</span>
        {job.match_reasons?.length > 0 && (
          <ul>
            {job.match_reasons.slice(0, 3).map((reason, index) => (
              <li key={`${reason}-${index}`}>{reason}</li>
            ))}
          </ul>
        )}
      </div>}
      {!!job.summary?.length && (
        <ul className="posting-summary" aria-label="Кратко о вакансии">
          {job.summary.map((item, index) => (
            <li key={`${item.field}-${index}`}>
              <strong>{summaryLabels[item.field] ?? item.field}</strong> · {item.value}
              {safeJobUrl(item.url) && <a href={safeJobUrl(item.url)!} target="_blank" rel="noreferrer" title={item.evidence}>Источник</a>}
            </li>
          ))}
        </ul>
      )}
      {job.language_evidence?.map((item, index) => (
        <p className="small" key={`${item.kind}-${index}`} title={item.evidence}>
          {item.kind === "required" ? "Требуются" : item.kind === "team" ? "Язык команды" : item.kind === "company_inferred" ? "Предположение по компании" : "Язык публикации, предположение"}
          {" · "}
          {item.alternatives?.length
            ? item.alternatives.map((group) => group.map((language) => languageNames[language] ?? language).join(" и ")).join(" или ")
            : item.languages.map((language) => languageNames[language] ?? language).join(", ")}
        </p>
      ))}
      {workTime && (
        <p className="small">
          {workTime.needs_review ? "Проверьте рабочие часы" : `${workTime.mandatory ? "Обязательные часы" : "Рабочие часы"} · ${workTime.start}–${workTime.end} ${workTime.zone}`}
          {workTime.local_start && workTime.local_end && ` · ваше время ${workTime.local_start.slice(11, 16)}–${workTime.local_end.slice(11, 16)}`}
          {" · "}{workTime.evidence}
        </p>
      )}
      {(qaTypes.length > 0 ||
        (!job.language_evidence && job.details.languages.length > 0) ||
        job.details.programming_languages.length > 0) && (
        <div className="tags">
          {qaTypes.map((type) => (
            <span className="tag tag-qa" key={type}>
              {type}
            </span>
          ))}
          {!job.language_evidence && job.details.languages.map((language) => (
            <span className="tag" key={language}>
              Язык: {language}
            </span>
          ))}
          {job.details.programming_languages.map((language) => (
            <span className="tag" key={language}>
              {language}
            </span>
          ))}
        </div>
      )}
      <div className="job-sections">
        {job.details.responsibilities.length > 0 && (
          <section>
            <h3>Чем предстоит заниматься</h3>
            <ul>
              {job.details.responsibilities.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          </section>
        )}
        {job.details.requirements.length > 0 && (
          <section>
            <h3>Что понадобится</h3>
            <ul>
              {job.details.requirements.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          </section>
        )}
      </div>
      {job.description && (
        <details className="job-description">
          <summary>
            {job.description_quality === "fragment" ? "Фрагмент описания" : "Полное описание"} <span aria-hidden="true">+</span>
          </summary>
          <p>{job.description}</p>
        </details>
      )}
      {!job.description && (
        <p className="muted small">
          Полное описание пока недоступно. Откройте оригинал вакансии.
        </p>
      )}
      {personal && (
        <div className="posting-check">
          <p role="status">
            {postingPending ? taskLabel(postingTask!.status, "posting_check") : postingTask?.status === "failed" ? postingTask.error : posting ? `${postingLabels[posting.status]} · ${formatCreatedAt(posting.checked_at)} · ${posting.evidence}`
              : "Статус объявления ещё не проверен."}
          </p>
          <button className="button secondary" type="button" disabled={checking || postingPending}
            onClick={() => void checkOriginal()}>
            {checking ? "Проверяем оригинал…" : job.description_quality === "fragment" || job.description_quality === "missing"
              ? "Найти полное описание" : "Проверить объявление"}
          </button>
        </div>
      )}
      {needsTemplate && (
        <Notice tone="info">
          Для документов нужен подтверждённый шаблон CV. Вакансия сохранена в разделе «Добавленные вручную».
          <button type="button" className="text-button" onClick={onProfile}>Открыть профиль</button>
        </Notice>
      )}
      {job.manual_version && <button className="text-button" type="button" onClick={() => setEditingJob(true)}>Изменить вакансию</button>}
      {editingJob && (
        <ManualJob job={job} onClose={() => setEditingJob(false)}
          onSave={() => { setEditingJob(false); onContentChange?.(); }} />
      )}
      {job.needs_refresh && (
        <Notice>
          Историческая вакансия без описания. Обновите её перед генерацией.
        </Notice>
      )}
      {job.freshness && job.freshness !== "legacy" && (
        <p className="job-freshness">
          {job.freshness === "retained"
            ? "Сохранённая вакансия: источник недоступен"
            : job.freshness === "unconfirmed"
              ? "Не подтверждена в последнем ограниченном фиде; закрытие не установлено"
              : "Получена из источника"}
          {job.fetched_at && ` · ${formatCreatedAt(job.fetched_at)}`}
          {job.location_status === "unknown" && " · Географию нужно проверить"}
        </p>
      )}
      {error && <Notice>{error}</Notice>}
      <div className="job-actions">
        <button
          className="button primary"
          disabled={
            !!busy ||
            !!job.needs_refresh ||
            documents.some(
              (task) => task.kind === "cv" && isPending(task.status),
            )
          }
          onClick={() => void generate("cv")}
        >
          <Icon name="file" size={16} />
          {busy === "cv" ? "Создаём задание…" : "Сделать CV"}
        </button>
        <button
          className="button secondary"
          disabled={
            !!busy ||
            !!job.needs_refresh ||
            documents.some(
              (task) => task.kind === "cover_letter" && isPending(task.status),
            )
          }
          onClick={() => void generate("cover_letter")}
        >
          {busy === "cover_letter"
            ? "Создаём задание…"
            : "Сделать кавер леттер"}
        </button>
        {url ? (
          <a
            className={`button vacancy-link${busy ? " link-disabled" : ""}`}
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(event) => {
              if (busy) {
                event.preventDefault();
                return;
              }
              setConfirmingApplied(true);
            }}
          >
            Перейти на страницу вакансии
            <Icon name="arrow" size={16} />
          </a>
        ) : (
          <span className="muted small">Ссылка недоступна</span>
        )}
      </div>
      {documents.length > 0 && (
        <div className="document-tasks">
          {documents.map((task) => (
            <TaskStatus
              key={task.id}
              task={task}
              onDiscuss={() => {
                setChatDocumentKind(task.kind as "cv" | "cover_letter");
                setChatOpen(true);
              }}
              onCancel={() => void cancel(task.id)}
              onDelete={() => void remove(task.id)}
              deleting={deletingTask === task.id}
            />
          ))}
        </div>
      )}
      <footer className="job-footer">
        <button
          className="text-button reject-button"
          disabled={!!busy}
          onClick={() => void state({ archived: !job.archived_at })}
        >
          <Icon name={job.archived_at ? "refresh" : "close"} size={14} />
          {job.archived_at ? "Вернуть из архива" : "Не подходит"}
        </button>
        {job.applied_at && (
          <button
            className="text-button"
            disabled={!!busy}
            onClick={() => void state({ applied: false })}
          >
            Отменить отметку об отклике
          </button>
        )}
      </footer>
      {chatOpen && (
        <Chat
          snapshotId={snapshotId}
          jobId={job.id}
          documentKind={chatDocumentKind}
          revision={revision}
          onGenerated={onTask}
          onClose={() => setChatOpen(false)}
        />
      )}
      {confirmingApplied && (
        <div className="modal-backdrop">
          <section
            className="confirmation-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby={`applied-question-${job.id}`}
          >
            <p className="eyebrow">СТАТУС ВАКАНСИИ</p>
            <h2 id={`applied-question-${job.id}`}>Ты откликнулся?</h2>
            <p className="muted">
              Если да, вакансия перейдёт из общего списка во вкладку Applied.
            </p>
            <div className="confirmation-actions">
              <button
                type="button"
                className="button primary"
                disabled={!!busy}
                onClick={async () => {
                  if (await state({ applied: true })) {
                    setConfirmingApplied(false);
                  }
                }}
              >
                {busy ? "Сохраняем…" : "Да"}
              </button>
              <button
                ref={declineApplied}
                type="button"
                className="button secondary"
                disabled={!!busy}
                onClick={() => setConfirmingApplied(false)}
              >
                Нет
              </button>
            </div>
          </section>
        </div>
      )}
    </article>
  );
}

export function Jobs({
  personal = false,
  tab,
  onManual,
  revision,
  tasks,
  hasTemplate,
  onProfile,
  onTask,
}: {
  personal?: boolean;
  tab: "active" | "applied" | "archive" | "manual";
  onManual?: () => void;
  revision: number;
  tasks: Task[];
  hasTemplate: boolean;
  onProfile: () => void;
  onTask: () => void;
}) {
  const [page, setPage] = useState(1);
  const [run, setRun] = useState("");
  const snapshot = useRef("");
  const [result, setResult] = useState<JobPage | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const searchOperations = useRef(new Map<string, string>());
  const [addJob, setAddJob] = useState(false);
  const [manualFeedback, setManualFeedback] = useState("");
  const [searching, setSearching] = useState(false);
  const [cancelingSearch, setCancelingSearch] = useState(false);
  const [dismissedSearchId, setDismissedSearchId] = useState("");
  const [startedSearch, setStartedSearch] = useState<Task | null>(null);
  const [undo, setUndo] = useState(false);

  const loadJobs = useCallback(async (signal: AbortSignal) => {
    setLoading(true);
    const query = new URLSearchParams({
      tab,
      page: String(page),
      page_size: "10",
    });
    if (snapshot.current) query.set("snapshot_id", snapshot.current);
    if (run) query.set("run_id", run);
    try {
      const data = await request<JobPage>(`/jobs?${query}`, { signal });
      if (signal.aborted) return;
      setError("");
      if (data.pages > 0 && page > data.pages) {
        setPage(data.pages);
        return;
      }
      setResult(data);
      snapshot.current = data.snapshot_id;
    } catch (failure) {
      if (!signal.aborted) setError(errorMessage(failure));
    } finally {
      if (!signal.aborted) setLoading(false);
    }
  }, [tab, page, run, refresh]);
  useRefreshEffect(loadJobs, revision);

  async function search() {
    setSearching(true);
    setError("");
    try {
      const task = await request<Task>("/search-runs", {
        method: "POST",
        body: { request_id: operationKey(searchOperations.current, "search") },
      });
      forgetOperation(searchOperations.current, "search");
      setStartedSearch(task);
      onTask();
    } catch (failure) {
      forgetOperation(searchOperations.current, "search", failure);
      setError(errorMessage(failure));
    } finally {
      setSearching(false);
    }
  }
  function changed() {
    setRefresh((value) => value + 1);
    setUndo(true);
  }
  function fresh() {
    setRun("");
    snapshot.current = "";
    setPage(1);
    setRefresh((value) => value + 1);
    setStartedSearch(null);
  }
  const latestSearch = tasks.find((task) => task.kind === "search");
  const previousProblem = personal && latestSearch && ["failed", "cancelled"].includes(latestSearch.status)
    && latestSearch.id !== dismissedSearchId ? latestSearch : null;
  const currentSearch = tasks.find((task) => task.id === startedSearch?.id) ?? startedSearch ?? previousProblem;
  const searchTask = tasks.find(
    (task) => task.kind === "search" && isPending(task.status),
  ) ?? (currentSearch && isPending(currentSearch.status) ? currentSearch : null);
  async function cancelSearch(task: Task) {
    setCancelingSearch(true);
    setError("");
    try {
      const response = await request<Task>(`/tasks/${encodeURIComponent(task.id)}/cancel`, { method: "POST" });
      setStartedSearch(response);
      onTask();
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setCancelingSearch(false);
    }
  }
  function dismissSearch(task: Task) {
    setDismissedSearchId(task.id);
    setStartedSearch(null);
  }
  const headings = {
    active: {
      eyebrow: "ВАШ СЛЕДУЮЩИЙ ШАГ",
      title: "Подходящие вакансии",
      description: "Возможности, с которых хочется начать разговор.",
    },
    applied: {
      eyebrow: "ВАШИ ОТКЛИКИ",
      title: "Отклики",
      description: "Вакансии, на которые вы уже откликнулись.",
    },
    manual: {
      eyebrow: "ЛИЧНЫЕ ВАКАНСИИ",
      title: "Добавленные вручную",
      description: "Вакансии, которые вы сохранили по ссылке или описанию.",
    },
    archive: {
      eyebrow: "ВСЁ ПОД КОНТРОЛЕМ",
      title: "Архив вакансий",
      description: "Вакансии, которые вы отложили. Любую можно вернуть.",
    },
  }[tab];
  return (
    <div>
      <div className="page-heading">
        <div>
          <p className="eyebrow">{headings.eyebrow}</p>
          <h1>
            {headings.title}
            <span className="heading-dot">.</span>
          </h1>
          <p className="muted">{headings.description}</p>
        </div>
        {tab === "active" && (
          <button
            className="button secondary refresh-button"
            onClick={() => void search()}
            disabled={searching || !!searchTask}
          >
            <Icon name="refresh" size={16} />
            {searchTask ? taskLabel(searchTask.status, "search") : searching ? "Запускаем поиск…" : "Обновить поиск"}
          </button>
        )}
      </div>
      {tab === "active" && searchTask && (
        <button type="button" className="text-button" disabled={cancelingSearch}
          onClick={() => void cancelSearch(searchTask)}>Отменить поиск</button>
      )}
      {personal && (tab === "active" || tab === "manual") && (
        <div className="source-actions">
          <button type="button" className="button secondary" onClick={() => setAddJob(true)}>Добавить вакансию</button>
          {tab === "active" && onManual && (
            <button type="button" className="text-button" onClick={onManual}>Добавленные вручную</button>
          )}
        </div>
      )}
      {addJob && (
        <ManualJob onClose={() => setAddJob(false)} onSave={(_, existing) => {
          setAddJob(false);
          setManualFeedback(existing ? "Эта вакансия уже добавлена." : "Вакансия сохранена.");
          if (tab === "active" && onManual) onManual();
          else fresh();
        }} />
      )}
      {manualFeedback && <Notice tone="success">{manualFeedback}</Notice>}
      <div className="list-meta">
        <span>
          {result ? `${result.total} вакансий` : "Ваши вакансии"}
          <span className="dot-separator">·</span>По 10 на странице
        </span>
        <span>
          {result?.last_search_at
            ? `Последний поиск: ${formatCreatedAt(result.last_search_at)}`
            : "Поиск ещё не завершался"}
          {result?.snapshot_at &&
            result.last_search_at &&
            new Date(result.snapshot_at).getTime() <
              new Date(result.last_search_at).getTime() &&
            " · Просматривается сохранённый снимок"}
        </span>
      </div>
      {error && (
        <Notice>
          {error}
          <button className="text-button" onClick={fresh}>
            Повторить
          </button>
        </Notice>
      )}
      {tab === "active" && result?.criteria_changed && <Notice tone="info">Критерии изменились. Запустите новый поиск, чтобы обновить выдачу.</Notice>}
      {(run ||
        (result?.latest_run_id &&
          result.search_run?.id !== result.latest_run_id)) && (
        <Notice tone="info">
          Просматривается сохранённый запуск.
          <button className="text-button" onClick={fresh}>
            Показать текущую выдачу
          </button>
        </Notice>
      )}
      {tab === "active" && !currentSearch && searchTask && <TaskStatus task={searchTask} />}
      {tab === "active" && currentSearch?.status === "failed" && (
        <Notice onClose={() => dismissSearch(currentSearch)}>
          {currentSearch.error || "Не удалось завершить поиск. Попробуйте запустить его ещё раз."}
        </Notice>
      )}
      {tab === "active" && currentSearch?.status === "cancelled" && (
        <Notice tone="info" onClose={() => dismissSearch(currentSearch)}>Поиск отменён.</Notice>
      )}
      {tab === "active" && currentSearch &&
        (isPending(currentSearch.status) || currentSearch.status === "ready") && (
        <Notice tone="info">
          {currentSearch.status === "ready"
            ? "Поиск завершён."
            : "Поиск запущен. Новые результаты появятся после завершения."}
          <button className="text-button" onClick={fresh}>
            Показать свежую выдачу <span aria-hidden="true">→</span>
          </button>
        </Notice>
      )}
      {undo && (
        <Notice tone="success" onClose={() => setUndo(false)}>
          Изменения сохранены.
        </Notice>
      )}
      {loading && !result ? (
        <Loading text="Подбираем ваше рабочее пространство…" />
      ) : result && result.items.length === 0 ? (
        <div className="empty-state">
          <div className="round-icon">
            <Icon
              name={
                tab === "active"
                  ? "jobs"
                  : tab === "applied"
                    ? "applied"
                    : "archive"
              }
              size={28}
            />
          </div>
          <h2>
            {tab === "active"
              ? "Новые возможности впереди"
              : tab === "applied"
                ? "Откликов пока нет"
                : tab === "manual" ? "Добавленных вручную вакансий пока нет" : "В архиве пока пусто"}
          </h2>
          <p>
            {tab === "active"
              ? "Запустите поиск, чтобы получить актуальные вакансии."
              : tab === "applied"
                ? "После подтверждения отклика вакансия появится здесь."
                : tab === "manual" ? "Добавьте вакансию по ссылке или вставьте её описание."
                  : "Здесь будут вакансии, для которых вы выбрали «Не подходит»."}
          </p>
          {tab === "active" && (
            <button
              className="button primary"
              disabled={searching || !!searchTask}
              onClick={() => void search()}
            >
              Найти вакансии
              <Icon name="arrow" />
            </button>
          )}
        </div>
      ) : (
        result && (
          <>
            <div
              className={`job-list${loading ? " list-loading" : ""}`}
              aria-busy={loading}
            >
              {result.items.map((job) => (
                <JobCard
                personal={personal}
                  key={`${result.snapshot_id}:${job.id}`}
                  snapshotId={result.snapshot_id}
                  job={job}
                  tasks={tasks}
                  hasTemplate={hasTemplate}
                  onProfile={onProfile}
                  onState={changed}
                  onContentChange={fresh}
                  onTask={onTask}
                  revision={revision}
                />
              ))}
            </div>
            {result.pages > 1 && (
              <nav className="pagination" aria-label="Пагинация вакансий">
                <span>
                  Страница {page} из {result.pages}
                </span>
                <div>
                  <button
                    className="page-button"
                    disabled={page === 1 || loading}
                    onClick={() => setPage((value) => value - 1)}
                    aria-label="Предыдущая страница"
                  >
                    ←
                  </button>
                  {Array.from({ length: result.pages }, (_, index) => index + 1)
                    .filter(
                      (number) =>
                        number === 1 ||
                        number === result.pages ||
                        Math.abs(number - page) <= 1,
                    )
                    .map((number, index, numbers) => (
                      <span key={number}>
                        {index > 0 && number - numbers[index - 1] > 1 && (
                          <span className="page-ellipsis">…</span>
                        )}
                        <button
                          className={`page-button${number === page ? " current" : ""}`}
                          aria-label={`Страница ${number}`}
                          aria-current={number === page ? "page" : undefined}
                          disabled={loading}
                          onClick={() => setPage(number)}
                        >
                          {number}
                        </button>
                      </span>
                    ))}
                  <button
                    className="page-button"
                    disabled={page === result.pages || loading}
                    onClick={() => setPage((value) => value + 1)}
                    aria-label="Следующая страница"
                  >
                    →
                  </button>
                </div>
              </nav>
            )}
          </>
        )
      )}
      {personal && (
        <SearchHistory
          revision={revision + refresh}
          onSelect={(id) => {
            setRun(id);
            snapshot.current = "";
            setPage(1);
            setStartedSearch(null);
          }}
        />
      )}
      {result?.search_run && <SearchRunSummary run={result.search_run} />}
    </div>
  );
}
