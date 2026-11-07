import { useEffect, useState } from "react";
import { errorMessage, request, setCsrfToken } from "./api";
import { Icon, Loading, Notice, TaskStatus } from "./components";
import { Jobs } from "./Jobs";
import { Login } from "./Login";
import { Profile } from "./Profile";
import type { Profile as ProfileData, Session, Task, View } from "./types";

export function Workspace({ onLogout }: { onLogout: () => void }) {
  const [view, setView] = useState<View>("active");
  const [profile, setProfile] = useState<ProfileData | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState("");
  const [connected, setConnected] = useState(true);
  const [loggingOut, setLoggingOut] = useState(false);
  const reload = () => setRevision((value) => value + 1);
  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      request<ProfileData>("/profile", { signal: controller.signal }),
      request<{ items: Task[] }>("/tasks", { signal: controller.signal }),
    ])
      .then(([data, results]) => {
        setProfile(data);
        setTasks(results.items);
        setError("");
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(errorMessage(failure));
      });
    return () => controller.abort();
  }, [revision]);
  useEffect(() => {
    const events = new EventSource("/api/events");
    events.onopen = () => {
      setConnected(true);
      reload();
    };
    events.onerror = () => setConnected(false);
    events.onmessage = reload;
    events.addEventListener("change", reload);
    const timer = window.setInterval(reload, 30000);
    return () => {
      events.close();
      window.clearInterval(timer);
    };
  }, []);
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
        <a className="brand" href="/" aria-label="QA Job Searcher">
          <span className="brand-mark">
            qa<span>.</span>
          </span>
          <span>job searcher</span>
        </a>
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
            <button
              className="mobile-logout icon-button"
              onClick={() => void logout()}
              disabled={loggingOut}
              aria-label="Выйти из аккаунта"
            >
              <Icon name="logout" size={16} />
            </button>
            <div className="owner-avatar" aria-label="Личный аккаунт">
              Я
            </div>
          </div>
        </header>
        <main id="main-content" className="main-content">
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
          {view === "profile" ? (
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
              key={view}
              tab={view}
              revision={revision}
              tasks={tasks}
              hasTemplate={!!profile?.active}
              onProfile={() => setView("profile")}
              onTask={reload}
            />
          )}
          <section className="background-tasks" aria-label="Состояние поиска">
            {tasks
              .filter(
                (task) => task.kind === "search" && task.status !== "ready",
              )
              .slice(0, 1)
              .map((task) => (
                <TaskStatus key={task.id} task={task} />
              ))}
          </section>
          <footer className="main-footer">
            <span>QA JOB SEARCHER</span>
            <span>Следующая хорошая возможность уже ближе.</span>
          </footer>
        </main>
      </div>
    </div>
  );
}

export function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  function updateSession(value: Session) {
    setCsrfToken(value.csrf_token);
    setSession(value);
    setError("");
  }
  useEffect(() => {
    let mounted = true;
    request<Session>("/session")
      .then((value) => {
        if (mounted) updateSession(value);
      })
      .catch((failure) => {
        if (mounted) setError(errorMessage(failure));
      });
    const expired = () =>
      updateSession({ authenticated: false, setup_required: false });
    window.addEventListener("session-expired", expired);
    return () => {
      mounted = false;
      window.removeEventListener("session-expired", expired);
    };
  }, [retry]);
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
  return session.authenticated ? (
    <Workspace
      onLogout={() =>
        updateSession({ authenticated: false, setup_required: false })
      }
    />
  ) : (
    <Login session={session} onLogin={updateSession} />
  );
}
