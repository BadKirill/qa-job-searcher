import { useEffect, useState } from "react";
import { request, errorMessage } from "./api";
import { Loading, Notice } from "./components";

type Pilot = {
  phase: "not_started" | "pair" | "cohort";
  limit: number | null;
  reserved: number;
  pair_verified: boolean;
};

export function PilotStatus() {
  const [status, setStatus] = useState<Pilot | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setError("");
    setStatus(null);
    request<Pilot>("/admin/pilot", { signal: controller.signal })
      .then((value) => {
        if (controller.signal.aborted) return;
        if (
          !value ||
          !["not_started", "pair", "cohort"].includes(value.phase) ||
          !Number.isInteger(value.reserved) ||
          value.reserved < 0 ||
          typeof value.pair_verified !== "boolean" ||
          (value.phase === "pair" && value.limit !== 2) ||
          (value.phase === "cohort" &&
            (value.limit !== 20 || !value.pair_verified)) ||
          (value.phase === "not_started" && value.limit !== null)
        )
          throw new Error("Не удалось получить состояние пилота.");
        setStatus(value);
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(errorMessage(failure));
      });
    return () => controller.abort();
  }, [revision]);
  return (
    <section className="pilot-panel" aria-label="Закрытый пилот">
      <h2>Закрытый пилот</h2>
      {error && <Notice>{error}</Notice>}
      {!status && !error && <Loading />}
      {status &&
        (status.phase === "not_started" ? (
          <p>Закрытый пилот ещё не подготовлен.</p>
        ) : (
          <>
            <p>
              Зарезервировано мест: {status.reserved} из {status.limit}.
            </p>
            <p>
              {status.phase === "pair"
                ? "Проверяются первые два аккаунта. Остальные приглашения откроются после завершения проверки."
                : "Проверка двух аккаунтов завершена. Можно приглашать участников закрытого пилота."}
            </p>
            <p>Неиспользованное приглашение тоже резервирует место.</p>
          </>
        ))}
      <button
        className="button secondary"
        onClick={() => setRevision((value) => value + 1)}
      >
        Обновить состояние пилота
      </button>
    </section>
  );
}
