import { render, screen } from "@testing-library/react";
import baseline from "../../tests/fixtures/search-baseline.json";
import { JobCard } from "../src/Jobs";
import { makeJob } from "./fixtures";

it.each(baseline.cases.filter((item) => item.baseline.accepted))(
  "renders the $id fixture without inventing QA or language labels",
  (item) => {
    render(
      <JobCard
        job={makeJob(item.id, {
          ...item.job,
          url: `https://example.com/baseline/${item.id}`,
          score: 75,
          match_reasons: [`роль: ${item.profile}`],
          salary: null,
          details: {
            qa_types: [],
            languages: [],
            programming_languages: [],
            responsibilities: [],
            requirements: [],
          },
        })}
        tasks={[]}
        hasTemplate={false}
        onProfile={vi.fn()}
        onState={vi.fn()}
        onTask={vi.fn()}
        revision={0}
      />,
    );
    expect(screen.getByRole("article", { name: item.job.title })).toBeVisible();
    expect(screen.getByText(item.job.location)).toBeVisible();
    expect(screen.getByText("75/100")).toBeVisible();
    expect(screen.getByText(`роль: ${item.profile}`)).toBeVisible();
    expect(screen.queryByText("English B2")).not.toBeInTheDocument();
    expect(screen.queryByText("Auto", { exact: true })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Перейти на страницу вакансии" }))
      .toHaveAttribute("href", `https://example.com/baseline/${item.id}`);
  },
);
