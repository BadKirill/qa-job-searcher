import {
  useEffect,
  useId,
  useRef,
  useState,
  type InputHTMLAttributes,
  type SelectHTMLAttributes,
} from "react";
import { ApiError, errorMessage, request } from "./api";
import { formatCreatedAt, Loading, Notice } from "./components";
import { PlacePicker } from "./PlacePicker";
import { SearchSources, type SourceType } from "./SearchSources";

export type SearchSpec = {
  remote_place_ids?: string[] | null;
  hybrid_place_ids?: string[] | null;
  office_place_ids?: string[] | null;
  preferred_place_ids?: string[];
  excluded_place_ids?: string[];
  availability?: { zone: string; start: string; end: string } | null;
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
  working_languages: ("ru" | "en")[];
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
  builtin?: boolean;
};
export type Preferences = {
  search_profile: {
    version: number;
    editor_version?: number;
    generation?: number;
    name?: string;
    variant_id?: string;
    saved_version?: number;
    saved_variant_id?: string;
    saved_name?: string;
    configured: boolean;
    profile: SearchSpec | null;
  };
  sources: { version: number; items: Source[] };
};
const fields = [
  ["role_keywords", "Названия ролей и синонимы", "обязательно, через запятую, например product designer, ux designer"],
  ["source_queries", "Запросы для сайтов вакансий", "до восьми отдельных фраз, например qa engineer, manual tester. Если поле пустое, используем первые восемь названий ролей"],
  ["excluded_title_keywords", "Исключения в названиях", "вакансии с этими словами в названии пропускаются, например lead, intern"],
  ["preferred_seniority", "Предпочтительные уровни", "совпадения повышают оценку, например middle, senior"],
  ["focus_qa_keywords", "Приоритетные направления", "совпадения повышают оценку, например design systems, ux research"],
  ["domain_keywords", "Отрасли", "совпадения повышают оценку, например fintech, education"],
  ["skill_keywords", "Навыки и инструменты", "совпадения повышают оценку, например figma, prototyping"],
  ["allowed_locations", "Предпочтения по локации вакансии", "если пусто, география не ограничена этим списком"],
  ["blocked_locations", "Исключения по локации вакансии", "оставьте пустым, если исключений нет"],
  ["candidate_locations", "Откуда могу работать", "ваши страны и города для проверки ограничений работодателя"],
  ["hybrid_locations", "Города для гибридной работы", "укажите города, где готовы приезжать в офис, например berlin"],
] as const;
const workplace = [
  ["remote", "Удалённо", "работа вне офиса", "remote_place_ids", "Откуда могу работать удалённо", "country,city,region,state"],
  ["hybrid", "Гибрид", "часть времени в офисе", "hybrid_place_ids", "Где готов ездить в офис при гибриде", "country,city"],
  ["on-site", "В офисе", "работа на месте", "office_place_ids", "Где готов работать в офисе", "country,city"],
] as const;
const workingLanguages = [
  ["ru", "Русскоязычные"],
  ["en", "Англоязычные"],
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
  working_languages: [],
  exclude_entry_level: false,
  qa_title_fallback: false,
  allow_unknown_remote_location: false,
  min_score: 35,
  matching_mode: "universal",
};
type SearchTemplates = { items: Record<string, SearchSpec>; labels?: Record<string, string> };
function FieldHint({ id, children }: { id: string; children: string }) {
  return <small className="field-hint" id={id}>({children})</small>;
}

function CriteriaInput({
  label,
  hint,
  issue,
  ...input
}: InputHTMLAttributes<HTMLInputElement> & { label: string; hint: string; issue?: string }) {
  const id = useId();
  const control = (
    <input
      {...input}
      aria-labelledby={id}
      aria-describedby={`${id}-hint${issue ? ` ${id}-issue` : ""}`}
      aria-invalid={issue ? true : undefined}
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
      {issue && <small id={`${id}-issue`} role="alert">{issue}</small>}
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

type ProfileVersion = {
  version: number;
  variant_id?: string;
  name?: string;
  active: boolean;
  deleted?: boolean;
  created_at?: string;
  profile: Partial<SearchSpec> & { require_remote?: boolean };
  editable_profile?: SearchSpec;
};

type VariantAction = "activate" | "rename" | "delete";

const geographicFields = [
  ["preferred_place_ids", "Предпочтения по локации вакансии", "country,city,region,state"],
  ["excluded_place_ids", "Исключения по локации вакансии", "country,city,region,state"],
] as const;

function ProfileHistory({ onLoad, onAction }: {
  onLoad: (item: ProfileVersion) => void;
  onAction: (item: ProfileVersion, action: VariantAction) => void;
}) {
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
      {[...items].reverse().filter((item) => !item.deleted).map((item) => {
        const { version, active, profile, name, created_at } = item;
        return (
        <li key={version} data-active={active} data-version={version}>
          <details>
            <summary>
              {name ?? `Версия ${version}`}{active ? " · используется в поиске" : ""}
              {created_at && <small> · ревизия {version} · {formatCreatedAt(created_at)}</small>}
            </summary>
            <p className="version-hint">Откройте критерии этой версии для правок или переключите поиск на неё.</p>
            <div className="source-actions">
              <button type="button" className="button secondary" onClick={() => onLoad(item)}>Редактировать эту версию</button>
              <button type="button" className="button secondary" disabled={active}
                onClick={() => onAction(item, "activate")}>Использовать в поиске</button>
              <button type="button" className="button secondary"
                onClick={() => onAction(item, "rename")}>Переименовать</button>
              <button type="button" className="button secondary"
                disabled={items.some((entry) => entry.active && (entry.variant_id ?? String(entry.version)) === (item.variant_id ?? String(item.version)))}
                onClick={() => onAction(item, "delete")}>Удалить вариант</button>
            </div>
            <dl>
              {fields.filter(([field]) => profile[field]?.length).map(([field, label]) => (
                <div key={field}>
                  <dt>{label}</dt>
                  <dd>{profile[field]?.join(", ")}</dd>
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
              <div>
                <dt>Рабочий язык</dt>
                <dd>
                  {workingLanguages
                    .filter(([language]) => profile.working_languages?.includes(language))
                    .map(([, label]) => label)
                    .join(", ") || "Любой"}
                </dd>
              </div>
              {flags.map(([field, label]) => (
                <div key={field}>
                  <dt>{label}</dt>
                  <dd>{profile[field] ? "Да" : "Нет"}</dd>
                </div>
              ))}
              <div>
                <dt>Порог совпадения с критериями</dt>
                <dd>{profile.min_score ?? "—"} из 100</dd>
              </div>
            </dl>
          </details>
        </li>
        );
      })}
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
  const [presets, setPresets] = useState<SearchTemplates>({ items: {} });
  const [form, setForm] = useState<SearchSpec>(emptySearch);
  const [terms, setTerms] = useState<Record<string, string>>({});
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [presetChoice, setPresetChoice] = useState<string | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const historyId = useId();
  const [edited, setEdited] = useState<{ id: string; name: string } | null>(null);
  const [baseline, setBaseline] = useState("");
  const [saveName, setSaveName] = useState("");
  const [showNew, setShowNew] = useState(false);
  const [rename, setRename] = useState<ProfileVersion | null>(null);
  const [replaceDraft, setReplaceDraft] = useState<(() => void) | null>(null);
  const [removing, setRemoving] = useState<ProfileVersion | null>(null);
  const [criteriaNotice, setCriteriaNotice] = useState("");
  const [legacyChoices, setLegacyChoices] = useState<Record<string, { id: string; label: string }[]> | null>(null);
  const [accountConflict, setAccountConflict] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const criteriaForm = useRef<HTMLFormElement>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const preset = presetChoice ?? "";
  const draft = JSON.stringify([form, terms]);
  const dirty = !!baseline && baseline !== draft;
  function loadDraft(profile: SearchSpec, markSaved: boolean) {
    const updatedTerms = Object.fromEntries(fields.map(([field]) => [field, profile[field]?.join(", ") ?? ""]));
    setForm(profile);
    setTerms(updatedTerms);
    if (markSaved) setBaseline(JSON.stringify([profile, updatedTerms]));
  }
  function receive(data: Preferences) {
    setState(data);
    const active = data.search_profile;
    setEdited(active.variant_id ? { id: active.variant_id, name: active.name ?? "" } : null);
    setPresetChoice(null);
    loadDraft(active.profile ?? emptySearch, true);
  }
  useEffect(() => {
    if (showNew || rename || replaceDraft || removing) dialog.current?.showModal();
    else dialog.current?.close();
  }, [showNew, rename, replaceDraft, removing]);
  function closeDialog() {
    setShowNew(false); setRename(null); setReplaceDraft(null); setRemoving(null); setSaveName("");
  }
  function replaceIfConfirmed(operation: () => void) {
    if (dirty) setReplaceDraft(() => operation);
    else operation();
  }
  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      request<Preferences>("/search-settings", { signal: controller.signal }),
      request<{ items: SourceType[] }>("/source-types", {
        signal: controller.signal,
      }),
      request<SearchTemplates>("/search-presets", {
        signal: controller.signal,
      }),
    ])
      .then(([data, catalog, examples]) => {
        if (controller.signal.aborted) return;
        receive(data);
        setTypes(catalog.items);
        setPresets(examples);
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
    setAccountConflict(false);
    setFieldErrors({});
    try {
      await operation();
    } catch (failure) {
      setError(errorMessage(failure));
      setAccountConflict(failure instanceof ApiError && failure.status === 409);
      if (failure instanceof ApiError && Object.keys(failure.fields).length) {
        setFieldErrors(failure.fields);
        const input = criteriaForm.current && Array.from(criteriaForm.current.elements)
          .find((element): element is HTMLInputElement => element instanceof HTMLInputElement && !!failure.fields[element.name]);
        if (input) {
          const options = input.closest("details");
          if (options) options.open = true;
          requestAnimationFrame(() => input.focus());
        }
      }
    } finally {
      setBusy(false);
    }
  }
  async function saveProfile(create = false) {
    if (!state) return;
    await act(async () => {
      setCriteriaNotice("");
      const profile = {
        ...form,
        ...Object.fromEntries(fields.map(([field]) => [
          field, (terms[field] ?? "").split(",").map((value) => value.trim()).filter(Boolean),
        ])),
      };
      const saved = await request<Preferences["search_profile"]>("/search-profile", {
        method: "PUT",
        body: {
          expected_version: state.search_profile.version,
          expected_editor_version: state.search_profile.editor_version ?? 0,
          variant_id: create ? null : edited?.id,
          name: create ? saveName.trim() : undefined,
          create,
          profile,
        },
      });
      setState({ ...state, search_profile: saved });
      if (saved.saved_variant_id) {
        setEdited({ id: saved.saved_variant_id, name: saved.saved_name ?? "" });
      }
      setBaseline(draft);
      setCriteriaNotice(`Сохранено · ${saved.saved_name ?? edited?.name ?? saved.name ?? "Профиль поиска"}`);
      closeDialog();
      onChange?.();
    });
  }
  function editRevision(item: ProfileVersion) {
    replaceIfConfirmed(() => {
      const { require_remote: formerMode, ...rawProfile } = item.profile;
      const profile = item.editable_profile ?? rawProfile;
      loadDraft({
        ...emptySearch,
        ...profile,
        workplace_types: profile.workplace_types ?? (formerMode ? ["remote", "hybrid"] : ["remote", "hybrid", "on-site"]),
      }, true);
      setEdited({ id: item.variant_id ?? String(item.version), name: item.name ?? `Версия ${item.version}` });
      setPresetChoice(null);
    });
  }
  async function versionAction(item: ProfileVersion, action: VariantAction) {
    if (!state) return;
    await act(async () => {
      const saved = await request<Preferences["search_profile"]>(`/search-profile/history/${item.version}`, {
        method: "POST",
        body: {
          expected_editor_version: state.search_profile.editor_version ?? 0,
          action,
          name: action === "rename" ? saveName.trim() : undefined,
        },
      });
      const itemId = item.variant_id ?? String(item.version);
      if (edited?.id === itemId && action === "rename") setEdited({ id: itemId, name: saveName.trim() });
      if (edited?.id === itemId && action === "delete") setEdited(null);
      setState({ ...state, search_profile: saved });
      setCriteriaNotice(action === "activate" ? `Используется в поиске · ${item.name ?? `Версия ${item.version}`}` : "Список вариантов обновлён.");
      closeDialog();
      onChange?.();
    });
  }
  async function suggestLegacyPlaces() {
    const values = fields.slice(-4).flatMap(([field]) =>
      (terms[field] ?? "").split(",").map((value) => value.trim()).filter(Boolean),
    );
    await act(async () => {
      const query = new URLSearchParams([...new Set(values)].map((value) => ["value", value]));
      setLegacyChoices(await request(`/places/legacy?${query}`));
    });
  }
  function adoptLegacyPlaces() {
    if (!legacyChoices) return;
    const idsFor = (field: string, filter: (id: string) => boolean) =>
      (terms[field] ?? "").split(",").map((value) => legacyChoices[value.trim()])
        .filter((matches) => matches?.length === 1 && filter(matches[0].id))
        .map((matches) => matches[0].id);
    const office = (id: string) => id.startsWith("city:") || id.startsWith("country:");
    setForm({
      ...form,
      remote_place_ids: form.remote_place_ids ?? (form.workplace_types.includes("remote")
        ? idsFor("candidate_locations", (id) => id !== "region:world") : []),
      hybrid_place_ids: form.hybrid_place_ids ?? (form.workplace_types.includes("hybrid")
        ? idsFor("hybrid_locations", office) : []),
      office_place_ids: form.office_place_ids ?? [],
      preferred_place_ids: form.preferred_place_ids?.length ? form.preferred_place_ids : idsFor("allowed_locations", () => true),
      excluded_place_ids: form.excluded_place_ids?.length ? form.excluded_place_ids : idsFor("blocked_locations", () => true),
    });
    setCriteriaNotice("Проверьте подставленные локации перед сохранением. Место офиса выберите отдельно.");
  }
  return (
    <section className="search-settings">
      <header className="page-heading">
        <div>
          <h1>
            {page === "criteria"
              ? "Критерии"
              : page === "sources"
                ? "Источники"
                : "Настройки поиска"}
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
          Критерии
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
          {(!state || accountConflict) && <button
            className="text-button"
            onClick={() => {
              if (!accountConflict || !state) {
                replaceIfConfirmed(() => setRevision((value) => value + 1));
              } else {
                void act(async () => {
                  const latest = await request<Preferences>("/search-settings");
                  setState(latest);
                  setCriteriaNotice("Актуальный вариант загружен в историю. Черновик сохранён в форме.");
                });
              }
            }}
          >
            Загрузить актуальные настройки
          </button>}
        </Notice>
      )}
      <dialog ref={dialog} className="workspace-dialog" aria-labelledby={`${historyId}-confirmation`}
        onCancel={(event) => { event.preventDefault(); closeDialog(); }}>
        <form onSubmit={(event) => {
          event.preventDefault();
          if (replaceDraft) { replaceDraft(); closeDialog(); }
          else if (rename) void versionAction(rename, "rename");
          else if (removing) void versionAction(removing, "delete");
          else void saveProfile(true);
        }}>
          <h2 id={`${historyId}-confirmation`}>{replaceDraft ? "Заменить несохранённые поля?" : removing
            ? "Удалить вариант из списка?" : rename ? "Переименовать вариант" : "Новая версия профиля"}</h2>
          {(showNew || rename) && (
            <label>Имя варианта<input required maxLength={120} value={saveName}
              onChange={(event) => setSaveName(event.target.value)} /></label>
          )}
          {error && (showNew || rename || replaceDraft || removing) && <Notice>{error}</Notice>}
          <div className="source-actions">
            <button className="button secondary" type="button" onClick={closeDialog}>{replaceDraft ? "Оставить черновик" : "Отмена"}</button>
            <button className="button primary" type="submit" disabled={busy}>{replaceDraft ? "Заменить" : removing ? "Удалить" : "Сохранить"}</button>
          </div>
        </form>
      </dialog>
      {!state ? (
        <Loading text="Загружаем настройки поиска…" />
      ) : (
        <>
          {!state.search_profile.configured && (
            <Notice tone="info">
              Профиль поиска ещё не настроен. Заполните критерии вручную или примените шаблон, затем сохраните критерии и подключите источники.
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
                        ? `В поиске используется ${state.search_profile.name ?? `версия ${state.search_profile.version}`}`
                        : "Здесь появятся ваши сохранённые критерии"}
                    </span>
                  </span>
                </summary>
                <div className="profile-history-content">
                  {historyOpen && (
                    <ProfileHistory key={state.search_profile.editor_version ?? state.search_profile.version}
                      onLoad={editRevision} onAction={(item, action) => {
                        if (action === "activate") void versionAction(item, action);
                        if (action === "rename") {
                          setSaveName(item.name ?? `Версия ${item.version}`);
                          setRename(item);
                        }
                        if (action === "delete") setRemoving(item);
                      }}
                    />
                  )}
                </div>
              </details>
              <fieldset className="settings-presets" disabled={busy}>
                <legend>Шаблоны поиска</legend>
                <p>Шаблон подставляет названия ролей, запросы, направления, навыки и инструменты в текущий черновик. Чтобы записать изменения, нажмите «Сохранить» внизу.</p>
                <SettingsSelect label="Шаблон поиска" hint="выберите профессию или направление"
                  value={preset} onChange={(event) => setPresetChoice(event.target.value)}>
                  <option value="">Выберите шаблон</option>
                  {Object.keys(presets.items).map((id) => <option key={id} value={id}>{presets.labels?.[id] ?? id}</option>)}
                </SettingsSelect>
                <button type="button" className="button secondary" disabled={!presets.items[preset]}
                  onClick={() => replaceIfConfirmed(() => {
                    const example = presets.items[preset];
                    setForm((current) => ({ ...current, qa_title_fallback: example.qa_title_fallback }));
                    setTerms((current) => ({
                      ...current,
                      role_keywords: example.role_keywords.join(", "),
                      source_queries: example.source_queries.join(", "),
                      focus_qa_keywords: example.focus_qa_keywords.join(", "),
                      skill_keywords: example.skill_keywords.join(", "),
                    }));
                    setCriteriaNotice(`Шаблон применён к черновику · ${presets.labels?.[preset] ?? preset}`);
                  })}>
                  Применить шаблон
                </button>
              </fieldset>
              <form ref={criteriaForm}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.nativeEvent.isComposing && event.target instanceof HTMLInputElement
                    && ["text", "number", "time"].includes(event.target.type)) event.preventDefault();
                }}
                onSubmit={(event) => { event.preventDefault(); void saveProfile(); }}>
                <fieldset className="criteria-fields" disabled={busy}>
                  <legend>Критерии поиска</legend>
                  <p>В редакторе · {edited?.name ?? "Новый вариант"}</p>
                  {fields.filter(([field]) =>
                    !["source_queries", "candidate_locations", "hybrid_locations", "allowed_locations", "blocked_locations"].includes(field)
                  ).map(([field, label, hint]) => (
                    <CriteriaInput key={field} label={label} hint={hint} name={field} issue={fieldErrors[field]}
                      required={field === "role_keywords"} value={terms[field] ?? ""}
                      onChange={(event) => setTerms({ ...terms, [field]: event.target.value })} />
                  ))}
                  <details>
                    <summary>Дополнительные настройки</summary>
                    <CriteriaInput label={fields[1][1]} hint={fields[1][2]} name="source_queries" issue={fieldErrors.source_queries}
                      value={terms.source_queries ?? ""}
                      onChange={(event) => setTerms({ ...terms, source_queries: event.target.value })} />
                  </details>
                  <fieldset>
                    <legend>Формат работы по локациям</legend>
                    <p className="muted">Укажите места для нужных форматов. Выбор места включает формат. Например, удалённо из ЕС, а в офисе в Тбилиси.</p>
                    <div className="workplace-options">
                      {workplace.map(([mode, label, hint, field, placeLabel, kinds]) => {
                        const places = form[field] ?? [];
                        const enabled = form.workplace_types.includes(mode) || places.length > 0;
                        return (
                          <div className="workplace-option" key={mode}>
                            <CriteriaInput label={label} hint={hint} type="checkbox" checked={enabled}
                              onChange={(event) => {
                                const checked = event.target.checked;
                                setForm({
                                  ...form,
                                  workplace_types: checked
                                    ? [...form.workplace_types, mode]
                                    : form.workplace_types.filter((value) => value !== mode),
                                  [field]: checked && mode === "remote" ? null : [],
                                });
                              }} />
                            <PlacePicker label={placeLabel} kinds={kinds} values={places}
                              onChange={(values) => setForm({
                                ...form,
                                [field]: mode === "remote" && !values.length ? null : values,
                                workplace_types: values.length && !form.workplace_types.includes(mode)
                                  ? [...form.workplace_types, mode] : form.workplace_types,
                              })} />
                          </div>
                        );
                      })}
                    </div>
                  </fieldset>
                  <fieldset>
                    <legend>География вакансии и рабочие часы</legend>
                    {geographicFields.map(([field, label, kinds]) => (
                      <PlacePicker key={field} label={label} kinds={kinds} values={form[field] ?? []}
                        onChange={(values) => setForm({ ...form, [field]: values })} />
                    ))}
                    {fields.slice(-4).some(([field]) => terms[field]) && (
                      <details>
                        <summary>География из прежнего профиля · проверьте перед заменой</summary>
                        {fields.slice(-4).filter(([field]) => terms[field]).map(([field, label]) => (
                          <p key={field}>{label} · {terms[field]}</p>
                        ))}
                        <button type="button" className="button secondary" onClick={() => void suggestLegacyPlaces()}>Сверить прежние локации</button>
                        {legacyChoices && (
                          <>
                            <ul>{Object.entries(legacyChoices).map(([value, matches]) => (
                              <li key={value}>{value} · {matches.length === 1 ? matches[0].label : "Требует уточнения"}</li>
                            ))}</ul>
                            <button type="button" className="button secondary" onClick={adoptLegacyPlaces}>Подставить распознанные локации</button>
                          </>
                        )}
                        <button type="button" className="button secondary" onClick={() => replaceIfConfirmed(() => {
                          setTerms({ ...terms, candidate_locations: "", hybrid_locations: "", allowed_locations: "", blocked_locations: "" });
                        })}>Убрать прежние значения</button>
                      </details>
                    )}
                    <details open={form.availability ? true : undefined}>
                      <summary>Часы доступности</summary>
                      <CriteriaInput label="Учитывать часы доступности" type="checkbox"
                        hint="сравниваем с обязательными часами работодателя" checked={!!form.availability}
                        onChange={(event) => setForm({ ...form, availability: event.target.checked
                          ? { zone: Intl.DateTimeFormat().resolvedOptions().timeZone, start: "09:00", end: "18:00" } : null })} />
                      {form.availability && (
                        <>
                          <CriteriaInput label="Часовой пояс" hint="например, Europe/Belgrade"
                            value={form.availability.zone} required name="availability.zone" issue={fieldErrors["availability.zone"]}
                            onChange={(event) => form.availability && setForm({ ...form, availability: { ...form.availability, zone: event.target.value } })} />
                          <CriteriaInput label="Доступен с" hint="местное время" type="time" required
                            value={form.availability.start}
                            onChange={(event) => form.availability && setForm({ ...form, availability: { ...form.availability, start: event.target.value } })} />
                          <CriteriaInput label="Доступен до" hint="местное время" type="time" required
                            value={form.availability.end}
                            onChange={(event) => form.availability && setForm({ ...form, availability: { ...form.availability, end: event.target.value } })} />
                        </>
                      )}
                    </details>
                  </fieldset>
                  <p className="muted" id={`${historyId}-language`}>
                    Рабочий язык <small className="field-hint">(без выбора — любой. Когда язык команды не указан, учитываем язык публикации)</small>
                  </p>
                  <div className="working-language-choices" role="group" aria-labelledby={`${historyId}-language`}>
                    {workingLanguages.map(([language, label]) => (
                      <label className="checkbox-row" key={language}>
                        <input
                          type="checkbox"
                          checked={form.working_languages.includes(language)}
                          onChange={(event) =>
                            setForm({
                              ...form,
                              working_languages: event.target.checked
                                ? [...form.working_languages, language]
                                : form.working_languages.filter((value) => value !== language),
                            })
                          }
                        />
                        {label}
                      </label>
                    ))}
                  </div>
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
                    label="Порог совпадения с критериями"
                    hint="например, порог 60 скроет вакансию с 59 баллами"
                    name="min_score"
                    issue={fieldErrors.min_score}
                    type="number"
                    min="0"
                    max="100"
                    required
                    value={form.min_score}
                    onChange={(event) =>
                      setForm({ ...form, min_score: Number(event.target.value) })
                    }
                  />
                  <div className="criteria-save-bar">
                    <div className={criteriaNotice ? "criteria-save-notice" : "sr-only"}>
                      <span role="status">{criteriaNotice}</span>
                      {criteriaNotice && (
                        <button type="button" className="text-button" onClick={() => setCriteriaNotice("")}>
                          Закрыть уведомление
                        </button>
                      )}
                    </div>
                    {dirty && <span role="status">Есть несохранённые изменения</span>}
                    <button className="button primary" type="submit">Сохранить</button>
                    <button className="button secondary" type="button" onClick={() => setShowNew(true)}>
                      Сохранить как новую версию
                    </button>
                  </div>
                </fieldset>
              </form>
            </>
          )}
          {page !== "criteria" && (
            <SearchSources sources={state.sources} types={types}
              onChange={(sources) => {
                setState((previous) => previous && { ...previous, sources });
                onChange?.();
              }} />
          )}
        </>
      )}
    </section>
  );
}
