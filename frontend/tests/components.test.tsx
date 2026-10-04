import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Loading, Notice, TaskStatus } from "../src/components";
import { makeTask } from "./fixtures";

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
  expect(screen.getByText("Unsupported experience")).toBeInTheDocument();
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
  expect(screen.getByRole("button")).toHaveTextContent("доработать");
  rerender(
    <TaskStatus task={makeTask({ kind: "search", status: "running" })} />,
  );
  expect(screen.getByText("Обновление поиска")).toBeInTheDocument();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});
