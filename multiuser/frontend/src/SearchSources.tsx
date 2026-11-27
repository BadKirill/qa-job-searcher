import { useEffect, useRef, useState } from "react";
import { errorMessage, request } from "./api";
import { formatCreatedAt, Notice } from "./components";
import type { Preferences, Source } from "./SearchSettings";

export type SourceType = {
  kind: string;
  name: string;
  limitations: string;
  builtin?: boolean;
  description?: string;
};
type Spec = { kind: string; config: Record<string, unknown>; enabled: boolean };
type ImportRow = { row: number; status: string; message: string; name?: string; spec?: Spec };
type SourceCheck = { status: string; items: number; reason: string | null; stale: boolean; checked_at: string };
type SourceMessage = { message: string; loading?: boolean; error?: boolean };
const aggregateKinds = ["remotive", "himalayas", "weworkremotely", "remote_com", "hirify"];

export function SearchSources({
  sources,
  types,
  onChange,
}: {
  sources: Preferences["sources"];
  types: SourceType[];
  onChange: (sources: Preferences["sources"]) => void;
}) {
  const [link, setLink] = useState("");
  const [preview, setPreview] = useState<Spec | null>(null);
  const [sheet, setSheet] = useState("");
  const [csvFile, setCsvFile] = useState<File | null>(null);
  const [rows, setRows] = useState<ImportRow[] | null>(null);
  const [selection, setSelection] = useState<number[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [checks, setChecks] = useState<Record<string, SourceMessage>>({});
  const [action, setAction] = useState<"recommend" | "delete" | "edit" | "new" | null>(null);
  const [editing, setEditing] = useState<Source | null>(null);
  const [kind, setKind] = useState("greenhouse");
  const [token, setToken] = useState("");
  const [name, setName] = useState("");
  const [region, setRegion] = useState("global");
  const [tags, setTags] = useState("");
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    if (action) dialog.current?.showModal();
    else dialog.current?.close();
  }, [action]);

  const builtins = types.filter((type) => type.builtin ?? aggregateKinds.includes(type.kind));
  const ownTypes = types.filter((type) => !(type.builtin ?? aggregateKinds.includes(type.kind)));
  const connected = (type: SourceType) => sources.items.find((source) => source.kind === type.kind);
  const specLabel = (spec: Spec) => types.find((type) => type.kind === spec.kind)?.name ?? spec.kind;
  async function perform(operation: () => Promise<void>, id?: string) {
    if (busy) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await operation();
    } catch (failure) {
      const text = errorMessage(failure);
      if (id) setChecks((current) => ({ ...current, [id]: { message: text, error: true } }));
      else setError(text);
    } finally {
      setBusy(false);
    }
  }
  async function reload() {
    onChange(await request<Preferences["sources"]>("/sources"));
  }
  async function check(source: Source) {
    if (checks[source.id]?.loading) return;
    setChecks((current) => ({ ...current, [source.id]: { message: "Проверяем…", loading: true } }));
    let result: SourceMessage;
    try {
      const data = await request<SourceCheck>(`/sources/${source.id}/check`, {
        method: "POST", body: { expected_version: source.version },
      });
      const feedback = data.stale
        ? "Настройки изменились во время проверки. Проверьте снова."
        : data.status === "ok" ? `Источник доступен. Объявлений в пробном ответе: ${data.items}.`
        : data.status === "empty" ? "Источник ответил. Пробная выборка пуста."
        : data.reason === "rate_limited" ? "Источник ограничил запросы. Повторите позже."
        : data.reason === "redirect" ? "Источник перенаправляет запрос. Автоматический переход запрещён."
        : "Не удалось проверить источник. Повторите позже.";
      result = { message: `${feedback} ${data.checked_at ? formatCreatedAt(data.checked_at) : ""}`, error: !["ok", "empty"].includes(data.status) };
    } catch (failure) {
      result = { message: errorMessage(failure), error: true };
    }
    setChecks((current) => ({ ...current, [source.id]: result }));
  }
  function toggle(source: Source) {
    void perform(async () => {
      await request(`/sources/${source.id}`, {
        method: "PATCH", body: { expected_version: source.version, enabled: !source.enabled },
      });
      await reload();
    }, source.id);
  }
  function edit(source?: Source) {
    setError("");
    setEditing(source ?? null);
    setKind(source?.kind ?? ownTypes[0]?.kind ?? "greenhouse");
    setToken(String(source?.config.token ?? source?.config.handle ?? ""));
    setName(String(source?.config.name ?? ""));
    setRegion(String(source?.config.region ?? "global"));
    setTags(Array.isArray(source?.config.tags) ? source.config.tags.join(", ") : "");
    setAction(source ? "edit" : "new");
  }
  async function changeSource() {
    await perform(async () => {
      if (action === "recommend") {
        const result = await request<{ items: Source[] }>("/sources/recommended", { method: "POST" });
        setMessage(`Подключено источников · ${result.items.length}`);
      } else if (action === "delete" && editing) {
        await request(`/sources/${editing.id}`, { method: "DELETE", body: { expected_version: editing.version } });
      } else {
        const extra = tags.split(",").map((value) => value.trim()).filter(Boolean);
        const config = kind === "telegram" ? { handle: token, ...(extra.length ? { tags: extra } : {}) }
          : { token, name: name.trim() || token, region, tags: extra };
        if (editing) await request(`/sources/${editing.id}`, { method: "PATCH", body: { expected_version: editing.version, config } });
        else await request("/sources", { method: "POST", body: { kind, config } });
      }
      await reload();
      if (action === "new" || action === "edit") setMessage("Источник сохранён.");
      if (action === "delete") setMessage("Источник удалён.");
      setAction(null);
    });
  }
  const renderCheck = (source: Source) => (
    <>
      <button className="button secondary" type="button" disabled={!source.valid || checks[source.id]?.loading}
        aria-label={`Проверить ${source.name}`} onClick={() => void check(source)}>
        {checks[source.id]?.loading ? "Проверяем…" : "Проверить"}
      </button>
      <p role="status">{checks[source.id]?.message}</p>
    </>
  );
  function revealBuiltin(kind: string) {
    const element = document.getElementById(`source-${kind}`);
    element?.focus();
    element?.scrollIntoView?.({ block: "nearest" });
  }
  const personalSources = sources.items.filter((source) => !(source.builtin ?? aggregateKinds.includes(source.kind)));
  return (
    <section className="source-workspace" aria-label="Источники">
      <p>Источник — сайт вакансий, карьерная доска компании или публичный канал, откуда сервис берёт объявления.</p>
      <a className="source-jump" href="#personal-sources">К моим источникам</a>
      <h2>Встроенные</h2>
      {builtins.some((type) => !connected(type)) && (
        <button type="button" className="button secondary" onClick={() => setAction("recommend")}>Подключить рекомендованный набор</button>
      )}
      <ul className="source-list">
        {builtins.map((type) => {
          const source = connected(type);
          return (
            <li id={`source-${type.kind}`} key={type.kind} tabIndex={-1}>
              <h3>{type.name}</h3>
              <p>{type.description ?? type.limitations}</p>
              <p className="status-badge" data-state={source?.enabled ? "active" : "disabled"}>
                {source ? source.enabled ? "Включён" : "Выключен" : "Ещё не подключён"}
              </p>
              <div className="source-actions">
                <button type="button" className="button secondary" disabled={busy}
                  aria-label={`${source?.enabled ? "Выключить" : "Включить"} ${type.name}`}
                  onClick={() => source ? toggle(source) : void perform(async () => {
                    await request("/sources", { method: "POST", body: { kind: type.kind } });
                    await reload();
                  }, type.kind)}>
                  {source?.enabled ? "Выключить" : "Включить"}
                </button>
                {source ? renderCheck(source) : <p role="status">{checks[type.kind]?.message}</p>}
              </div>
            </li>
          );
        })}
      </ul>
      <h2 id="personal-sources" tabIndex={-1}>Мои источники</h2>
      {!personalSources.length && <p>Источники пока не добавлены.</p>}
      <p>Кнопка «Проверить» запрашивает пробный ответ и показывает доступность площадки.</p>
      <ul className="source-list">
        {personalSources.map((source) => (
          <li key={source.id}>
            <h3>{source.name}</h3>
            <p>{source.limitations}</p>
            {!source.valid && <Notice>Исправьте параметры подключения.</Notice>}
            <div className="source-actions">
              <button className="button secondary" type="button" disabled={busy} onClick={() => toggle(source)}
                aria-label={`${source.enabled ? "Выключить" : "Включить"} ${source.name}`}>{source.enabled ? "Выключить" : "Включить"}</button>
              <button className="button secondary" type="button" onClick={() => edit(source)} aria-label={`Изменить ${source.name}`}>Изменить</button>
              {renderCheck(source)}
              <button className="button secondary" type="button" onClick={() => { setEditing(source); setAction("delete"); }}
                aria-label={`Удалить ${source.name}`}>Удалить</button>
            </div>
          </li>
        ))}
      </ul>
      <div className="source-connect">
        <form className="inline-form-fields" onSubmit={(event) => {
          event.preventDefault();
          void perform(async () => {
            const data = await request<{ spec: Spec; existing: Source | null }>("/sources/resolve", {
              method: "POST", body: { url: link.trim() },
            });
            if (aggregateKinds.includes(data.spec.kind)) {
              setPreview(null);
              revealBuiltin(data.spec.kind);
            } else if (data.existing) {
              setPreview(null);
              setMessage(`Уже подключён · ${data.existing.name}`);
            } else setPreview(data.spec);
          });
        }}>
          <label>Добавить по ссылке<input required maxLength={2048} value={link}
            onChange={(event) => { setLink(event.target.value); setPreview(null); }} /></label>
          <button type="submit" className="button secondary" disabled={busy}>Определить источник</button>
        </form>
        {preview && (
          <div>
            <p>{specLabel(preview)} · {String(preview.config.token ?? preview.config.handle ?? "")}</p>
            <button type="button" className="button primary" disabled={busy} onClick={() => void perform(async () => {
              await request("/sources", { method: "POST", body: preview });
              await reload();
              setPreview(null);
              setLink("");
              setMessage("Источник подключён.");
            })}>Подключить источник</button>
          </div>
        )}
        <button type="button" className="text-button" onClick={() => edit()}>Указать параметры вручную</button>
      </div>
      <details className="source-import">
        <summary>Импортировать компании и источники</summary>
        <p>CSV до 256 КБ и 100 строк. Колонки «Компания», «Ссылка» и необязательные «Теги».</p>
        <form onSubmit={(event) => {
          event.preventDefault();
          void perform(async () => {
            setRows(null);
            let data: { items: ImportRow[] };
            if (csvFile) {
              const body = new FormData();
              body.append("file", csvFile);
              data = await request("/sources/import/file", { method: "POST", body });
            } else {
              data = await request("/sources/import/link", { method: "POST", body: { url: sheet.trim() } });
            }
            setRows(data.items);
            setSelection(data.items.filter((row) => row.status === "new").map((row) => row.row));
          });
        }}>
          <label>Файл CSV<input type="file" accept=".csv,text/csv" onChange={(event) => setCsvFile(event.target.files?.[0] ?? null)} /></label>
          <label>Или публичная ссылка Google Sheets<input type="url" value={sheet}
            onChange={(event) => setSheet(event.target.value)} disabled={!!csvFile} /></label>
          <button type="submit" className="button secondary" disabled={busy || (!csvFile && !sheet.trim())}>Предпросмотр импорта</button>
        </form>
        {rows && (
          <>
            {rows.length ? (
              <ul aria-label="Строки импорта">
                {rows.map((row) => (
                  <li key={row.row}>
                    <label className="checkbox-row">
                      <input type="checkbox" disabled={row.status !== "new"}
                        checked={selection.includes(row.row)}
                        onChange={(event) => setSelection(event.target.checked
                          ? [...selection, row.row] : selection.filter((item) => item !== row.row))} />
                      Строка {row.row} · {row.name ? `${row.name} · ` : ""}{row.message}
                    </label>
                  </li>
                ))}
              </ul>
            ) : <p>В CSV нет строк для импорта.</p>}
            <button type="button" className="button primary" disabled={busy || !selection.length}
              onClick={() => void perform(async () => {
                const result = await request<{ items: Source[] }>("/sources/import", {
                  method: "POST",
                  body: { items: rows.filter((row) => selection.includes(row.row)).map((row) => row.spec) },
                });
                await reload();
                setRows(null);
                setMessage(`Добавлено источников · ${result.items.length}`);
              })}>Добавить выбранные</button>
          </>
        )}
      </details>
      <p role="status">{message}</p>
      {error && !action && <Notice>{error}</Notice>}
      <dialog ref={dialog} className="workspace-dialog" aria-labelledby="source-dialog-title" onCancel={(event) => { event.preventDefault(); setAction(null); }}>
        <form onSubmit={(event) => { event.preventDefault(); void changeSource(); }}>
          <h2 id="source-dialog-title">{action === "recommend" ? "Рекомендованные источники"
            : action === "delete" ? `Удалить ${editing?.name}?` : action === "new" ? "Добавить подключение" : "Изменить подключение"}</h2>
          {action === "recommend" && <ul>{builtins.filter((type) => !connected(type)).map((type) => <li key={type.kind}>{type.name}</li>)}</ul>}
          {(action === "new" || action === "edit") && (
            <>
              <label>Тип источника<select value={kind} disabled={action === "edit"} onChange={(event) => { setKind(event.target.value); setRegion("global"); }}>
                {ownTypes.map((type) => <option key={type.kind} value={type.kind}>{type.name}</option>)}
              </select></label>
              <label>{kind === "telegram" ? "Канал Telegram" : "Идентификатор доски"}
                <input required value={token} onChange={(event) => setToken(event.target.value)} /></label>
              {kind !== "telegram" && <label>Название компании<input value={name} onChange={(event) => setName(event.target.value)} /></label>}
              {kind === "lever" && <label>Регион<select value={region} onChange={(event) => setRegion(event.target.value)}>
                <option value="global">Глобальный</option><option value="eu">Европейский</option>
              </select></label>}
              <label>Теги через запятую<input value={tags} onChange={(event) => setTags(event.target.value)} /></label>
            </>
          )}
          {error && <Notice>{error}</Notice>}
          <div className="source-actions">
            <button className="button secondary" type="button" onClick={() => { setError(""); setAction(null); }}>Отмена</button>
            <button className="button primary" type="submit" disabled={busy}>{action === "delete" ? "Удалить" : action === "recommend" ? "Подключить" : "Сохранить"}</button>
          </div>
        </form>
      </dialog>
    </section>
  );
}
