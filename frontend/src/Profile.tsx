import { useRef, useState } from "react";
import { errorMessage, request, requestId } from "./api";
import { Chat } from "./Chat";
import { Icon, Notice } from "./components";
import type { Profile as ProfileData, Template } from "./types";

export function Profile({
  profile,
  revision,
  onChange,
}: {
  profile: ProfileData;
  revision: number;
  onChange: () => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [draft, setDraft] = useState<Template | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [expectedVersion, setExpectedVersion] = useState<string | null>(null);

  function openDraft(value: Template) {
    setDraft(structuredClone(value));
    setExpectedVersion(profile.active?.version ?? null);
    setConfirmed(false);
    setError("");
    setNotice("");
  }
  async function upload(file?: File) {
    if (!file) return;
    setError("");
    setNotice("");
    if (!/\.(pdf|docx)$/i.test(file.name)) {
      setError("Выберите файл PDF или DOCX.");
      return;
    }
    if (file.size === 0 || file.size > 10 * 1024 * 1024) {
      setError("Файл должен быть непустым и не больше 10 МБ.");
      return;
    }
    const body = new FormData();
    body.append("file", file);
    setBusy(true);
    try {
      openDraft(
        await request<Template>("/template-drafts", { method: "POST", body }),
      );
      onChange();
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(false);
      if (input.current) input.current.value = "";
    }
  }
  async function save(activate: boolean) {
    if (!draft || busy) return;
    setError("");
    if (draft.facts.some((fact) => !fact.text.trim())) {
      setError("Заполните или удалите пустые факты.");
      return;
    }
    setBusy(true);
    try {
      const saved = await request<Template>(
        `/template-drafts/${encodeURIComponent(draft.id)}`,
        {
          method: "PATCH",
          body: { facts: draft.facts, instructions: draft.instructions },
        },
      );
      setDraft(saved);
      if (activate) {
        await request(
          `/template-drafts/${encodeURIComponent(draft.id)}/activate`,
          {
            method: "POST",
            body: { expected_version: expectedVersion, confirmed: true },
          },
        );
        setDraft(null);
        setNotice(
          "Эталон сохранён. Он будет использоваться для новых документов.",
        );
      } else {
        setNotice(
          "Черновик сохранён. Подтвердите факты, чтобы сделать его эталоном.",
        );
      }
      onChange();
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  }
  function changeFact(index: number, text: string) {
    setDraft(
      (current) =>
        current && {
          ...current,
          facts: current.facts.map((fact, position) =>
            position === index
              ? { ...fact, text, source: "user_confirmed" }
              : fact,
          ),
        },
    );
    setConfirmed(false);
  }

  return (
    <div className="profile-page">
      <div className="page-heading">
        <div>
          <p className="eyebrow">ОСНОВА КАЖДОГО ОТКЛИКА</p>
          <h1>Профиль и шаблон</h1>
          <p className="muted">
            Ваш опыт, ваш стиль, только подтверждённые факты.
          </p>
        </div>
      </div>
      {error && <Notice>{error}</Notice>}
      {notice && (
        <Notice tone="success" onClose={() => setNotice("")}>
          {notice}
        </Notice>
      )}
      <input
        ref={input}
        type="file"
        accept=".pdf,.docx"
        className="sr-only"
        aria-label="Файл эталонного CV"
        onChange={(event) => void upload(event.target.files?.[0])}
      />
      <section className="template-summary">
        <div className="template-symbol">
          <Icon name="file" size={32} />
        </div>
        <div className="template-summary-text">
          <p className="eyebrow">ЭТАЛОННОЕ CV</p>
          <h2>
            {profile.active
              ? profile.active.filename
              : "Один шаблон для всех возможностей"}
          </h2>
          <p className="muted">
            {profile.active
              ? `${profile.active.facts.length} подтверждённых фактов · CV на английском`
              : "Загрузите CV и проверьте извлечённые факты. Мы сохраним их как основу для ваших документов."}
          </p>
          {profile.active && (
            <span className="badge badge-active">
              <Icon name="check" size={13} />
              Активный эталон
            </span>
          )}
        </div>
        <button
          className="button primary"
          onClick={() => input.current?.click()}
          disabled={busy}
        >
          <Icon name={profile.active ? "refresh" : "plus"} />
          {busy
            ? "Обрабатываем файл…"
            : profile.active
              ? "Изменить эталонный шаблон"
              : "Задать эталонный шаблон"}
        </button>
      </section>
      <p className="template-format-note muted small">
        Из DOCX сохраняются базовый шрифт, поля и размер страницы. Колонки,
        фотографии и таблицы не копируются. Из PDF извлекается только текст.
      </p>
      <div className="language-note">
        <Icon name="check" size={16} />
        <span>CV всегда на английском. Cover letter — на языке вакансии.</span>
      </div>
      {!draft && profile.drafts.length > 0 && (
        <section className="draft-list">
          <h3>Незавершённые черновики</h3>
          {profile.drafts.map((item) => (
            <button
              className="draft-item"
              key={item.id}
              onClick={() => openDraft(item)}
            >
              <Icon name="file" />
              <span>{item.filename}</span>
              <span className="muted">
                Продолжить <span aria-hidden="true">→</span>
              </span>
            </button>
          ))}
        </section>
      )}
      {draft ? (
        <div className="profile-editor">
          <section className="facts-editor">
            <header>
              <div>
                <p className="eyebrow">ПРОВЕРЬТЕ ПЕРЕД СОХРАНЕНИЕМ</p>
                <h2>Факты из вашего CV</h2>
              </div>
              <button
                className="icon-button"
                aria-label="Закрыть черновик"
                onClick={() => {
                  setDraft(null);
                  setError("");
                  setNotice("");
                }}
                disabled={busy}
              >
                <Icon name="close" />
              </button>
            </header>
            <p className="muted">
              Исправьте неточности и удалите лишнее. Только подтверждённые вами
              сведения попадут в документы.
            </p>
            <p className="file-caption">
              <Icon name="file" size={16} />
              {draft.filename}
            </p>
            <div className="fact-list">
              {draft.facts.map((fact, index) => (
                <div className="fact-row" key={fact.id}>
                  <span className="fact-number">
                    {String(index + 1).padStart(2, "0")}
                  </span>
                  <label className="fact-field">
                    <span className="sr-only">Факт {index + 1}</span>
                    <textarea
                      aria-label={`Факт ${index + 1}`}
                      rows={2}
                      value={fact.text}
                      onChange={(event) =>
                        changeFact(index, event.target.value)
                      }
                      maxLength={3000}
                    />
                    <small>Источник: {fact.source}</small>
                  </label>
                  <button
                    className="icon-button"
                    aria-label={`Удалить факт ${index + 1}`}
                    onClick={() => {
                      setDraft({
                        ...draft,
                        facts: draft.facts.filter(
                          (item) => item.id !== fact.id,
                        ),
                      });
                      setConfirmed(false);
                    }}
                  >
                    <Icon name="close" size={16} />
                  </button>
                </div>
              ))}
            </div>
            <button
              className="text-button"
              onClick={() => {
                setDraft({
                  ...draft,
                  facts: [
                    ...draft.facts,
                    { id: requestId(), text: "", source: "user_confirmed" },
                  ],
                });
                setConfirmed(false);
              }}
            >
              <Icon name="plus" size={16} />
              Добавить факт
            </button>
            <label className="instructions-field">
              Пожелания к документам
              <textarea
                rows={4}
                maxLength={6000}
                value={draft.instructions}
                onChange={(event) =>
                  setDraft({ ...draft, instructions: event.target.value })
                }
                placeholder="Стиль, порядок разделов, акценты…"
              />
            </label>
            <details className="source-preview">
              <summary>Исходный текст CV</summary>
              <pre>{draft.extracted_text}</pre>
            </details>
            <label className="confirm-checkbox">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(event) => setConfirmed(event.target.checked)}
              />
              <span>Подтверждаю достоверность указанных фактов.</span>
            </label>
            <div className="editor-actions">
              <button
                className="button secondary"
                disabled={busy}
                onClick={() => void save(false)}
              >
                Сохранить черновик
              </button>
              <button
                className="button primary"
                disabled={busy || !confirmed || draft.facts.length === 0}
                onClick={() => void save(true)}
              >
                {busy ? "Сохраняем…" : "Сохранить эталон"}
                <Icon name="check" size={16} />
              </button>
            </div>
            {profile.active && (
              <p className="muted small">
                До сохранения нового эталона используется текущая версия.
              </p>
            )}
          </section>
          <Chat templateId={draft.id} revision={revision} />
        </div>
      ) : (
        <section className="profile-help">
          <span className="eyebrow">КАК ЭТО РАБОТАЕТ</span>
          <div>
            <article>
              <span>01</span>
              <h3>Загрузите свой CV</h3>
              <p>
                PDF или DOCX, до 10 МБ. Шаблон и факты сохранятся для следующих
                откликов.
              </p>
            </article>
            <article>
              <span>02</span>
              <h3>Подтвердите опыт</h3>
              <p>
                Обсудите детали прямо здесь и проверьте факты перед сохранением.
              </p>
            </article>
            <article>
              <span>03</span>
              <h3>Создавайте отклики</h3>
              <p>
                Выберите вакансию. Документы станут доступны после отдельной
                проверки.
              </p>
            </article>
          </div>
        </section>
      )}
    </div>
  );
}
