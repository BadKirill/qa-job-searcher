import { useEffect, useId, useState } from "react";
import { errorMessage, request } from "./api";

type Place = { id: string; label: string };

export function PlacePicker({
  label, kinds, values, onChange,
}: {
  label: string;
  kinds: string;
  values: string[];
  onChange: (values: string[]) => void;
}) {
  const id = useId();
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<Place[]>([]);
  const [known, setKnown] = useState<Record<string, string>>({});
  const [active, setActive] = useState(-1);
  const [open, setOpen] = useState(false);
  const [feedback, setFeedback] = useState("");
  const lookup = new URLSearchParams(values.map((value) => ["value", value])).toString();
  useEffect(() => {
    if (!lookup) return;
    const controller = new AbortController();
    void request<Record<string, Place[]>>(`/places/legacy?${lookup}`, { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) {
          setKnown(Object.fromEntries(Object.values(data).flat().map((item) => [item.id, item.label])));
        }
      }).catch(() => undefined);
    return () => controller.abort();
  }, [lookup]);
  useEffect(() => {
    if (!open || !query.trim()) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({ q: query, kind: kinds });
      void request<{ items: Place[]; corrected: boolean }>(`/places?${params}`, { signal: controller.signal })
        .then((data) => {
          if (controller.signal.aborted) return;
          setItems(data.items);
          setFeedback(data.corrected ? "Возможно, вы имели в виду" : data.items.length ? "" : "Локация не найдена");
        }).catch((failure) => {
          if (!controller.signal.aborted) setFeedback(errorMessage(failure));
        });
    }, 180);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [query, open, kinds]);
  const choices = items.filter((item) => !values.includes(item.id));
  function select(item: Place) {
    setKnown({ ...known, [item.id]: item.label });
    onChange([...values, item.id]);
    setItems([]);
    setQuery("");
    setActive(-1);
    setOpen(false);
  }
  return (
    <div className="place-picker">
      <label htmlFor={id}>{label}</label>
      <ul className="place-chips" aria-label={`Выбрано · ${label}`}>
        {values.map((value) => (
          <li key={value}>
            <button type="button" className="button secondary"
              aria-label={`Убрать ${known[value] ?? value}`}
              onClick={() => onChange(values.filter((item) => item !== value))}>
              {known[value] ?? value} ×
            </button>
          </li>
        ))}
      </ul>
      <input id={id} type="text" role="combobox" aria-autocomplete="list"
        aria-expanded={open && choices.length > 0} aria-controls={`${id}-options`}
        aria-activedescendant={open && active >= 0 && choices[active] ? `${id}-${active}` : undefined}
        value={query} maxLength={120} autoComplete="off"
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onChange={(event) => {
          setQuery(event.target.value); setOpen(true); setActive(-1); setItems([]); setFeedback("");
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape" && open) {
            event.preventDefault(); setOpen(false);
          } else if (choices.length && ["ArrowDown", "ArrowUp"].includes(event.key)) {
            event.preventDefault(); setOpen(true);
            setActive(event.key === "ArrowDown" ? Math.min(active + 1, choices.length - 1) : Math.max(active - 1, 0));
          } else if (event.key === "Enter" && query && open) {
            event.preventDefault();
            const item = choices[active];
            if (item) select(item);
          }
        }}
      />
      <span role="status">{open && query ? feedback : ""}</span>
      <ul id={`${id}-options`} role="listbox" aria-label={label} hidden={!open || !choices.length}>
        {choices.map((item, index) => (
          <li key={item.id} id={`${id}-${index}`} role="option" aria-selected={active === index}
            onMouseDown={(event) => event.preventDefault()} onClick={() => select(item)}>
            {item.label}
          </li>
        ))}
      </ul>
    </div>
  );
}
