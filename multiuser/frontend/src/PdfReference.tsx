import { useEffect, useState } from "react";
import { Icon, Notice } from "./components";
import type { Template } from "./types";

export type PdfSelection = { page: number; block: number; sequence: number };

export function pdfFactLocation(source: string) {
  const match =
    /^(?:user_confirmed; original:)?page:([1-9]\d*):block:([1-9]\d*)$/.exec(
      source,
    );
  if (!match) return null;
  const page = Number(match[1]);
  const block = Number(match[2]);
  return Number.isSafeInteger(page) && Number.isSafeInteger(block)
    ? { page, block }
    : null;
}

export function editedFactSource(source: string) {
  const location = pdfFactLocation(source);
  return location
    ? `user_confirmed; original:page:${location.page}:block:${location.block}`
    : "user_confirmed";
}

export function PdfFactSource({
  source,
  pageCount,
  onSelect,
}: {
  source: string;
  pageCount: number;
  onSelect: (page: number, block: number) => void;
}) {
  const location = pdfFactLocation(source);
  return (
    <small className="fact-source">
      {location && location.page <= pageCount ? (
        <>
          {source.startsWith("user_confirmed;")
            ? "Уточнено вами · исходник: "
            : "Источник: "}
          <button
            type="button"
            className="text-button fact-source-link"
            onClick={() => onSelect(location.page, location.block)}
            aria-label={`Показать источник: страница ${location.page}, блок ${location.block}`}
          >
            Стр. {location.page}, блок {location.block}
          </button>
        </>
      ) : (
        <>Источник: {source}</>
      )}
    </small>
  );
}

function PdfPage({ templateId, page }: { templateId: string; page: number }) {
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [zoomed, setZoomed] = useState(false);
  const path = `/api/templates/${encodeURIComponent(templateId)}/pages/${page}`;
  return (
    <div className="pdf-page">
      <div className="pdf-page-actions">
        <span>Страница {page}</span>
        <div>
          <button
            className="text-button"
            type="button"
            aria-pressed={zoomed}
            onClick={() => setZoomed((value) => !value)}
          >
            {zoomed ? "Уместить по ширине" : "Увеличить"}
          </button>
          <a
            className="text-button"
            href={path}
            target="_blank"
            rel="noopener noreferrer"
          >
            Открыть страницу
            <Icon name="arrow" size={13} />
          </a>
        </div>
      </div>
      {failed ? (
        <Notice>
          Не удалось загрузить страницу PDF. Проверьте соединение и доступ к
          аккаунту.
          <button
            type="button"
            className="text-button"
            onClick={() => {
              setFailed(false);
              setAttempt((value) => value + 1);
            }}
          >
            Повторить загрузку страницы
          </button>
        </Notice>
      ) : (
        <div className={`pdf-page-image${zoomed ? " pdf-page-zoomed" : ""}`}>
          <img
            key={attempt}
            src={`${path}?attempt=${attempt}`}
            alt={`Исходный PDF, страница ${page}`}
            onError={() => setFailed(true)}
          />
        </div>
      )}
    </div>
  );
}

export function PdfReference({
  template,
  selected,
  onReparse,
  busy,
  initiallyOpen = false,
}: {
  template: Template;
  selected?: PdfSelection | null;
  onReparse: () => void;
  busy: boolean;
  initiallyOpen?: boolean;
}) {
  const [open, setOpen] = useState(initiallyOpen);
  const [page, setPage] = useState(1);
  const pageCount = template.reference?.page_count ?? 0;
  useEffect(() => {
    if (selected && selected.page <= pageCount) {
      setPage(selected.page);
      setOpen(true);
    }
  }, [selected, pageCount]);

  return (
    <section className="pdf-reference" aria-label="Исходный PDF">
      <header className="pdf-reference-heading">
        <div>
          <span className="eyebrow">СВЕРЬТЕСЬ С ОРИГИНАЛОМ</span>
          <h3>Исходный PDF</h3>
          <p>{template.filename}</p>
        </div>
        <Icon name="file" size={23} />
      </header>
      <p className="muted pdf-reference-explanation">
        Сравните текст, порядок разделов и оформление. Выберите источник факта,
        чтобы открыть нужную страницу и сверить абзац с оригиналом.
      </p>
      <div className="pdf-reference-actions">
        <a
          className="text-button"
          href={`/api/templates/${encodeURIComponent(template.id)}/source`}
          download={template.filename}
        >
          Скачать исходный PDF
          <Icon name="arrow" size={13} />
        </a>
        <button
          type="button"
          className="text-button"
          onClick={onReparse}
          disabled={busy}
        >
          <Icon name="refresh" size={13} />
          {busy ? "Обрабатываем PDF…" : "Перечитать PDF"}
        </button>
      </div>
      {pageCount > 0 ? (
        <details
          className="pdf-pages"
          open={open}
          onToggle={(event) => setOpen(event.currentTarget.open)}
        >
          <summary>
            Страницы исходника <span>{pageCount}</span>
          </summary>
          {open && (
            <>
              <nav className="pdf-pagination" aria-label="Страницы PDF">
                <button
                  type="button"
                  className="page-button"
                  aria-label="Предыдущая страница PDF"
                  disabled={page === 1}
                  onClick={() => setPage((value) => value - 1)}
                >
                  ←
                </button>
                <span>
                  Страница {page} из {pageCount}
                </span>
                <button
                  type="button"
                  className="page-button"
                  aria-label="Следующая страница PDF"
                  disabled={page === pageCount}
                  onClick={() => setPage((value) => value + 1)}
                >
                  →
                </button>
              </nav>
              {selected?.page === page && (
                <p className="pdf-selected-source" role="status">
                  Источник выбранного факта: страница {page}, блок{" "}
                  {selected.block}.
                </p>
              )}
              <PdfPage
                key={`${template.id}:${page}`}
                templateId={template.id}
                page={page}
              />
            </>
          )}
        </details>
      ) : (
        <p className="pdf-legacy-note">
          У этого эталона ещё нет изображений страниц. Нажмите «Перечитать PDF»,
          чтобы получить новый черновик для проверки. Активный эталон сохранится
          до подтверждения.
        </p>
      )}
    </section>
  );
}
