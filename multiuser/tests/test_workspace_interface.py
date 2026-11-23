import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_accounts import api_world as api_world
from test_personal_documents import seed
from test_personal_search_runs import configure

from qa_job_bot import pg_schema as s
from qa_job_bot.accounts import CurrentUser
from qa_job_bot.pg_store import PostgresStore
from qa_job_bot.search_models import SourceSpec
from qa_job_bot.web_store import StoreError
from qa_job_bot.workspace_routes import workspace_summary


def test_workspace_summary_is_personal_and_only_contains_readiness_metadata(api_world):
    admin, member, app, admin_id, member_id = api_world
    first, other = [PostgresStore(app.state.database, uid) for uid in (admin_id, member_id)]
    configure(first)
    seed(first, "Private Administrator CV content.")
    first.enqueue("search", "own-queue", {}, "own-queue")
    value = admin.get("/api/workspace")
    assert value.status_code == 200
    result = value.json()
    assert result["setup"]["search_configured"] and result["setup"]["template_ready"]
    assert result["setup"]["valid_sources"] == 1
    assert result["queue"] == {"queued": 1, "active": 0, "attention": 0}
    text = json.dumps(result)
    assert "Private Administrator" not in text and "facts" not in text and "path" not in text
    assert value.headers["cache-control"] == "no-store"
    assert value.headers["referrer-policy"] == "no-referrer"
    fresh = member.get("/api/workspace?user_id=" + admin_id).json()
    assert not fresh["setup"]["search_configured"] and not fresh["setup"]["template_ready"]
    assert fresh["queue"] == {"queued": 0, "active": 0, "attention": 0}
    assert fresh == workspace_summary(other)


def test_summary_counts_each_queue_phase_and_ignores_disabled_or_invalid_sources(api_world):
    admin, member, app, admin_id, member_id = api_world
    store = PostgresStore(app.state.database, member_id)
    configure(store)
    board = store.add_source(SourceSpec(kind="lever", config={"token": "example"}))
    with store.write() as connection:
        connection.execute(
            s.user_sources.update()
            .where(s.user_sources.c.id == board["id"])
            .values(payload={"token": "../private"})
        )
    value = workspace_summary(store)
    assert value["setup"]["enabled_sources"] == 2 and value["setup"]["valid_sources"] == 1
    statuses = [
        "queued",
        "running",
        "verifying",
        "rendering",
        "failed",
        "blocked",
        "needs_input",
        "cancelled",
        "ready",
        "deleted",
    ]
    for index, status in enumerate(statuses):
        queued = store.enqueue("search", str(index), {}, str(index))
        with store.write() as connection:
            connection.execute(
                s.tasks.update().where(s.tasks.c.id == queued["id"]).values(status=status)
            )
    assert workspace_summary(store)["queue"] == {"queued": 1, "active": 3, "attention": 3}
    with store.write() as connection:
        connection.execute(
            s.user_sources.update().where(s.user_sources.c.id == board["id"]).values(enabled=False)
        )
    assert workspace_summary(store)["setup"]["enabled_sources"] == 1
    assert workspace_summary(store)["setup"]["valid_sources"] == 1
    assert admin.get("/api/workspace").json()["queue"]["queued"] == 0


def test_account_name_update_preserves_identity_sessions_roles_and_other_people(api_world):
    admin, member, app, admin_id, member_id = api_world
    before = member.get("/api/session").json()
    saved = member.patch("/api/account", json={"display_name": "  Designer Example  "})
    assert saved.status_code == 200
    assert saved.json() == {**before["user"], "display_name": "Designer Example"}
    after = member.get("/api/session").json()
    assert after["user"]["display_name"] == "Designer Example"
    assert after["user"]["id"] == member_id and after["csrf_token"] == before["csrf_token"]
    assert after["user"]["role"] == "user"
    assert admin.get("/api/me").json()["display_name"] == "Administrator"
    assert member.get("/api/admin/users").status_code == 403
    response = admin.get("/api/admin/users").json()["items"]
    assert (
        next(row for row in response if row["id"] == member_id)["display_name"]
        == "Designer Example"
    )


@pytest.mark.parametrize("name", ["", "   ", "x" * 121, "Bad\nName", "\ud800", "Null\0Name"])
def test_invalid_name_changes_are_rejected_without_partial_update(api_world, name):
    admin, member, app, admin_id, member_id = api_world
    response = member.patch(
        "/api/account",
        content=json.dumps({"display_name": name}),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    assert member.get("/api/me").json()["display_name"] == "Member"


def test_profile_update_refuses_owner_role_overrides_and_requires_csrf_session(api_world):
    admin, member, app, admin_id, member_id = api_world
    for extra in [{"user_id": admin_id}, {"role": "admin"}, {"login": "new-login"}]:
        assert (
            member.patch("/api/account", json={"display_name": "Attempt", **extra}).status_code
            == 422
        )
    token = member.headers.pop("x-csrf-token")
    assert member.patch("/api/account", json={"display_name": "Attempt"}).status_code == 403
    member.headers["x-csrf-token"] = token
    member.cookies.clear()
    assert member.get("/api/workspace").status_code == 401
    assert member.patch("/api/account", json={"display_name": "Attempt"}).status_code == 401


def test_name_edits_from_two_connections_are_atomic_and_disabled_account_cannot_edit(api_world):
    admin, member, app, admin_id, member_id = api_world
    user = CurrentUser.from_row(app.state.database.user(member_id))
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(
            pool.map(
                lambda value: app.state.accounts.update_profile(user, value),
                ["Alice", "Александра"],
            )
        )
    assert {value["display_name"] for value in values} == {"Alice", "Александра"}
    assert member.get("/api/me").json()["display_name"] in {"Alice", "Александра"}
    app.state.accounts.set_disabled(
        CurrentUser.from_row(app.state.database.user(admin_id)), member_id, True
    )
    with pytest.raises(StoreError) as error:
        app.state.accounts.update_profile(user, "Late edit")
    assert error.value.status == 403
