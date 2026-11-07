import { useState, type ReactNode } from "react";
import { errorMessage, isPending, request, taskLabel } from "./api";
import type { DocumentPreview, Task } from "./types";

export function formatCreatedAt(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Время создания неизвестно";
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

const paths = {
  jobs: "M4 7h16v13H4z M8 7V4h8v3 M4 12h16 M10 12v3h4v-3",
  applied: "M4 12l5 5L20 6",
  archive: "M3 4h18v4H3z M5 8v12h14V8 M9 12h6",
  profile: "M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0 M4 21v-2a8 8 0 0 1 16 0v2",
  arrow: "M7 17 17 7 M7 7h10v10",
  refresh: "M20 11a8 8 0 1 0-2 7 M20 4v7h-7",
  file: "M14 3H5v18h14V8z M14 3v5h5 M8 12h8 M8 16h6",
  check: "m5 12 4 4L19 6",
  close: "m6 6 12 12 M6 18 18 6",
  plus: "M12 5v14 M5 12h14",
  logout: "M9 4H4v16h5 M9 12h12 M17 8l4 4-4 4",
  send: "m3 3 19 9-19 9 4-9z M7 12h15",
};

export function Icon({
  name,
  size = 18,
}: {
  name: keyof typeof paths;
  size?: number;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name]} />
    </svg>
  );
}

export function Notice({
  children,
  tone = "error",
  onClose,
}: {
  children: ReactNode;
  tone?: "error" | "success" | "info";
  onClose?: () => void;
}) {
  return (
    <div
      className={`notice notice-${tone}`}
      role={tone === "error" ? "alert" : "status"}
    >
      <span>{children}</span>
      {onClose && (
        <button
          type="button"
          className="icon-button"
          aria-label="Закрыть уведомление"
          onClick={onClose}
        >
          <Icon name="close" />
        </button>
      )}
    </div>
  );
}

export function Loading({ text = "Загружаем…" }: { text?: string }) {
  return (
    <div className="loading" role="status">
      <span className="spinner" />
      {text}
    </div>
  );
}

export function TaskStatus({
  task,
  onDiscuss,
  onCancel,
  onDelete,
  deleting = false,
}: {
  task: Task;
  onDiscuss?: () => void;
  onCancel?: () => void;
  onDelete?: () => void;
  deleting?: boolean;
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [preview, setPreview] = useState<DocumentPreview | null>(null);
  const [previewError, setPreviewError] = useState("");
  const isDocument = ["cv", "cover_letter"].includes(task.kind);
  const createdAt = formatCreatedAt(task.created_at);
  async function togglePreview() {
    if (previewOpen) {
      setPreviewOpen(false);
      return;
    }
    setPreviewOpen(true);
    if (preview) return;
    setPreviewLoading(true);
    setPreviewError("");
    try {
      setPreview(
        await request<DocumentPreview>(`/tasks/${encodeURIComponent(task.id)}/document-preview`),
      );
    } catch (failure) {
      setPreviewError(errorMessage(failure));
    } finally {
      setPreviewLoading(false);
    }
  }
  return (
    <div className={`task-status task-${task.status}`}>
      <div className="task-heading">
        <span>
          {task.kind === "cv"
            ? "CV"
            : task.kind === "cover_letter"
              ? "Cover letter"
              : "Обновление поиска"}
        </span>
        <div className="task-meta">
          {isDocument && (
            <time dateTime={task.created_at} title="Дата и время создания">
              {createdAt}
            </time>
          )}
          <span className="task-label">
            {isPending(task.status) && <span className="spinner" />}
            {taskLabel(task.status)}
          </span>
        </div>
      </div>
      {task.error && <p className="task-error">{task.error}</p>}
      {onCancel && isPending(task.status) && (
        <button className="text-button" onClick={onCancel}>
          Отменить создание
        </button>
      )}
      {task.status === "ready" && (
        <div className="download-row">
          {task.artifacts.map((artifact) => (
            <span className="artifact-links" key={artifact.id}>
              {artifact.format === "pdf" && (
                <a
                  href={`/api/artifacts/${encodeURIComponent(artifact.id)}/preview`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="download-link"
                >
                  Просмотреть PDF
                </a>
              )}
              <a
                href={`/api/artifacts/${encodeURIComponent(artifact.id)}/download`}
                className="download-link"
                download={artifact.filename}
              >
                <Icon name="file" size={15} />
                Скачать {artifact.format.toUpperCase()}
              </a>
            </span>
          ))}
        </div>
      )}
      {task.status === "ready" && task.preview_available && (
        <>
          <button type="button" className="text-button" onClick={() => void togglePreview()}>
            {previewOpen ? "Скрыть содержание" : "Содержание и подтверждённые факты"}
          </button>
          {previewOpen && (
            <div className="document-preview" role="region" aria-label="Проверенное содержание документа">
              {previewLoading && <Loading text="Загружаем содержание…" />}
              {previewError && <Notice>{previewError}</Notice>}
              {preview && (
                <>
                  <h4>{preview.draft.title}</h4>
                  {preview.draft.sections.map((section, sectionIndex) => (
                    <section key={sectionIndex}>
                      {section.heading && <h5>{section.heading}</h5>}
                      {section.paragraphs.map((paragraph, paragraphIndex) => (
                        <div className="preview-paragraph" key={paragraphIndex}>
                          <p>{paragraph.text}</p>
                          <details>
                            <summary>Подтверждённые факты</summary>
                            <ul>
                              {paragraph.fact_ids.map((id) => {
                                const fact = preview.facts.find((item) => item.id === id);
                                return fact ? <li key={id}>{fact.text}</li> : null;
                              })}
                            </ul>
                          </details>
                        </div>
                      ))}
                    </section>
                  ))}
                </>
              )}
            </div>
          )}
        </>
      )}
      {onDiscuss &&
        ["needs_input", "blocked", "failed", "ready"].includes(task.status) && (
          <button className="text-button" onClick={onDiscuss}>
            {task.status === "ready"
              ? "Обсудить и доработать"
              : "Обсудить с агентом"}{" "}
            <span aria-hidden="true">↗</span>
          </button>
        )}
      {onDelete && isDocument && !isPending(task.status) &&
        (confirmingDelete ? (
          <div className="task-delete-confirmation" role="group" aria-label="Подтверждение удаления">
            <span>Документ и его файлы будут удалены.</span>
            <div>
              <button
                type="button"
                className="text-button task-delete-button"
                disabled={deleting}
                onClick={onDelete}
              >
                {deleting ? "Удаляем…" : "Удалить документ"}
              </button>
              <button
                type="button"
                className="text-button"
                disabled={deleting}
                onClick={() => setConfirmingDelete(false)}
              >
                Отмена
              </button>
            </div>
          </div>
        ) : (
          <button
            type="button"
            className="text-button task-delete-button"
            onClick={() => setConfirmingDelete(true)}
          >
            Удалить
          </button>
        ))}
    </div>
  );
}
