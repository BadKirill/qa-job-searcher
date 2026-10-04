import { useState } from "react";
import { errorMessage, request } from "./api";
import { Icon, Notice } from "./components";
import type { Session } from "./types";

export function Login({
  session,
  onLogin,
}: {
  session: Session;
  onLogin: (session: Session) => void;
}) {
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
        await request<Session>(session.setup_required ? "/setup" : "/login", {
          method: "POST",
          body: session.setup_required ? { token, password } : { password },
        }),
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
        <a className="brand" href="/" aria-label="QA Job Searcher">
          <span className="brand-mark">
            qa<span>.</span>
          </span>
          <span>job searcher</span>
        </a>
        <div>
          <p className="eyebrow">МЕНЬШЕ ПОИСКА. БОЛЬШЕ ВОЗМОЖНОСТЕЙ.</p>
          <h1>
            Следующая работа
            <br />
            начинается здесь<span>.</span>
          </h1>
          <p className="login-description">
            Подходящие QA-вакансии, ваше резюме и продуманные отклики — в одном
            рабочем пространстве.
          </p>
        </div>
        <span className="login-footnote">Ваш личный поиск работы</span>
      </div>
      <div className="login-form-wrap">
        <form className="login-form" onSubmit={submit}>
          <div className="round-icon">
            <Icon name="profile" size={24} />
          </div>
          <p className="eyebrow">ЛИЧНОЕ ПРОСТРАНСТВО</p>
          <h2>
            {session.setup_required ? "Добро пожаловать" : "С возвращением"}
          </h2>
          <p className="muted">
            {session.setup_required
              ? "Задайте пароль, чтобы вакансии, резюме и документы были доступны только вам."
              : "Войдите, чтобы продолжить поиск."}
          </p>
          {error && <Notice>{error}</Notice>}
          {session.setup_required && (
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
          <label>
            Пароль
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete={
                session.setup_required ? "new-password" : "current-password"
              }
              required
              minLength={session.setup_required ? 12 : 1}
            />
            {session.setup_required && <small>Не менее 12 символов.</small>}
          </label>
          <button className="button primary wide" disabled={busy}>
            {busy
              ? "Подождите…"
              : session.setup_required
                ? "Создать рабочее пространство"
                : "Войти"}
            <Icon name="arrow" />
          </button>
        </form>
      </div>
    </main>
  );
}
