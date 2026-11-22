import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Chat } from "../src/Chat";
import { TaskStatus } from "../src/components";
import { JobCard } from "../src/Jobs";
import { makeJob, makeTask, server } from "./fixtures";

it("pins the displayed snapshot for generation and discussion from a job card", async () => {
  const backend = server();
  render(
    <JobCard
      snapshotId="historical-snapshot"
      job={makeJob("job-1")}
      tasks={[makeTask({ status: "ready" })]}
      hasTemplate
      revision={0}
      onProfile={vi.fn()}
      onState={vi.fn()}
      onTask={vi.fn()}
    />,
  );
  await userEvent.click(screen.getByRole("button", { name: "Сделать CV" }));
  expect(
    backend.calls.find((call) => call.path.endsWith("/documents"))?.body,
  ).toMatchObject({ kind: "cv", snapshot_id: "historical-snapshot" });
  await userEvent.click(screen.getByRole("button", { name: /доработать/ }));
  await screen.findByText("Здесь сохранится ваш диалог.");
  expect(
    backend.calls.find((call) => call.path === "/conversations")?.body,
  ).toEqual({ job_id: "job-1", snapshot_id: "historical-snapshot" });
});

it("regenerates against the conversation and snapshot selected by the user", async () => {
  const backend = server();
  render(
    <Chat jobId="job-1" snapshotId="old" documentKind="cv" revision={0} />,
  );
  await screen.findByText("Здесь сохранится ваш диалог.");
  await userEvent.click(
    screen.getByRole("button", { name: "Сгенерировать заново" }),
  );
  await screen.findByText("Повторная генерация запущена.");
  expect(
    backend.calls.find((call) => call.path.endsWith("/documents"))?.body,
  ).toMatchObject({
    kind: "cv",
    language: "en",
    snapshot_id: "old",
    conversation_id: "conversation-1",
  });
});

it("clears another version's messages and unsent input while its conversation loads", async () => {
  const backend = server();
  backend.state.messages = [
    {
      id: "old-message",
      role: "user",
      text: "Original instructions",
      created_at: "2026-10-03",
    },
  ];
  const { rerender } = render(
    <Chat jobId="job-1" snapshotId="old" revision={0} />,
  );
  await screen.findByText("Original instructions");
  await userEvent.type(screen.getByRole("textbox"), "Unsent old instruction");
  let finish!: (value: Response) => void;
  backend.fetcher.mockImplementationOnce(
    () =>
      new Promise<Response>((resolve) => {
        finish = resolve;
      }),
  );
  rerender(<Chat jobId="job-1" snapshotId="new" revision={0} />);
  expect(screen.queryByText("Original instructions")).not.toBeInTheDocument();
  expect(screen.getByRole("textbox")).toHaveValue("");
  expect(screen.getByRole("textbox")).toBeDisabled();
  backend.state.messages = [];
  await act(async () =>
    finish(new Response(JSON.stringify({ id: "new-conversation" }))),
  );
  await screen.findByText("Здесь сохранится ваш диалог.");
  expect(screen.getByRole("textbox")).toBeEnabled();
});

it("shows the template and vacancy pinned to a document with safe legacy fallbacks", () => {
  const task = makeTask({
    input_summary: {
      template_id: "private-template-id",
      template_version: "private-version-id",
      template_filename: "Original Designer.docx",
      confirmed_at: "2026-10-03T10:00:00Z",
      job_title: "Product Designer",
      job_version: { snapshot_id: "private-snapshot", content_hash: "hash" },
    },
  });
  const { rerender } = render(<TaskStatus task={task} />);
  expect(screen.getByText(/Эталон: Original Designer.docx/)).toHaveTextContent(
    "Вакансия: Product Designer · версия вакансии закреплена",
  );
  expect(screen.getByText(/Эталон:/)).toHaveTextContent("подтверждён");
  expect(screen.queryByText(/private-/)).not.toBeInTheDocument();
  rerender(
    <TaskStatus
      task={{
        ...task,
        input_summary: {
          template_id: "",
          template_version: "",
          template_filename: "",
          confirmed_at: null,
          job_title: "",
          job_version: null,
        },
      }}
    />,
  );
  expect(screen.getByText("Эталон: CV")).toBeInTheDocument();
  rerender(<TaskStatus task={makeTask()} />);
  expect(screen.queryByText(/Эталон:/)).not.toBeInTheDocument();
});
