import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from qa_job_bot import accounts as module
from qa_job_bot import pg_schema as s
from qa_job_bot.accounts import (
    Accounts,
    account_changes,
    check_password,
    valid_identity,
)
from qa_job_bot.legacy_cli import bootstrap_owner
from qa_job_bot.pg_store import PostgresStore
from qa_job_bot.web import create_app
from qa_job_bot.web_security import WebSettings, password_hash, token_hash
from qa_job_bot.web_store import StoreError

PASSWORD = "synthetic-account-password"
NEW_PASSWORD = "changed-synthetic-password"


@pytest.fixture
def accounts(pg_database, tmp_path):
    bootstrap_owner(pg_database, "admin", "Administrator")
    result = Accounts(pg_database, WebSettings(tmp_path))
    return result


def setup(accounts):
    issued = accounts.setup(accounts.setup_file.read_text(), PASSWORD, "setup-ip")
    return accounts.session(issued["token"])["user"], issued


def member(accounts, admin, login="member"):
    invitation = accounts.invite(admin, login, login)
    issued = accounts.redeem("invite", invitation["token"], PASSWORD, "member-ip")
    return accounts.session(issued["token"])["user"], issued


def test_initial_setup_is_private_single_use_and_restarts_safely(accounts):
    assert accounts.setup_required()
    original = accounts.setup_file.read_text()
    assert accounts.setup_file.stat().st_mode & 0o777 == 0o600
    assert Accounts(accounts.database, accounts.settings).setup_file.read_text() == original
    with pytest.raises(StoreError) as error:
        accounts.setup("wrong", PASSWORD, "ip")
    assert error.value.status == 403 and accounts.setup_required()
    admin, issued = setup(accounts)
    assert admin.role == "admin" and admin.login == "admin"
    assert not accounts.setup_required() and not accounts.setup_file.exists()
    assert not Accounts(accounts.database, accounts.settings).setup_required()
    assert accounts.authorize(issued["token"], issued["csrf_token"])["user"] == admin
    with pytest.raises(StoreError):
        accounts.setup(original, PASSWORD, "ip")


@pytest.mark.parametrize(
    "login,password",
    [("missing", PASSWORD), ("admin", "wrong"), ("", PASSWORD), ("  ADMIN  ", "wrong")],
)
def test_login_does_not_reveal_account_existence(accounts, login, password):
    setup(accounts)
    with pytest.raises(StoreError) as error:
        accounts.login(login, password, "ip")
    assert error.value.status == 401 and str(error.value) == module.AUTH_ERROR


def test_login_normalizes_names_and_session_expiry_csrf_logout(accounts, monkeypatch):
    admin, initial = setup(accounts)
    issued = accounts.login("  ＡDMIN  ", PASSWORD, "ip")
    assert issued["user"] == asdict(admin) and issued["token"] != initial["token"]
    with accounts.database.engine.connect() as connection:
        stored = (
            connection.execute(
                select(s.sessions).where(s.sessions.c.token_hash == token_hash(issued["token"]))
            )
            .mappings()
            .one()
        )
        assert issued["token"] != stored["token_hash"]
    for csrf in ("", "wrong", "подмена"):
        with pytest.raises(StoreError) as error:
            accounts.authorize(issued["token"], csrf)
        assert error.value.status == 403
    monkeypatch.setattr(module.time, "time", lambda: stored["expires_at"])
    assert accounts.session(issued["token"]) is None
    with pytest.raises(StoreError) as error:
        accounts.authorize(issued["token"])
    assert error.value.status == 401
    assert accounts.session("") is None
    accounts.logout(initial["token"])
    accounts.logout(initial["token"])


@pytest.mark.parametrize("saved", [None, "malformed", "0:bad", 5])
def test_corrupt_or_missing_password_hash_never_authenticates(saved):
    assert not check_password(PASSWORD, saved)


@pytest.mark.parametrize(
    "login,name",
    [
        ("x", "Name"),
        ("valid", ""),
        ("bad space", "Name"),
        ("a" * 101, "Name"),
        ("valid", "x" * 121),
    ],
)
def test_invalid_invitation_identity_is_rejected(login, name):
    with pytest.raises(StoreError) as error:
        valid_identity(login, name)
    assert error.value.status == 422


def test_account_and_ip_rate_limits_include_unknown_users_and_expire(accounts, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    for index in range(10):
        accounts.rate_limit("login", "missing", f"ip-{index}")
    with pytest.raises(StoreError) as error:
        accounts.rate_limit("login", "missing", "another-ip")
    assert error.value.status == 429
    for index in range(60):
        accounts.rate_limit("login", f"other-{index}", "shared-ip")
    with pytest.raises(StoreError) as error:
        accounts.rate_limit("login", "new-identity", "shared-ip")
    assert error.value.status == 429
    clock[0] += 900
    accounts.rate_limit("login", "missing", "shared-ip")
    with accounts.database.engine.connect() as connection:
        rows = connection.execute(select(s.auth_attempts)).mappings().all()
        assert (
            len(rows) == 1
            and rows[0]["ip_hash"] != "shared-ip"
            and rows[0]["key_hash"] != "missing"
        )


def test_invitation_acceptance_is_atomic_under_concurrent_replay(accounts):
    admin, _ = setup(accounts)
    invitation = accounts.invite(admin, "member", "Member")
    with accounts.database.engine.connect() as connection:
        saved = connection.execute(select(s.invitations)).mappings().one()
        assert saved["token_hash"] == token_hash(invitation["token"])
        assert invitation["token"] not in json.dumps(accounts.invitations(admin))

    def accept(index):
        try:
            return accounts.redeem("invite", invitation["token"], PASSWORD, f"ip-{index}")
        except StoreError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(accept, range(2)))
    assert sum(isinstance(value, dict) for value in results) == 1
    assert 403 in results
    with accounts.database.engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(s.users)).scalar_one() == 2


@pytest.mark.parametrize("reason", ["expired", "revoked", "used", "unknown", "disabled"])
def test_invalid_invitations_return_identical_failure(accounts, reason):
    admin, _ = setup(accounts)
    invitation = accounts.invite(admin, "member", "Member")
    token = invitation["token"]
    with accounts.database.transaction() as connection:
        if reason in {"expired", "revoked", "used"}:
            field = {"expired": "expires_at", "revoked": "revoked_at", "used": "used_at"}[reason]
            connection.execute(update(s.invitations).values(**{field: 1}))
        elif reason == "disabled":
            connection.execute(
                update(s.users).where(s.users.c.id == invitation["user_id"]).values(disabled=True)
            )
    with pytest.raises(StoreError) as error:
        accounts.redeem("invite", "unknown-token" if reason == "unknown" else token, PASSWORD, "ip")
    assert error.value.status == 403 and str(error.value) == module.TOKEN_ERROR


def test_password_changes_and_resets_revoke_sessions_and_old_links(accounts):
    admin, _ = setup(accounts)
    user, initial = member(accounts, admin)
    other = accounts.login(user.login, PASSWORD, "ip")
    reset = accounts.issue_access(admin, user.id, "reset")
    with pytest.raises(StoreError):
        accounts.change_password(user, "wrong", NEW_PASSWORD)
    issued = accounts.change_password(user, PASSWORD, NEW_PASSWORD)
    assert accounts.session(initial["token"]) is None and accounts.session(other["token"]) is None
    assert accounts.session(issued["token"])["user"] == user
    with pytest.raises(StoreError):
        accounts.redeem("reset", reset["token"], PASSWORD, "ip")
    reset = accounts.issue_access(admin, user.id, "reset")
    assert accounts.redeem("reset", reset["token"], PASSWORD, "ip") is None
    assert accounts.session(issued["token"]) is None
    with pytest.raises(StoreError):
        accounts.redeem("reset", reset["token"], PASSWORD, "ip")
    assert accounts.login(user.login, PASSWORD, "ip")["user"]["id"] == user.id


def test_administrator_manages_invitations_and_disables_without_personal_access(accounts):
    admin, _ = setup(accounts)
    user, issued = member(accounts, admin)
    pending = accounts.invite(admin, "pending", "Pending")
    reissued = accounts.issue_access(admin, pending["user_id"], "invite")
    with pytest.raises(StoreError):
        accounts.redeem("invite", pending["token"], PASSWORD, "ip")
    accounts.revoke_invitation(admin, reissued["id"])
    with pytest.raises(StoreError):
        accounts.redeem("invite", reissued["token"], PASSWORD, "ip")
    with pytest.raises(StoreError) as error:
        accounts.invite(admin, "MEMBER", "Duplicate")
    assert error.value.status == 409
    for identifier, kind, status in [
        ("missing", "reset", 404),
        (user.id, "invite", 409),
        (pending["user_id"], "reset", 409),
    ]:
        with pytest.raises(StoreError) as error:
            accounts.issue_access(admin, identifier, kind)
        assert error.value.status == status
    with pytest.raises(StoreError):
        accounts.revoke_invitation(admin, "missing")
    with pytest.raises(StoreError):
        accounts.set_disabled(admin, "missing", True)
    with pytest.raises(StoreError):
        accounts.set_disabled(admin, admin.id, True)
    accounts.set_disabled(admin, user.id, True)
    assert accounts.session(issued["token"]) is None
    with pytest.raises(StoreError) as error:
        accounts.login(user.login, PASSWORD, "ip")
    assert error.value.status == 401 and str(error.value) == module.AUTH_ERROR
    with pytest.raises(StoreError):
        accounts.issue_access(admin, user.id, "reset")
    accounts.set_disabled(admin, user.id, False)
    assert accounts.session(issued["token"]) is None
    assert accounts.login(user.login, PASSWORD, "ip")
    assert {row["login"] for row in accounts.list_users(admin)} == {"admin", "member", "pending"}
    for operation in [
        lambda: accounts.list_users(user),
        lambda: accounts.invite(user, "new", "New"),
        lambda: accounts.invitations(user),
        lambda: accounts.set_disabled(user, admin.id, True),
    ]:
        with pytest.raises(StoreError) as error:
            operation()
        assert error.value.status == 403


@pytest.mark.parametrize("ending", ["logout", "disabled", "revoke", "reset"])
async def test_personal_event_stream_stops_after_revocation(accounts, monkeypatch, ending):
    admin, _ = setup(accounts)
    user, issued = member(accounts, admin)
    with accounts.database.transaction() as connection:
        connection.execute(s.revisions.insert().values(user_id=user.id, revision=7))
        connection.execute(s.revisions.insert().values(user_id=admin.id, revision=900))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    stream = account_changes(
        SimpleNamespace(is_disconnected=AsyncMock(return_value=False)),
        accounts,
        issued["token"],
        user.id,
    )
    assert '"revision":7' in await anext(stream)
    assert await anext(stream) == ": heartbeat\n\n"
    if ending == "logout":
        accounts.logout(issued["token"])
    elif ending == "disabled":
        accounts.set_disabled(admin, user.id, True)
    elif ending == "revoke":
        accounts.revoke_sessions(user)
    else:
        reset = accounts.issue_access(admin, user.id, "reset")
        accounts.redeem("reset", reset["token"], PASSWORD, "ip")
    assert "session-ended" in await anext(stream)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.fixture
def api_world(pg_database, tmp_path):
    user = bootstrap_owner(pg_database, "admin", "Administrator")
    settings = WebSettings(tmp_path, ("http://testserver",))
    app = create_app(settings, database=pg_database)
    with TestClient(app, headers={"origin": "http://testserver"}) as admin:
        result = admin.post(
            "/api/setup",
            json={"token": app.state.accounts.setup_file.read_text(), "password": PASSWORD},
        )
        assert result.status_code == 200, result.text
        admin.headers["x-csrf-token"] = result.json()["csrf_token"]
        with TestClient(app, headers={"origin": "http://testserver"}) as other:
            invitation = admin.post(
                "/api/admin/invitations", json={"login": "member", "display_name": "Member"}
            ).json()
            accepted = other.post(
                "/api/invitations/accept", json={"token": invitation["token"], "password": PASSWORD}
            )
            assert accepted.status_code == 200, accepted.text
            other.headers["x-csrf-token"] = accepted.json()["csrf_token"]
            yield admin, other, app, user["user_id"], invitation["user_id"]


def test_api_login_me_rotation_password_revoke_and_csrf(api_world):
    admin, other, app, admin_id, member_id = api_world
    assert admin.get("/api/me").json()["id"] == admin_id
    assert other.get("/api/me").json()["id"] == member_id
    assert other.get("/api/session").json()["user"]["id"] == member_id
    assert other.get("/api/admin/users").status_code == 403
    assert (
        other.post(
            "/api/admin/invitations", json={"login": "forged", "display_name": "Forged"}
        ).status_code
        == 403
    )
    old_cookie = other.cookies.get("qa_session")
    assert (
        other.post(
            "/api/account/password",
            json={"current_password": PASSWORD, "password": NEW_PASSWORD},
            headers={"x-csrf-token": "wrong"},
        ).status_code
        == 403
    )
    changed = other.post(
        "/api/account/password", json={"current_password": PASSWORD, "password": NEW_PASSWORD}
    )
    assert changed.status_code == 200 and other.cookies.get("qa_session") != old_cookie
    other.headers["x-csrf-token"] = changed.json()["csrf_token"]
    assert app.state.accounts.session(old_cookie) is None
    assert other.post("/api/account/sessions/revoke").status_code == 200
    assert other.get("/api/me").status_code == 401
    assert (
        other.post("/api/login", json={"login": "member", "password": PASSWORD}).status_code == 401
    )
    logged = other.post("/api/login", json={"login": "member", "password": NEW_PASSWORD})
    assert logged.status_code == 200
    assert logged.headers["set-cookie"].find("HttpOnly") >= 0
    assert "SameSite=strict" in logged.headers["set-cookie"]
    other.headers["x-csrf-token"] = logged.json()["csrf_token"]
    reset = admin.post(f"/api/admin/users/{member_id}/password-resets").json()
    assert (
        other.post(
            "/api/password-resets/accept", json={"token": reset["token"], "password": PASSWORD}
        ).status_code
        == 200
    )
    assert other.get("/api/me").status_code == 401
    assert (
        other.post(
            "/api/password-resets/accept", json={"token": reset["token"], "password": PASSWORD}
        ).status_code
        == 403
    )


def test_admin_api_no_secrets_reissue_revoke_disable_and_forgery(api_world):
    admin, other, app, _, member_id = api_world
    assert other.patch(f"/api/admin/users/{member_id}", json={"disabled": True}).status_code == 403
    users = admin.get("/api/admin/users").json()
    assert len(users["items"]) == 2 and "password" not in json.dumps(users)
    invitation = admin.post(
        "/api/admin/invitations", json={"login": "pending", "display_name": "Pending"}
    ).json()
    rows = admin.get("/api/admin/invitations").json()
    assert invitation["token"] not in json.dumps(rows) and "token_hash" not in json.dumps(rows)
    assert admin.delete(f"/api/admin/invitations/{invitation['id']}").status_code == 204
    renewed = admin.post(f"/api/admin/users/{invitation['user_id']}/invitations").json()
    assert renewed["token"] != invitation["token"]
    assert admin.patch(f"/api/admin/users/{member_id}", json={"disabled": True}).status_code == 200
    assert other.get("/api/jobs").status_code == 401
    assert (
        admin.post(
            "/api/admin/invitations",
            json={"login": "forged", "display_name": "Forged", "role": "admin"},
        ).status_code
        == 422
    )
    assert (
        admin.post(
            "/api/login", json={"login": "admin", "password": PASSWORD, "user_id": member_id}
        ).status_code
        == 422
    )


def seed_resources(client, user_id, index):
    from e2e_server import FixtureGateway, FixtureSearch

    from qa_job_bot.models import Job
    from qa_job_bot.worker import Worker

    app = client.app
    store = PostgresStore(app.state.database, user_id)
    job = Job(
        "Synthetic account",
        str(index),
        f"Owner {index} QA",
        "Synthetic Company",
        "Europe",
        f"https://example.com/accounts/{index}",
        score=80,
        description="We are looking for a quality engineer to test software and APIs.",
    )
    store.upsert_jobs([job])
    fixture = Path(__file__).parent / "fixtures/candidate.pdf"
    uploaded = client.post(
        "/api/template-drafts", files={"file": ("candidate.pdf", fixture.read_bytes())}
    )
    assert uploaded.status_code == 201, uploaded.text
    template = uploaded.json()
    assert (
        client.patch(
            f"/api/template-drafts/{template['id']}",
            json={"facts": template["facts"], "instructions": f"Private owner {index}"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/template-drafts/{template['id']}/activate",
            json={"confirmed": True, "expected_version": None},
        ).status_code
        == 200
    )
    conversation = client.post("/api/conversations", json={"job_id": job.fingerprint}).json()
    store.add_message(conversation["id"], "user", f"Private message {index}", "same-request")
    task = client.post(
        f"/api/jobs/{job.fingerprint}/documents",
        json={"kind": "cv", "language": "en", "request_id": "same-document-request"},
    ).json()
    worker = Worker(store, app.state.settings, FixtureGateway(), FixtureSearch(store))
    asyncio.run(worker.once())
    ready = store.task(task["id"])
    assert ready["status"] == "ready", ready["error"]
    return {
        "job": job.fingerprint,
        "template": template["id"],
        "conversation": conversation["id"],
        "task": ready["id"],
        "artifacts": ready["artifacts"],
        "snapshot": client.get("/api/jobs").json()["snapshot_id"],
        "facts": template["facts"],
    }


def private_routes(resource):
    return [
        ("GET", f"/api/jobs?snapshot_id={resource['snapshot']}", None),
        ("GET", f"/api/templates/{resource['template']}/source", None),
        ("GET", f"/api/templates/{resource['template']}/pages/1", None),
        ("GET", f"/api/tasks/{resource['task']}", None),
        ("GET", f"/api/tasks/{resource['task']}/review", None),
        ("GET", f"/api/tasks/{resource['task']}/document-preview", None),
        ("GET", f"/api/conversations/{resource['conversation']}/messages", None),
        *[("GET", f"/api/artifacts/{item['id']}/download", None) for item in resource["artifacts"]],
        *[
            ("GET", f"/api/artifacts/{item['id']}/preview", None)
            for item in resource["artifacts"]
            if item["format"] == "pdf"
        ],
        ("PATCH", f"/api/jobs/{resource['job']}/state", {"applied": True}),
        (
            "POST",
            f"/api/jobs/{resource['job']}/documents",
            {"kind": "cv", "language": "en", "request_id": "new-document"},
        ),
        ("POST", f"/api/templates/{resource['template']}/reparse", {}),
        ("PATCH", f"/api/template-drafts/{resource['template']}", {"facts": resource["facts"]}),
        (
            "POST",
            f"/api/template-drafts/{resource['template']}/activate",
            {"confirmed": True, "expected_version": resource["template"]},
        ),
        ("POST", "/api/conversations", {"template_id": resource["template"]}),
        ("POST", "/api/conversations", {"job_id": resource["job"]}),
        (
            "POST",
            f"/api/conversations/{resource['conversation']}/messages",
            {"text": "Only my facts", "request_id": "new-message"},
        ),
        ("POST", f"/api/tasks/{resource['task']}/cancel", {}),
        ("DELETE", f"/api/tasks/{resource['task']}", None),
    ]


def test_every_personal_endpoint_uses_the_session_owner_in_one_shared_application(api_world):
    admin, other, app, admin_id, member_id = api_world
    resources = [seed_resources(admin, admin_id, 1), seed_resources(other, member_id, 2)]
    assert app.state.store is None
    with TestClient(app, headers={"origin": "http://testserver"}) as anonymous:
        for index, current in enumerate((admin, other)):
            own, foreign = resources[index], resources[1 - index]
            for method, route, body in private_routes(foreign):
                assert current.request(method, route, json=body).status_code == 404, (method, route)
            for method, route, body in private_routes(own):
                assert anonymous.request(method, route, json=body).status_code == 401, (
                    method,
                    route,
                )
                if method == "GET":
                    assert current.get(route).status_code == 200, route
            for route in (
                "/api/jobs",
                "/api/profile",
                "/api/tasks",
                "/api/me",
                "/api/events",
                "/api/admin/users",
            ):
                assert anonymous.get(route).status_code == 401
            listing = current.get(
                "/api/jobs",
                params={"user_id": member_id if index == 0 else admin_id},
                headers={"X-User-ID": "forged"},
            ).json()
            assert [row["id"] for row in listing["items"]] == [own["job"]]
            assert "Private owner " + str(index + 1) in json.dumps(
                current.get("/api/profile").json()
            )
            assert (
                current.post(
                    f"/api/jobs/{own['job']}/documents",
                    json={
                        "kind": "cv",
                        "language": "en",
                        "request_id": "forged",
                        "user_id": member_id,
                    },
                ).status_code
                == 422
            )
            for method, route, body in private_routes(own):
                if method != "GET":
                    response = current.request(method, route, json=body)
                    assert response.status_code in {200, 201, 202, 204, 409}, (
                        method,
                        route,
                        response.text,
                    )
            assert (app.state.settings.data_dir / "users").is_dir()


@pytest.mark.parametrize("change", ["password", "disabled"])
def test_login_rechecks_account_after_password_verification(accounts, monkeypatch, change):
    setup(accounts)
    original = module.check_password

    def concurrently_changed(password, saved):
        valid = original(password, saved)
        with accounts.database.transaction() as connection:
            connection.execute(
                update(s.users).values(
                    **(
                        {"disabled": True}
                        if change == "disabled"
                        else {"password_hash": password_hash(NEW_PASSWORD)}
                    )
                )
            )
        return valid

    monkeypatch.setattr(module, "check_password", concurrently_changed)
    with pytest.raises(StoreError) as error:
        accounts.login("admin", PASSWORD, "ip")
    assert error.value.status == 401


def test_bootstrap_consumption_is_atomic_with_simultaneous_requests(accounts):
    token = accounts.setup_file.read_text()

    def attempt(index):
        try:
            accounts.setup(token, PASSWORD, f"ip-{index}")
            return 200
        except StoreError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, range(2)))
    assert results.count(200) == 1 and set(results) <= {200, 403, 409}
    with accounts.database.engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(s.sessions)).scalar_one() == 1


def test_all_registered_private_routes_reject_anonymous_requests_before_body_parsing(api_world):
    admin, other, app, _, _ = api_world
    public = {
        "/api/login",
        "/api/setup",
        "/api/session",
        "/api/invitations/accept",
        "/api/password-resets/accept",
    }
    import re

    with TestClient(app, headers={"origin": "http://testserver"}) as anonymous:
        for route in app.routes:
            if not route.path.startswith("/api/") or route.path in public:
                continue
            path = re.sub(r"\{[^}]+\}", "unknown", route.path)
            for method in route.methods:
                assert (
                    anonymous.request(method, path, content=b"invalid JSON").status_code == 401
                ), (method, path)
    assert admin.get("/api/me").status_code == 200 and other.get("/api/me").status_code == 200


def test_bound_and_shared_application_configuration_cannot_be_mixed(pg_database, tmp_path):
    with pytest.raises(ValueError):
        create_app(WebSettings(tmp_path), PostgresStore(pg_database, "owner"), database=pg_database)


def test_creator_is_recorded_and_administrator_can_revoke_reset_link(api_world):
    admin, other, app, admin_id, member_id = api_world
    issued = admin.post(f"/api/admin/users/{member_id}/password-resets").json()
    with app.state.database.engine.connect() as connection:
        assert (
            connection.execute(
                select(s.password_resets.c.created_by_id).where(
                    s.password_resets.c.id == issued["id"]
                )
            ).scalar_one()
            == admin_id
        )
        assert connection.execute(select(s.invitations.c.created_by_id)).scalar_one() == admin_id
    assert other.delete(f"/api/admin/password-resets/{issued['id']}").status_code == 403
    assert admin.delete(f"/api/admin/password-resets/{issued['id']}").status_code == 204
    response = other.post(
        "/api/password-resets/accept", json={"token": issued["token"], "password": NEW_PASSWORD}
    )
    assert response.status_code == 403
    assert other.get("/api/me").status_code == 200
