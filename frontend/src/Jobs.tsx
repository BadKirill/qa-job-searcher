import { useEffect, useRef, useState } from "react";
import { errorMessage, isPending, request, requestId, safeJobUrl } from "./api";
import { Chat } from "./Chat";
import { formatCreatedAt, Icon, Loading, Notice, TaskStatus } from "./components";
import type { Job, JobPage, Task } from "./types";

export function JobCard({
  job,
  tasks,
  hasTemplate,
  onProfile,
  onState,
  onTask,
  revision,
}: {
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
          <span>{job.company || "Компания не указана"}</span>
          {job.location && (
            <>
              <span className="dot-separator">·</span>
              <span className="job-location">{job.location}</span>
            </>
          )}
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
      {(job.details.qa_types.length > 0 ||
        job.details.languages.length > 0 ||
        job.details.programming_languages.length > 0) && (
        <div className="tags">
          {job.details.qa_types.map((type) => (
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
      {error && <Notice>{error}</Notice>}
      <div className="job-actions">
        <button
          className="button primary"
          disabled={
            !!busy ||
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
  tab,
  revision,
  tasks,
  hasTemplate,
  onProfile,
  onTask,
}: {
  tab: "active" | "applied" | "archive";
  revision: number;
  tasks: Task[];
  hasTemplate: boolean;
  onProfile: () => void;
  onTask: () => void;
}) {
  const [page, setPage] = useState(1);
  const [snapshot, setSnapshot] = useState("");
  const [result, setResult] = useState<JobPage | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [searching, setSearching] = useState(false);
  const [searchNotice, setSearchNotice] = useState(false);
  const [undo, setUndo] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    const query = new URLSearchParams({
      tab,
      page: String(page),
      page_size: "10",
    });
    if (snapshot) query.set("snapshot_id", snapshot);
    request<JobPage>(`/jobs?${query}`, { signal: controller.signal })
      .then((data) => {
        setError("");
        if (data.pages > 0 && page > data.pages) {
          setPage(data.pages);
          return;
        }
        setResult(data);
        setSnapshot(data.snapshot_id);
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(errorMessage(failure));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [tab, page, snapshot, refresh, revision]);

  async function search() {
    setSearching(true);
    setError("");
    try {
      await request("/search-runs", { method: "POST", body: {} });
      setSearchNotice(true);
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
    setSnapshot("");
    setPage(1);
    setRefresh((value) => value + 1);
    setSearchNotice(false);
  }
  const searchTask = tasks.find(
    (task) => task.kind === "search" && isPending(task.status),
  );
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
          <p className="eyebrow">
            {headings.eyebrow}
          </p>
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
            new Date(result.snapshot_at).getTime() < new Date(result.last_search_at).getTime() &&
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
      {searchNotice && (
        <Notice tone="info">
          Поиск запущен. Новые результаты появятся после завершения.
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
              name={tab === "active" ? "jobs" : tab === "applied" ? "applied" : "archive"}
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
              ? "Запустите поиск, чтобы получить актуальные QA-вакансии."
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
                  key={job.id}
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
    </div>
  );
}
