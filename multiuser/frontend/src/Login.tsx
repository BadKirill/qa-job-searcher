import { useEffect, useId, useState } from "react";
import { errorMessage, request, withCredentialChange } from "./api";
import { Brand, Icon, Notice } from "./components";
import type { Session } from "./types";

export function Login({
  session,
  onLogin,
  accessLink,
  notice,
  onCancelAccess,
}: {
  session: Session;
  onLogin: (session: Session) => void;
  accessLink?: string;
  notice?: string;
  onCancelAccess?: () => void;
}) {
  const errorId = useId();
  const [link, setLink] = useState(() => {
    const values = new URLSearchParams(
      (accessLink ?? window.location.hash).slice(1),
    );
    const kind = values.has("invite")
      ? "invite"
      : values.has("reset")
        ? "reset"
        : "";
    return { kind, token: values.get(kind) ?? "" };
  });
  const [login, setLogin] = useState("");
  useEffect(() => {
    if (link.kind)
      window.history.replaceState(
        null,
        "",
        window.location.pathname + window.location.search,
      );
  }, [link.kind]);
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      onLogin(
        await withCredentialChange(() =>
          request<Session>(
            link.kind === "invite"
              ? "/invitations/accept"
              : link.kind === "reset"
                ? "/password-resets/accept"
                : session.setup_required
                  ? "/setup"
                  : "/login",
            {
              method: "POST",
              body: link.kind
                ? { token: link.token, password }
                : session.setup_required
                  ? { token, password }
                  : session.account_mode
                    ? { login, password }
                    : { password },
            },
          ),
        ),
      );
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="login-screen">
      <div className="login-intro">
        <Brand />
        <div>
          <p className="login-description">
            Поиск вакансий и подготовка документов для отклика.
          </p>
        </div>
      </div>
      <div className="login-form-wrap">
        <form className="login-form" onSubmit={submit}>
          <h1>
            {link.kind === "invite"
              ? "Принять приглашение"
              : link.kind === "reset"
                ? "Восстановить доступ"
                : session.setup_required
                  ? "Добро пожаловать"
                  : "Вход в getCareer"}
          </h1>
          <p className="muted">
            {link.kind === "invite"
              ? "Задайте пароль для своего личного кабинета."
              : link.kind === "reset"
                ? "Задайте новый пароль. Затем войдите со своим логином."
                : session.setup_required
                  ? "Задайте пароль, чтобы вакансии, резюме и документы были доступны только вам."
                  : "Войдите, чтобы продолжить поиск."}
          </p>
          {notice && <Notice tone="info">{notice}</Notice>}
          {error && (
            <div id={errorId}>
              <Notice>{error}</Notice>
            </div>
          )}
          {link.kind && !link.token && (
            <Notice>
              Одноразовая ссылка неполная. Попросите администратора выдать
              новую.
            </Notice>
          )}
          {session.setup_required && !link.kind && (
            <label>
              Код первоначальной настройки
              <input
                value={token}
                onChange={(event) => setToken(event.target.value)}
                autoComplete="off"
                required
                type="password"
              />
              <small>Код хранится в локальном файле настройки сервиса.</small>
            </label>
          )}
          {session.account_mode && !session.setup_required && !link.kind && (
            <label>
              Логин
              <input
                value={login}
                onChange={(event) => setLogin(event.target.value)}
                autoComplete="username"
                required
                maxLength={100}
                aria-invalid={!!error}
                aria-describedby={error ? errorId : undefined}
              />
            </label>
          )}
          <label>
            Пароль
            <input
              aria-label="Пароль"
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete={
                session.setup_required || link.kind
                  ? "new-password"
                  : "current-password"
              }
              required
              minLength={session.setup_required || link.kind ? 12 : 1}
              maxLength={256}
              aria-invalid={!!error}
              aria-describedby={
                error
                  ? errorId
                  : session.setup_required || link.kind
                    ? `${errorId}-hint`
                    : undefined
              }
            />
            {(session.setup_required || link.kind) && (
              <small id={`${errorId}-hint`}>Не менее 12 символов.</small>
            )}
          </label>
          <button
            className="button primary wide"
            disabled={busy || (!!link.kind && !link.token)}
          >
            {busy
              ? "Подождите…"
              : link.kind === "invite"
                ? "Принять приглашение"
                : link.kind === "reset"
                  ? "Сохранить новый пароль"
                  : session.setup_required
                    ? "Создать рабочее пространство"
                    : "Войти"}
            <Icon name="arrow" />
          </button>
          {link.kind ? (
            <button
              type="button"
              className="text-button"
              disabled={busy}
              onClick={() => {
                setLink({ kind: "", token: "" });
                setPassword("");
                setError("");
                onCancelAccess?.();
              }}
            >
              Вернуться ко входу
            </button>
          ) : (
            session.account_mode &&
            !session.setup_required && (
              <p className="muted small">
                Для регистрации или восстановления пароля попросите
                администратора выдать одноразовую ссылку.
              </p>
            )
          )}
        </form>
      </div>
    </main>
  );
}
