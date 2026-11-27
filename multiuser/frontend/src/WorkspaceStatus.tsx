import { useCallback, useState } from "react";
import { errorMessage, request } from "./api";
import { Loading, Notice } from "./components";
import { useRefreshEffect } from "./useRefreshEffect";

export type WorkspaceSummary = {
  setup: {
    search_configured: boolean;
    profile_version: number;
    enabled_sources: number;
    valid_sources: number;
    sources_version: number;
    template_ready: boolean;
    template_name: string | null;
  };
  queue: { queued: number; active: number; attention: number };
  usage: {
    daily_calls: number;
    used_calls: number;
    unknown_calls: number;
    cooldown_until: number;
  };
};
export type SetupPage = "criteria" | "sources" | "profile" | "usage";

export function WorkspaceStatus({
  revision,
  onNavigate,
  showSetup = true,
}: {
  revision: number;
  onNavigate: (page: SetupPage) => void;
  showSetup?: boolean;
}) {
  const [summary, setSummary] = useState<WorkspaceSummary | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const loadSummary = useCallback(async (signal: AbortSignal) => {
    try {
      const data = await request<WorkspaceSummary>("/workspace", { signal });
      if (signal.aborted) return;
      setSummary(data);
      setError("");
    } catch (failure) {
      if (!signal.aborted) setError(errorMessage(failure));
    }
  }, []);
  useRefreshEffect(loadSummary, revision + retry);
  const setup = summary?.setup;
  const sourcesReady =
    !!setup &&
    setup.enabled_sources > 0 &&
    setup.enabled_sources === setup.valid_sources;
  const complete =
    setup?.search_configured && sourcesReady && setup.template_ready;
  return (
    <section
      className="workspace-status"
      aria-label="Состояние личного кабинета"
    >
      {error && (
        <Notice>
          {error}
          <button
            className="text-button"
            onClick={() => setRetry((value) => value + 1)}
          >
            Повторить загрузку кабинета
          </button>
        </Notice>
      )}
      {!summary ? (
        !error && <Loading text="Загружаем состояние кабинета…" />
      ) : (
        <>
          <div
            className="queue-summary"
            aria-label="Личная очередь"
            aria-live="polite"
          >
            {summary.queue.queued > 0 && <span>Заданий в очереди: {summary.queue.queued}</span>}
            {summary.queue.active > 0 && <span>Заданий выполняется: {summary.queue.active}</span>}
            {summary.queue.attention > 0 && (
              <span>Требуют внимания: {summary.queue.attention}</span>
            )}
            <button className="text-button" onClick={() => onNavigate("usage")}>
              OpenAI за сутки: {summary.usage.used_calls} из{" "}
              {summary.usage.daily_calls}
            </button>
          </div>
          {summary.usage.used_calls >= summary.usage.daily_calls && (
            <Notice tone="info">
              Личный лимит OpenAI исчерпан. Поиск вакансий доступен; для новых
              документов обратитесь к администратору или дождитесь следующего
              дня UTC.
            </Notice>
          )}
          {summary.usage.cooldown_until * 1000 > Date.now() && (
            <Notice tone="info">
              OpenAI запросил паузу. Задания ожидают доступности API.
            </Notice>
          )}
          {showSetup && !complete && (
            <section className="setup-guide" aria-labelledby="setup-heading">
              <h2 id="setup-heading">Подготовьте личный кабинет</h2>
              <p>
                Укажите, что ищете, и подключите источники. Эталон CV нужен для
                создания документов; поиск можно начать без него.
              </p>
              <ol>
                <li>
                  <span>
                    {setup?.search_configured
                      ? "Критерии поиска сохранены"
                      : "Укажите желаемую работу"}
                  </span>
                  <button
                    className="button secondary"
                    onClick={() => onNavigate("criteria")}
                  >
                    Настроить критерии
                  </button>
                </li>
                <li>
                  <span>
                    {sourcesReady
                      ? `Источники готовы: ${setup?.enabled_sources}`
                      : setup?.enabled_sources
                        ? "Исправьте параметры включённых источников"
                        : "Подключите хотя бы один источник"}
                  </span>
                  <button
                    className="button secondary"
                    onClick={() => onNavigate("sources")}
                  >
                    Подключить источники
                  </button>
                </li>
                <li>
                  <span>
                    {setup?.template_ready
                      ? `Эталон подтверждён: ${setup.template_name}`
                      : "Загрузите CV и подтвердите факты"}
                  </span>
                  <button
                    className="button secondary"
                    onClick={() => onNavigate("profile")}
                  >
                    Открыть эталон CV
                  </button>
                </li>
              </ol>
            </section>
          )}
        </>
      )}
    </section>
  );
}
