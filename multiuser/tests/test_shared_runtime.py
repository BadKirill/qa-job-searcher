import asyncio
import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import httpx
import pytest
from alembic import command
from e2e_server import FixtureGateway
from pg_fixtures import personal_store
from sqlalchemy import inspect, select
from test_accounts import api_world as api_world
from test_personal_documents import seed
from test_personal_search_runs import configure

from qa_job_bot import pg_schema as s
from qa_job_bot.accounts import CurrentUser
from qa_job_bot.openai_transport import OpenAITransport, OpenAITransportError, retry_delay
from qa_job_bot.pg_database import Database
from qa_job_bot.pg_queue import QueueRepository
from qa_job_bot.pg_store import PostgresStore
from qa_job_bot.runtime_limits import DEFAULTS, QuotaExceeded, RuntimeLimits
from qa_job_bot.runtime_worker import RuntimeWorker
from qa_job_bot.scheduler import Scheduler, schedule_in
from qa_job_bot.web_security import WebSettings


def actor(store):
    with store.database.transaction() as connection:
        connection.execute(
            s.users.update().where(s.users.c.id == store.user_id).values(role="admin")
        )
    return CurrentUser.from_row(store.database.user(store.user_id))


def ready(store):
    with store.database.transaction() as connection:
        connection.execute(
            s.users.update()
            .where(s.users.c.id == store.user_id)
            .values(password_hash="synthetic-runtime-password-hash")
        )
    return store


def claimed(store, kind="search", key="one", worker="worker"):
    ready(store)
    task = store.enqueue(kind, key, {}, key)
    owned = store.claim(worker)
    assert owned["id"] == task["id"]
    return {"owner_id": store.user_id, "task_id": task["id"], "execution_id": worker}


def process_claim(url, schema, worker, lane, result):
    database = Database(url, schema)
    try:
        task = QueueRepository(database).claim(worker, lane=lane)
        result.put(task["id"] if task else None)
    finally:
        database.close()


def test_document_slots_and_fairness_are_atomic_across_processes(pg_store):
    other = ready(personal_store(pg_store.database, "other-worker"))
    ready(pg_store)
    tasks = []
    for store in [pg_store, pg_store, other]:
        tasks.append(store.enqueue("cv", str(len(tasks)), {}, str(len(tasks))))
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    processes = [
        context.Process(
            target=process_claim,
            args=(
                str(pg_store.database.engine.url.render_as_string(hide_password=False)),
                pg_store.database.schema,
                f"worker-{index}",
                "document",
                result,
            ),
        )
        for index in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    values = [result.get(timeout=5) for _ in processes]
    selected = [value for value in values if value]
    assert len(selected) == len(set(selected)) == 2
    with pg_store.database.transaction() as connection:
        owners = (
            connection.execute(select(s.tasks.c.user_id).where(s.tasks.c.id.in_(selected)))
            .scalars()
            .all()
        )
    assert set(owners) == {pg_store.user_id, other.user_id}


def test_busy_owner_does_not_starve_another_and_lanes_are_independent(pg_store):
    other = ready(personal_store(pg_store.database, "fair-other"))
    ready(pg_store)
    admin = actor(pg_store)
    RuntimeLimits(pg_store.database).configure(admin, limits={"document_slots": 1})
    a = pg_store.enqueue("cv", "a", {}, "a")
    b = pg_store.enqueue("cv", "b", {}, "b")
    c = other.enqueue("cv", "c", {}, "c")
    queue = QueueRepository(pg_store.database)
    first = queue.claim("first", lane="document")
    assert first["id"] == a["id"]
    assert queue.claim("blocked", lane="document") is None
    search = other.enqueue("search", "search", {}, "search")
    assert queue.claim("search", lane="search")["id"] == search["id"]
    assert pg_store.finish_task(first["id"], "first", "ready")
    assert queue.claim("second", lane="document")["id"] == c["id"]
    assert other.finish_task(c["id"], "second", "ready")
    assert queue.claim("third", lane="document")["id"] == b["id"]


def test_expired_search_reclaims_with_new_execution_and_old_worker_cannot_publish(pg_store):
    ready(pg_store)
    configure(pg_store)
    queued = pg_store.enqueue_search("reclaim")
    queue = QueueRepository(pg_store.database)
    first = queue.claim("old", lease_seconds=-1, lane="search")
    second = queue.claim("new", lane="search")
    assert first["id"] == second["id"] == queued["id"] and second["attempts"] == 2
    assert not queue.heartbeat(first["id"], "old")
    assert not pg_store.finish_task(first["id"], "old", "ready")
    assert queue.heartbeat(second["id"], "new")
    assert pg_store.finish_task(second["id"], "new", "ready")


def test_exhausted_search_retry_reports_an_interrupted_search_in_history(pg_store):
    ready(pg_store)
    configure(pg_store)
    task = pg_store.enqueue_search("interrupted-search")
    queue = QueueRepository(pg_store.database)
    for attempt in range(1, 4):
        run_task = queue.claim(f"worker-{attempt}", lease_seconds=-1, lane="search")
        assert run_task["id"] == task["id"] and run_task["attempts"] == attempt
    assert queue.claim("late-worker", lane="search") is None
    run = pg_store.search_run(task["id"])
    assert run["state"] == "failed"
    assert run["task_error"] == "Поиск прервался. Запустите его ещё раз."


def test_paid_task_is_not_blindly_restarted_and_expired_reservation_stays_charged(pg_store):
    context = claimed(pg_store, "cv")
    limits = RuntimeLimits(pg_store.database)
    identifier = limits.reserve(context, "generator")
    limits.progress(identifier, "sent", {})
    with pg_store.database.transaction() as connection:
        connection.execute(
            s.tasks.update().where(s.tasks.c.id == context["task_id"]).values(lease_until=0)
        )
        row = connection.execute(
            select(s.usage_reservations.c.payload).where(s.usage_reservations.c.id == identifier)
        ).scalar_one()
        connection.execute(
            s.usage_reservations.update()
            .where(s.usage_reservations.c.id == identifier)
            .values(payload={**row, "expires_at": 0})
        )
    assert QueueRepository(pg_store.database).claim("restart", lane="document") is None
    assert pg_store.task(context["task_id"])["status"] == "failed"
    assert limits.reap() == 1
    assert limits.summary(pg_store.user_id)["used_calls"] == 1
    assert limits.summary(pg_store.user_id)["unknown_calls"] == 1
    with pytest.raises(QuotaExceeded):
        limits.progress(identifier, "sent", {})


def test_quota_reservation_is_atomic_for_user_global_and_slots(pg_store):
    admin = actor(pg_store)
    context = claimed(pg_store)
    limits = RuntimeLimits(pg_store.database)
    limits.configure(admin, limits={"api_slots": 1, "daily_calls": 2, "user_daily_calls": 2})
    with ThreadPoolExecutor(max_workers=6) as pool:
        values = list(pool.map(lambda _: limits.reserve(context, "chat"), range(6)))
    identifiers = [value for value in values if value]
    assert len(identifiers) == 1
    limits.progress(identifiers[0], "sent", {})
    limits.finish(identifiers[0], False)
    assert limits.reserve(context, "reviewer") is None
    with pg_store.database.transaction() as connection:
        row = connection.execute(
            select(s.usage_reservations.c.payload).where(
                s.usage_reservations.c.id == identifiers[0]
            )
        ).scalar_one()
        connection.execute(
            s.usage_reservations.update()
            .where(s.usage_reservations.c.id == identifiers[0])
            .values(payload={**row, "expires_at": 0})
        )
    limits.configure(admin, limits={"api_slots": 2})
    second = limits.reserve(context, "reviewer")
    limits.finish(second, True)
    with pytest.raises(QuotaExceeded, match="Общий"):
        limits.reserve(context, "generator")
    limits.configure(admin, limits={"daily_calls": 10})
    with pytest.raises(QuotaExceeded, match="Ваш"):
        limits.reserve(context, "generator")
    limits.configure(admin, user_id=pg_store.user_id, daily_calls=3)
    third = limits.reserve(context, "visual_review")
    limits.finish(third, False)
    assert limits.summary(pg_store.user_id)["used_calls"] == 2
    limits.finish(third, False)
    with pg_store.read() as connection:
        assert len(connection.execute(select(s.usage_events)).all()) == 3


async def test_all_roles_and_negative_reviewer_attempts_are_metered(pg_store, tmp_path):
    ready(pg_store)
    job_id, _ = seed(pg_store)
    task = pg_store.enqueue_document(job_id, "cv", "en", "metered")

    class Revising(FixtureGateway):
        def __init__(self):
            self.reviews = 0

        async def review(self, context, draft):
            result = await super().review(context, draft)
            self.reviews += 1
            if self.reviews == 1:
                result.update(verdict="revise", issues=["Improve wording"])
            return result

    worker = RuntimeWorker(pg_store.database, WebSettings(tmp_path), Revising())
    assert await worker.once("document")
    assert pg_store.task(task["id"])["status"] == "ready"
    with pg_store.read() as connection:
        roles = connection.execute(select(s.usage_events.c.role)).scalars().all()
    assert (
        roles.count("generator") == 2
        and roles.count("reviewer") == 2
        and roles.count("visual_reviewer") == 1
    )
    assert RuntimeLimits(pg_store.database).summary(pg_store.user_id)["used_calls"] == 5


async def test_heartbeat_keeps_slow_task_live_and_cancellation_stops_late_result(
    pg_store, tmp_path, monkeypatch
):
    ready(pg_store)
    job_id, _ = seed(pg_store)
    queued = pg_store.enqueue_document(job_id, "cv", "en", "slow")
    entered, kept_alive, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    initial = time.time()
    clock = [initial]
    monkeypatch.setattr("qa_job_bot.pg_queue.time.time", lambda: clock[0])
    heartbeat = QueueRepository.heartbeat
    renewals = []

    def advance(self, *args):
        clock[0] += 0.08
        accepted = heartbeat(self, *args)
        renewals.append(accepted)
        if len(renewals) == 3:
            kept_alive.set()
        return accepted

    monkeypatch.setattr(QueueRepository, "heartbeat", advance)

    class Slow(FixtureGateway):
        async def generate(self, context):
            entered.set()
            await release.wait()
            return await super().generate(context)

    worker = RuntimeWorker(
        pg_store.database,
        WebSettings(tmp_path),
        Slow(),
        heartbeat_seconds=0.001,
        lease_seconds=0.12,
    )
    running = asyncio.create_task(worker.once("document"))
    await entered.wait()
    await asyncio.wait_for(kept_alive.wait(), 5)
    assert clock[0] > initial + 0.12 and all(renewals)
    assert QueueRepository(pg_store.database).claim("thief", lane="document") is None
    pg_store.cancel_task(queued["id"])
    await running
    assert pg_store.task(queued["id"])["status"] == "cancelled"
    assert pg_store.task(queued["id"])["artifacts"] == []


def test_scheduler_spreads_defaults_and_multiple_ticks_cannot_duplicate(pg_store):
    configure(pg_store)
    other = personal_store(pg_store.database, "scheduled-other")
    configure(other)
    with pg_store.database.transaction() as connection:
        for store in [pg_store, other]:
            connection.execute(
                s.users.update()
                .where(s.users.c.id == store.user_id)
                .values(password_hash="synthetic")
            )
            schedule_in(connection, store.user_id, time.time())
        defaults = connection.execute(select(s.search_schedules)).mappings().all()
        assert all(row["interval_seconds"] == 10800 for row in defaults)
        assert len({row["next_run_at"] for row in defaults}) == 2
        connection.execute(s.search_schedules.update().values(next_run_at=1))
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: Scheduler(pg_store.database).tick(), range(3)))
    assert sum(len(result) for result in results) == 2
    assert len(pg_store.tasks()) == len(other.tasks()) == 1
    assert Scheduler(pg_store.database).tick() == []


def test_schedule_skips_unconfigured_disabled_and_switched_off(pg_store):
    other = personal_store(pg_store.database, "unconfigured")
    with pg_store.database.transaction() as connection:
        for store in [pg_store, other]:
            connection.execute(
                s.users.update()
                .where(s.users.c.id == store.user_id)
                .values(password_hash="synthetic")
            )
            schedule_in(connection, store.user_id, 0)
        connection.execute(
            s.search_schedules.update()
            .where(s.search_schedules.c.user_id == pg_store.user_id)
            .values(enabled=False)
        )
    assert Scheduler(pg_store.database).tick() == []
    assert pg_store.tasks() == other.tasks() == []


def test_runtime_api_permissions_validation_personal_usage_and_schedule(api_world):
    admin, member, app, admin_id, member_id = api_world
    assert admin.get("/api/admin/runtime").status_code == 200
    assert member.get("/api/admin/runtime").status_code == 403
    assert member.put("/api/admin/runtime", json=DEFAULTS).status_code == 403
    assert admin.put("/api/admin/runtime", json={**DEFAULTS, "api_slots": 0}).status_code == 422
    assert admin.put("/api/admin/users/missing/limits", json={"daily_calls": 0}).status_code == 404
    assert (
        admin.put(f"/api/admin/users/{member_id}/limits", json={"daily_calls": 0}).status_code
        == 200
    )
    assert member.get("/api/usage").json()["daily_calls"] == 0
    assert admin.get("/api/usage").json()["daily_calls"] == 60
    assert member.get("/api/usage?user_id=" + admin_id).json()["daily_calls"] == 0
    assert (
        member.put("/api/schedule", json={"enabled": False, "interval_seconds": 3600}).status_code
        == 200
    )
    assert not member.get("/api/schedule").json()["enabled"]
    assert admin.get("/api/schedule").json()["enabled"]
    assert (
        member.put("/api/schedule", json={"enabled": True, "interval_seconds": 1}).status_code
        == 422
    )
    assert (
        member.put("/api/schedule", json={"enabled": True, "user_id": admin_id}).status_code == 422
    )
    store = PostgresStore(app.state.database, member_id)
    context = claimed(store)
    with pytest.raises(QuotaExceeded):
        RuntimeLimits(app.state.database).reserve(context, "chat")
    assert admin.patch(f"/api/admin/users/{member_id}", json={"disabled": True}).status_code == 200
    with app.state.database.transaction() as connection:
        status = connection.execute(
            select(s.tasks.c.status).where(s.tasks.c.id == context["task_id"])
        ).scalar_one()
    assert status == "cancelled"
    assert member.get("/api/usage").status_code == 401


@pytest.mark.parametrize(
    "header,expected", [(None, 2), ("broken", 2), ("NaN", 2), ("0", 1), ("600", 600)]
)
def test_retry_after_validation(header, expected):
    assert retry_delay(header) == expected


async def test_transport_retries_only_explicit_429_with_same_key_and_audits_cooldown(monkeypatch):
    events, keys = [], []
    from qa_job_bot.api_audit import CALL_AUDIT

    token = CALL_AUDIT.set(lambda event, data: events.append((event, data)))
    sleep = AsyncMock()
    monkeypatch.setattr("qa_job_bot.openai_transport.asyncio.sleep", sleep)

    async def response(request):
        keys.append(request.headers.get("Idempotency-Key"))
        return httpx.Response(429, headers={"Retry-After": "1"})

    transport = OpenAITransport(
        "synthetic-key",
        "agent_synthetic",
        "model",
        "medium",
        http_transport=httpx.MockTransport(response),
    )
    try:
        with pytest.raises(OpenAITransportError, match="429"):
            await transport.run(instructions="safe", schema={}, content="data")
    finally:
        CALL_AUDIT.reset(token)
    assert len(keys) == 3 and len(set(keys)) == 1
    assert sleep.await_count == 2
    assert len([event for event, _ in events if event == "rate_limit"]) == 3


async def test_transport_never_resubmits_unknown_paid_request(monkeypatch):
    requests = []

    async def response(request):
        requests.append(request)
        raise httpx.ReadTimeout("Synthetic unknown network outcome")

    transport = OpenAITransport(
        "synthetic-key",
        "agent_synthetic",
        "model",
        "medium",
        http_transport=httpx.MockTransport(response),
    )
    with pytest.raises(OpenAITransportError, match="ReadTimeout"):
        await transport.run(instructions="safe", schema={}, content="data")
    assert len(requests) == 1


def test_stage7_migration_roundtrip_preserves_previous_data(pg_store):
    job_id, template = seed(pg_store)
    with pg_store.database.engine.begin() as connection:
        command.downgrade(pg_store.database.migration_config(connection), "0006_document_context")
    assert "search_schedules" not in inspect(pg_store.database.engine).get_table_names()
    pg_store.database.migrate()
    assert pg_store.profile()["active"]["id"] == template["id"]
    assert pg_store.get_job(job_id)["title"] == "Backend Engineer"


def process_reserve(url, schema, context, result):
    database = Database(url, schema)
    try:
        result.put(RuntimeLimits(database).reserve(context, "generator"))
    finally:
        database.close()


def test_api_slots_are_atomic_across_processes_and_one_user_cannot_occupy_both(pg_store):
    other = personal_store(pg_store.database, "api-other")
    first, second = claimed(pg_store), claimed(other)
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    url = pg_store.database.engine.url.render_as_string(hide_password=False)
    processes = [
        context.Process(target=process_reserve, args=(url, pg_store.database.schema, value, result))
        for value in [first, second, first, second]
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    identifiers = [result.get(timeout=5) for _ in processes]
    assert len([value for value in identifiers if value]) == 2
    assert RuntimeLimits(pg_store.database).summary(pg_store.user_id)["used_calls"] == 1
    assert RuntimeLimits(pg_store.database).summary(other.user_id)["used_calls"] == 1


async def test_cancel_during_native_render_never_registers_late_files(
    pg_store, tmp_path, monkeypatch
):
    import threading

    import qa_job_bot.worker as module

    ready(pg_store)
    job_id, _ = seed(pg_store)
    queued = pg_store.enqueue_document(job_id, "cv", "en", "cancel-render")
    entered, released, finished = threading.Event(), threading.Event(), threading.Event()
    render = module.render_documents

    def delayed(*args):
        entered.set()
        assert released.wait(5)
        try:
            return render(*args)
        finally:
            finished.set()

    monkeypatch.setattr(module, "render_documents", delayed)
    worker = RuntimeWorker(
        pg_store.database, WebSettings(tmp_path), FixtureGateway(), heartbeat_seconds=0.02
    )
    running = asyncio.create_task(worker.once("document"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        pg_store.cancel_task(queued["id"])
        await running
    finally:
        released.set()
    assert await asyncio.to_thread(finished.wait, 5)
    task = pg_store.task(queued["id"])
    assert task["status"] == "cancelled" and task["artifacts"] == []
    with pg_store.read() as connection:
        assert connection.execute(select(s.artifacts)).first() is None


async def test_metered_failure_before_dispatch_releases_but_unknown_network_spends(pg_store):
    from qa_job_bot.agent_gateway import AgentUnavailable
    from qa_job_bot.api_audit import audit_call
    from qa_job_bot.metered_gateway import MeteredGateway

    context = claimed(pg_store)
    limits = RuntimeLimits(pg_store.database)

    class Offline:
        async def chat(self, context, messages):
            raise AgentUnavailable("Configuration unavailable")

    gateway = MeteredGateway(Offline(), limits)
    with pytest.raises(AgentUnavailable):
        await gateway.chat(context, [])
    assert limits.summary(pg_store.user_id)["used_calls"] == 0

    class Unknown:
        async def chat(self, context, messages):
            audit_call("sent")
            audit_call("session", session_id="sess_unknown")
            raise AgentUnavailable("Network outcome unknown")

    with pytest.raises(AgentUnavailable):
        await MeteredGateway(Unknown(), limits).chat(context, [])
    summary = limits.summary(pg_store.user_id)
    assert summary["used_calls"] == summary["unknown_calls"] == 1
    with pg_store.read() as connection:
        event = connection.execute(
            select(s.usage_events.c.payload).where(
                s.usage_events.c.payload["state"].astext == "unknown"
            )
        ).scalar_one()
    assert event["session_id"] == "sess_unknown" and event["usage"] is None


async def test_metering_waits_for_common_cooldown_and_preserves_exact_known_usage(
    pg_store, monkeypatch
):
    from qa_job_bot.agent_result import AgentText
    from qa_job_bot.metered_gateway import MeteredGateway
    from qa_job_bot.runtime_limits import control, save_control

    context = claimed(pg_store)
    limits = RuntimeLimits(pg_store.database)
    identifier = limits.reserve(context, "chat")
    limits.progress(identifier, "rate_limit", {"retry_after": 30})
    limits.finish(identifier, False)

    class Responding:
        async def chat(self, context, messages):
            return AgentText(
                "Confirmed reply",
                {
                    "session_id": "sess_known",
                    "turn_id": "turn_known",
                    "usage": {"input_tokens": 12, "output_tokens": 5, "total_tokens": 17},
                },
            )

    pauses = []

    async def pause(seconds):
        pauses.append(seconds)
        with pg_store.database.transaction() as connection:
            state = control(connection)
            state["cooldown_until"] = 0
            save_control(connection, state)

    monkeypatch.setattr("qa_job_bot.metered_gateway.asyncio.sleep", pause)
    assert await MeteredGateway(Responding(), limits).chat(context, []) == "Confirmed reply"
    assert pauses == [0.25]
    summary = limits.summary(pg_store.user_id)
    assert summary["used_calls"] == 1 and summary["tokens"] == 17 and summary["unknown_calls"] == 0


async def test_usage_is_kept_when_provider_output_validation_fails(pg_store):
    from qa_job_bot.agent_gateway import AgentUnavailable
    from qa_job_bot.api_audit import audit_call
    from qa_job_bot.metered_gateway import MeteredGateway

    context = claimed(pg_store)
    limits = RuntimeLimits(pg_store.database)

    class InvalidOutput:
        async def generate(self, context):
            audit_call("sent")
            audit_call(
                "usage",
                session_id="sess_invalid",
                turn_id="turn_invalid",
                usage={"input_tokens": 10, "output_tokens": 1, "total_tokens": 11},
            )
            raise AgentUnavailable("Invalid output schema")

    with pytest.raises(AgentUnavailable):
        await MeteredGateway(InvalidOutput(), limits).generate(context)
    assert limits.summary(pg_store.user_id)["tokens"] == 11
    with pg_store.read() as connection:
        assert (
            connection.execute(select(s.usage_events.c.payload)).scalar_one()["state"] == "failed"
        )


def test_pending_account_cannot_receive_paid_task_and_no_slot_is_used(pg_store):
    task = pg_store.enqueue("cv", "pending", {}, "pending")
    assert QueueRepository(pg_store.database).claim("worker", lane="document") is None
    assert pg_store.task(task["id"])["status"] == "queued"


@pytest.mark.parametrize("module", ["runtime_worker", "scheduler"])
async def test_runtime_entrypoints_close_database_and_register_stop_signals(
    pg_store, monkeypatch, module
):
    import importlib
    from unittest.mock import Mock

    target = importlib.import_module("qa_job_bot." + module)
    close = Mock()
    monkeypatch.setattr(target.Database, "from_env", lambda: pg_store.database)
    monkeypatch.setattr(pg_store.database, "close", close)
    captures = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda sig, callback: captures.update({sig: callback})
    )
    run = AsyncMock()
    if module == "scheduler":
        monkeypatch.setattr(target.Scheduler, "run", run)
    else:
        monkeypatch.delenv("WORKER_LANE", raising=False)
        monkeypatch.setattr(target.RuntimeWorker, "run", run)
    await target.async_main()
    assert run.await_count == 1 and len(captures) == 2 and close.call_count == 1
    next(iter(captures.values()))()
    assert run.await_args.args[0].is_set()
    monkeypatch.setattr(pg_store.database, "revision", lambda: "wrong")
    with pytest.raises(RuntimeError, match="migrations"):
        await target.async_main()


async def test_worker_launches_all_accounts_without_web_owner_id(monkeypatch):
    from qa_job_bot import runtime_worker, worker

    run = AsyncMock()
    monkeypatch.setattr(runtime_worker, "async_main", run)
    monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic")
    monkeypatch.delenv("WEB_OWNER_ID", raising=False)
    await worker.async_main()
    run.assert_awaited_once()


async def test_idle_runtime_and_scheduler_exit_on_stop(pg_store, tmp_path, monkeypatch):
    stop = asyncio.Event()
    dispatcher = RuntimeWorker(pg_store.database, WebSettings(tmp_path), FixtureGateway())
    run = AsyncMock(side_effect=lambda lane: stop.set())
    monkeypatch.setattr(dispatcher, "once", run)
    await dispatcher.run(stop, {"search": 1})
    assert run.await_count == 1
    stop = asyncio.Event()
    scheduler = Scheduler(pg_store.database)

    def tick():
        stop.set()

    monkeypatch.setattr(scheduler, "tick", tick)
    await scheduler.run(stop)


async def test_runtime_paces_empty_polls_without_delaying_work_or_shutdown(
    pg_store, tmp_path, monkeypatch
):
    stop = asyncio.Event()
    dispatcher = RuntimeWorker(pg_store.database, WebSettings(tmp_path), FixtureGateway())
    run = AsyncMock(side_effect=[True, False, False])
    monkeypatch.setattr(dispatcher, "once", run)
    idle = []

    async def wait_for(awaitable, timeout):
        awaitable.close()
        idle.append((run.await_count, timeout))
        if len(idle) == 1:
            raise TimeoutError
        stop.set()

    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    await dispatcher.serve("search", stop)
    assert idle == [(2, 1), (3, 1)]


async def test_real_mcp_and_real_transport_write_usage_and_external_ids(pg_store, monkeypatch):
    from types import SimpleNamespace

    from test_openai_transport import API

    from qa_job_bot.agent_gateway import AgentGateway, AgentUnavailable
    from qa_job_bot.metered_gateway import MeteredGateway

    job_id, template = seed(pg_store)
    context = {
        **claimed(pg_store, "chat"),
        "kind": "chat",
        "language": "en",
        "template": template,
        "job": {"id": job_id, "title": "Backend Engineer"},
    }
    api = API()
    settings = SimpleNamespace(
        api_key="sk-synthetic",
        agent_id="agent_test",
        model="gpt-5.6-luna",
        reasoning_effort="medium",
        project_id=None,
    )
    monkeypatch.setattr(
        "qa_job_bot.agent_gateway.OpenAITransport",
        lambda **kwargs: OpenAITransport(**kwargs, http_transport=httpx.MockTransport(api)),
    )
    limits = RuntimeLimits(pg_store.database)
    gateway = MeteredGateway(AgentGateway(settings), limits)
    assert await gateway.chat(context, [{"role": "user", "text": "Keep confirmed facts"}]) == "ok"
    api.items[0]["content"][0]["text"] = "invalid JSON"
    with pytest.raises(AgentUnavailable):
        await gateway.chat(context, [{"role": "user", "text": "Keep confirmed facts"}])
    summary = limits.summary(pg_store.user_id)
    assert summary["used_calls"] == 2 and summary["tokens"] == 32
    with pg_store.read() as connection:
        events = connection.execute(select(s.usage_events.c.payload)).scalars().all()
    assert {event["state"] for event in events} == {"completed", "failed"}
    assert all(
        event["session_id"] == "sess_test" and event["turn_id"] == "turn_test" for event in events
    )
    assert len([request for request in api.calls if request.url.path == "/v1/agents/sessions"]) == 2
