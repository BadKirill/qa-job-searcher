import json
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from pg_fixtures import personal_store
from test_accounts import api_world as api_world
from test_accounts import member, setup

from qa_job_bot import pg_schema as s
from qa_job_bot import pilot as module
from qa_job_bot.accounts import Accounts
from qa_job_bot.legacy_cli import bootstrap_owner
from qa_job_bot.web_security import WebSettings
from qa_job_bot.web_store import StoreError


@pytest.fixture
def world(pg_database, tmp_path):
    bootstrap_owner(pg_database, "owner", "Synthetic Owner")
    accounts = Accounts(pg_database, WebSettings(tmp_path))
    owner, _ = setup(accounts)
    pilot = module.Pilot(pg_database)
    return pilot, accounts, owner


def evidence(world):
    pilot, accounts, owner = world
    pilot.begin(owner.id)
    tester, _ = member(accounts, owner, "tester")
    source_hash = "a" * 64
    with accounts.database.transaction() as connection:
        connection.execute(
            s.import_runs.insert().values(
                id="synthetic-import", user_id=owner.id, source_hash=source_hash, payload={}
            )
        )
    return {
        "owner_id": owner.id,
        "tester_id": tester.id,
        "source_hash": source_hash,
        "checks": {name: True for name in module.CHECKS},
    }


def test_pilot_starts_with_one_owner_and_is_durable_idempotent_and_private(world):
    pilot, accounts, owner = world
    assert pilot.status() == {
        "phase": "not_started",
        "limit": None,
        "reserved": 1,
        "pair_verified": False,
    }
    state = pilot.begin(owner.id)
    assert state == {"phase": "pair", "limit": 2, "reserved": 1, "pair_verified": False}
    assert module.Pilot(accounts.database).status() == state == pilot.begin(owner.id)
    assert owner.id not in json.dumps(state)
    with pytest.raises(StoreError):
        pilot.begin("another-owner")


def test_pilot_refuses_unknown_nonadmin_disabled_and_multiple_owners(world):
    pilot, accounts, owner = world
    for identifier in ("missing", personal_store(accounts.database, "other").user_id):
        with pytest.raises(StoreError):
            pilot.begin(identifier)
    with pytest.raises(StoreError):
        pilot.begin(owner.id)


def test_pair_capacity_is_atomic_for_concurrent_invites_and_includes_pending_accounts(world):
    pilot, accounts, owner = world
    pilot.begin(owner.id)

    def invite(index):
        try:
            return accounts.invite(owner, f"member-{index}", "Synthetic Member")
        except StoreError as error:
            assert error.status == 409
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        issued = [value for value in pool.map(invite, range(6)) if value]
    assert len(issued) == 1 and pilot.status()["reserved"] == 2
    accounts.issue_access(owner, issued[0]["user_id"], "invite")
    assert pilot.status()["reserved"] == 2
    accounts.set_disabled(owner, issued[0]["user_id"], True)
    replacement = accounts.invite(owner, "replacement", "Replacement")
    with pytest.raises(StoreError, match="Сначала завершите"):
        accounts.set_disabled(owner, issued[0]["user_id"], False)
    accounts.set_disabled(owner, replacement["user_id"], True)
    accounts.set_disabled(owner, issued[0]["user_id"], False)
    assert pilot.status()["reserved"] == 2


def test_pilot_requires_matching_import_two_activated_accounts_and_every_check(world):
    report = evidence(world)
    pilot, accounts, owner = world
    assert pilot.expand(report) == {
        "phase": "cohort",
        "limit": 20,
        "reserved": 2,
        "pair_verified": True,
    }
    assert pilot.expand(report) == pilot.status()
    with pytest.raises(StoreError):
        pilot.begin(owner.id)
    altered = {**report, "source_hash": "b" * 64}
    with pytest.raises(StoreError):
        pilot.expand(altered)
    for index in range(18):
        accounts.invite(owner, f"cohort-{index:02}", "Synthetic Participant")
    with pytest.raises(StoreError, match="20 мест"):
        accounts.invite(owner, "overflow", "Overflow")
    assert pilot.status()["reserved"] == 20


@pytest.mark.parametrize(
    "damage", ["checks", "value", "unknown", "owner", "tester", "source", "type", "same"]
)
def test_invalid_or_unrelated_evidence_never_opens_more_invitations(world, damage):
    report = evidence(world)
    pilot, _, _ = world
    if damage == "checks":
        report["checks"].pop("documents")
    elif damage == "value":
        report["checks"]["documents"] = 1
    elif damage == "unknown":
        report["PRIVATE_EXTRA"] = "private contents"
    elif damage in {"owner", "tester"}:
        report[damage + "_id"] = "wrong"
    elif damage == "source":
        report["source_hash"] = "b" * 64
    elif damage == "type":
        report["owner_id"] = []
    else:
        report["tester_id"] = report["owner_id"]
    with pytest.raises(StoreError):
        pilot.expand(report)
    assert pilot.status()["phase"] == "pair"


@pytest.mark.parametrize("report", [None, [], "private contents", {}, {"checks": {}}])
def test_malformed_evidence_is_rejected_without_reading_candidate_data(world, report):
    pilot, _, _ = world
    with pytest.raises(StoreError) as error:
        pilot.expand(report)
    assert error.value.status == 422


def test_unactivated_or_disabled_tester_does_not_satisfy_pair_verification(world):
    report = evidence(world)
    pilot, accounts, owner = world
    with accounts.database.transaction() as connection:
        connection.execute(
            s.users.update().where(s.users.c.id == report["tester_id"]).values(password_hash=None)
        )
    with pytest.raises(StoreError):
        pilot.expand(report)
    accounts.set_disabled(owner, report["tester_id"], True)
    with pytest.raises(StoreError):
        pilot.expand(report)


def test_evidence_cannot_open_a_pilot_that_was_never_started(world):
    _, accounts, _ = world
    report = {
        "owner_id": "one",
        "tester_id": "two",
        "source_hash": "a" * 64,
        "checks": {name: True for name in module.CHECKS},
    }
    with pytest.raises(StoreError):
        module.Pilot(accounts.database).expand(report)


def test_pilot_api_is_admin_only_and_never_returns_owner_or_import_identifiers(api_world):
    admin, member_client, app, owner_id, _ = api_world
    assert member_client.get("/api/admin/pilot").status_code == 403
    result = admin.get("/api/admin/pilot")
    assert result.status_code == 200 and result.json()["phase"] == "not_started"
    assert owner_id not in result.text
    with app.state.database.transaction() as connection:
        state = module.lock(connection)
        assert state == {}
        module.save(connection, {"phase": "pair", "limit": 2, "owner_id": owner_id})
    response = admin.post(
        "/api/admin/invitations", json={"login": "third", "display_name": "Third"}
    )
    assert response.status_code == 409
    denied = member_client.post(
        "/api/admin/invitations", json={"login": "third", "display_name": "Third"}
    )
    assert denied.status_code == 403


@pytest.mark.parametrize("command", ["status", "begin", "expand", "invalid"])
def test_pilot_cli_uses_private_evidence_and_only_prints_summary(
    world, tmp_path, monkeypatch, capsys, command
):
    pilot, accounts, owner = world
    monkeypatch.setattr(module.Database, "from_env", lambda: accounts.database)
    arguments = ["pilot", command]
    if command == "begin":
        arguments += ["--owner-id", owner.id]
    elif command in {"expand", "invalid"}:
        report = evidence(world)
        path = tmp_path / "evidence.json"
        path.write_text(json.dumps(report) if command == "expand" else "PRIVATE invalid JSON")
        path.chmod(0o600)
        arguments = ["pilot", "expand", "--evidence", str(path)]
    monkeypatch.setattr(sys, "argv", arguments)
    if command == "invalid":
        with pytest.raises(SystemExit) as error:
            module.main()
        assert error.value.code == 1
    else:
        module.main()
    output = capsys.readouterr().out
    assert "PRIVATE" not in output and owner.id not in output
    assert "pilot_unavailable" in output if command == "invalid" else "phase" in json.loads(output)
