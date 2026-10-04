import type { ReactNode } from "react";
import { isPending, taskLabel } from "./api";
import type { Task } from "./types";

const paths = {
  jobs: "M4 7h16v13H4z M8 7V4h8v3 M4 12h16 M10 12v3h4v-3",
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
}: {
  task: Task;
  onDiscuss?: () => void;
  onCancel?: () => void;
}) {
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
        <span className="task-label">
          {isPending(task.status) && <span className="spinner" />}
          {taskLabel(task.status)}
        </span>
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
            <a
              key={artifact.id}
              href={`/api/artifacts/${encodeURIComponent(artifact.id)}/download`}
              className="download-link"
              download={artifact.filename}
            >
              <Icon name="file" size={15} />
              Скачать {artifact.format.toUpperCase()}
            </a>
          ))}
        </div>
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
    </div>
  );
}
