import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage, isPending, request, requestId, safeJobUrl } from "./api";
import { SearchHistory, SearchRunSummary } from "./SearchHistory";
import { Chat } from "./Chat";
import {
  formatCreatedAt,
  Icon,
  Loading,
  Notice,
  TaskStatus,
} from "./components";
import type { Job, JobPage, Task } from "./types";
import { useRefreshEffect } from "./useRefreshEffect";

export function JobCard({
  snapshotId,
  job,
  tasks,
  hasTemplate,
  onProfile,
  onState,
  onTask,
  revision,
}: {
  snapshotId?: string;
  job: Job;
  tasks: Task[];
  hasTemplate: boolean;
  onProfile: () => void;
  onState: () => void;
  onTask: () => void;
  revision: number;
}) {
  const [busy, setBusy] = useState("");
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
      onProfile();
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
          request_id: requestId(),
          ...(snapshotId ? { snapshot_id: snapshotId } : {}),
        },
      });
      onTask();
    } catch (failure) {
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
            applied
          </span>
        )}
      </div>
      <div className="job-title-row">
        <h2>{job.title}</h2>
        {job.salary && <span className="salary">{job.salary}</span>}
      </div>
      <div className="job-match" aria-label="Причины подбора">
        <strong>{job.score}/100</strong>
        <span>Рейтинг по фильтрам</span>
        {job.match_reasons?.length > 0 && (
          <ul>
            {job.match_reasons.slice(0, 3).map((reason, index) => (
              <li key={`${reason}-${index}`}>{reason}</li>
            ))}
          </ul>
        )}
      </div>
      {(qaTypes.length > 0 ||
        job.details.languages.length > 0 ||
        job.details.programming_languages.length > 0) && (
        <div className="tags">
          {qaTypes.map((type) => (
            <span className="tag tag-qa" key={type}>
              {type}
            </span>
          ))}
          {job.details.languages.map((language) => (
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
            Полное описание <span aria-hidden="true">+</span>
          </summary>
          <p>{job.description}</p>
        </details>
      )}
      {!job.description && (
        <p className="muted small">
          Полное описание пока недоступно. Откройте оригинал вакансии.
        </p>
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
            Отменить applied
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
  revision,
  tasks,
  hasTemplate,
  onProfile,
  onTask,
}: {
  personal?: boolean;
  tab: "active" | "applied" | "archive";
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
  const [searching, setSearching] = useState(false);
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
        body: { request_id: requestId() },
      });
      setStartedSearch(task);
      onTask();
    } catch (failure) {
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
  const currentSearch = tasks.find((task) => task.id === startedSearch?.id) ?? startedSearch;
  const searchTask = tasks.find(
    (task) => task.kind === "search" && isPending(task.status),
  ) ?? (currentSearch && isPending(currentSearch.status) ? currentSearch : null);
  const headings = {
    active: {
      eyebrow: "ВАШ СЛЕДУЮЩИЙ ШАГ",
      title: "Подходящие вакансии",
      description: "Возможности, с которых хочется начать разговор.",
    },
    applied: {
      eyebrow: "ВАШИ ОТКЛИКИ",
      title: "Applied",
      description: "Вакансии, на которые вы уже откликнулись.",
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
            {searching || searchTask ? "Обновляем…" : "Обновить поиск"}
          </button>
        )}
      </div>
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
        <Notice onClose={() => setStartedSearch(null)}>
          {currentSearch.error || "Не удалось завершить поиск. Попробуйте запустить его ещё раз."}
        </Notice>
      )}
      {tab === "active" && currentSearch?.status === "cancelled" && (
        <Notice tone="info" onClose={() => setStartedSearch(null)}>Поиск отменён.</Notice>
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
                : "В архиве пока пусто"}
          </h2>
          <p>
            {tab === "active"
              ? "Запустите поиск, чтобы получить актуальные вакансии."
              : tab === "applied"
                ? "После подтверждения отклика вакансия появится здесь."
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
                  key={`${result.snapshot_id}:${job.id}`}
                  snapshotId={result.snapshot_id}
                  job={job}
                  tasks={tasks}
                  hasTemplate={hasTemplate}
                  onProfile={onProfile}
                  onState={changed}
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
