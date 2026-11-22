import asyncio
import json
import sys
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_accounts import api_world as api_world
from test_shared_runtime import claimed

from qa_job_bot import main as telegram_entry
from qa_job_bot import operations as module
from qa_job_bot import pg_schema as s
from qa_job_bot.pg_database import REVISION
from qa_job_bot.runtime_limits import RuntimeLimits


def test_readiness_checks_revision_and_hides_failures():
    for value, expected in [(REVISION, True), (None, False), ("0001_storage", False)]:
        assert module.ready(SimpleNamespace(revision=lambda value=value: value)) is expected
    database = SimpleNamespace(revision=Mock(side_effect=RuntimeError("private database URL")))
    assert not module.ready(database)


async def test_legacy_telegram_cannot_start_against_shared_accounts(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic")
    with pytest.raises(RuntimeError, match="Legacy Telegram polling"):
        await telegram_entry.async_main()


def test_public_health_and_admin_only_diagnostics(api_world, monkeypatch):
    admin, member, app, *_ = api_world
    anonymous = TestClient(app)
    assert anonymous.get("/health/live").json() == {"alive": True}
    assert anonymous.get("/health/ready").json() == {"ready": True}
    assert anonymous.get("/api/admin/diagnostics").status_code == 401
    assert member.get("/api/admin/diagnostics").status_code == 403
    result = admin.get("/api/admin/diagnostics")
    assert result.status_code == 200
    assert result.json()["expired_leases"] == 0
    monkeypatch.setattr("qa_job_bot.web.ready", lambda database: False)
    failed = anonymous.get("/health/ready")
    assert failed.status_code == 503 and failed.json() == {"ready": False}
    assert failed.headers["cache-control"] == "no-store"


def test_diagnostics_reports_counts_without_personal_payloads(pg_store):
    context = claimed(pg_store)
    runtime = RuntimeLimits(pg_store.database)
    reservation = runtime.reserve(context, "generator")
    runtime.progress(reservation, "sent", {})
    runtime.progress(reservation, "rate_limit", {"retry_after": 80})
    runtime.finish(reservation, False)
    pg_store.enqueue("chat", "private-scope", {"private": "PRIVATE_CV"}, "private-request")
    with pg_store.database.transaction() as connection:
        connection.execute(
            s.tasks.update()
            .where(s.tasks.c.id == context["task_id"])
            .values(lease_until=time.time() - 1, error="PRIVATE_CV and API_KEY")
        )
        connection.execute(
            s.search_runs.insert().values(
                id="private-run",
                user_id=pg_store.user_id,
                payload={"sources": [{"error": "SECRET_SOURCE"}, {"error": None}]},
            )
        )
        connection.execute(
            s.runtime_control.insert(),
            [
                {"key": "service:fresh", "payload": {"service": "search", "at": time.time()}},
                {"key": "service:stale", "payload": {"service": "search", "at": 1}},
            ],
        )
    status = module.diagnostics(pg_store.database)
    assert status["expired_leases"] == 1
    assert status["unknown_usage"] == 1
    assert status["usage_states"] == {"unknown": 1}
    assert 78 <= status["cooldown_seconds"] <= 80
    assert status["source_errors"] == 1 and status["services"]["search"] == 1
    assert {row["kind"] for row in status["queue"]} == {"search", "chat"}
    assert "PRIVATE" not in json.dumps(status) and "SECRET" not in json.dumps(status)
    assert pg_store.user_id not in json.dumps(status)


@pytest.mark.parametrize("failure", ["ConnectError", "CV content with spaces", None])
def test_api_failure_journal_records_only_a_safe_exception_type(pg_store, failure):
    context = claimed(pg_store)
    runtime = RuntimeLimits(pg_store.database)
    reservation = runtime.reserve(context, "generator")
    runtime.progress(reservation, "sent", {})
    runtime.progress(reservation, "failure", {"failure_type": failure})
    runtime.finish(reservation, False)
    with pg_store.database.transaction() as connection:
        payload = connection.execute(
            select(s.usage_reservations.c.payload).where(s.usage_reservations.c.id == reservation)
        ).scalar_one()
    assert payload["state"] == "unknown" and payload["usage"] is None
    assert payload.get("failure_type") == (failure if failure == "ConnectError" else None)


async def test_supervision_pulses_while_working_and_cleans_up_on_stop(pg_database, tmp_path):
    marker = tmp_path / "health"
    marker.write_text("stale marker from terminated process")
    stop = asyncio.Event()
    started = asyncio.Event()

    async def operation():
        started.set()
        await stop.wait()

    task = asyncio.create_task(module.supervised(pg_database, "search", operation, stop, marker))
    await started.wait()
    assert module.check_service(pg_database, marker)
    assert marker.stat().st_mode & 0o777 == 0o600
    assert module.diagnostics(pg_database)["services"]["search"] == 1
    stop.set()
    await task
    assert not marker.exists()
    assert module.diagnostics(pg_database)["services"]["search"] == 0


async def test_supervision_failure_stops_siblings_and_does_not_leave_a_healthy_service(
    pg_database, tmp_path
):
    marker = tmp_path / "health"
    stop = asyncio.Event()

    async def failure():
        raise RuntimeError("controlled failure")

    with pytest.raises(ExceptionGroup):
        await module.supervised(pg_database, "chat", failure, stop, marker)
    assert stop.is_set() and not marker.exists()
    with pg_database.transaction() as connection:
        assert connection.execute(select(s.runtime_control.c.key)).first() is None


def test_service_health_rejects_missing_symlink_public_corrupt_and_stale_markers(
    pg_database, tmp_path
):
    path = tmp_path / "health"
    assert not module.check_service(pg_database, path)
    path.write_text("bad JSON")
    path.chmod(0o600)
    assert not module.check_service(pg_database, path)
    path.write_text(json.dumps({"id": "service:one", "service": "search"}))
    assert not module.check_service(pg_database, path)
    with pg_database.transaction() as connection:
        connection.execute(
            s.runtime_control.insert().values(
                key="service:one", payload={"service": "search", "at": 1}
            )
        )
    assert not module.check_service(pg_database, path)
    path.chmod(0o644)
    assert not module.check_service(pg_database, path)
    link = tmp_path / "link"
    link.symlink_to(path)
    assert not module.check_service(pg_database, link)


@pytest.mark.parametrize("healthy", [True, False])
def test_health_cli_service_has_a_machine_readable_exit_status(
    pg_database, tmp_path, monkeypatch, healthy
):
    marker = tmp_path / "health"
    monkeypatch.setattr(sys, "argv", ["health", "service"])
    monkeypatch.setenv("SERVICE_HEALTH_FILE", str(marker))
    monkeypatch.setattr(module.Database, "from_env", lambda: pg_database)
    monkeypatch.setattr(module, "check_service", lambda database, path: healthy)
    with pytest.raises(SystemExit) as result:
        module.main()
    assert result.value.code == (0 if healthy else 1)


@pytest.mark.parametrize("status", [200, 503, None])
def test_health_cli_api_does_not_print_server_details(monkeypatch, status, capsys):
    monkeypatch.setattr(sys, "argv", ["health", "api"])

    @contextmanager
    def response(*args, **kwargs):
        if status is None:
            raise OSError("private server details")
        yield SimpleNamespace(status=status)

    monkeypatch.setattr(module, "urlopen", response)
    with pytest.raises(SystemExit) as result:
        module.main()
    assert result.value.code == (0 if status == 200 else 1)
    assert capsys.readouterr().out == ""
