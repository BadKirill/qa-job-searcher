import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Account } from "../src/Account";
import { App } from "../src/App";
import { Login } from "../src/Login";
import type { AccountUser, Session } from "../src/types";
import { server, TestEvents } from "./fixtures";

const admin: AccountUser = {
  id: "admin",
  login: "admin",
  display_name: "Administrator",
  role: "admin",
};
const user: AccountUser = {
  id: "member",
  login: "member",
  display_name: "Member",
  role: "user",
};
const session: Session = {
  authenticated: false,
  setup_required: false,
  account_mode: true,
};
const password = "synthetic-password-long";
afterEach(() => window.history.replaceState(null, "", "/"));

function accountServer() {
  let members = [
    { ...admin, disabled: false, pending: false },
    { ...user, disabled: false, pending: false },
    {
      ...user,
      id: "pending",
      login: "pending",
      display_name: "Pending",
      disabled: false,
      pending: true,
    },
    {
      ...user,
      id: "disabled",
      login: "disabled",
      display_name: "Disabled",
      disabled: true,
      pending: false,
    },
  ];
  let invitations = [
    {
      id: "active",
      user_id: "pending",
      expires_at: Date.now() / 1000 + 3600,
      used_at: null,
      revoked_at: null,
    },
    {
      id: "expired",
      user_id: "unknown",
      expires_at: 1,
      used_at: null,
      revoked_at: null,
    },
    {
      id: "used",
      user_id: "unknown",
      expires_at: Date.now() / 1000 + 3600,
      used_at: 1,
      revoked_at: null,
    },
    {
      id: "revoked",
      user_id: "unknown",
      expires_at: Date.now() / 1000 + 3600,
      used_at: null,
      revoked_at: 1,
    },
  ];
  const failures: string[] = [];
  const calls: { path: string; body: any; headers: any }[] = [];
  const fetcher = vi.fn(async (path: string, options: RequestInit = {}) => {
    const body =
      typeof options.body === "string" ? JSON.parse(options.body) : null;
    calls.push({ path, body, headers: options.headers });
    if (failures[0] === path) {
      failures.shift();
      return new Response('{"detail":"Synthetic failure"}', { status: 409 });
    }
    let value: unknown = {};
    if (path === "/api/admin/users") value = { items: members };
    else if (path === "/api/admin/invitations" && !options.method)
      value = { items: invitations };
    else if (options.method === "DELETE") {
      if (path.startsWith("/api/admin/invitations/")) invitations = [];
      return new Response(null, { status: 204 });
    } else if (options.method === "PATCH")
      members = members.map((member) =>
        path.endsWith(member.id)
          ? { ...member, disabled: body.disabled }
          : member,
      );
    else if (path === "/api/account/password")
      value = {
        authenticated: true,
        setup_required: false,
        csrf_token: "rotated-csrf",
        account_mode: true,
        user,
      };
    else
      value = {
        id: "new-access",
        token: "private-once-token",
        expires_at: Date.now() / 1000 + 3600,
        authenticated: false,
      };
    return new Response(JSON.stringify(value));
  });
  vi.stubGlobal("fetch", fetcher);
  return { failures, calls, fetcher };
}

it("requires a login in shared mode and sends it with the password", async () => {
  const backend = accountServer();
  const onLogin = vi.fn();
  render(<Login session={session} onLogin={onLogin} />);
  await userEvent.type(
    screen.getByLabelText("Логин", { exact: true }),
    "member",
  );
  await userEvent.type(
    screen.getByLabelText("Пароль", { exact: true }),
    password,
  );
  await userEvent.click(screen.getByRole("button", { name: "Войти" }));
  expect(backend.calls[0].body).toEqual({ login: "member", password });
  expect(onLogin).toHaveBeenCalled();
});

it.each(["invite", "reset"])(
  "consumes a %s fragment without retaining it in the address bar",
  async (kind) => {
    window.history.replaceState(null, "", `/#${kind}=secret-token`);
    const backend = accountServer();
    const onLogin = vi.fn();
    render(<Login session={session} onLogin={onLogin} />);
    expect(window.location.hash).toBe("");
    expect(
      screen.queryByLabelText("Логин", { exact: true }),
    ).not.toBeInTheDocument();
    await userEvent.type(
      screen.getByLabelText("Пароль", { exact: true }),
      password,
    );
    await userEvent.click(
      screen.getByRole("button", {
        name:
          kind === "invite" ? "Принять приглашение" : "Сохранить новый пароль",
      }),
    );
    expect(backend.calls[0].path).toBe(
      kind === "invite"
        ? "/api/invitations/accept"
        : "/api/password-resets/accept",
    );
    expect(backend.calls[0].body).toEqual({ token: "secret-token", password });
    expect(onLogin).toHaveBeenCalled();
  },
);

it("keeps expired invitation errors visible and permits retry", async () => {
  window.history.replaceState(null, "", "/#invite=expired");
  const backend = accountServer();
  backend.failures.push("/api/invitations/accept");
  render(<Login session={session} onLogin={vi.fn()} />);
  await userEvent.type(
    screen.getByLabelText("Пароль", { exact: true }),
    password,
  );
  await userEvent.click(
    screen.getByRole("button", { name: "Принять приглашение" }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Synthetic failure",
  );
  expect(
    screen.getByRole("button", { name: "Принять приглашение" }),
  ).toBeEnabled();
});

it("changes the password, rotates CSRF and revokes all sessions for an ordinary user", async () => {
  const backend = accountServer();
  const onLogout = vi.fn();
  render(<Account user={user} onLogout={onLogout} />);
  expect(screen.queryByText("Пригласить пользователя")).not.toBeInTheDocument();
  await userEvent.type(screen.getByLabelText("Текущий пароль"), password);
  await userEvent.type(screen.getByLabelText("Новый пароль"), password + "new");
  await userEvent.click(
    screen.getByRole("button", { name: "Изменить пароль" }),
  );
  expect(
    await screen.findByText("Пароль изменён. Остальные сессии завершены."),
  ).toBeVisible();
  expect(backend.calls[0].body).toEqual({
    current_password: password,
    password: password + "new",
  });
  await userEvent.click(
    screen.getByRole("button", { name: "Завершить все сессии" }),
  );
  expect(onLogout).toHaveBeenCalledOnce();
  expect(backend.calls[1].headers["X-CSRF-Token"]).toBe("rotated-csrf");
});

it("shows a failed password change without clearing the entered data", async () => {
  const backend = accountServer();
  backend.failures.push("/api/account/password");
  render(<Account user={user} onLogout={vi.fn()} />);
  await userEvent.type(screen.getByLabelText("Текущий пароль"), "wrong");
  await userEvent.type(screen.getByLabelText("Новый пароль"), password);
  await userEvent.click(
    screen.getByRole("button", { name: "Изменить пароль" }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Synthetic failure",
  );
  expect(screen.getByLabelText("Новый пароль")).toHaveValue(password);
});

it("lets administrators invite, reissue, reset, disable, reenable and revoke without showing saved tokens", async () => {
  const backend = accountServer();
  render(<Account user={admin} onLogout={vi.fn()} />);
  await screen.findByText("Member · member");
  expect(
    screen.getAllByRole("button", { name: "Отозвать приглашение" }),
  ).toHaveLength(1);
  await userEvent.type(
    screen.getByLabelText("Логин нового пользователя"),
    "designer",
  );
  await userEvent.type(screen.getByLabelText("Имя пользователя"), "Designer");
  await userEvent.click(
    screen.getByRole("button", { name: "Создать приглашение" }),
  );
  expect(await screen.findByLabelText("Одноразовая ссылка")).toHaveValue(
    `${window.location.origin}/#invite=private-once-token`,
  );
  fireEvent.focus(screen.getByLabelText("Одноразовая ссылка"));
  await userEvent.click(
    screen.getByRole("button", { name: "Новое приглашение" }),
  );
  expect(screen.getByLabelText("Одноразовая ссылка")).toHaveValue(
    `${window.location.origin}/#invite=private-once-token`,
  );
  const member = screen.getByText("Member · member").closest("li")!;
  await userEvent.click(
    within(member).getByRole("button", { name: "Ссылка восстановления" }),
  );
  expect(screen.getByLabelText("Одноразовая ссылка")).toHaveValue(
    `${window.location.origin}/#reset=private-once-token`,
  );
  await userEvent.click(
    screen.getByRole("button", { name: "Отозвать ссылку восстановления" }),
  );
  expect(
    await screen.findByText("Ссылка восстановления отозвана."),
  ).toBeVisible();
  await userEvent.click(
    within(member).getByRole("button", { name: "Отключить аккаунт" }),
  );
  await waitFor(() =>
    expect(
      within(member).getByRole("button", { name: "Включить аккаунт" }),
    ).toBeVisible(),
  );
  expect(screen.queryByLabelText("Одноразовая ссылка")).not.toBeInTheDocument();
  await userEvent.click(
    within(member).getByRole("button", { name: "Включить аккаунт" }),
  );
  await screen.findAllByRole("button", { name: "Отключить аккаунт" });
  await userEvent.click(
    screen.getByRole("button", { name: "Отозвать приглашение" }),
  );
  await waitFor(() =>
    expect(
      screen.queryByRole("button", { name: "Отозвать приглашение" }),
    ).not.toBeInTheDocument(),
  );
  expect(
    backend.calls.some(
      (call) => call.path === "/api/admin/users/member/password-resets",
    ),
  ).toBe(true);
});

it("surfaces failed administrator reads and mutations", async () => {
  const backend = accountServer();
  backend.failures.push("/api/admin/users");
  render(<Account user={admin} onLogout={vi.fn()} />);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Synthetic failure",
  );
  backend.failures.push("/api/admin/invitations");
  await userEvent.type(
    screen.getByLabelText("Логин нового пользователя"),
    "duplicate",
  );
  await userEvent.type(screen.getByLabelText("Имя пользователя"), "Duplicate");
  await userEvent.click(
    screen.getByRole("button", { name: "Создать приглашение" }),
  );
  expect(screen.getByRole("alert")).toHaveTextContent("Synthetic failure");
});

it("clears an expired account workspace and preserves shared login mode", async () => {
  const backend = server();
  backend.state.session = {
    authenticated: true,
    setup_required: false,
    account_mode: true,
    user,
  };
  render(<App />);
  await screen.findByRole("button", { name: "Аккаунт и доступ" });
  backend.state.session = {
    authenticated: false,
    setup_required: false,
    account_mode: true,
  };
  await waitFor(() =>
    expect(TestEvents.latest?.listeners["session-ended"]).toBeDefined(),
  );
  await act(async () => TestEvents.latest.listeners["session-ended"]());
  expect(screen.queryByRole("article")).not.toBeInTheDocument();
  expect(await screen.findByLabelText("Логин", { exact: true })).toBeVisible();
});

it("shows an invitation over an existing session and enters the accepted account", async () => {
  window.history.replaceState(null, "", "/#invite=secret");
  const backend = server();
  backend.state.session = {
    authenticated: true,
    setup_required: false,
    account_mode: true,
    user: admin,
  };
  const original = globalThis.fetch;
  vi.stubGlobal("fetch", async (path: string, options: RequestInit) =>
    path === "/api/invitations/accept"
      ? new Response(JSON.stringify({ ...backend.state.session, user }))
      : original(path, options),
  );
  render(<App />);
  await userEvent.type(
    await screen.findByLabelText("Пароль", { exact: true }),
    password,
  );
  await userEvent.click(
    screen.getByRole("button", { name: "Принять приглашение" }),
  );
  await screen.findByRole("button", { name: "Аккаунт и доступ" });
  expect(screen.getByLabelText("Личный аккаунт")).toHaveTextContent("M");
  await userEvent.click(
    screen.getByRole("button", { name: "Аккаунт и доступ" }),
  );
  expect(screen.getByText("Member · member")).toBeVisible();
});

it("opens new recovery links during same-page navigation and replaces previous token state", async () => {
  const backend = server();
  backend.state.session = {
    authenticated: false,
    setup_required: false,
    account_mode: true,
  };
  const original = globalThis.fetch;
  const submitted: unknown[] = [];
  vi.stubGlobal("fetch", async (url: string, options: RequestInit) => {
    if (url === "/api/password-resets/accept") {
      submitted.push(JSON.parse(options.body as string));
      return new Response(
        JSON.stringify({
          authenticated: false,
          setup_required: false,
          account_mode: true,
        }),
      );
    }
    return original(url, options);
  });
  render(<App />);
  await screen.findByLabelText("Логин", { exact: true });
  await act(async () => {
    window.location.hash = "invite=old-token";
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });
  await screen.findByRole("button", { name: "Принять приглашение" });
  await act(async () => {
    window.location.hash = "reset=new-token";
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });
  await userEvent.type(
    screen.getByLabelText("Пароль", { exact: true }),
    password,
  );
  await userEvent.click(
    screen.getByRole("button", { name: "Сохранить новый пароль" }),
  );
  expect(submitted).toEqual([{ token: "new-token", password }]);
  expect(await screen.findByLabelText("Логин", { exact: true })).toBeVisible();
  expect(window.location.hash).toBe("");
});

it("waits for password rotation before checking a revoked old event stream", async () => {
  const backend = server();
  backend.state.session = {
    authenticated: true,
    setup_required: false,
    account_mode: true,
    csrf_token: "old-csrf",
    user,
  };
  const original = globalThis.fetch;
  let resolve!: (response: Response) => void;
  const changing = new Promise<Response>((done) => {
    resolve = done;
  });
  vi.stubGlobal("fetch", (url: string, options: RequestInit) =>
    url === "/api/account/password" ? changing : original(url, options),
  );
  render(<App />);
  await userEvent.click(
    await screen.findByRole("button", { name: "Аккаунт и доступ" }),
  );
  await userEvent.type(screen.getByLabelText("Текущий пароль"), password);
  await userEvent.type(
    screen.getByLabelText("Новый пароль"),
    password + "-new",
  );
  const previous = TestEvents.latest;
  await userEvent.click(
    screen.getByRole("button", { name: "Изменить пароль" }),
  );
  const renewed = { ...backend.state.session, csrf_token: "new-csrf" };
  backend.state.session = {
    authenticated: false,
    setup_required: false,
    account_mode: true,
  };
  await act(async () => previous.listeners["session-ended"]());
  expect(
    screen.queryByLabelText("Логин", { exact: true }),
  ).not.toBeInTheDocument();
  backend.state.session = renewed;
  await act(async () => {
    resolve(new Response(JSON.stringify(backend.state.session)));
  });
  expect(
    await screen.findByText("Пароль изменён. Остальные сессии завершены."),
  ).toBeVisible();
  await waitFor(() => expect(TestEvents.latest).not.toBe(previous));
  expect(previous.close).toHaveBeenCalled();
});

it("refreshes the actual account before displaying data after a session change", async () => {
  const backend = server();
  const initialEvents = TestEvents.latest;
  backend.state.session = {
    authenticated: true,
    setup_required: false,
    account_mode: true,
    csrf_token: "first",
    user: admin,
  };
  render(<App />);
  await screen.findByRole("button", { name: "Аккаунт и доступ" });
  await waitFor(() => expect(TestEvents.latest).not.toBe(initialEvents));
  const previous = TestEvents.latest;
  backend.state.session = {
    ...backend.state.session,
    csrf_token: "second",
    user,
  };
  await act(async () => previous.listeners["session-ended"]());
  await waitFor(() =>
    expect(screen.getByLabelText("Личный аккаунт")).toHaveTextContent("M"),
  );
  expect(previous.close).toHaveBeenCalled();
});

it("clears the private workspace when the current session cannot be revalidated", async () => {
  const backend = server();
  backend.state.session = {
    authenticated: true,
    setup_required: false,
    account_mode: true,
    user,
  };
  render(<App />);
  await screen.findByRole("button", { name: "Аккаунт и доступ" });
  backend.failures.push({
    path: "/session",
    status: 500,
    detail: "Unavailable",
  });
  await waitFor(() =>
    expect(TestEvents.latest?.listeners["session-ended"]).toBeDefined(),
  );
  await act(async () => TestEvents.latest.listeners["session-ended"]());
  expect(await screen.findByLabelText("Логин", { exact: true })).toBeVisible();
  expect(screen.queryByRole("article")).not.toBeInTheDocument();
});

it("ignores an older session response after another account has been confirmed", async () => {
  const backend = server();
  backend.state.session = {
    authenticated: true,
    setup_required: false,
    account_mode: true,
    user: admin,
    csrf_token: "old",
  };
  render(<App />);
  await screen.findByRole("button", { name: "Аккаунт и доступ" });
  const original = globalThis.fetch;
  let resolve!: (response: Response) => void;
  const late = new Promise<Response>((done) => {
    resolve = done;
  });
  let checks = 0;
  vi.stubGlobal("fetch", (url: string, options: RequestInit) =>
    url === "/api/session" && checks++ === 0 ? late : original(url, options),
  );
  const old = { ...backend.state.session };
  await waitFor(() =>
    expect(TestEvents.latest?.listeners["session-ended"]).toBeDefined(),
  );
  await act(async () => TestEvents.latest.listeners["session-ended"]());
  backend.state.session = { ...backend.state.session, user, csrf_token: "new" };
  await waitFor(() =>
    expect(TestEvents.latest?.listeners["session-ended"]).toBeDefined(),
  );
  await act(async () => TestEvents.latest.listeners["session-ended"]());
  await waitFor(() =>
    expect(screen.getByLabelText("Личный аккаунт")).toHaveTextContent("M"),
  );
  await act(async () => {
    resolve(new Response(JSON.stringify(old)));
  });
  expect(screen.getByLabelText("Личный аккаунт")).toHaveTextContent("M");
});
