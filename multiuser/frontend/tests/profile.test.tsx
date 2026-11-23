import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../src/App";
import { Profile } from "../src/Profile";
import { pdfTemplate, server, template } from "./fixtures";

it("uploads a template, reviews facts, saves the draft and confirms an active version", async () => {
  const backend = server();
  backend.state.profile.active = null;
  render(<App />);
  await screen.findByText(/11 вакансий/);
  await userEvent.click(
    screen.getByRole("button", { name: "Профиль и шаблон" }),
  );
  const main = screen.getByRole("main");
  await userEvent.click(
    within(main).getByRole("button", { name: "Задать эталонный шаблон" }),
  );
  await userEvent.upload(
    screen.getByLabelText("Файл эталонного CV"),
    new File(["valid docx"], "Baseline.docx"),
  );
  await screen.findByRole("heading", { name: "Факты из вашего CV" });
  expect(
    screen.getByRole("button", { name: "Сохранить эталон" }),
  ).toBeDisabled();
  await userEvent.clear(screen.getByRole("textbox", { name: "Факт 1" }));
  await userEvent.click(
    screen.getByRole("button", { name: "Сохранить черновик" }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Заполните или удалите пустые факты",
  );
  await userEvent.type(
    screen.getByRole("textbox", { name: "Факт 1" }),
    "API testing since 2020.",
  );
  await userEvent.click(screen.getByRole("button", { name: "Добавить факт" }));
  await userEvent.type(
    screen.getByRole("textbox", { name: "Факт 2" }),
    "English B2.",
  );
  await userEvent.click(screen.getByRole("button", { name: "Удалить факт 1" }));
  expect(screen.getByRole("textbox", { name: "Факт 1" })).toHaveValue(
    "English B2.",
  );
  await userEvent.type(
    screen.getByRole("textbox", { name: "Пожелания к документам" }),
    "Keep it concise.",
  );
  await userEvent.click(screen.getByText("Исходный текст CV"));
  expect(
    screen.getByText("Five years of API testing experience."),
  ).toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("button", { name: "Сохранить черновик" }),
  );
  await screen.findByText(/Черновик сохранён/);
  await userEvent.click(
    screen.getByRole("button", { name: "Закрыть уведомление" }),
  );
  await userEvent.click(screen.getByRole("checkbox"));
  await userEvent.click(
    screen.getByRole("button", { name: "Сохранить эталон" }),
  );
  await screen.findByText(/Эталон сохранён/);
  expect(backend.state.profile.active).toMatchObject({
    facts: [{ text: "English B2." }],
  });
  expect(backend.state.profile.active).toMatchObject({
    instructions: "Keep it concise.",
  });
  expect(
    backend.calls.find((call) => call.path.endsWith("/activate"))?.body,
  ).toEqual({ expected_version: null, confirmed: true });
  expect(
    within(main).getByRole("button", { name: "Изменить эталонный шаблон" }),
  ).toBeInTheDocument();
});

it("preserves the active template on upload failure, cancellation and version conflict", async () => {
  const backend = server();
  backend.state.profile.drafts = [structuredClone(template)];
  render(<App />);
  await screen.findByText(/11 вакансий/);
  await userEvent.click(
    screen.getByRole("button", { name: "Профиль и шаблон" }),
  );
  backend.failures.push({
    path: "/template-drafts",
    status: 422,
    detail: "Повреждённый DOCX",
  });
  await userEvent.upload(
    screen.getByLabelText("Файл эталонного CV"),
    new File(["invalid"], "Broken.docx"),
  );
  await screen.findByText("Повреждённый DOCX");
  expect(screen.getByText("Активный эталон")).toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("button", { name: /Candidate.docx.*Продолжить/ }),
  );
  await screen.findByText(
    "До сохранения нового эталона используется текущая версия.",
  );
  await userEvent.click(
    screen.getByRole("button", { name: "Закрыть черновик" }),
  );
  expect(
    screen.queryByRole("heading", { name: "Факты из вашего CV" }),
  ).not.toBeInTheDocument();
  expect(backend.state.profile.active?.version).toBe("1");
  await userEvent.click(
    screen.getByRole("button", { name: /Candidate.docx.*Продолжить/ }),
  );
  backend.failures.push({
    path: "/template-drafts/template-1/activate",
    status: 409,
    detail: "Эталон уже изменён в другой вкладке",
  });
  await userEvent.click(screen.getByRole("checkbox"));
  await userEvent.click(
    screen.getByRole("button", { name: "Сохранить эталон" }),
  );
  await screen.findByText("Эталон уже изменён в другой вкладке");
  expect(backend.state.profile.active?.version).toBe("1");
  expect(
    screen.getByRole("heading", { name: "Факты из вашего CV" }),
  ).toBeInTheDocument();
  expect(
    backend.calls.find((call) => call.path.endsWith("/activate"))?.body,
  ).toEqual({ expected_version: "1", confirmed: true });
});

it("rejects unsupported, empty and oversized input and handles cancelled file selection", async () => {
  server();
  render(
    <Profile
      profile={{ active: null, drafts: [] }}
      revision={0}
      onChange={vi.fn()}
    />,
  );
  const input = screen.getByLabelText("Файл эталонного CV");
  fireEvent.change(input, { target: { files: [] } });
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  fireEvent.change(input, {
    target: { files: [new File(["x"], "unsafe.html")] },
  });
  expect(screen.getByRole("alert")).toHaveTextContent(
    "Выберите файл PDF или DOCX",
  );
  fireEvent.change(input, { target: { files: [new File([], "empty.pdf")] } });
  expect(screen.getByRole("alert")).toHaveTextContent("непустым");
  const huge = new File(["x"], "huge.pdf");
  Object.defineProperty(huge, "size", { value: 10 * 1024 * 1024 + 1 });
  fireEvent.change(input, { target: { files: [huge] } });
  expect(screen.getByRole("alert")).toHaveTextContent("не больше 10 МБ");
  const bound = new File(["x"], "at-limit.PDF");
  Object.defineProperty(bound, "size", { value: 10 * 1024 * 1024 });
  fireEvent.change(input, { target: { files: [bound] } });
  await screen.findByRole("heading", { name: "Факты из вашего CV" });
});

it("rechecks confirmation when facts change and prevents activating an empty profile", async () => {
  server();
  render(
    <Profile
      profile={{ active: null, drafts: [structuredClone(template)] }}
      revision={0}
      onChange={vi.fn()}
    />,
  );
  await userEvent.click(screen.getByRole("button", { name: /Продолжить/ }));
  await userEvent.click(screen.getByRole("checkbox"));
  expect(
    screen.getByRole("button", { name: "Сохранить эталон" }),
  ).toBeEnabled();
  await userEvent.type(
    screen.getByRole("textbox", { name: "Факт 1" }),
    " More detail.",
  );
  expect(screen.getByRole("checkbox")).not.toBeChecked();
  await userEvent.click(screen.getByRole("button", { name: "Удалить факт 1" }));
  await userEvent.click(screen.getByRole("checkbox"));
  expect(
    screen.getByRole("button", { name: "Сохранить эталон" }),
  ).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: "Добавить факт" }));
  expect(screen.getByRole("checkbox")).not.toBeChecked();
});

it("keeps unsaved edits after a server error", async () => {
  const backend = server();
  backend.state.profile.drafts = [structuredClone(template)];
  render(
    <Profile profile={backend.state.profile} revision={0} onChange={vi.fn()} />,
  );
  await userEvent.click(screen.getByRole("button", { name: /Продолжить/ }));
  backend.failures.push({
    path: "/template-drafts/template-1",
    status: 500,
    detail: "Не удалось записать черновик",
  });
  await userEvent.type(
    screen.getByRole("textbox", { name: "Пожелания к документам" }),
    "Keep dates.",
  );
  await userEvent.click(
    screen.getByRole("button", { name: "Сохранить черновик" }),
  );
  await screen.findByText("Не удалось записать черновик");
  expect(
    screen.getByRole("textbox", { name: "Пожелания к документам" }),
  ).toHaveValue("Keep dates.");
  await userEvent.click(
    screen.getByRole("button", { name: "Сохранить черновик" }),
  );
  await waitFor(() =>
    expect(
      screen.queryByText("Не удалось записать черновик"),
    ).not.toBeInTheDocument(),
  );
});

it("shows uploaded PDF pages and keeps the source and paragraph kind after correcting a fact", async () => {
  const backend = server();
  render(
    <Profile profile={backend.state.profile} revision={0} onChange={vi.fn()} />,
  );
  await userEvent.upload(
    screen.getByLabelText("Файл эталонного CV"),
    new File(["pdf"], "Reference.pdf"),
  );
  await screen.findByRole("heading", { name: "Факты из вашего CV" });
  expect(
    screen.getByRole("img", { name: "Исходный PDF, страница 1" }),
  ).toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("button", {
      name: "Показать источник: страница 2, блок 1",
    }),
  );
  expect(
    screen.getByRole("img", { name: "Исходный PDF, страница 2" }),
  ).toBeInTheDocument();
  await userEvent.click(screen.getByRole("checkbox"));
  await userEvent.clear(screen.getByRole("textbox", { name: "Факт 2" }));
  await userEvent.type(
    screen.getByRole("textbox", { name: "Факт 2" }),
    "Four years of API testing.",
  );
  expect(screen.getByRole("checkbox")).not.toBeChecked();
  expect(
    screen.getByRole("button", { name: "Сохранить эталон" }),
  ).toBeDisabled();
  expect(screen.getByText(/Уточнено вами/)).toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("button", { name: "Сохранить черновик" }),
  );
  await screen.findByText(/Черновик сохранён/);
  expect(backend.state.profile.drafts[0].facts[1]).toEqual({
    id: "pdf-fact-2",
    text: "Four years of API testing.",
    source: "user_confirmed; original:page:2:block:1",
    kind: "body",
  });
  await userEvent.click(
    screen.getByRole("button", { name: "Закрыть черновик" }),
  );
  await userEvent.click(
    screen.getByRole("button", { name: /Reference.pdf.*Продолжить/ }),
  );
  expect(
    screen.getByRole("button", {
      name: "Показать источник: страница 2, блок 1",
    }),
  ).toBeInTheDocument();
});

it("reparses a legacy PDF into an unconfirmed draft without modifying the active profile", async () => {
  const backend = server();
  const active = pdfTemplate({
    status: "active",
    version: "old-version",
    reference: undefined,
  });
  backend.state.profile.active = active;
  render(
    <Profile profile={backend.state.profile} revision={0} onChange={vi.fn()} />,
  );
  expect(screen.getByText(/ещё нет изображений страниц/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Перечитать PDF" }));
  await screen.findByRole("heading", { name: "Факты из вашего CV" });
  expect(screen.getByText(/PDF прочитан заново/)).toBeInTheDocument();
  expect(screen.getByRole("checkbox")).not.toBeChecked();
  expect(
    screen.getByRole("button", { name: "Сохранить эталон" }),
  ).toBeDisabled();
  expect(backend.state.profile.active).toEqual(active);
  expect(
    backend.calls.find((call) => call.path.endsWith("/reparse")),
  ).toMatchObject({
    path: "/templates/pdf-template/reparse",
    method: "POST",
    body: {},
  });
  expect(
    screen.getByRole("link", { name: /Скачать исходный PDF/ }),
  ).toHaveAttribute("href", "/api/templates/pdf-template-reparsed/source");
  await userEvent.click(screen.getByRole("button", { name: "Перечитать PDF" }));
  await waitFor(() =>
    expect(
      screen.getByRole("link", { name: /Скачать исходный PDF/ }),
    ).toHaveAttribute(
      "href",
      "/api/templates/pdf-template-reparsed-reparsed/source",
    ),
  );
  expect(backend.state.profile.active).toEqual(active);
});

it("keeps the existing PDF profile when rereading is denied", async () => {
  const backend = server();
  const active = pdfTemplate({ status: "active", version: "active-version" });
  backend.state.profile.active = active;
  backend.failures.push({
    path: "/templates/pdf-template/reparse",
    status: 422,
    detail: "Исходный PDF повреждён",
  });
  render(
    <Profile profile={backend.state.profile} revision={0} onChange={vi.fn()} />,
  );
  await userEvent.click(screen.getByRole("button", { name: "Перечитать PDF" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Исходный PDF повреждён",
  );
  expect(
    screen.queryByRole("heading", { name: "Факты из вашего CV" }),
  ).not.toBeInTheDocument();
  expect(backend.state.profile.active).toEqual(active);
  expect(backend.state.profile.drafts).toHaveLength(0);
});
