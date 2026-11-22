import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { JobCard, Jobs } from "../src/Jobs";
import { makeJob, server } from "./fixtures";

it("renders a refreshed personal score independently of the shared vacancy identifier", () => {
  const job = makeJob("shared-public-id", {
    score: 80,
    match_reasons: ["предпочтение Python"],
  });
  const props = {
    tasks: [], hasTemplate: false, onProfile: vi.fn(),
    onState: vi.fn(), onTask: vi.fn(), revision: 0,
  };
  const { rerender } = render(<JobCard job={job} {...props} />);
  expect(screen.getByText("80/100")).toBeVisible();
  expect(screen.getByText("предпочтение Python")).toBeVisible();
  rerender(<JobCard job={{ ...job, score: 25, match_reasons: ["предпочтение Figma"] }} {...props} />);
  expect(screen.getByText("25/100")).toBeVisible();
  expect(screen.queryByText("80/100")).not.toBeInTheDocument();
  expect(screen.queryByText("предпочтение Python")).not.toBeInTheDocument();
  expect(screen.getByText("предпочтение Figma")).toBeVisible();
});

it("surfaces an unavailable personal snapshot and recovers through a fresh API request", async () => {
  const backend = server();
  backend.failures.push({ path: "/jobs", status: 404, detail: "Выдача недоступна. Обновите страницу." });
  render(<Jobs tab="active" tasks={[]} hasTemplate={false} revision={0}
    onProfile={vi.fn()} onTask={vi.fn()} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Выдача недоступна");
  expect(screen.queryByRole("article")).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Повторить" }));
  expect(await screen.findAllByRole("article")).toHaveLength(10);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});
