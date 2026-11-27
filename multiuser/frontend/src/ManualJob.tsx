import { useEffect, useRef, useState } from "react";
import { errorMessage, forgetOperation, operationKey, request } from "./api";
import { Notice } from "./components";
import type { Job, PostingStatus } from "./types";

const empty = { title: "", company: "", location: "", url: "", description: "" };
type ManualContent = typeof empty;

export function ManualJob({
  job,
  onSave,
  onClose,
}: {
  job?: Job;
  onSave: (id: string, existing: boolean) => void;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const operations = useRef(new Map<string, string>());
  const [data, setData] = useState<ManualContent>(() => job ? {
    title: job.title, company: job.company, location: job.location,
    url: job.url, description: job.description,
  } : empty);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState<"preview" | "save" | null>(null);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  function change(field: keyof ManualContent, value: string) {
    setData({ ...data, [field]: value });
  }
  async function preview() {
    if (busy) return;
    setBusy("preview");
    setError("");
    setNotice("");
    try {
      const result = await request<{ posting: PostingStatus; job: ManualContent | null }>("/manual-jobs/preview", {
        method: "POST", body: { url: data.url.trim() },
      });
      if (result.job) {
        const { title, company, location, url, description } = result.job;
        setData({
          title: data.title || title, company: data.company || company,
          location: data.location || location, url: url || data.url,
          description: data.description || description,
        });
        setNotice(description ? "Проверьте данные объявления перед сохранением." : "Добавьте недостающее описание.");
      } else setNotice(`${result.posting.evidence} Вставьте текст вакансии ниже.`);
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(null);
    }
  }
  async function save() {
    if (busy) return;
    setBusy("save");
    setError("");
    const key = JSON.stringify(data);
    try {
      if (job?.manual_version) {
        await request(`/manual-jobs/${encodeURIComponent(job.id)}`, {
          method: "PATCH", body: { ...data, expected_version: job.manual_version },
        });
        onSave(job.id, false);
      } else {
        const result = await request<{ id: string; existing: boolean }>("/manual-jobs", {
          method: "POST", body: { ...data, request_id: operationKey(operations.current, key) },
        });
        forgetOperation(operations.current, key);
        onSave(result.id, result.existing);
      }
    } catch (failure) {
      forgetOperation(operations.current, key, failure);
      setError(errorMessage(failure));
    } finally {
      setBusy(null);
    }
  }
  return (
    <dialog ref={dialog} className="manual-job-dialog" aria-labelledby="manual-job-title"
      onCancel={(event) => { event.preventDefault(); if (!busy) onClose(); }}>
      <form onSubmit={(event) => { event.preventDefault(); void save(); }}>
        <h2 id="manual-job-title">{job ? "Изменить вакансию" : "Добавить вакансию"}</h2>
        <label>Ссылка на вакансию<input type="url" value={data.url} maxLength={2048}
          onChange={(event) => change("url", event.target.value)} /></label>
        <button type="button" className="button secondary" disabled={!!busy || !data.url.trim()}
          onClick={() => void preview()}>{busy === "preview" ? "Получаем объявление…" : "Получить предпросмотр"}</button>
        <p role="status">{notice}</p>
        <label>Название вакансии<input required value={data.title} maxLength={240}
          onChange={(event) => change("title", event.target.value)} /></label>
        <label>Компания<input required value={data.company} maxLength={240}
          onChange={(event) => change("company", event.target.value)} /></label>
        <label>Локация<input value={data.location} maxLength={240}
          onChange={(event) => change("location", event.target.value)} /></label>
        <label>Описание вакансии<textarea required minLength={40} maxLength={60000} rows={7}
          value={data.description} onChange={(event) => change("description", event.target.value)} /></label>
        {error && <Notice>{error}</Notice>}
        <div className="source-actions">
          <button type="button" className="button secondary" disabled={!!busy} onClick={onClose}>Отмена</button>
          <button type="submit" className="button primary" disabled={!!busy}>Сохранить вакансию</button>
        </div>
      </form>
    </dialog>
  );
}
