import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { errorMessage, request, setCsrfToken, credentialsReady } from "./api";
import { Brand, Icon, Loading, Notice } from "./components";
import { SearchSettings } from "./SearchSettings";
import { Account } from "./Account";
import { Jobs } from "./Jobs";
import { Login } from "./Login";
import { Profile } from "./Profile";
import { WorkspaceStatus, type SetupPage } from "./WorkspaceStatus";
import { useRefreshEffect } from "./useRefreshEffect";
import type {
  AccountUser,
  Profile as ProfileData,
  Session,
  Task,
  View,
} from "./types";

export function Workspace({
  onLogout,
  user,
  csrfToken,
}: {
  onLogout: () => void;
  user?: AccountUser | null;
  csrfToken?: string;
}) {
  const [view, setView] = useState<
    View | "account" | "search_settings" | SetupPage
  >("active");
  const [profile, setProfile] = useState<ProfileData | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState("");
  const [connected, setConnected] = useState(true);
  const [loggingOut, setLoggingOut] = useState(false);
  const main = useRef<HTMLElement>(null);
  const reload = useCallback(() => setRevision((value) => value + 1), []);
  useEffect(() => {
    const heading = main.current?.querySelector("h1");
    heading?.setAttribute("tabindex", "-1");
    heading?.focus();
  }, [view]);
  const loadWorkspace = useCallback(async (signal: AbortSignal) => {
    try {
      const [data, results] = await Promise.all([
        request<ProfileData>("/profile", { signal }),
        request<{ items: Task[] }>("/tasks", { signal }),
      ]);
      if (signal.aborted) return;
      setProfile(data);
      setTasks(results.items);
      setError("");
    } catch (failure) {
      if (!signal.aborted) setError(errorMessage(failure));
    }
  }, []);
  useRefreshEffect(loadWorkspace, revision);
  useEffect(() => {
    let active = true;
    const events = new EventSource("/api/events");
    events.onopen = () => {
      if (!active) return;
      setConnected(true);
    };
    events.onerror = () => {
      if (active) setConnected(false);
    };
    const change = () => {
      if (active) reload();
    };
    events.onmessage = change;
    events.addEventListener("change", change);
    events.addEventListener("session-ended", () => {
      if (active) window.dispatchEvent(new Event("session-expired"));
    });
    events.addEventListener("maintenance", () => {
      if (active) window.dispatchEvent(new Event("maintenance-started"));
    });
    const timer = window.setInterval(change, 30000);
    return () => {
      active = false;
      events.close();
      window.clearInterval(timer);
    };
  }, [csrfToken, reload]);
  async function logout() {
    setLoggingOut(true);
    try {
      await request("/logout", { method: "POST" });
      onLogout();
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setLoggingOut(false);
    }
  }
  return (
    <div className="workspace">
      <a href="#main-content" className="skip-link">
        К содержимому
      </a>
      <aside className="sidebar">
        <Brand />
        <div className="workspace-label">РАБОЧЕЕ ПРОСТРАНСТВО</div>
        <nav aria-label="Основная навигация">
          <button
            className={view === "active" ? "nav-item selected" : "nav-item"}
            onClick={() => setView("active")}
            aria-current={view === "active" ? "page" : undefined}
          >
            <Icon name="jobs" />
            Вакансии<span className="nav-arrow">↗</span>
          </button>
          <button
            className={view === "applied" ? "nav-item selected" : "nav-item"}
            onClick={() => setView("applied")}
            aria-current={view === "applied" ? "page" : undefined}
          >
            <Icon name="applied" />
            Applied
          </button>
          <button
            className={view === "archive" ? "nav-item selected" : "nav-item"}
            onClick={() => setView("archive")}
            aria-current={view === "archive" ? "page" : undefined}
          >
            <Icon name="archive" />
            Архив
          </button>
          <button
            className={view === "profile" ? "nav-item selected" : "nav-item"}
            onClick={() => setView("profile")}
            aria-current={view === "profile" ? "page" : undefined}
          >
            <Icon name="profile" />
            Профиль и шаблон
          </button>
          {user && (
            <button
              className={
                ["search_settings", "criteria", "sources"].includes(view)
                  ? "nav-item selected"
                  : "nav-item"
              }
              onClick={() => setView("search_settings")}
              aria-current={
                ["search_settings", "criteria", "sources"].includes(view)
                  ? "page"
                  : undefined
              }
            >
              <Icon name="search" />
              Поиск и источники
            </button>
          )}
          {user && (
            <button
              className={
                ["account", "usage"].includes(view)
                  ? "nav-item selected"
                  : "nav-item"
              }
              onClick={() => setView("account")}
              aria-current={
                ["account", "usage"].includes(view) ? "page" : undefined
              }
            >
              <Icon name="access" />
              Аккаунт и доступ
            </button>
          )}
        </nav>
        <div className="sidebar-template">
          <div className="sidebar-template-icon">
            <Icon name="file" />
          </div>
          <h3>Сильный отклик начинается с вас</h3>
          <p>
            {profile?.active
              ? "Эталон готов. Создавайте CV и письма прямо из карточек вакансий."
              : "Задайте эталон CV, чтобы подготовить документы под каждую вакансию."}
          </p>
          <button onClick={() => setView("profile")} className="text-button">
            {profile?.active
              ? "Изменить эталонный шаблон"
              : "Задать эталонный шаблон"}{" "}
            <Icon name="arrow" size={15} />
          </button>
        </div>
        <div className="sidebar-bottom">
          <span className="private-label">
            <span className="status-dot" />
            Личное пространство
          </span>
          <button
            className="text-button"
            onClick={() => void logout()}
            disabled={loggingOut}
          >
            <Icon name="logout" size={16} />
            Выйти
          </button>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <span>Ваш поиск. Ваш следующий шаг.</span>
          <div className="topbar-account">
            {user && (
              <span
                className="current-user"
                aria-label="Текущий пользователь"
                title={user.display_name}
              >
                {user.display_name}
              </span>
            )}
            <button
              className="mobile-logout icon-button"
              onClick={() => void logout()}
              disabled={loggingOut}
              aria-label="Выйти из аккаунта"
            >
              <Icon name="logout" size={16} />
            </button>
            <div className="owner-avatar" aria-label="Личный аккаунт">
              {user?.display_name.slice(0, 1) || "Я"}
            </div>
          </div>
        </header>
        <main
          id="main-content"
          className="main-content"
          tabIndex={-1}
          ref={main}
        >
          {!connected && (
            <Notice tone="info">
              Связь с обновлениями потеряна. Повторяем подключение; данные
              обновляются каждые 30 секунд.
            </Notice>
          )}
          {error && (
            <Notice>
              {error}
              <button className="text-button" onClick={reload}>
                Повторить
              </button>
            </Notice>
          )}
          {user && <WorkspaceStatus revision={revision} onNavigate={setView} />}
          {["search_settings", "criteria", "sources"].includes(view) ? (
            <SearchSettings
              section={view === "criteria" || view === "sources" ? view : "all"}
              onSection={setView}
              onChange={reload}
            />
          ) : (view === "account" || view === "usage") && user ? (
            <Account
              key={view}
              user={user}
              onLogout={onLogout}
              initialRuntime={view === "usage"}
            />
          ) : view === "profile" ? (
            profile ? (
              <Profile
                profile={profile}
                revision={revision}
                onChange={reload}
              />
            ) : (
              <Loading text="Загружаем профиль…" />
            )
          ) : (
            <Jobs
              personal={!!user}
              key={view}
              tab={view === "applied" || view === "archive" ? view : "active"}
              revision={revision}
              tasks={tasks}
              hasTemplate={!!profile?.active}
              onProfile={() => setView("profile")}
              onTask={reload}
            />
          )}
          <footer className="main-footer">
            <span>getCareer</span>
            <span>Следующая хорошая возможность уже ближе.</span>
          </footer>
        </main>
      </div>
    </div>
  );
}

export function App() {
  const sessionVersion = useRef(0);
  const [session, setSession] = useState<Session | null>(null);
  const [accessLink, setAccessLink] = useState(() =>
    /(?:^#|&)(?:invite|reset)=/.test(window.location.hash)
      ? window.location.hash
      : "",
  );
  const [linkVersion, setLinkVersion] = useState(0);
  const [authNotice, setAuthNotice] = useState("");
  useLayoutEffect(() => {
    if (accessLink)
      window.history.replaceState(
        null,
        "",
        window.location.pathname + window.location.search,
      );
  }, [accessLink]);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  function updateSession(value: Session) {
    setAuthNotice(
      !value.authenticated &&
        new URLSearchParams(accessLink.slice(1)).has("reset")
        ? "Пароль обновлён. Войдите с новым паролем."
        : "",
    );
    sessionVersion.current += 1;
    setCsrfToken(value.csrf_token);
    setSession((previous) => ({
      ...value,
      account_mode: value.account_mode ?? previous?.account_mode,
    }));
    setAccessLink("");
    setError("");
  }
  function endSession() {
    sessionVersion.current += 1;
    setCsrfToken(undefined);
    setSession((previous) => ({
      authenticated: false,
      setup_required: false,
      account_mode: previous?.account_mode,
    }));
    setError("");
  }
  useEffect(() => {
    let mounted = true;
    const initialVersion = sessionVersion.current;
    request<Session>("/session")
      .then((value) => {
        if (mounted && initialVersion === sessionVersion.current) {
          setCsrfToken(value.csrf_token);
          setSession(value);
        }
      })
      .catch((failure) => {
        if (mounted) setError(errorMessage(failure));
      });
    const expired = async () => {
      const version = ++sessionVersion.current;
      await credentialsReady();
      if (!mounted || version !== sessionVersion.current) return;
      try {
        const current = await request<Session>("/session");
        if (mounted && version === sessionVersion.current) {
          setCsrfToken(current.csrf_token);
          setSession(current);
        }
      } catch {
        if (mounted && version === sessionVersion.current) endSession();
      }
    };
    const accessChanged = () => {
      if (/(?:^#|&)(?:invite|reset)=/.test(window.location.hash)) {
        setAccessLink(window.location.hash);
        setLinkVersion((value) => value + 1);
        setAuthNotice("");
      }
    };
    const paused = () => {
      sessionVersion.current += 1;
      setCsrfToken(undefined);
      setSession({
        authenticated: false,
        setup_required: false,
        maintenance: true,
      });
      setError("");
    };
    window.addEventListener("hashchange", accessChanged);
    window.addEventListener("session-expired", expired);
    window.addEventListener("session-changed", expired);
    window.addEventListener("maintenance-started", paused);
    return () => {
      mounted = false;
      window.removeEventListener("hashchange", accessChanged);
      window.removeEventListener("session-expired", expired);
      window.removeEventListener("session-changed", expired);
      window.removeEventListener("maintenance-started", paused);
    };
  }, [retry]);
  if (session?.maintenance)
    return (
      <main className="maintenance-screen">
        <Brand />
        <section className="maintenance-card" aria-label="Обслуживание сервиса">
          <div className="maintenance-icon" aria-hidden="true">
            <Icon name="refresh" size={28} />
          </div>
          <div className="maintenance-label">СЕРВИС ОБНОВЛЯЕТСЯ</div>
          <h1>Плановое обслуживание</h1>
          <p>Мы обновляем сервис. Попробуйте зайти позже.</p>
          <button
            className="button primary"
            onClick={() => {
              sessionVersion.current += 1;
              setError("");
              setSession(null);
              setRetry((value) => value + 1);
            }}
          >
            Проверить доступность
          </button>
        </section>
      </main>
    );
  if (!session)
    return (
      <main className="startup">
        {error ? (
          <Notice>
            {error}
            <button
              className="button secondary"
              onClick={() => {
                setError("");
                setRetry((value) => value + 1);
              }}
            >
              Повторить
            </button>
          </Notice>
        ) : (
          <Loading text="Открываем ваше пространство…" />
        )}
      </main>
    );
  return session.authenticated && !accessLink ? (
    <Workspace
      key={session.user?.id ?? "legacy"}
      user={session.user}
      csrfToken={session.csrf_token}
      onLogout={endSession}
    />
  ) : (
    <Login
      key={accessLink ? `access-${linkVersion}` : "login"}
      session={session}
      onLogin={updateSession}
      accessLink={accessLink}
      notice={authNotice}
      onCancelAccess={() => {
        setAccessLink("");
        setAuthNotice("");
      }}
    />
  );
}
