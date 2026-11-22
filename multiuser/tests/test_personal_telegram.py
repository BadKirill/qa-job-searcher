import asyncio
import json
import multiprocessing
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pg_fixtures import personal_store
from sqlalchemy import select
from test_personal_documents import vacancy
from test_personal_search_runs import configure
from test_shared_runtime import ready

from qa_job_bot import main as entry
from qa_job_bot import personal_telegram as module
from qa_job_bot import pg_schema as s
from qa_job_bot.pg_database import Database
from qa_job_bot.web_store import StoreError


def update(command, chat_id=111, update_id=1, **changes):
    message = {"chat": {"id": chat_id, "type": "private"}, "from": {"id": chat_id}, "text": command}
    message.update(changes)
    return {"update_id": update_id, "message": message}


def bot(store, enabled=True):
    ready(store)
    module.bind(store.database, 111, "synthetic-owner", enabled)
    api = SimpleNamespace(send=AsyncMock(), call=AsyncMock(), close=AsyncMock())
    return module.PersonalTelegram(store.database, api), api


def publish(store, count=1):
    jobs = []
    for index in range(count):
        job = vacancy()
        job.source_id = str(index)
        job.url += f"/{index}"
        jobs.append(job)
    store.upsert_jobs(jobs)
    store.publish_search(jobs, [])
    return [job.fingerprint for job in jobs]


def test_explicit_binding_is_idempotent_preserves_deliveries_and_never_reassigns_a_chat(pg_store):
    ready(pg_store)
    assert module.bind(pg_store.database, 111, "SYNTHETIC-OWNER") == {
        "bound": True,
        "alerts": False,
    }
    job_id = publish(pg_store)[0]
    with pg_store.write() as connection:
        connection.execute(
            s.deliveries.insert().values(user_id=pg_store.user_id, chat_id=111, job_id=job_id)
        )
    assert module.bind(pg_store.database, 111, "synthetic-owner", True)["alerts"]
    assert module.bind(pg_store.database, 111, "synthetic-owner")["alerts"]
    other = ready(personal_store(pg_store.database, "other"))
    with pytest.raises(StoreError) as error:
        module.bind(pg_store.database, 111, "other")
    assert error.value.status == 409
    with pg_store.read() as connection:
        assert connection.execute(select(s.deliveries.c.job_id)).scalar_one() == job_id
        assert (
            connection.execute(select(s.telegram_bindings.c.user_id)).scalar_one() != other.user_id
        )


@pytest.mark.parametrize("chat_id", [True, 0, -1, 2**63, "111"])
def test_binding_rejects_non_private_chat_identifiers(pg_store, chat_id):
    with pytest.raises(StoreError) as error:
        module.bind(pg_store.database, chat_id, "synthetic-owner")
    assert error.value.status == 422


def test_binding_rejects_unactivated_disabled_and_unknown_accounts(pg_store):
    for login in ("synthetic-owner", "missing"):
        with pytest.raises(StoreError) as error:
            module.bind(pg_store.database, 111, login)
        assert error.value.status == 404
    ready(pg_store)
    with pg_store.write() as connection:
        connection.execute(
            s.users.update().where(s.users.c.id == pg_store.user_id).values(disabled=True)
        )
    with pytest.raises(StoreError):
        module.bind(pg_store.database, 111, "synthetic-owner")


@pytest.mark.parametrize(
    "message",
    [
        {},
        update("/search", chat={"id": 111, "type": "group"}),
        update("/search", **{"from": {"id": 222}}),
        update("/search", chat_id=-1),
        update("/search", **{"from": {"id": True}}),
    ],
)
async def test_malformed_group_and_mismatched_sender_messages_never_receive_private_data(
    pg_store, message
):
    instance, api = bot(pg_store)
    await instance.handle(message)
    api.send.assert_not_awaited()


async def test_unbound_users_receive_only_binding_instructions_and_their_own_id(pg_store):
    instance, api = bot(pg_store)
    await instance.handle(update("/whoami", chat_id=222))
    assert api.send.call_args.args == (222, "Ваш Telegram ID: <code>222</code>")
    await instance.handle(update("/settings", chat_id=222))
    assert "не привязан" in api.send.call_args.args[1]


@pytest.mark.parametrize(
    "command", ["/settings", "/sources", "/stats", "/help", "", "/unknown@bot"]
)
async def test_commands_read_only_the_explicitly_bound_personal_store(pg_store, command):
    instance, api = bot(pg_store)
    configure(pg_store)
    publish(pg_store)
    await instance.handle(update(command))
    assert api.send.call_args.args[0] == 111
    value = api.send.call_args.args[1]
    assert "PRIVATE" not in value
    if command == "/settings":
        assert "backend engineer" in value
    if command == "/sources":
        assert "Remotive" in value
    if command == "/stats":
        assert "1 вакансий" in value


async def test_search_is_personal_queued_and_delivered_later_even_when_alerts_are_off(pg_store):
    instance, api = bot(pg_store, enabled=False)
    configure(pg_store)
    await instance.handle(update("/search"))
    task = pg_store.tasks()[0]
    assert task["kind"] == "search" and task["status"] == "queued"
    await instance.pending()
    assert api.send.await_count == 1
    claimed = pg_store.claim("worker")
    assert pg_store.finish_task(claimed["id"], "worker", "ready")
    publish(pg_store)
    await instance.pending()
    assert "Backend Engineer" in api.send.call_args.args[1]
    await instance.pending()
    assert api.send.await_count == 2
    await instance.alerts()
    assert api.send.await_count == 2
    with pg_store.read() as connection:
        assert connection.execute(select(s.deliveries.c.job_id)).first() is not None


@pytest.mark.parametrize("status", ["ready", "failed"])
async def test_manual_search_reports_empty_or_failed_results_without_private_errors(
    pg_store, status
):
    instance, api = bot(pg_store, enabled=False)
    configure(pg_store)
    await instance.handle(update("/search"))
    task = pg_store.claim("worker")
    pg_store.finish_task(task["id"], "worker", status, error="PRIVATE provider contents")
    await instance.pending()
    response = api.send.call_args.args[1]
    assert "PRIVATE" not in response
    assert ("пока нет" if status == "ready" else "не завершён") in response


async def test_alert_opt_in_takes_a_baseline_and_opt_out_stops_new_messages(pg_store):
    instance, api = bot(pg_store, enabled=False)
    publish(pg_store)
    await instance.handle(update("/alerts_on"))
    await instance.alerts()
    assert api.send.await_count == 1
    publish(pg_store, count=2)
    await instance.alerts()
    assert api.send.await_count == 2
    await instance.alerts()
    assert api.send.await_count == 2
    await instance.handle(update("/alerts_off"))
    publish(pg_store, count=3)
    assert await instance.deliver(111, pg_store) == 0
    await instance.alerts()
    assert api.send.await_count == 3


async def test_two_accounts_never_receive_each_others_jobs_and_disable_revokes_delivery(pg_store):
    instance, api = bot(pg_store)
    other = ready(personal_store(pg_store.database, "other"))
    module.bind(pg_store.database, 222, "other", True)
    own = publish(pg_store)[0]
    job = vacancy()
    job.source_id, job.title, job.url = (
        "designer",
        "Product Designer",
        "https://example.test/design",
    )
    other.upsert_jobs([job])
    other.publish_search([job], [])
    await instance.alerts()
    values = {call.args[0]: call.args[1] for call in api.send.call_args_list}
    assert "Backend Engineer" in values[111] and "Product Designer" not in values[111]
    assert "Product Designer" in values[222] and "Backend Engineer" not in values[222]
    with pg_store.write() as connection:
        connection.execute(
            s.users.update().where(s.users.c.id == pg_store.user_id).values(disabled=True)
        )
    with pytest.raises(StoreError):
        await instance.send(111, "private", pg_store.user_id)
    await instance.alerts()
    assert api.send.await_count == 2
    assert own != job.fingerprint


async def test_delivery_skips_applied_archive_and_existing_deliveries_and_limits_a_batch(pg_store):
    instance, api = bot(pg_store)
    identifiers = publish(pg_store, count=9)
    pg_store.change_state(identifiers[0], {"applied": True})
    pg_store.change_state(identifiers[1], {"archived": True})
    instance.acknowledge(pg_store, 111, [identifiers[2]])
    assert await instance.deliver(111, pg_store) == 5
    assert api.send.await_count == 5
    assert await instance.deliver(111, pg_store) == 1


async def test_delivery_failures_preserve_pending_work_and_never_log_provider_contents(
    pg_store, caplog
):
    instance, api = bot(pg_store)
    publish(pg_store)
    api.send.side_effect = RuntimeError("PRIVATE token and message")
    await instance.alerts()
    assert "PRIVATE" not in caplog.text and "RuntimeError" in caplog.text
    api.send.side_effect = None
    await instance.alerts()
    assert api.send.await_count == 2


async def test_search_without_configuration_reports_a_safe_error(pg_store):
    instance, api = bot(pg_store)
    await instance.handle(update("/search"))
    assert "Действие недоступно" in api.send.call_args.args[1]
    assert pg_store.tasks() == []


def process_lock(url, schema, output):
    database = Database(url, schema)
    try:
        try:
            with module.singleton(database):
                output.put(True)
        except RuntimeError:
            output.put(False)
    finally:
        database.close()


def test_only_one_polling_process_can_hold_the_postgresql_lock_and_restart_releases_it(pg_database):
    context = multiprocessing.get_context("spawn")
    output = context.Queue()

    def attempt():
        process = context.Process(
            target=process_lock,
            args=(
                pg_database.engine.url.render_as_string(hide_password=False),
                pg_database.schema,
                output,
            ),
        )
        process.start()
        process.join(15)
        assert process.exitcode == 0
        return output.get(timeout=2)

    with module.singleton(pg_database):
        assert attempt() is False
    assert attempt() is True


async def test_polling_persists_offset_releases_its_lock_and_closes_api(pg_store):
    instance, api = bot(pg_store)
    stop = asyncio.Event()

    async def call(method, **values):
        if method == "deleteWebhook":
            return None
        assert values["offset"] == 0 and values["allowed_updates"] == ["message"]
        stop.set()
        return [update("/whoami", update_id=10)]

    api.call.side_effect = call
    await instance.run(stop)
    api.close.assert_awaited_once()
    assert instance.lock is None
    with pg_store.read() as connection:
        value = connection.execute(
            select(s.runtime_control.c.payload).where(s.runtime_control.c.key == "telegram-offset")
        ).scalar_one()
    assert value == {"offset": 11}
    with module.singleton(pg_store.database):
        pass


async def test_entrypoint_requires_explicit_personal_mode_and_routes_to_personal_runtime(
    monkeypatch,
):
    monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic")
    monkeypatch.setenv("TELEGRAM_ACCOUNT_MODE", "personal")
    runtime = AsyncMock()
    monkeypatch.setattr(module, "async_main", runtime)
    await entry.async_main()
    runtime.assert_awaited_once()


@pytest.mark.parametrize(
    "available,token", [(False, None), (True, "invalid"), (True, "123:" + "x" * 24)]
)
async def test_personal_startup_checks_readiness_and_private_configuration(
    pg_database, tmp_path, monkeypatch, available, token
):
    monkeypatch.setenv("WEB_DATA_DIR", str(tmp_path))
    path = tmp_path / "telegram-bot-token"
    if token:
        path.write_text(token)
        path.chmod(0o600)
    monkeypatch.setattr(Database, "from_env", lambda: pg_database)
    monkeypatch.setattr(module, "ready", lambda database: available)
    supervised = AsyncMock()
    monkeypatch.setattr(module, "supervised", supervised)
    api = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(module, "TelegramAPI", lambda token, timeout: api)
    if not available or token == "invalid":
        with pytest.raises((RuntimeError, ValueError)):
            await module.async_main()
    else:
        await module.async_main()
        supervised.assert_awaited_once()


@pytest.mark.parametrize("command", ["bind", "run"])
def test_operator_cli_can_bind_without_exposing_tokens_and_start_selected_runtime(
    pg_store, monkeypatch, capsys, command
):
    ready(pg_store)
    monkeypatch.setattr(Database, "from_env", lambda: pg_store.database)
    if command == "bind":
        monkeypatch.setattr(
            sys, "argv", ["telegram", "bind", "--chat-id", "111", "--login", "synthetic-owner"]
        )
        module.main()
        assert json.loads(capsys.readouterr().out) == {"bound": True, "alerts": False}
    else:
        monkeypatch.setattr(sys, "argv", ["telegram", "run"])
        routine = AsyncMock()
        monkeypatch.setattr(module, "async_main", routine)
        module.main()
        routine.assert_awaited_once()
