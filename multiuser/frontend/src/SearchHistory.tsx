import { useEffect, useState } from "react";
import { errorMessage, request } from "./api";
import { formatCreatedAt, Loading, Notice } from "./components";
import type { SearchRun } from "./types";

export function sourceErrorMessage(value: string) {
  const separator = value.lastIndexOf(":");
  const name = separator < 0 ? value : value.slice(0, separator);
  const code = separator < 0 ? "" : value.slice(separator + 1).trim();
  const reasons: Record<string, string> = {
    ReadTimeout: "источник не ответил вовремя",
    ConnectTimeout: "не удалось подключиться вовремя",
    HTTPStatusError: "источник вернул ошибку",
    SourceNetworkError: "ответ источника не прошёл сетевую проверку",
    JSONDecodeError: "не удалось разобрать ответ источника",
    ValueError: "не удалось разобрать ответ источника",
    request_limit: "достигнут лимит запросов к источнику",
    run_request_limit: "достигнут лимит запросов этого поиска",
    response_limit: "ответ источника превышает допустимый размер",
    run_response_limit: "достигнут допустимый объём данных этого поиска",
    blocked_address: "адрес источника не прошёл проверку безопасности",
    blocked_resolution: "сетевой адрес источника не прошёл проверку безопасности",
    unsupported_encoding: "источник прислал неподдерживаемый формат сжатия",
    HTTP_429: "источник временно ограничил частоту запросов",
    HTTP_403: "источник отказал в доступе",
    HTTP_404: "страница источника не найдена; проверьте настройки источника",
  };
  const reason = reasons[code] ?? (/^HTTP_5\d\d$/.test(code)
    ? "на стороне источника произошёл сбой"
    : "не удалось получить вакансии");
  return `${name}: ${reason}. Повторите поиск позже.`;
}

function SourceErrors({ errors }: { errors: string[] }) {
  return (
    <details>
      <summary>Проблемы источников: {errors.length}</summary>
      <ul>{errors.map((error, index) => <li key={index}>{sourceErrorMessage(error)}</li>)}</ul>
    </details>
  );
}

export function SearchHistory({
  revision,
  onSelect,
}: {
  revision: number;
  onSelect: (id: string) => void;
}) {
  const [page, setPage] = useState(1);
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<{
    items: SearchRun[];
    has_more: boolean;
  } | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    request<{ items: SearchRun[]; has_more: boolean }>(
      `/search-runs?page=${page}`,
      { signal: controller.signal },
    )
      .then((result) => {
        if (!controller.signal.aborted) {
          setData(result);
          setError("");
        }
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(errorMessage(failure));
      });
    return () => controller.abort();
  }, [page, revision, retry, open]);
  const labels: Record<string, string> = {
    pending: "Выполняется",
    published: "Опубликован",
    superseded: "Настройки изменились: сохранён в истории",
    cancelled: "Отменён",
    failed: "Ошибка",
    ready: "Завершён",
    queued: "В очереди",
    running: "Выполняется",
  };
  return (
    <details
      className="search-history"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>История поиска</summary>
      {error && (
        <Notice>
          {error}
          <button
            className="text-button"
            onClick={() => setRetry((value) => value + 1)}
          >
            Повторить загрузку истории
          </button>
        </Notice>
      )}
      {!data ? (
        <Loading text="Загружаем историю…" />
      ) : (
        <>
          {data.items.length === 0 && <p>Поисковых запусков пока нет.</p>}
          <ol>
            {data.items.map((run) => (
              <li key={run.id}>
                <strong>{formatCreatedAt(run.created_at)}</strong> ·{" "}
                {labels[run.state ?? run.task_status] ?? run.task_status}
                {run.profile_version && (
                  <span> · профиль v{run.profile_version}</span>
                )}
                {run.completed_at && (
                  <>
                    <span> · вакансий: {run.count ?? 0}</span>
                    <button
                      className="text-button"
                      onClick={() => onSelect(run.id)}
                    >
                      Открыть выдачу запуска {run.id.slice(0, 8)}
                    </button>
                  </>
                )}
                {run.task_error && <p>{run.task_error}</p>}
                {!!run.errors?.length && <SourceErrors errors={[...new Set(run.errors)]} />}
              </li>
            ))}
          </ol>
          <div className="pagination">
            <button
              className="button secondary"
              disabled={page === 1}
              onClick={() => setPage((value) => value - 1)}
            >
              Предыдущие запуски
            </button>
            <span>Страница истории {page}</span>
            <button
              className="button secondary"
              disabled={!data.has_more}
              onClick={() => setPage((value) => value + 1)}
            >
              Следующие запуски
            </button>
          </div>
        </>
      )}
    </details>
  );
}

export function SearchRunSummary({ run }: { run: SearchRun }) {
  const errors = run.sources?.some((source) => source.error)
    ? run.sources.filter((source) => source.error).map((source) => `${source.name}: ${source.error}`)
    : [...new Set(run.errors ?? [])];
  return (
    <section
      className="search-run-summary"
      aria-label="Результат поискового запуска"
    >
      <p>
        Профиль поиска v{run.profile_version ?? "—"}
        {run.published === false &&
          " · Сохранён в истории, текущая выдача не заменена"}
      </p>
      {errors.length > 0 && (
        <Notice>
          Не все источники загружены полностью. Доступные результаты сохранены.
          <SourceErrors errors={errors} />
        </Notice>
      )}
      {!!run.sources?.length && (
        <details>
          <summary>Источники поиска: {run.sources.length}</summary>
          {run.sources.some((source) => source.limited) &&
            <p>Некоторые источники предоставляют ограниченное окно объявлений. Отсутствие вакансии в ответе не подтверждает её закрытие.</p>}
          <ul>{run.sources.map((source) => (
            <li key={source.id}>
              {source.name} · {source.error ? (source.count ? "получено частично" : "ошибка получения") : "получено"}
              {source.count !== undefined && ` · объявлений: ${source.count}`}
              {" · "}{formatCreatedAt(source.fetched_at)}
              {source.cached && " · из кэша"}
            </li>
          ))}</ul>
        </details>
      )}
    </section>
  );
}
