import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import func, select
from test_accounts import api_world as api_world
from test_telegram_behavior import bot as bot

from qa_job_bot import maintenance as module
from qa_job_bot import pg_schema as s
from qa_job_bot.accounts import account_changes
from qa_job_bot.personal_telegram import PersonalTelegram
from qa_job_bot.runtime_worker import RuntimeWorker
from qa_job_bot.scheduler import Scheduler
from qa_job_bot.web import changes
from qa_job_bot.web_security import WebSettings
from qa_job_bot.worker import Worker


def test_maintenance_is_private_idempotent_and_persists_across_instances(tmp_path):
    root = tmp_path / "data"
    assert not module.active(root)
    assert module.set_mode(root, False) == {"maintenance": False}
    assert module.set_mode(root, True) == {"maintenance": True}
    assert module.set_mode(root, True) == {"maintenance": True}
    assert (root / module.MARKER).stat().st_mode & 0o777 == 0o700
    assert module.active(Path(str(root)))
    assert module.set_mode(root, False) == {"maintenance": False}


@pytest.mark.parametrize("kind", ["file", "public", "link", "nonempty"])
def test_corrupt_maintenance_markers_fail_closed_and_are_not_silently_removed(tmp_path, kind):
    root = tmp_path / "data"
    root.mkdir()
    marker = root / module.MARKER
    if kind == "file":
        marker.write_text("private unexpected content")
    elif kind == "link":
        target = tmp_path / "other"
        target.mkdir()
        marker.symlink_to(target)
    else:
        marker.mkdir(mode=0o700)
        if kind == "public":
            marker.chmod(0o755)
        else:
            (marker / "unexpected").write_text("preserve this file")
    assert module.active(root)
    with pytest.raises((ValueError, OSError)):
        module.set_mode(root, False)
    assert marker.exists() or marker.is_symlink()


def test_maintenance_refuses_a_symlink_root_and_stays_closed_on_unreadable_marker(
    tmp_path, monkeypatch
):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        module.set_mode(link, True)
    monkeypatch.setattr(Path, "lstat", Mock(side_effect=PermissionError))
    assert module.active(target)


@pytest.mark.parametrize("mode", ["on", "status", "off"])
def test_maintenance_cli_does_not_print_files_or_credentials(tmp_path, monkeypatch, capsys, mode):
    monkeypatch.setattr(sys, "argv", ["maintenance", mode, "--data-dir", str(tmp_path)])
    module.main()
    assert json.loads(capsys.readouterr().out) == {"maintenance": mode == "on"}


def table_counts(database):
    with database.transaction() as connection:
        return {
            table.name: connection.execute(select(func.count()).select_from(table)).scalar_one()
            for table in s.metadata.sorted_tables
        }


def test_maintenance_blocks_all_api_writes_and_snapshot_reads_without_changing_database(api_world):
    admin, member, app, *_ = api_world
    before = table_counts(app.state.database)
    module.set_mode(app.state.settings.data_dir, True)
    for client in (admin, member):
        session = client.get("/api/session")
        assert session.status_code == 200
        assert session.json() == {
            "authenticated": False,
            "setup_required": False,
            "maintenance": True,
        }
        for method, path, body in [
            ("GET", "/api/jobs", None),
            ("GET", "/api/events", None),
            ("GET", "/api/admin/diagnostics", None),
            ("POST", "/api/login", {"password": "PRIVATE"}),
            ("POST", "/api/search-runs", {}),
            ("PATCH", "/api/account", {"display_name": "PRIVATE"}),
            ("POST", "/api/admin/invitations", {"login": "member-new", "display_name": "Private"}),
        ]:
            response = client.request(method, path, json=body)
            assert response.status_code == 503 and response.json()["maintenance"]
            assert response.headers["retry-after"] == "60"
            assert response.headers["cache-control"] == "no-store"
            assert "PRIVATE" not in response.text
    assert admin.get("/health/live").status_code == 200
    assert admin.get("/health/ready").status_code == 503
    assert admin.get("/health/live", headers={"host": "untrusted.test"}).status_code == 400
    assert table_counts(app.state.database) == before
    module.set_mode(app.state.settings.data_dir, False)
    assert admin.get("/api/me").status_code == 200
    assert member.get("/api/me").status_code == 200


async def test_both_sse_modes_announce_maintenance_before_any_session_or_store_access(api_world):
    _, _, app, *_ = api_world
    module.set_mode(app.state.settings.data_dir, True)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    legacy = SimpleNamespace(settings=app.state.settings, session=Mock(side_effect=AssertionError))
    accounts = SimpleNamespace(
        settings=app.state.settings, session=Mock(side_effect=AssertionError)
    )
    for stream in (
        changes(request, legacy, "cookie"),
        account_changes(request, accounts, "cookie", "owner"),
    ):
        assert await anext(stream) == "event: maintenance\ndata: {}\n\n"
        with pytest.raises(StopAsyncIteration):
            await anext(stream)


async def test_workers_and_scheduler_do_not_claim_or_schedule_during_maintenance(tmp_path):
    settings = WebSettings(tmp_path)
    module.set_mode(tmp_path, True)
    store, database = Mock(), Mock()
    worker = Worker(store, settings, gateway=Mock(), search=Mock())
    assert not await worker.once()
    store.claim.assert_not_called()
    shared = RuntimeWorker(database, settings, gateway=Mock())
    assert not await shared.once("document")
    database.transaction.assert_not_called()
    assert Scheduler(database, settings).tick() == []
    database.transaction.assert_not_called()
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0)
    stop.set()
    await task
    store.get_setting.assert_not_called()


async def test_personal_telegram_pauses_before_commands_delivery_and_polling(pg_database, tmp_path):
    module.set_mode(tmp_path, True)
    api = SimpleNamespace(call=AsyncMock(), send=AsyncMock(), close=AsyncMock())
    instance = PersonalTelegram(pg_database, api, WebSettings(tmp_path))
    await instance.handle({"message": {}})
    await instance.pending()
    await instance.alerts()
    stop = asyncio.Event()
    task = asyncio.create_task(instance.run(stop))
    await asyncio.sleep(0)
    stop.set()
    await task
    api.call.assert_not_awaited()
    api.send.assert_not_awaited()
    api.close.assert_awaited_once()


async def test_legacy_telegram_pauses_startup_commands_and_both_loops(bot, monkeypatch):
    module.set_mode(bot.settings.database_path.parent, True)
    with pytest.raises(RuntimeError, match="maintenance"):
        await bot.run()
    await bot._handle_update({"message": {"text": "/search"}})
    bot.api.call.assert_not_awaited()
    bot.api.send.assert_not_awaited()

    async def stop_polling(delay):
        bot._stop.set()

    monkeypatch.setattr("qa_job_bot.telegram.asyncio.sleep", stop_polling)
    await bot._poll_loop()
    bot.api.call.assert_not_awaited()
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError])
    monkeypatch.setattr("qa_job_bot.telegram.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await bot._alert_loop()
    bot.service.refresh.assert_not_awaited()
