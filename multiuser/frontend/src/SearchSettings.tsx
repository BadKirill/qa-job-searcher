import {
  useEffect,
  useId,
  useState,
  type InputHTMLAttributes,
  type SelectHTMLAttributes,
} from "react";
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
  ["role_keywords", "Названия ролей и синонимы", "обязательно, через запятую, например product designer, ux designer"],
  ["source_queries", "Запросы к агрегаторам (до 8)", "если пусто, используются первые 8 названий ролей"],
  ["excluded_title_keywords", "Исключения в названиях", "вакансии с этими словами в названии пропускаются, например lead, intern"],
  ["preferred_seniority", "Предпочтительные уровни", "совпадения повышают оценку, например middle, senior"],
  ["focus_qa_keywords", "Приоритетные направления", "совпадения повышают оценку, например design systems, ux research"],
  ["domain_keywords", "Отрасли", "совпадения повышают оценку, например fintech, education"],
  ["skill_keywords", "Навыки", "совпадения повышают оценку, например figma, prototyping"],
  ["allowed_locations", "Подходящие страны и регионы", "если пусто, география не ограничена этим списком"],
  ["blocked_locations", "Исключённые страны и регионы", "оставьте пустым, если исключений нет"],
  ["candidate_locations", "Где вы можете работать", "ваши страны и города для проверки ограничений работодателя"],
  ["hybrid_locations", "Города для гибридной работы", "укажите города, где готовы приезжать в офис, например berlin"],
] as const;
const workplace = [
  ["remote", "Удалённо", "работа вне офиса"],
  ["hybrid", "Гибрид", "часть времени в офисе"],
  ["on-site", "В офисе", "работа на месте"],
] as const;
const flags = [
  ["exclude_entry_level", "Исключать Junior и стажировки", "пропускать вакансии начального уровня"],
  ["qa_title_fallback", "Учитывать дополнительные QA-названия", "искать также по встроенному списку QA-ролей"],
  [
    "allow_unknown_remote_location",
    "Показывать удалённые вакансии с неясной географией",
    "включать, когда регион удалённой работы не удалось определить",
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
const boardKinds = ["greenhouse", "lever", "ashby"];
const hasSourceOptions = (kind: string) =>
  boardKinds.includes(kind) || kind === "telegram";
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

function FieldHint({ id, children }: { id: string; children: string }) {
  return <small className="field-hint" id={id}>({children})</small>;
}

function CriteriaInput({
  label,
  hint,
  ...input
}: InputHTMLAttributes<HTMLInputElement> & { label: string; hint: string }) {
  const id = useId();
  const control = (
    <input
      {...input}
      aria-labelledby={id}
      aria-describedby={`${id}-hint`}
      autoComplete="off"
      data-1p-ignore
    />
  );
  return (
    <label className={input.type === "checkbox" ? "checkbox-row" : undefined}>
      {input.type === "checkbox" && control}
      <span>
        <span id={id}>{label}</span>
        <FieldHint id={`${id}-hint`}>{hint}</FieldHint>
      </span>
      {input.type !== "checkbox" && control}
    </label>
  );
}

function SettingsSelect({
  label,
  hint,
  ...select
}: SelectHTMLAttributes<HTMLSelectElement> & { label: string; hint: string }) {
  const id = useId();
  return (
    <label>
      <span>
        <span id={id}>{label}</span>
        <FieldHint id={`${id}-hint`}>{hint}</FieldHint>
      </span>
      <span className="settings-select">
        <select {...select} aria-labelledby={id} aria-describedby={`${id}-hint`} />
      </span>
    </label>
  );
}

function matchingPreset(
  profile: SearchSpec | null,
  presets: Record<string, SearchSpec>,
) {
  if (!profile) return "";
  return (
    Object.keys(presets).find((name) =>
      (Object.keys(presets[name]) as (keyof SearchSpec)[]).every(
        (key) => JSON.stringify(presets[name][key]) === JSON.stringify(profile[key]),
      ),
    ) ?? ""
  );
}

type ProfileVersion = {
  version: number;
  active: boolean;
  profile: Partial<Omit<SearchSpec, "matching_mode">> & { require_remote?: boolean };
};

function ProfileHistory() {
  const [items, setItems] = useState<ProfileVersion[] | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    request<{ items: ProfileVersion[] }>("/search-profile/history", {
      signal: controller.signal,
    })
      .then((data) => {
        if (!controller.signal.aborted) {
          setItems(data.items);
          setError("");
        }
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(errorMessage(failure));
      });
    return () => controller.abort();
  }, [retry]);
  if (error) {
    return (
      <Notice>
        {error}
        <button
          type="button"
          className="text-button"
          onClick={() => {
            setError("");
            setRetry((value) => value + 1);
          }}
        >
          Повторить загрузку версий
        </button>
      </Notice>
    );
  }
  if (!items) return <Loading text="Загружаем версии профиля…" />;
  if (!items.length) return <p>Сохранённых версий пока нет.</p>;
  return (
    <ol className="profile-versions" role="list">
      {[...items].reverse().map(({ version, active, profile }) => (
        <li key={version} data-active={active}>
          <details>
            <summary>
              Версия {version}{active ? " · используется в поиске" : ""}
            </summary>
            <dl>
              {fields.map(([field, label]) => (
                <div key={field}>
                  <dt>{label}</dt>
                  <dd>{profile[field]?.join(", ") || "Пусто"}</dd>
                </div>
              ))}
              <div>
                <dt>Формат работы</dt>
                <dd>
                  {workplace
                    .filter(([mode]) => profile.workplace_types?.includes(mode))
                    .map(([, label]) => label)
                    .join(", ") ||
                    (profile.require_remote ? "Удалённо или допустимый гибрид" : "Любой")}
                </dd>
              </div>
              {flags.map(([field, label]) => (
                <div key={field}>
                  <dt>{label}</dt>
                  <dd>{profile[field] ? "Да" : "Нет"}</dd>
                </div>
              ))}
              <div>
                <dt>Минимальная оценка</dt>
                <dd>{profile.min_score ?? "—"} из 100</dd>
              </div>
            </dl>
          </details>
        </li>
      ))}
    </ol>
  );
}

export function SearchSettings({
  section = "all",
  onSection,
  onChange,
}: {
  section?: "all" | "criteria" | "sources";
  onSection?: (page: "search_settings" | "criteria" | "sources") => void;
  onChange?: () => void;
}) {
  const [page, setPage] = useState(section);
  useEffect(() => setPage(section), [section]);
  const choosePage = (value: "all" | "criteria" | "sources") => {
    setPage(value);
    onSection?.(value === "all" ? "search_settings" : value);
  };
  const [state, setState] = useState<Preferences | null>(null);
  const [types, setTypes] = useState<SourceType[]>([]);
  const [presets, setPresets] = useState<Record<string, SearchSpec>>({});
  const [form, setForm] = useState<SearchSpec>(emptySearch);
  const [terms, setTerms] = useState<Record<string, string>>({});
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [selectedKind, setKind] = useState("remotive");
  const [token, setToken] = useState("");
  const [name, setName] = useState("");
  const [region, setRegion] = useState("global");
  const [tags, setTags] = useState("");
  const [editing, setEditing] = useState<Source | null>(null);
  const [presetChoice, setPresetChoice] = useState<string | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const historyId = useId();
  const savedProfile = state?.search_profile.profile ?? null;
  const savedPreset = matchingPreset(savedProfile, presets);
  const preset = presetChoice ?? savedPreset;
  function receive(data: Preferences) {
    setState(data);
    setPresetChoice(null);
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
        setPresets(examples.items);
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
    onChange?.();
  }
  function resetSource() {
    setEditing(null);
    setToken("");
    setName("");
    setRegion("global");
    setTags("");
  }
  const sources = state?.sources.items ?? [];
  const connectedAggregators = new Set(
    sources
      .filter((source) => source.valid && !hasSourceOptions(source.kind))
      .map((source) => source.kind),
  );
  const kind = editing?.kind ?? (connectedAggregators.has(selectedKind) ? "" : selectedKind);
  const isBoard = boardKinds.includes(kind);
  const isChannel = kind === "telegram";
  const normalizedToken = token.normalize("NFKC").trim().toLowerCase();
  const duplicate = sources.find((source) => {
    if (!source.valid || source.kind !== kind || source.id === editing?.id) return false;
    if (isChannel) {
      return String(source.config.handle).toLowerCase() === normalizedToken.replace(/^@/, "");
    }
    return (
      isBoard &&
      String(source.config.token).toLowerCase() === normalizedToken &&
      (source.config.region ?? "global") === region
    );
  });
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
          <p className="muted">Ваши критерии и подключения. Значения в списках разделяйте запятыми.</p>
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
              <details
                className="profile-history"
                open={historyOpen}
                onToggle={(event) => setHistoryOpen(event.currentTarget.open)}
              >
                <summary aria-labelledby={historyId} aria-describedby={`${historyId}-status`}>
                  <span className="profile-history-heading">
                    <strong id={historyId}>История версий профиля</strong>
                    <span id={`${historyId}-status`}>
                      {state.search_profile.configured
                        ? `В поиске используется версия ${state.search_profile.version}`
                        : "Здесь появятся ваши сохранённые критерии"}
                    </span>
                  </span>
                </summary>
                <div className="profile-history-content">
                  <p className="muted">Каждое сохранение или применение примера создаёт новую версию. Откройте версию, чтобы посмотреть её критерии.</p>
                  {historyOpen && <ProfileHistory key={state.search_profile.version} />}
                </div>
              </details>
              <fieldset className="settings-presets" disabled={busy}>
                <legend>Примеры для начала</legend>
                <p>
                  Применение заменит профиль и подключит недостающие агрегаторы
                  из набора пяти. Ваши доски, каналы и состояние подключений
                  сохранятся.
                </p>
                <SettingsSelect
                  label="Пример профиля"
                  hint="выберите пример, затем нажмите «Применить пример»"
                  value={preset}
                  onChange={(event) => setPresetChoice(event.target.value)}
                >
                  <option value="">Выберите пример</option>
                  {Object.keys(presets).map((id) => (
                    <option key={id} value={id}>{labels[id] ?? id}</option>
                  ))}
                </SettingsSelect>
                <button
                  type="button"
                  className="button secondary"
                  disabled={!preset}
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
                        "Пример применён. Критерии сохранены в вашем аккаунте.",
                      );
                      onChange?.();
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
                    receive({ ...state, search_profile: saved });
                    setNotice("Профиль поиска сохранён.");
                    onChange?.();
                  });
                }}
              >
                <fieldset className="criteria-fields" disabled={busy}>
                  <legend>Профиль поиска</legend>
                  <p className="muted">
                    Критерии сохраняются в вашем аккаунте. Для поиска используется последняя сохранённая версия.
                    Пустые необязательные поля оставляют соответствующий критерий без дополнительных настроек.
                  </p>
                  {fields.map(([field, label, hint]) => (
                    <CriteriaInput
                      key={field}
                      label={label}
                      hint={hint}
                      required={field === "role_keywords"}
                      value={terms[field] ?? ""}
                      onChange={(event) =>
                        setTerms({ ...terms, [field]: event.target.value })
                      }
                    />
                  ))}
                  <p className="muted">Формат работы <small className="field-hint">(выберите хотя бы один)</small></p>
                  {workplace.map(([mode, label, hint]) => (
                    <CriteriaInput
                      key={mode}
                      label={label}
                      hint={hint}
                      type="checkbox"
                      checked={form.workplace_types.includes(mode)}
                      onChange={(event) =>
                        setForm({
                          ...form,
                          workplace_types: event.target.checked
                            ? [...form.workplace_types, mode]
                            : form.workplace_types.filter((value) => value !== mode),
                        })
                      }
                    />
                  ))}
                  {flags
                    .filter(
                      ([field]) =>
                        field !== "qa_title_fallback" ||
                        form.qa_title_fallback ||
                        preset === "qa" ||
                        /\bqa\b|тестиров/i.test(terms.role_keywords ?? ""),
                    )
                    .map(([field, label, hint]) => (
                      <CriteriaInput
                        key={field}
                        label={label}
                        hint={hint}
                        type="checkbox"
                        checked={form[field]}
                        onChange={(event) =>
                          setForm({ ...form, [field]: event.target.checked })
                        }
                      />
                    ))}
                  <CriteriaInput
                    label="Минимальная оценка"
                    hint="от 0 до 100, вакансии с более низкой оценкой пропускаются"
                    type="number"
                    min="0"
                    max="100"
                    required
                    value={form.min_score}
                    onChange={(event) =>
                      setForm({ ...form, min_score: Number(event.target.value) })
                    }
                  />
                  <button className="button primary" type="submit">
                    Сохранить профиль поиска
                  </button>
                </fieldset>
              </form>
            </>
          )}
          {page !== "criteria" && (
            <>
              <h2>Мои источники</h2>
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
                  if (!kind || duplicate) return;
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
                  <SettingsSelect
                    label="Тип источника"
                    hint={
                      types.find((type) => type.kind === kind)?.limitations ??
                      "управлять подключёнными агрегаторами можно в списке «Мои источники»"
                    }
                    value={kind}
                    disabled={!!editing}
                    onChange={(event) => {
                      setKind(event.target.value);
                      setRegion("global");
                    }}
                  >
                    <option value="" disabled>
                      Выберите источник
                    </option>
                    {types.map((type) => {
                      const connected = !editing && connectedAggregators.has(type.kind);
                      return (
                        <option key={type.kind} value={type.kind} disabled={connected}>
                          {type.name}{connected && " · уже подключён"}
                        </option>
                      );
                    })}
                  </SettingsSelect>
                  {(isBoard || isChannel) && (
                    <CriteriaInput
                      label={
                        isChannel ? "Handle публичного канала" : "Идентификатор доски"
                      }
                      hint={
                        isChannel
                          ? "имя из ссылки t.me/jobs_channel, например jobs_channel"
                          : "часть адреса доски после домена, например company"
                      }
                      required
                      value={token}
                      onChange={(event) => setToken(event.target.value)}
                      placeholder={isChannel ? "jobs_channel" : "company"}
                    />
                  )}
                  {isBoard && (
                    <>
                      <CriteriaInput
                        label="Название подключения"
                        hint="необязательно, например Доска Acme"
                        value={name}
                        onChange={(event) => setName(event.target.value)}
                      />
                      <CriteriaInput
                        label="Теги источника"
                        hint="необязательно, через запятую, например fintech, remote"
                        value={tags}
                        onChange={(event) => setTags(event.target.value)}
                      />
                    </>
                  )}
                  {kind === "lever" && (
                    <SettingsSelect
                      label="Регион API"
                      hint="EU для jobs.eu.lever.co, Global для jobs.lever.co"
                      value={region}
                      onChange={(event) => setRegion(event.target.value)}
                    >
                      <option value="global">Global</option>
                      <option value="eu">EU</option>
                    </SettingsSelect>
                  )}
                  {duplicate && (
                    <Notice tone="info">
                      Этот источник уже есть в списке «Мои источники» как «{duplicate.name}».
                    </Notice>
                  )}
                  <button
                    className="button primary"
                    type="submit"
                    disabled={!kind || !!duplicate}
                  >
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
