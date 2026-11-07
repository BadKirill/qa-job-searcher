import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  formatCreatedAt,
  Loading,
  Notice,
  TaskStatus,
} from "../src/components";
import { makeTask, server } from "./fixtures";

it("announces errors and lets a user dismiss informational notifications", async () => {
  const close = vi.fn();
  const { rerender } = render(<Notice>Не сохранено</Notice>);
  expect(screen.getByRole("alert")).toHaveTextContent("Не сохранено");
  rerender(
    <Notice tone="info" onClose={close}>
      Сохранено
    </Notice>,
  );
  expect(screen.getByRole("status")).toHaveTextContent("Сохранено");
  await userEvent.click(
    screen.getByRole("button", { name: "Закрыть уведомление" }),
  );
  expect(close).toHaveBeenCalledOnce();
  rerender(<Loading />);
  expect(screen.getByRole("status")).toHaveTextContent("Загружаем");
});

it("exposes downloads only for ready artifacts and discussions for actionable tasks", async () => {
  const discuss = vi.fn();
  const artifacts = [{ id: "a/1", format: "pdf", filename: "cv.pdf" }];
  const { rerender } = render(
    <TaskStatus
      task={makeTask({
        status: "blocked",
        error: "Unsupported experience",
        artifacts,
      })}
      onDiscuss={discuss}
    />,
  );
  expect(screen.queryByRole("link")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Содержание и подтверждённые факты" })).not.toBeInTheDocument();
  expect(screen.getByText("Unsupported experience")).toBeInTheDocument();
  expect(screen.getByTitle("Дата и время создания")).toHaveAttribute(
    "datetime",
    "2026-10-03",
  );
  expect(formatCreatedAt("invalid")).toBe("Время создания неизвестно");
  await userEvent.click(
    screen.getByRole("button", { name: /Обсудить с агентом/ }),
  );
  expect(discuss).toHaveBeenCalledOnce();
  rerender(
    <TaskStatus
      task={makeTask({ kind: "cover_letter", status: "ready", artifacts })}
      onDiscuss={discuss}
    />,
  );
  expect(screen.getByRole("link", { name: "Скачать PDF" })).toHaveAttribute(
    "href",
    "/api/artifacts/a%2F1/download",
  );
  expect(screen.getByRole("link", { name: "Просмотреть PDF" })).toHaveAttribute(
    "href",
    "/api/artifacts/a%2F1/preview",
  );
  expect(screen.queryByRole("button", { name: "Содержание и подтверждённые факты" })).not.toBeInTheDocument();
  expect(screen.getByRole("button")).toHaveTextContent("доработать");
  rerender(
    <TaskStatus task={makeTask({ kind: "search", status: "running" })} />,
  );
  expect(screen.getByText("Обновление поиска")).toBeInTheDocument();
  expect(screen.queryByTitle("Дата и время создания")).not.toBeInTheDocument();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

it("shows approved text beside its confirmed facts and handles preview failure", async () => {
  const backend = server();
  const task = makeTask({ status: "ready", preview_available: true });
  backend.failures.push({
    path: "/tasks/task-1/document-preview",
    status: 503,
    detail: "Просмотр временно недоступен",
  });
  render(<TaskStatus task={task} />);
  await userEvent.click(
    screen.getByRole("button", { name: "Содержание и подтверждённые факты" }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent("Просмотр временно недоступен");
  await userEvent.click(screen.getByRole("button", { name: "Скрыть содержание" }));
  await userEvent.click(
    screen.getByRole("button", { name: "Содержание и подтверждённые факты" }),
  );
  expect(await screen.findByText("Tested APIs at Acme.")).toBeInTheDocument();
  await userEvent.click(screen.getByText("Подтверждённые факты"));
  expect(screen.getByText("API testing at Acme")).toBeInTheDocument();
  expect(backend.calls.filter((call) => call.path.endsWith("document-preview"))).toHaveLength(2);
});

it("requires confirmation before deleting a finished document", async () => {
  const remove = vi.fn();
  const { rerender } = render(
    <TaskStatus
      task={makeTask({ status: "failed" })}
      onDelete={remove}
    />,
  );

  await userEvent.click(screen.getByRole("button", { name: "Удалить" }));
  expect(screen.getByRole("group", { name: "Подтверждение удаления" })).toHaveTextContent(
    "Документ и его файлы будут удалены",
  );
  await userEvent.click(screen.getByRole("button", { name: "Отмена" }));
  expect(screen.queryByRole("group", { name: "Подтверждение удаления" })).not.toBeInTheDocument();
  expect(remove).not.toHaveBeenCalled();

  await userEvent.click(screen.getByRole("button", { name: "Удалить" }));
  await userEvent.click(screen.getByRole("button", { name: "Удалить документ" }));
  expect(remove).toHaveBeenCalledOnce();

  rerender(
    <TaskStatus
      task={makeTask({ status: "ready" })}
      onDelete={remove}
      deleting
    />,
  );
  expect(screen.getByRole("button", { name: "Удаляем…" })).toBeDisabled();

  rerender(
    <TaskStatus task={makeTask({ status: "running" })} onDelete={remove} />,
  );
  expect(screen.queryByRole("button", { name: /Удал/ })).not.toBeInTheDocument();
});
