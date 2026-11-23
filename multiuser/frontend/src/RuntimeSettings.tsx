import { useEffect, useState } from "react";
import { errorMessage, request } from "./api";
import { Loading, Notice } from "./components";
import { Diagnostics } from "./Diagnostics";

type Usage = {
  day: string;
  daily_calls: number;
  used_calls: number;
  unknown_calls: number;
  tokens: number;
  cooldown_until: number;
};
type Schedule = {
  enabled: boolean;
  interval_seconds: number;
  next_run_at: number;
};
type Configuration = {
  limits: Record<string, number>;
  users: (Usage & { id: string; login: string })[];
};
const labels: Record<string, string> = {
  search_slots: "Одновременные поиски",
  chat_slots: "Одновременные чаты",
  document_slots: "Одновременные документы",
  per_user_documents: "Документы одного пользователя",
  api_slots: "Одновременные вызовы OpenAI",
  daily_calls: "Общий лимит вызовов в сутки",
  user_daily_calls: "Лимит пользователя по умолчанию",
};

export function RuntimeSettings({ admin }: { admin: boolean }) {
  const [usage, setUsage] = useState<Usage | null>(null);
  const [schedule, setSchedule] = useState<Schedule | null>(null);
  const [configuration, setConfiguration] = useState<Configuration | null>(
    null,
  );
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [diagnostics, setDiagnostics] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      request<Usage>("/usage", { signal: controller.signal }),
      request<Schedule>("/schedule", { signal: controller.signal }),
      admin
        ? request<Configuration>("/admin/runtime", {
            signal: controller.signal,
          })
        : Promise.resolve(null),
    ])
      .then(([nextUsage, nextSchedule, nextConfiguration]) => {
        if (controller.signal.aborted) return;
        setUsage(nextUsage);
        setSchedule(nextSchedule);
        setConfiguration(nextConfiguration);
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(errorMessage(failure));
      });
    return () => controller.abort();
  }, [admin, revision]);
  async function save(path: string, body: unknown) {
    if (busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await request(path, { method: "PUT", body });
      setNotice("Настройки исполнения сохранены.");
      setRevision((value) => value + 1);
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="runtime-settings" aria-label="Расписание и лимиты">
      {error && <Notice>{error}</Notice>}
      {notice && <Notice tone="info">{notice}</Notice>}
      {!usage || !schedule ? (
        <Loading />
      ) : (
        <>
          <h2>Использование OpenAI</h2>
          <p>
            Вызовы за {usage.day} (UTC): {usage.used_calls} из{" "}
            {usage.daily_calls}.
          </p>
          <p>
            Токены с известным расходом: {usage.tokens}. Вызовы без точного
            usage: {usage.unknown_calls}.
          </p>
          <p>
            Генерация, каждая проверка, исправление и чат учитываются отдельно.
            Неизвестный исход сохраняет резерв.
          </p>
          {usage.cooldown_until * 1000 > Date.now() && (
            <Notice tone="info">
              OpenAI запросил паузу. Задания ожидают доступности API.
            </Notice>
          )}
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void save("/schedule", {
                enabled: schedule.enabled,
                interval_seconds: schedule.interval_seconds,
              });
            }}
          >
            <h2>Ваше расписание поиска</h2>
            <label>
              <input
                type="checkbox"
                checked={schedule.enabled}
                onChange={(event) =>
                  setSchedule({ ...schedule, enabled: event.target.checked })
                }
              />
              Автоматический поиск
            </label>
            <label>
              Интервал поиска в минутах
              <input
                type="number"
                required
                min={15}
                max={1440}
                value={schedule.interval_seconds / 60}
                onChange={(event) =>
                  setSchedule({
                    ...schedule,
                    interval_seconds: Number(event.target.value) * 60,
                  })
                }
              />
            </label>
            <p>
              Следующий запуск:{" "}
              {new Date(schedule.next_run_at * 1000).toLocaleString()}. Нужны
              настроенный профиль и включённые источники.
            </p>
            <button className="button primary" disabled={busy}>
              Сохранить расписание
            </button>
          </form>
          {configuration && (
            <>
              <button
                className="button secondary"
                onClick={() => setDiagnostics((value) => !value)}
              >
                {diagnostics ? "Закрыть диагностику" : "Открыть диагностику"}
              </button>
              {diagnostics && <Diagnostics />}
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  void save("/admin/runtime", configuration.limits);
                }}
              >
                <h2>Общие ограничения исполнения</h2>
                {Object.entries(configuration.limits).map(([key, value]) => (
                  <label key={key}>
                    {labels[key]}
                    <input
                      type="number"
                      required
                      min={key.includes("calls") ? 0 : 1}
                      max={
                        key.includes("calls")
                          ? 1000000
                          : key === "per_user_documents"
                            ? 4
                            : 16
                      }
                      value={value}
                      onChange={(event) =>
                        setConfiguration({
                          ...configuration,
                          limits: {
                            ...configuration.limits,
                            [key]: Number(event.target.value),
                          },
                        })
                      }
                    />
                  </label>
                ))}
                <button className="button primary" disabled={busy}>
                  Сохранить общие лимиты
                </button>
              </form>
              {configuration.users.map((member) => (
                <form
                  key={member.id}
                  onSubmit={(event) => {
                    event.preventDefault();
                    void save(
                      `/admin/users/${encodeURIComponent(member.id)}/limits`,
                      { daily_calls: member.daily_calls },
                    );
                  }}
                >
                  <label>
                    Суточный лимит {member.login}
                    <input
                      type="number"
                      required
                      min={0}
                      max={1000000}
                      value={member.daily_calls}
                      onChange={(event) =>
                        setConfiguration({
                          ...configuration,
                          users: configuration.users.map((item) =>
                            item.id === member.id
                              ? {
                                  ...item,
                                  daily_calls: Number(event.target.value),
                                }
                              : item,
                          ),
                        })
                      }
                    />
                  </label>
                  <p>Зарезервировано и использовано: {member.used_calls}.</p>
                  <button className="button secondary" disabled={busy}>
                    Сохранить лимит {member.login}
                  </button>
                </form>
              ))}
            </>
          )}
        </>
      )}
    </section>
  );
}
