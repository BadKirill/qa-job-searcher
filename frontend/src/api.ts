export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

let csrfToken = "";

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
  });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    const detail =
      typeof data?.detail === "string"
        ? data.detail
        : "Запрос не выполнен. Попробуйте ещё раз.";
    if (response.status === 401)
      window.dispatchEvent(new Event("session-expired"));
    throw new ApiError(response.status, detail);
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

export function taskLabel(status: string) {
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
