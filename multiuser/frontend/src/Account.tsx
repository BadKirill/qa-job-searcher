import { useEffect, useRef, useState } from "react";
import {
  errorMessage,
  request,
  setCsrfToken,
  withCredentialChange,
} from "./api";
import { Loading, Notice } from "./components";
import type { AccountUser, Session } from "./types";
import { RuntimeSettings } from "./RuntimeSettings";
import { PilotStatus } from "./PilotStatus";

type Member = AccountUser & { disabled: boolean; pending: boolean };
type Invitation = {
  id: string;
  user_id: string;
  expires_at: number;
  used_at: number | null;
  revoked_at: number | null;
};
type Access = { id: string; token: string; expires_at: number };
type Confirmation = { title: string; description: string; label: string; run: () => Promise<void> };

export function Account({
  user,
  onLogout,
  initialRuntime = false,
}: {
  user: AccountUser;
  onLogout: () => void;
  initialRuntime?: boolean;
}) {
  const [members, setMembers] = useState<Member[] | null>(null);
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [access, setAccess] = useState("");
  const [resetId, setResetId] = useState("");
  const [login, setLogin] = useState("");
  const [name, setName] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [password, setPassword] = useState("");
  const [showRuntime, setShowRuntime] = useState(initialRuntime);
  const [displayName, setDisplayName] = useState(user.display_name);
  const [loadError, setLoadError] = useState("");
  const [showPilot, setShowPilot] = useState(false);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const errorSummary = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (confirmation) dialog.current?.showModal();
    else dialog.current?.close();
  }, [confirmation]);
  useEffect(() => {
    if (error) errorSummary.current?.focus();
  }, [error]);
  useEffect(() => {
    if (user.role !== "admin") return;
    const controller = new AbortController();
    setLoadError("");
    Promise.all([
      request<{ items: Member[] }>("/admin/users", {
        signal: controller.signal,
      }),
      request<{ items: Invitation[] }>("/admin/invitations", {
        signal: controller.signal,
      }),
    ])
      .then(([users, links]) => {
        if (controller.signal.aborted) return;
        setMembers(users.items);
        setInvitations(links.items);
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setLoadError(errorMessage(failure));
      });
    return () => controller.abort();
  }, [user.role, revision]);
  async function action(operation: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    setAccess("");
    setResetId("");
    try {
      await operation();
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  }
  function showLink(issued: Access, kind: "invite" | "reset") {
    setResetId(kind === "reset" ? issued.id : "");
    setAccess(
      `${window.location.origin}/#${kind}=${encodeURIComponent(issued.token)}`,
    );
    setNotice(
      `Ссылка действует до ${new Date(issued.expires_at * 1000).toLocaleString()}. Скопируйте и передайте её адресату.`,
    );
    setRevision((value) => value + 1);
  }
  return (
    <section className="account-settings">
      <header className="page-heading">
        <div>
          <h1>Аккаунт и доступ</h1>
          <p className="muted">{user.display_name} · {user.login}</p>
        </div>
      </header>
      <div className="account-sections">
        <button
          className="button secondary"
          onClick={() => setShowRuntime((value) => !value)}
          aria-expanded={showRuntime}
        >
          Расписание и лимиты
        </button>
        {user.role === "admin" && (
          <button
            className="button secondary"
            aria-expanded={showPilot}
            onClick={() => setShowPilot((value) => !value)}
          >
            Состояние закрытого пилота
          </button>
        )}
      </div>
      {showRuntime && <RuntimeSettings admin={user.role === "admin"} />}
      {user.role === "admin" && showPilot && <PilotStatus />}
      {error && <div ref={errorSummary} tabIndex={-1}><Notice>{error}</Notice></div>}
      {notice && <Notice tone="success">{notice}</Notice>}
      <dialog ref={dialog} className="workspace-dialog" aria-labelledby="account-confirmation-title"
        aria-describedby="account-confirmation-description" onCancel={(event) => {
          event.preventDefault();
          setConfirmation(null);
        }}>
        {confirmation && <div className="confirmation-content">
          <h2 id="account-confirmation-title">{confirmation.title}</h2>
          <p id="account-confirmation-description">{confirmation.description}</p>
          <div className="source-actions">
            <button type="button" className="button secondary" onClick={() => setConfirmation(null)}>Отмена</button>
            <button type="button" className="button primary" disabled={busy} onClick={() => {
              setConfirmation(null);
              void action(confirmation.run);
            }}>{confirmation.label}</button>
          </div>
        </div>}
      </dialog>
      {loadError && !error && (
        <Notice>
          {loadError}
          <button
            className="text-button"
            onClick={() => setRevision((value) => value + 1)}
          >
            Повторить загрузку администрирования
          </button>
        </Notice>
      )}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void action(async () => {
            const updated = await request<AccountUser>("/account", {
              method: "PATCH",
              body: { display_name: displayName.trim() },
            });
            setDisplayName(updated.display_name);
            setNotice("Имя обновлено.");
            window.dispatchEvent(new Event("session-changed"));
          });
        }}
      >
        <h2>Личные данные</h2>
        <label>
          Отображаемое имя
          <input
            required
            maxLength={120}
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            autoComplete="name"
          />
        </label>
        <button className="button primary" disabled={busy}>
          Сохранить имя
        </button>
      </form>
      {access && (
        <label>
          Одноразовая ссылка
          <input
            value={access}
            readOnly
            onFocus={(event) => event.target.select()}
          />
        </label>
      )}
      {resetId && (
        <button
          className="button secondary"
          disabled={busy}
          onClick={() =>
            void action(async () => {
              await request(
                `/admin/password-resets/${encodeURIComponent(resetId)}`,
                { method: "DELETE" },
              );
              setNotice("Ссылка восстановления отозвана.");
            })
          }
        >
          Отозвать ссылку восстановления
        </button>
      )}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void action(async () => {
            const session = await withCredentialChange(() =>
              request<Session>("/account/password", {
                method: "POST",
                body: { current_password: currentPassword, password },
              }),
            );
            setCsrfToken(session.csrf_token);
            setCurrentPassword("");
            setPassword("");
            setNotice("Пароль изменён. Остальные сессии завершены.");
          });
        }}
      >
        <h2>Смена пароля</h2>
        <label>
          Текущий пароль
          <input
            type="password"
            autoComplete="current-password"
            required
            value={currentPassword}
            onChange={(event) => setCurrentPassword(event.target.value)}
            maxLength={256}
          />
        </label>
        <label>
          Новый пароль
          <input
            type="password"
            autoComplete="new-password"
            required
            minLength={12}
            maxLength={256}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        <button className="button primary" disabled={busy}>
          Изменить пароль
        </button>
      </form>
      <button
        className="button secondary"
        disabled={busy}
        onClick={() => setConfirmation({
          title: "Завершить все сессии?",
          description: "Вы выйдете из аккаунта на всех устройствах, включая это. Для продолжения потребуется войти снова.",
          label: "Завершить сессии",
          run: async () => {
            await request("/account/sessions/revoke", { method: "POST" });
            onLogout();
          },
        })}
      >
        Завершить все сессии
      </button>
      {user.role === "admin" && (
        <>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void action(async () => {
                showLink(
                  await request<Access>("/admin/invitations", {
                    method: "POST",
                    body: { login, display_name: name },
                  }),
                  "invite",
                );
                setLogin("");
                setName("");
              });
            }}
          >
            <h2>Пригласить пользователя</h2>
            <label>
              Логин нового пользователя
              <input
                autoComplete="off"
                required
                minLength={3}
                maxLength={100}
                value={login}
                onChange={(event) => setLogin(event.target.value)}
              />
            </label>
            <label>
              Имя пользователя
              <input
                autoComplete="off"
                required
                maxLength={120}
                value={name}
                onChange={(event) => setName(event.target.value)}
              />
            </label>
            <button className="button primary" disabled={busy}>
              Создать приглашение
            </button>
          </form>
          <h2>Пользователи</h2>
          {!members ? (
            loadError ? (
              <p>Список пользователей временно недоступен.</p>
            ) : (
              <Loading text="Загружаем пользователей…" />
            )
          ) : (
            <ul className="account-members">
              {members.map((member) => (
                <li key={member.id}>
                  <strong>
                    {member.display_name} · {member.login}
                  </strong>
                  <span className="status-badge" data-state={member.disabled ? "disabled" : member.pending ? "pending" : "active"}>
                    {member.disabled
                      ? "Отключён"
                      : member.pending
                        ? "Ожидает приглашения"
                        : "Активен"}
                  </span>
                  {!member.disabled && (
                    <button
                      className="button secondary"
                      disabled={busy}
                      onClick={() =>
                        void action(async () => {
                          showLink(
                            await request<Access>(
                              `/admin/users/${encodeURIComponent(member.id)}/${member.pending ? "invitations" : "password-resets"}`,
                              { method: "POST" },
                            ),
                            member.pending ? "invite" : "reset",
                          );
                        })
                      }
                    >
                      {member.pending
                        ? "Новое приглашение"
                        : "Ссылка восстановления"}
                    </button>
                  )}
                  {member.id !== user.id && (
                    <button
                      className="button secondary"
                      disabled={busy}
                      onClick={() => {
                        const run = async () => {
                          await request(
                            `/admin/users/${encodeURIComponent(member.id)}`,
                            {
                              method: "PATCH",
                              body: { disabled: !member.disabled },
                            },
                          );
                          setRevision((value) => value + 1);
                        };
                        if (member.disabled) void action(run);
                        else setConfirmation({
                          title: `Отключить аккаунт ${member.display_name}?`,
                          description: "Доступ будет закрыт, текущие сессии завершатся. Аккаунт можно включить снова.",
                          label: "Отключить доступ",
                          run,
                        });
                      }}
                    >
                      {member.disabled
                        ? "Включить аккаунт"
                        : "Отключить аккаунт"}
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
          <h2>Приглашения</h2>
          {members &&
            !invitations.some(
              (item) =>
                !item.used_at &&
                !item.revoked_at &&
                item.expires_at * 1000 > Date.now(),
            ) && <p>Нет действующих приглашений.</p>}
          {invitations
            .filter(
              (item) =>
                !item.used_at &&
                !item.revoked_at &&
                item.expires_at * 1000 > Date.now(),
            )
            .map((item) => (
              <p key={item.id}>
                {members?.find((member) => member.id === item.user_id)?.login ??
                  "Приглашение"}
                <button
                  className="button secondary"
                  disabled={busy}
                  onClick={() =>
                    void action(async () => {
                      await request(
                        `/admin/invitations/${encodeURIComponent(item.id)}`,
                        { method: "DELETE" },
                      );
                      setRevision((value) => value + 1);
                    })
                  }
                >
                  Отозвать приглашение
                </button>
              </p>
            ))}
        </>
      )}
    </section>
  );
}
