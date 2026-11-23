import { useEffect, useState } from "react";
import { errorMessage, request } from "./api";
import { Loading, Notice } from "./components";

export type SearchSpec = {
  role_keywords: string[];
  source_queries: string[];
  excluded_title_keywords: string[];
  preferred_seniority: string[];
  focus_qa_keywords: string[];
  domain_keywords: string[];
  skill_keywords: string[];
  allowed_locations: string[];
  blocked_locations: string[];
  candidate_locations: string[];
  hybrid_locations: string[];
  workplace_types: string[];
  exclude_entry_level: boolean;
  qa_title_fallback: boolean;
  allow_unknown_remote_location: boolean;
  min_score: number;
  matching_mode: "universal";
};
export type Source = {
  id: string;
  kind: string;
  enabled: boolean;
  version: number;
  config: Record<string, unknown>;
  name: string;
  valid: boolean;
  limitations: string;
};
export type Preferences = {
  search_profile: {
    version: number;
    configured: boolean;
    profile: SearchSpec | null;
  };
  sources: { version: number; items: Source[] };
};
type SourceType = { kind: string; name: string; limitations: string };
const fields = [
  ["role_keywords", "Названия ролей и синонимы"],
  ["source_queries", "Запросы к агрегаторам (до 8)"],
  ["excluded_title_keywords", "Исключения в названиях"],
  ["preferred_seniority", "Предпочтительные уровни"],
  ["focus_qa_keywords", "Приоритетные направления"],
  ["domain_keywords", "Отрасли"],
  ["skill_keywords", "Навыки"],
  ["allowed_locations", "Подходящие страны и регионы"],
  ["blocked_locations", "Исключённые страны и регионы"],
  ["candidate_locations", "Где вы можете работать"],
  ["hybrid_locations", "Города для гибридной работы"],
] as const;
const flags = [
  ["exclude_entry_level", "Исключать Junior и стажировки"],
  ["qa_title_fallback", "Учитывать дополнительные QA-названия"],
  [
    "allow_unknown_remote_location",
    "Показывать удалённые вакансии с неясной географией",
  ],
] as const;
export const emptySearch: SearchSpec = {
  role_keywords: [],
  source_queries: [],
  excluded_title_keywords: [],
  preferred_seniority: [],
  focus_qa_keywords: [],
  domain_keywords: [],
  skill_keywords: [],
  allowed_locations: [],
  blocked_locations: [],
  candidate_locations: [],
  hybrid_locations: [],
  workplace_types: ["remote"],
  exclude_entry_level: false,
  qa_title_fallback: false,
  allow_unknown_remote_location: false,
  min_score: 35,
  matching_mode: "universal",
};
const labels: Record<string, string> = {
  qa: "QA · удалённо и гибрид в Сербии",
  developer: "Разработка",
  designer: "Дизайн",
};
const reasons: Record<string, string> = {
  redirect: "Источник перенаправляет запрос. Автоматический переход запрещён.",
  rate_limited: "Источник временно ограничил запросы.",
  http_error: "Источник вернул ошибку HTTP.",
  unavailable: "Источник не ответил вовремя.",
  invalid_response: "Ответ не соответствует формату источника.",
  blocked_address: "Адрес не разрешён.",
  blocked_resolution: "Адрес сети не разрешён.",
  response_limit: "Ответ превышает ограничение размера.",
  request_limit: "Превышен предел запросов.",
  unsupported_encoding: "Неподдерживаемое сжатие ответа.",
};
export function SearchSettings({
  section = "all",
  onSection,
}: {
  section?: "all" | "criteria" | "sources";
  onSection?: (page: "search_settings" | "criteria" | "sources") => void;
}) {
  const [page, setPage] = useState(section);
  useEffect(() => setPage(section), [section]);
  const choosePage = (value: "all" | "criteria" | "sources") => {
    setPage(value);
    onSection?.(value === "all" ? "search_settings" : value);
  };
  const [state, setState] = useState<Preferences | null>(null);
  const [types, setTypes] = useState<SourceType[]>([]);
  const [presets, setPresets] = useState<string[]>([]);
  const [form, setForm] = useState<SearchSpec>(emptySearch);
  const [terms, setTerms] = useState<Record<string, string>>({});
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [kind, setKind] = useState("remotive");
  const [token, setToken] = useState("");
  const [name, setName] = useState("");
  const [region, setRegion] = useState("global");
  const [tags, setTags] = useState("");
  const [editing, setEditing] = useState<Source | null>(null);
  const [preset, setPreset] = useState("developer");
  function receive(data: Preferences) {
    setState(data);
    const profile = data.search_profile.profile ?? emptySearch;
    setForm(profile);
    setTerms(
      Object.fromEntries(
        fields.map(([field]) => [field, profile[field].join(", ")]),
      ),
    );
  }
  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      request<Preferences>("/search-settings", { signal: controller.signal }),
      request<{ items: SourceType[] }>("/source-types", {
        signal: controller.signal,
      }),
      request<{ items: Record<string, SearchSpec> }>("/search-presets", {
        signal: controller.signal,
      }),
    ])
      .then(([data, catalog, examples]) => {
        if (controller.signal.aborted) return;
        receive(data);
        setTypes(catalog.items);
        setPresets(Object.keys(examples.items));
        setError("");
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(errorMessage(failure));
      });
    return () => controller.abort();
  }, [revision]);
  async function act(operation: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await operation();
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  }
  async function reloadSources() {
    const sources = await request<Preferences["sources"]>("/sources");
    setState((previous) => previous && { ...previous, sources });
  }
  function resetSource() {
    setEditing(null);
    setToken("");
    setName("");
    setRegion("global");
    setTags("");
  }
  const isBoard = ["greenhouse", "lever", "ashby"].includes(kind);
  const isChannel = kind === "telegram";
  return (
    <section className="search-settings">
      <header className="page-heading">
        <div>
          <p className="eyebrow">ЛИЧНЫЙ ПОИСК</p>
          <h1>
            {page === "criteria"
              ? "Что ищу"
              : page === "sources"
                ? "Источники"
                : "Поиск и источники"}
          </h1>
          <p className="muted">Ваши критерии и подключения. Списки вводятся через запятую; без ограничений по профессии.</p>
        </div>
      </header>
      <nav className="settings-sections" aria-label="Разделы поиска">
        <button
          className="button secondary"
          aria-pressed={page === "criteria"}
          onClick={() => choosePage("criteria")}
        >
          Что ищу
        </button>
        <button
          className="button secondary"
          aria-pressed={page === "sources"}
          onClick={() => choosePage("sources")}
        >
          Источники
        </button>
        <button
          className="button secondary"
          aria-pressed={page === "all"}
          onClick={() => choosePage("all")}
        >
          Все настройки поиска
        </button>
      </nav>
      {error && (
        <Notice>
          {error}
          <button
            className="text-button"
            onClick={() => setRevision((value) => value + 1)}
          >
            Загрузить актуальные настройки
          </button>
        </Notice>
      )}
      {notice && <Notice tone="info">{notice}</Notice>}
      {!state ? (
        <Loading text="Загружаем настройки поиска…" />
      ) : (
        <>
          {!state.search_profile.configured && (
            <Notice tone="info">
              Профиль поиска ещё не настроен. Добавьте свои критерии и источники
              или явно примените пример.
            </Notice>
          )}
          {page !== "sources" && (
            <>
              <fieldset className="settings-presets" disabled={busy}>
                <legend>Примеры для начала</legend>
                <p>
                  Применение заменит профиль и добавит пять общих агрегаторов.
                  Ваши доски и каналы сохранятся, выключенные источники
                  останутся выключенными.
                </p>
                <label>
                  Пример профиля
                  <select
                    value={preset}
                    onChange={(event) => setPreset(event.target.value)}
                  >
                    {presets.map((id) => (
                      <option key={id} value={id}>
                        {labels[id]}
                      </option>
                    ))}
                  </select>
                </label>
                <button
                  className="button secondary"
                  onClick={() =>
                    void act(async () => {
                      receive(
                        await request<Preferences>(
                          `/search-presets/${preset}/apply`,
                          {
                            method: "POST",
                            body: {
                              expected_version: state.search_profile.version,
                              expected_sources_version: state.sources.version,
                            },
                          },
                        ),
                      );
                      setNotice(
                        "Пример применён. Проверьте критерии перед поиском.",
                      );
                    })
                  }
                >
                  Применить пример
                </button>
              </fieldset>
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  void act(async () => {
                    const profile = {
                      ...form,
                      ...Object.fromEntries(
                        fields.map(([field]) => [
                          field,
                          (terms[field] ?? "")
                            .split(",")
                            .map((value) => value.trim())
                            .filter(Boolean),
                        ]),
                      ),
                    };
                    const saved = await request<Preferences["search_profile"]>(
                      "/search-profile",
                      {
                        method: "PUT",
                        body: {
                          expected_version: state.search_profile.version,
                          profile,
                        },
                      },
                    );
                    setState({ ...state, search_profile: saved });
                    setNotice("Профиль поиска сохранён.");
                  });
                }}
              >
                <fieldset className="criteria-fields" disabled={busy}>
                  <legend>
                    Профиль поиска · версия {state.search_profile.version}
                  </legend>
                  {fields.map(([field, label]) => (
                    <label key={field}>
                      {label}
                      <input
                        required={field === "role_keywords"}
                        value={terms[field] ?? ""}
                        onChange={(event) =>
                          setTerms({ ...terms, [field]: event.target.value })
                        }
                      />
                    </label>
                  ))}
                  <p>
                    Пустая география допускает любые страны. Указывайте страну
                    кандидата для проверки явных исключений работодателя.
                  </p>
                  {(
                    [
                      ["remote", "Удалённо"],
                      ["hybrid", "Гибрид"],
                      ["on-site", "В офисе"],
                    ] as const
                  ).map(([mode, label]) => (
                    <label key={mode} className="checkbox-row">
                      <input
                        type="checkbox"
                        checked={form.workplace_types.includes(mode)}
                        onChange={(event) =>
                          setForm({
                            ...form,
                            workplace_types: event.target.checked
                              ? [...form.workplace_types, mode]
                              : form.workplace_types.filter(
                                  (value) => value !== mode,
                                ),
                          })
                        }
                      />
                      {label}
                    </label>
                  ))}
                  {flags
                    .filter(
                      ([field]) =>
                        field !== "qa_title_fallback" ||
                        form.qa_title_fallback ||
                        preset === "qa" ||
                        /\bqa\b|тестиров/i.test(terms.role_keywords ?? ""),
                    )
                    .map(([field, label]) => (
                      <label key={field} className="checkbox-row">
                        <input
                          type="checkbox"
                          checked={form[field]}
                          onChange={(event) =>
                            setForm({ ...form, [field]: event.target.checked })
                          }
                        />
                        {label}
                      </label>
                    ))}
                  <label>
                    Минимальная оценка
                    <input
                      type="number"
                      min="0"
                      max="100"
                      required
                      value={form.min_score}
                      onChange={(event) =>
                        setForm({
                          ...form,
                          min_score: Number(event.target.value),
                        })
                      }
                    />
                  </label>
                  <button className="button primary" type="submit">
                    Сохранить профиль поиска
                  </button>
                </fieldset>
              </form>
            </>
          )}
          {page !== "criteria" && (
            <>
              <h2>Мои источники · версия {state.sources.version}</h2>
              {state.sources.items.length === 0 && (
                <p>Источники пока не добавлены.</p>
              )}
              <ul className="source-list">
                {state.sources.items.map((source) => (
                  <li key={source.id}>
                    <h3>{source.name}</h3>
                    <p>{source.limitations}</p>
                    {!source.valid && (
                      <Notice>Исправьте параметры подключения.</Notice>
                    )}
                    <label className="checkbox-row">
                      <input
                        type="checkbox"
                        aria-label={`Включить ${source.name}`}
                        checked={source.enabled}
                        disabled={busy}
                        onChange={(event) =>
                          void act(async () => {
                            await request(`/sources/${source.id}`, {
                              method: "PATCH",
                              body: {
                                expected_version: source.version,
                                enabled: event.target.checked,
                              },
                            });
                            await reloadSources();
                          })
                        }
                      />
                      Включён
                    </label>
                    <div className="source-actions">
                      <button
                        className="button secondary"
                        aria-label={`Изменить ${source.name}`}
                        disabled={busy}
                        onClick={() => {
                          setEditing(source);
                          setKind(source.kind);
                          setToken(
                            String(
                              source.config.token ?? source.config.handle ?? "",
                            ),
                          );
                          setName(String(source.config.name ?? ""));
                          setRegion(String(source.config.region ?? "global"));
                          setTags(
                            ((source.config.tags ?? []) as string[]).join(", "),
                          );
                        }}
                      >
                        Изменить
                      </button>
                      <button
                        className="button secondary"
                        aria-label={`Проверить ${source.name}`}
                        disabled={busy || !source.valid}
                        onClick={() =>
                          void act(async () => {
                            const result = await request<{
                              status: string;
                              items: number;
                              reason: string | null;
                              stale: boolean;
                            }>(`/sources/${source.id}/check`, {
                              method: "POST",
                              body: { expected_version: source.version },
                            });
                            setNotice(
                              result.stale
                                ? "Настройки изменились во время проверки. Повторите её."
                                : result.status === "ok"
                                  ? `Источник доступен. Объявлений в проверенном ответе: ${result.items}. Это не результат поиска.`
                                  : result.status === "empty"
                                    ? "Источник доступен, проверенный ответ пуст."
                                    : (reasons[result.reason ?? ""] ??
                                      "Не удалось проверить источник."),
                            );
                          })
                        }
                      >
                        Проверить
                      </button>
                      <button
                        className="button secondary danger-button"
                        aria-label={`Удалить ${source.name}`}
                        disabled={busy}
                        onClick={() =>
                          void act(async () => {
                            await request(`/sources/${source.id}`, {
                              method: "DELETE",
                              body: { expected_version: source.version },
                            });
                            if (editing?.id === source.id) resetSource();
                            await reloadSources();
                          })
                        }
                      >
                        Удалить
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  void act(async () => {
                    const config = isChannel
                      ? { handle: token }
                      : isBoard
                        ? {
                            token,
                            name: name || token,
                            region,
                            tags: tags
                              .split(",")
                              .map((value) => value.trim())
                              .filter(Boolean),
                          }
                        : {};
                    await request(
                      editing ? `/sources/${editing.id}` : "/sources",
                      {
                        method: editing ? "PATCH" : "POST",
                        body: editing
                          ? { expected_version: editing.version, config }
                          : { kind, config },
                      },
                    );
                    resetSource();
                    await reloadSources();
                    setNotice("Источник сохранён.");
                  });
                }}
              >
                <fieldset disabled={busy}>
                  <legend>
                    {editing ? "Изменить подключение" : "Добавить источник"}
                  </legend>
                  <label>
                    Тип источника
                    <select
                      value={kind}
                      disabled={!!editing}
                      onChange={(event) => {
                        setKind(event.target.value);
                        setRegion("global");
                      }}
                    >
                      {types.map((type) => (
                        <option key={type.kind} value={type.kind}>
                          {type.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <p>{types.find((type) => type.kind === kind)?.limitations}</p>
                  {(isBoard || isChannel) && (
                    <label>
                      {isChannel
                        ? "Handle публичного канала"
                        : "Идентификатор доски"}
                      <input
                        required
                        value={token}
                        onChange={(event) => setToken(event.target.value)}
                        placeholder={isChannel ? "jobs_channel" : "company"}
                      />
                    </label>
                  )}
                  {isBoard && (
                    <>
                      <label>
                        Название подключения
                        <input
                          value={name}
                          onChange={(event) => setName(event.target.value)}
                        />
                      </label>
                      <label>
                        Теги источника
                        <input
                          value={tags}
                          onChange={(event) => setTags(event.target.value)}
                        />
                      </label>
                    </>
                  )}
                  {kind === "lever" && (
                    <label>
                      Регион API
                      <select
                        value={region}
                        onChange={(event) => setRegion(event.target.value)}
                      >
                        <option value="global">Global</option>
                        <option value="eu">EU</option>
                      </select>
                    </label>
                  )}
                  <button className="button primary" type="submit">
                    {editing ? "Сохранить подключение" : "Добавить источник"}
                  </button>
                  {editing && (
                    <button
                      type="button"
                      className="button secondary"
                      onClick={resetSource}
                    >
                      Отменить изменение
                    </button>
                  )}
                </fieldset>
              </form>
            </>
          )}
        </>
      )}
    </section>
  );
}
