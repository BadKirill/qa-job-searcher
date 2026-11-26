export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public code = "",
    public fields: Record<string, string> = {},
  ) {
    super(message);
  }
}

let csrfToken = "";
let credentialChanges: Promise<void> = Promise.resolve();

export function withCredentialChange<T>(
  operation: () => Promise<T>,
): Promise<T> {
  const result = operation();
  credentialChanges = result.then(
    () => undefined,
    () => undefined,
  );
  return result;
}

export async function credentialsReady() {
  await credentialChanges;
}

export function setCsrfToken(token?: string) {
  csrfToken = token ?? "";
}

export async function request<T>(
  path: string,
  options: { method?: string; body?: unknown; signal?: AbortSignal } = {},
): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  let body: BodyInit | undefined;
  if (options.body instanceof FormData) {
    body = options.body;
  } else if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  if (options.method && options.method !== "GET")
    headers["X-CSRF-Token"] = csrfToken;
  const response = await fetch(`/api${path}`, {
    ...options,
    body,
    headers,
    credentials: "same-origin",
  }).catch((error: unknown) => {
    if (options.signal?.aborted) throw error;
    throw new ApiError(0, "Нет связи с сервером. Проверьте подключение и повторите.", "network");
  });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    const fallback: Record<number, string> = {
      401: "Сессия закончилась. Войдите снова.",
      413: path === "/template-drafts" ? "Максимальный размер шаблона — 10 МБ." : "Запрос слишком большой.",
      422: "Проверьте заполненные данные.",
      429: "Достигнут лимит запросов. Подождите и повторите.",
      502: "Сервис временно недоступен. Попробуйте позже.",
      503: "Сервис временно недоступен. Попробуйте позже.",
    };
    const detail = response.status === 401 || (response.status === 413 && path === "/template-drafts")
      ? fallback[response.status]
      : typeof data?.detail === "string" ? data.detail
      : fallback[response.status] ?? "Запрос не выполнен. Попробуйте ещё раз.";
    if (
      response.status === 503 &&
      data?.maintenance === true &&
      !options.signal?.aborted
    )
      window.dispatchEvent(new Event("maintenance-started"));
    if (
      response.status === 401 &&
      path !== "/session" &&
      !options.signal?.aborted
    )
      window.dispatchEvent(new Event("session-expired"));
    throw new ApiError(response.status, detail, data?.code, data?.fields ?? {});
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function errorMessage(error: unknown) {
  return error instanceof Error
    ? error.message
    : "Не удалось связаться с сервером. Попробуйте ещё раз.";
}

export function safeJobUrl(value: string) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) &&
      !url.username &&
      !url.password
      ? url.href
      : null;
  } catch {
    return null;
  }
}

export function requestId() {
  return crypto.randomUUID();
}

export function operationKey(storage: Map<string, string>, operation: string) {
  const id = storage.get(operation) ?? requestId();
  storage.set(operation, id);
  return id;
}

export function forgetOperation(storage: Map<string, string>, operation: string, error?: unknown) {
  if (error instanceof ApiError && (error.status === 0 || error.status >= 500)) return;
  storage.delete(operation);
}

export function taskLabel(status: string, kind?: string) {
  if (kind === "posting_check" && status === "queued") return "Проверка объявления в очереди";
  if (kind === "posting_check" && status === "running") return "Проверяем оригинал…";
  if (kind === "search" && status === "queued") return "Поиск в очереди";
  if (kind === "search" && status === "running") return "Собираем вакансии";
  if (kind === "cv" && status === "running") return "Создаём CV";
  if (kind === "cover_letter" && status === "running") return "Создаём сопроводительное письмо";
  const labels: Record<string, string> = {
    queued: "В очереди",
    running: "Создаётся",
    verifying: "Проверяем факты",
    rendering: "Собираем документ",
    ready: "Готово",
    needs_input: "Нужны данные",
    blocked: "Проверка не пройдена",
    failed: "Ошибка",
    cancelled: "Отменено",
  };
  return labels[status] ?? status;
}

export function isPending(status: string) {
  return ["queued", "running", "verifying", "rendering"].includes(status);
}
