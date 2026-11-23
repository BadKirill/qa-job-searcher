import { useEffect, useState } from "react";
import { errorMessage, request } from "./api";
import { Loading, Notice } from "./components";

type Status = {
  queue: { kind: string; status: string; count: number }[];
  expired_leases: number;
  oldest_wait_seconds: number;
  usage_states: Record<string, number>;
  unknown_usage: number;
  cooldown_seconds: number;
  source_errors: number;
  services: Record<string, number>;
};

const services: Record<string, string> = {
  all: "Все очереди",
  telegram: "Telegram",
  scheduler: "Планировщик",
  search: "Поиск",
  chat: "Чаты",
  document: "Документы",
};

export function Diagnostics() {
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setError("");
    setStatus(null);
    request<Status>("/admin/diagnostics", { signal: controller.signal })
      .then((result) => {
        if (!controller.signal.aborted) setStatus(result);
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(errorMessage(failure));
      });
    return () => controller.abort();
  }, [revision]);
  return (
    <section className="diagnostics-panel" aria-label="Диагностика исполнения">
      <h2>Диагностика исполнения</h2>
      <button
        className="button secondary"
        onClick={() => setRevision((value) => value + 1)}
      >
        Обновить диагностику
      </button>
      {error && <Notice>{error}</Notice>}
      {!status && !error && <Loading />}
      {status && (
        <>
          <p>
            Просроченные задания: {status.expired_leases}. Самое долгое
            ожидание: {status.oldest_wait_seconds} с.
          </p>
          <p>
            Вызовы без точного usage: {status.unknown_usage}. Пауза OpenAI:{" "}
            {status.cooldown_seconds} с.
          </p>
          <p>Ошибки источников за всю историю: {status.source_errors}.</p>
          <h3>Сервисы с сигналом за последнюю минуту</h3>
          <ul>
            {Object.entries(status.services).map(([name, count]) => (
              <li key={name}>
                {services[name]}: {count}
              </li>
            ))}
          </ul>
          <h3>Очередь</h3>
          {status.queue.length === 0 ? (
            <p>Заданий пока нет.</p>
          ) : (
            <ul>
              {status.queue.map((item) => (
                <li key={`${item.kind}:${item.status}`}>
                  {item.kind} / {item.status}: {item.count}
                </li>
              ))}
            </ul>
          )}
          <h3>Состояния вызовов API за всю историю</h3>
          {Object.keys(status.usage_states).length === 0 ? (
            <p>Вызовов пока нет.</p>
          ) : (
            <ul>
              {Object.entries(status.usage_states).map(([name, count]) => (
                <li key={name}>
                  {name}: {count}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
