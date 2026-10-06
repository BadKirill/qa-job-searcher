import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  editedFactSource,
  pdfFactLocation,
  PdfFactSource,
  PdfReference,
} from "../src/PdfReference";
import { pdfTemplate } from "./fixtures";

it.each([
  "paragraph:1",
  "page:0:block:1",
  "page:1:block:0",
  "page:-1:block:2",
  "page:1:block:2<script>",
  "page:9007199254740992:block:1",
  "page:1:block:9007199254740992",
  "user_confirmed",
])("does not infer a PDF location from invalid source %s", (source) => {
  expect(pdfFactLocation(source)).toBeNull();
  expect(editedFactSource(source)).toBe("user_confirmed");
});

it("retains the original location of user-corrected PDF facts", () => {
  expect(pdfFactLocation("page:2:block:7")).toEqual({ page: 2, block: 7 });
  const source = editedFactSource("page:2:block:7");
  expect(source).toBe("user_confirmed; original:page:2:block:7");
  expect(editedFactSource(source)).toBe(source);
});

it("links only pages present in the reference and labels corrected evidence", async () => {
  const onSelect = vi.fn();
  const { rerender } = render(
    <PdfFactSource source="page:2:block:7" pageCount={2} onSelect={onSelect} />,
  );
  await userEvent.click(
    screen.getByRole("button", {
      name: "Показать источник: страница 2, блок 7",
    }),
  );
  expect(onSelect).toHaveBeenCalledWith(2, 7);
  rerender(
    <PdfFactSource
      source="user_confirmed; original:page:2:block:7"
      pageCount={2}
      onSelect={onSelect}
    />,
  );
  expect(screen.getByText(/Уточнено вами/)).toBeInTheDocument();
  rerender(
    <PdfFactSource source="page:3:block:7" pageCount={2} onSelect={onSelect} />,
  );
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
  expect(screen.getByText("Источник: page:3:block:7")).toBeInTheDocument();
});

it("opens the original PNG pages, paginates, zooms and downloads the source", async () => {
  const onReparse = vi.fn();
  const template = pdfTemplate({ id: "pdf/id" });
  render(
    <PdfReference
      template={template}
      busy={false}
      onReparse={onReparse}
      initiallyOpen
    />,
  );
  expect(
    screen.getByRole("img", { name: "Исходный PDF, страница 1" }),
  ).toHaveAttribute("src", "/api/templates/pdf%2Fid/pages/1?attempt=0");
  expect(
    screen.getByRole("button", { name: "Предыдущая страница PDF" }),
  ).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: "Увеличить" }));
  expect(
    screen.getByRole("button", { name: "Уместить по ширине" }),
  ).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("img").parentElement).toHaveClass("pdf-page-zoomed");
  await userEvent.click(
    screen.getByRole("button", { name: "Уместить по ширине" }),
  );
  expect(screen.getByRole("img").parentElement).not.toHaveClass(
    "pdf-page-zoomed",
  );
  await userEvent.click(
    screen.getByRole("button", { name: "Следующая страница PDF" }),
  );
  expect(
    screen.getByRole("img", { name: "Исходный PDF, страница 2" }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "Следующая страница PDF" }),
  ).toBeDisabled();
  expect(
    screen.getByRole("link", { name: /Открыть страницу/ }),
  ).toHaveAttribute("href", "/api/templates/pdf%2Fid/pages/2");
  await userEvent.click(
    screen.getByRole("button", { name: "Предыдущая страница PDF" }),
  );
  const source = screen.getByRole("link", { name: /Скачать исходный PDF/ });
  expect(source).toHaveAttribute("href", "/api/templates/pdf%2Fid/source");
  expect(source).toHaveAttribute("download", "Candidate.pdf");
  await userEvent.click(screen.getByRole("button", { name: "Перечитать PDF" }));
  expect(onReparse).toHaveBeenCalledOnce();
});

it("opens a selected fact page and keeps a collapsed reference unloaded", async () => {
  const template = pdfTemplate();
  const props = { template, busy: false, onReparse: vi.fn() };
  const { rerender } = render(<PdfReference {...props} />);
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
  rerender(
    <PdfReference {...props} selected={{ page: 2, block: 3, sequence: 1 }} />,
  );
  expect(
    await screen.findByRole("img", { name: "Исходный PDF, страница 2" }),
  ).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("страница 2, блок 3");
  await userEvent.click(screen.getByText("Страницы исходника"));
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
  rerender(
    <PdfReference {...props} selected={{ page: 2, block: 3, sequence: 2 }} />,
  );
  expect(
    await screen.findByRole("img", { name: "Исходный PDF, страница 2" }),
  ).toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("button", { name: "Предыдущая страница PDF" }),
  );
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  rerender(
    <PdfReference {...props} selected={{ page: 5, block: 3, sequence: 3 }} />,
  );
  expect(
    screen.getByRole("img", { name: "Исходный PDF, страница 1" }),
  ).toBeInTheDocument();
});

it("shows a recoverable error for denied or missing images without embedding the PDF", async () => {
  render(
    <PdfReference
      template={pdfTemplate()}
      busy={false}
      onReparse={vi.fn()}
      initiallyOpen
    />,
  );
  fireEvent.error(screen.getByRole("img"));
  expect(screen.getByRole("alert")).toHaveTextContent(
    "Не удалось загрузить страницу PDF",
  );
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
  expect(document.querySelector("iframe, object, embed")).toBeNull();
  await userEvent.click(
    screen.getByRole("button", { name: "Повторить загрузку страницы" }),
  );
  expect(screen.getByRole("img")).toHaveAttribute(
    "src",
    "/api/templates/pdf-template/pages/1?attempt=1",
  );
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("offers reparse for legacy references and prevents repeated clicks while busy", () => {
  const onReparse = vi.fn();
  render(
    <PdfReference
      template={pdfTemplate({ reference: undefined })}
      busy
      onReparse={onReparse}
    />,
  );
  expect(screen.getByText(/ещё нет изображений страниц/)).toBeInTheDocument();
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "Обрабатываем PDF…" }),
  ).toBeDisabled();
  expect(onReparse).not.toHaveBeenCalled();
});
