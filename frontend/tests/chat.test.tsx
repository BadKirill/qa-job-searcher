import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Chat } from "../src/Chat";
import { server } from "./fixtures";

it("sends only nonempty messages, preserves history and renders untrusted text safely", async () => {
  const backend = server();
  backend.state.messages = [
    {
      id: "1",
      role: "assistant",
      text: "<script>stealCV()</script>",
      status: "ready",
      created_at: "2026-10-03",
    },
  ];
  const { rerender } = render(<Chat templateId="template-1" revision={0} />);
  await screen.findByText("<script>stealCV()</script>");
  expect(document.querySelector("script")).toBeNull();
  expect(screen.getByRole("button", { name: "Отправить" })).toBeDisabled();
  fireEvent.submit(
    screen.getByRole("button", { name: "Отправить" }).closest("form")!,
  );
  expect(
    backend.calls.filter(
      (call) => call.path.endsWith("/messages") && call.method === "POST",
    ),
  ).toHaveLength(0);
  await userEvent.type(
    screen.getByRole("textbox", { name: "Сообщение агенту" }),
    "  Focus on APIs  ",
  );
  await userEvent.click(screen.getByRole("button", { name: "Отправить" }));
  await screen.findByText("Focus on APIs");
  expect(screen.getByRole("textbox")).toHaveValue("");
  expect(screen.getByText("Ожидает ответа")).toBeInTheDocument();
  expect(
    backend.calls.find(
      (call) => call.path.endsWith("/messages") && call.method === "POST",
    )?.body,
  ).toMatchObject({ text: "Focus on APIs" });
  backend.state.messages.push({
    id: "3",
    role: "assistant",
    text: "Проверяю уточнения.",
    status: "running",
    created_at: "2026-10-03",
  });
  rerender(<Chat templateId="template-1" revision={1} />);
  await screen.findByText("Проверяю уточнения.");
  expect(screen.getByText("running")).toBeInTheDocument();
});

it("preserves unsent input when agent message submission fails", async () => {
  const backend = server();
  render(<Chat jobId="job-1" revision={0} />);
  await screen.findByText("Здесь сохранится ваш диалог.");
  backend.failures.push(
    {
      path: "/conversations/conversation-1/messages",
      status: 503,
      detail: "Агент недоступен",
    },
    {
      path: "/conversations/conversation-1/messages",
      status: 503,
      detail: "Агент недоступен",
    },
    {
      path: "/conversations/conversation-1/messages",
      status: 503,
      detail: "Агент недоступен",
    },
  );
  await userEvent.type(screen.getByRole("textbox"), "My instruction");
  await userEvent.click(screen.getByRole("button", { name: "Отправить" }));
  await screen.findByText("Агент недоступен");
  expect(screen.getByRole("textbox")).toHaveValue("My instruction");
  backend.failures.length = 0;
  await userEvent.click(screen.getByRole("button", { name: "Отправить" }));
  await screen.findByText("My instruction");
});

it("reports failed conversation initialization and history read", async () => {
  const backend = server();
  backend.failures.push({
    path: "/conversations",
    status: 500,
    detail: "Не удалось открыть диалог",
  });
  const { rerender } = render(<Chat templateId="template-1" revision={0} />);
  await screen.findByText("Не удалось открыть диалог");
  expect(screen.getByRole("textbox")).toBeDisabled();
  backend.failures.push({
    path: "/conversations/conversation-1/messages",
    status: 500,
    detail: "Не удалось загрузить историю",
  });
  rerender(<Chat templateId="template-2" revision={0} />);
  await screen.findByText("Не удалось загрузить историю");
  rerender(<Chat templateId="template-2" revision={1} />);
  await waitFor(() =>
    expect(
      screen.queryByText("Не удалось загрузить историю"),
    ).not.toBeInTheDocument(),
  );
});

it("avoids duplicate messages while sending", async () => {
  const backend = server();
  render(<Chat jobId="job-1" revision={0} />);
  await screen.findByText("Здесь сохранится ваш диалог.");
  let finish!: (value: Response) => void;
  backend.fetcher.mockImplementationOnce(
    () =>
      new Promise<Response>((resolve) => {
        finish = resolve;
      }),
  );
  fireEvent.change(screen.getByRole("textbox"), {
    target: { value: "Important facts" },
  });
  fireEvent.submit(
    screen.getByRole("button", { name: "Отправить" }).closest("form")!,
  );
  fireEvent.submit(
    screen.getByRole("button", { name: "Отправляем…" }).closest("form")!,
  );
  expect(screen.getByRole("button", { name: "Отправляем…" })).toBeDisabled();
  await act(async () => finish(new Response("{}")));
  expect(screen.getByRole("textbox")).toHaveValue("");
});
