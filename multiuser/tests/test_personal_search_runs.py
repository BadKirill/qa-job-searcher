import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from alembic import command
from pg_fixtures import personal_store
from sqlalchemy import func, select, update
from test_accounts import api_world as api_world

from qa_job_bot import pg_schema as s
from qa_job_bot.models import Job, utcnow
from qa_job_bot.search_models import SearchSpec, SourceSpec
from qa_job_bot.search_pipeline import PersonalSearchPipeline, SourceBatch, load_job, source_key
from qa_job_bot.source_http import SourceClient
from qa_job_bot.web_security import WebSettings
from qa_job_bot.web_store import StoreError
from qa_job_bot.worker import Worker

FETCHED = "2026-11-01T12:00:00+00:00"


def configure(store, **changes):
    store.save_search_profile(SearchSpec(role_keywords=["backend engineer"], **changes), 0)
    return store.add_source(SourceSpec(kind="remotive"))


def job(index=0, **changes):
    return replace(
        Job(
            "Remotive",
            str(index),
            "Senior Backend Engineer",
            f"Acme {index}",
            "Worldwide",
            f"https://example.test/jobs/{index}",
            description="Python software development.",
            remote=True,
            workplace_type="remote",
        ),
        **changes,
    )


def started(store, request_id=None, worker="worker"):
    queued = store.enqueue_search(request_id)
    task = store.claim(worker)
    assert task["id"] == queued["id"]
    return task


def batch(task, jobs=None, error=None, fetched_at=FETCHED, index=0):
    return SourceBatch(
        task["payload"]["search_inputs"]["sources"][index], jobs or [], fetched_at, error
    )


def complete(store, jobs=None, error=None, fetched_at=FETCHED, request_id=None):
    task = started(store, request_id)
    assert store.complete_search(task, "worker", [batch(task, jobs, error, fetched_at)])
    return task["id"]


def test_raw_catalog_and_personal_matching_are_separate_and_do_not_mutate_input(pg_store):
    configure(pg_store)
    task = started(pg_store)
    raw = job(score=99, match_reasons=["untouched"])
    rejected = job(1, title="QA Engineer")
    assert pg_store.complete_search(task, "worker", [batch(task, [raw, rejected])])
    assert (
        raw.score == 99 and raw.match_reasons == ["untouched"] and raw.resolved_fingerprint is None
    )
    with pg_store.read() as connection:
        public = connection.execute(select(s.jobs)).mappings().all()
        assert len(public) == 2
        assert all(
            not {"score", "match_reasons", "location_status", "user_id"} & row["payload"].keys()
            for row in public
        )
        assert len(connection.execute(select(s.source_fetch_jobs)).all()) == 2
    rows = pg_store.list_jobs("active", 1)
    assert rows["total"] == 1 and rows["items"][0]["score"] == 60
    with pytest.raises(StoreError) as error:
        pg_store.get_job(rejected.fingerprint)
    assert error.value.status == 404
    assert rows["search_run"]["profile_version"] == "1"
    assert rows["items"][0]["fetched_at"] == FETCHED
    assert rows["items"][0]["freshness"] == "fresh"


def test_two_users_have_independent_scores_cards_errors_revisions_and_states(pg_store):
    other = personal_store(pg_store.database, "other-searcher")
    configure(pg_store, skill_keywords=["python"])
    configure(other)
    first = complete(pg_store, [job()])
    a = pg_store.list_jobs("active", 1)
    revision = pg_store.get_setting("revision")
    second = complete(
        other,
        [job(description="Changed by another public fetch.")],
        fetched_at="2026-11-02T10:00:00+00:00",
    )
    b = other.list_jobs("active", 1)
    assert a["items"][0]["id"] == b["items"][0]["id"]
    assert a["items"][0]["score"] == 63 and b["items"][0]["score"] == 60
    assert pg_store.get_setting("revision") == revision
    again = pg_store.list_jobs("active", 1, a["snapshot_id"])
    assert again == a
    assert pg_store.get_job(a["items"][0]["id"])["description"] == job().description
    pg_store.change_state(a["items"][0]["id"], {"applied": True})
    assert other.get_job(b["items"][0]["id"])["applied_at"] is None
    assert pg_store.list_jobs("applied", 1)["total"] == 1
    assert other.list_jobs("active", 1)["total"] == 1
    for action in [
        lambda: other.search_run(first),
        lambda: pg_store.search_run(second),
        lambda: other.list_jobs("active", 1, run_id=first),
    ]:
        with pytest.raises(StoreError) as error:
            action()
        assert error.value.status == 404


def test_working_language_filters_fresh_and_retained_jobs_with_saved_profile(pg_store):
    configure(pg_store)
    russian = job(1, description="Our working language is Russian.")
    english = job(2, description="Рабочий язык в команде — английский.")
    unknown = job(3, description="Python, SQL")
    first = complete(pg_store, [russian, english, unknown])
    assert pg_store.list_jobs("active", 1)["total"] == 3

    roles = {"role_keywords": ["backend engineer"]}
    pg_store.save_search_profile(SearchSpec(**roles, working_languages=["ru"]), 1)
    complete(pg_store, error="http_error")
    items = pg_store.list_jobs("active", 1)["items"]
    assert [(row["description"], row["freshness"]) for row in items] == [
        (russian.description, "retained")
    ]

    pg_store.save_search_profile(SearchSpec(**roles, working_languages=["en", "ru"]), 2)
    complete(pg_store, [russian, english, unknown])
    items = pg_store.list_jobs("active", 1)["items"]
    assert {row["description"] for row in items} == {russian.description, english.description}
    assert all(row["score"] == 60 for row in items)
    assert pg_store.list_jobs("active", 1, run_id=first)["total"] == 3


def test_inputs_are_pinned_at_enqueue_deduplicated_and_new_settings_allow_new_run(pg_store):
    source = configure(pg_store)
    queued = pg_store.enqueue_search("same-request")
    assert pg_store.enqueue_search("same-request")["id"] == queued["id"]
    assert pg_store.enqueue_search("second-click")["id"] == queued["id"]
    pg_store.save_search_profile(SearchSpec(role_keywords=["designer"]), 1)
    pg_store.update_source(source["id"], 1, enabled=False)
    old = pg_store.task(queued["id"], internal=True)["payload"]["search_inputs"]
    assert old["profile"]["role_keywords"] == ["backend engineer"] and old["sources"][0]["enabled"]
    assert pg_store.enqueue_search("same-request")["id"] == queued["id"]
    with pytest.raises(StoreError):
        pg_store.enqueue_search("new-search")
    pg_store.update_source(source["id"], 2, enabled=True)
    new = pg_store.enqueue_search("new-search")
    assert new["id"] != queued["id"]
    assert pg_store.search_run(queued["id"])["inputs"] == old


def test_concurrent_clicks_share_one_task_and_history(pg_store):
    configure(pg_store)
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(
            pool.map(lambda index: pg_store.enqueue_search("click-" + str(index))["id"], range(4))
        )
    assert len(set(ids)) == 1 and len(pg_store.search_runs()["items"]) == 1


def test_older_run_finishing_after_new_settings_never_replaces_new_result(pg_store):
    configure(pg_store)
    old = started(pg_store, "old", "old-worker")
    pg_store.save_search_profile(SearchSpec(role_keywords=["product designer"]), 1)
    new = started(pg_store, "new", "new-worker")
    assert pg_store.complete_search(
        new, "new-worker", [batch(new, [job(title="Product Designer")])]
    )
    listing = pg_store.list_jobs("active", 1)
    assert pg_store.complete_search(old, "old-worker", [batch(old, [job(1)])])
    assert pg_store.get_setting("current_search_run") == new["id"]
    assert pg_store.list_jobs("active", 1)["items"] == listing["items"]
    history = pg_store.search_run(old["id"])
    assert history["state"] == "superseded" and history["published"] is False
    old_listing = pg_store.list_jobs("active", 1, run_id=old["id"])
    assert old_listing["items"][0]["title"] == "Senior Backend Engineer"
    assert old_listing["search_run"]["published"] is False


def test_older_run_with_same_inputs_loses_to_newer_requested_generation(pg_store):
    configure(pg_store)
    old = started(pg_store)
    pg_store.cancel_task(old["id"])
    new = started(pg_store)
    assert not pg_store.complete_search(old, "worker", [batch(old, [job()])])
    assert pg_store.complete_search(new, "worker", [batch(new, [job(1)])])
    assert pg_store.search_run(old["id"])["state"] == "cancelled"


@pytest.mark.parametrize("mode", ["cancel", "lease", "foreign-worker", "finished"])
def test_late_results_cannot_publish_or_ingest_public_data(pg_store, mode):
    configure(pg_store)
    task = started(pg_store)
    if mode == "cancel":
        pg_store.cancel_task(task["id"])
    elif mode == "lease":
        with pg_store.write() as connection:
            connection.execute(
                update(s.tasks)
                .where(s.tasks.c.id == task["id"])
                .values(lease_until=time.time() - 1)
            )
    elif mode == "finished":
        pg_store.finish_task(task["id"], "worker", "failed")
    assert not pg_store.complete_search(
        task, "other" if mode == "foreign-worker" else "worker", [batch(task, [job()])]
    )
    assert pg_store.get_setting("current_search_run") is None
    with pg_store.read() as connection:
        assert connection.execute(select(func.count()).select_from(s.jobs)).scalar() == 0


def test_completion_rolls_back_catalog_matches_run_and_settings_together(pg_store, monkeypatch):
    configure(pg_store)
    task = started(pg_store)
    original = pg_store.put_setting

    def fail(connection, key, value):
        if key == "last_search_at":
            raise RuntimeError("synthetic transaction failure")
        original(connection, key, value)

    monkeypatch.setattr(pg_store, "put_setting", fail)
    with pytest.raises(RuntimeError):
        pg_store.complete_search(task, "worker", [batch(task, [job()])])
    with pg_store.read() as connection:
        for table in (s.jobs, s.matches, s.source_fetches, s.search_run_jobs):
            assert connection.execute(select(func.count()).select_from(table)).scalar() == 0
    assert pg_store.get_setting("current_search_run") is None
    assert pg_store.search_run(task["id"])["state"] == "running"


def test_failed_source_keeps_only_own_previous_matches_and_actual_fetch_time(pg_store):
    configure(pg_store)
    other = personal_store(pg_store.database, "other-retention")
    configure(other)
    complete(pg_store, [job()])
    complete(other, [job(2)])
    own = pg_store.list_jobs("active", 1)["items"][0]
    failed = complete(pg_store, error="ReadTimeout", fetched_at="2026-11-03T00:00:00+00:00")
    rows = pg_store.list_jobs("active", 1)
    assert [row["id"] for row in rows["items"]] == [own["id"]]
    assert rows["items"][0]["freshness"] == "retained"
    assert rows["items"][0]["fetched_at"] == FETCHED
    assert rows["search_run"]["errors"] == ["Remotive: ReadTimeout"]
    assert pg_store.search_run(failed)["sources"][0]["fetched_at"] != FETCHED
    assert other.get_setting("search_errors") == []
    complete(pg_store, [])
    assert pg_store.list_jobs("active", 1)["items"][0]["freshness"] == "unconfirmed"
    assert pg_store.get_job(own["id"])["fetched_at"] == FETCHED


def test_retention_rechecks_profile_and_deleted_or_reconfigured_sources(pg_store):
    source = configure(pg_store)
    first = complete(pg_store, [job()])
    pg_store.save_search_profile(SearchSpec(role_keywords=["product designer"]), 1)
    complete(pg_store, error="ReadTimeout")
    assert pg_store.list_jobs("active", 1)["total"] == 0
    assert pg_store.list_jobs("active", 1, run_id=first)["total"] == 1
    assert pg_store.get_job(job().fingerprint)["title"] == job().title
    pg_store.delete_source(source["id"], 1)
    pg_store.add_source(SourceSpec(kind="remotive"))
    complete(pg_store, error="ReadTimeout")
    assert pg_store.list_jobs("active", 1)["total"] == 0


def test_partial_failure_retains_failed_source_and_ingests_other_sources(pg_store):
    configure(pg_store)
    pg_store.add_source(SourceSpec(kind="himalayas"))
    task = started(pg_store)
    assert pg_store.complete_search(
        task, "worker", [batch(task, [job()]), batch(task, [job(1, source="Himalayas")], index=1)]
    )
    task = started(pg_store)
    assert pg_store.complete_search(
        task,
        "worker",
        [batch(task, error="TimeoutError"), batch(task, [job(2, source="Himalayas")], index=1)],
    )
    rows = pg_store.list_jobs("active", 1)["items"]
    assert len(rows) == 3 and {row["freshness"] for row in rows} == {
        "fresh",
        "retained",
        "unconfirmed",
    }


@pytest.mark.parametrize("count", [0, 1, 10, 11, 21])
def test_frozen_personal_pages_keep_content_scores_and_order_across_refreshes(pg_store, count):
    configure(pg_store)
    first = complete(pg_store, [job(index) for index in range(count)])
    before = pg_store.list_jobs("active", 1)
    expected = [
        row["id"]
        for page in range(1, before["pages"] + 1)
        for row in pg_store.list_jobs("active", page, before["snapshot_id"])["items"]
    ]
    complete(pg_store, [job(index, description="New content.") for index in range(count)])
    actual = [
        row
        for page in range(1, before["pages"] + 1)
        for row in pg_store.list_jobs("active", page, before["snapshot_id"])["items"]
    ]
    assert [row["id"] for row in actual] == expected
    assert all(row["description"] == job().description for row in actual)
    assert pg_store.list_jobs("active", 1, before["snapshot_id"])["search_run"]["id"] == first
    assert all(
        len(pg_store.list_jobs("active", page, before["snapshot_id"])["items"]) == 10
        for page in range(1, before["pages"])
    )


def test_states_and_access_survive_new_filters_empty_runs_and_source_deletion(pg_store):
    source = configure(pg_store)
    run = complete(pg_store, [job(), job(1)])
    listing = pg_store.list_jobs("active", 1)
    one, two = [row["id"] for row in listing["items"]]
    pg_store.change_state(one, {"applied": True})
    pg_store.change_state(two, {"archived": True})
    pg_store.save_search_profile(SearchSpec(role_keywords=["designer"]), 1)
    complete(pg_store, [])
    pg_store.delete_source(source["id"], 1)
    assert pg_store.list_jobs("applied", 1)["items"][0]["id"] == one
    assert pg_store.list_jobs("archive", 1)["items"][0]["id"] == two
    pg_store.change_state(two, {"archived": False})
    assert pg_store.get_job(two)["archived_at"] is None
    assert pg_store.search_run(run)["count"] == 2


def test_api_history_is_personal_and_idempotent_and_rejects_forgery(api_world):
    admin, member, app, admin_id, member_id = api_world
    from qa_job_bot.pg_store import PostgresStore

    store = PostgresStore(app.state.database, member_id)
    configure(store)
    for data in [{"user_id": admin_id}, {"request_id": True}, {"request_id": ""}]:
        assert member.post("/api/search-runs", json=data).status_code == 422
    assert (
        member.post(
            "/api/search-runs",
            content=json.dumps({"request_id": "\ud800"}),
            headers={"content-type": "application/json"},
        ).status_code
        == 422
    )
    queued = member.post("/api/search-runs", json={"request_id": "clicked"}).json()
    assert (
        member.post("/api/search-runs", json={"request_id": "clicked"}).json()["id"] == queued["id"]
    )
    assert admin.get("/api/search-runs").json()["items"] == []
    assert admin.get("/api/search-runs/" + queued["id"]).status_code == 404
    assert member.get("/api/search-runs/" + queued["id"]).json()["state"] == "queued"
    assert member.get("/api/search-runs?page=0").status_code == 422
    task = store.claim("worker")
    assert store.complete_search(task, "worker", [batch(task, [job()])])
    result = member.get("/api/search-runs/" + task["id"]).json()
    assert result["count"] == 1 and result["published"]
    assert member.get("/api/jobs?run_id=" + task["id"]).json()["total"] == 1
    assert admin.get("/api/jobs?run_id=" + task["id"]).status_code == 404
    member.patch("/api/jobs/" + job().fingerprint + "/state", json={"archived": True})
    assert member.get("/api/jobs?tab=archive").json()["total"] == 1


def test_upgrade_five_freezes_existing_cards_and_snapshots(pg_store):
    raw = job(score=72)
    pg_store.upsert_jobs([raw])
    pg_store.publish_search([raw], [])
    snap = pg_store.list_jobs("active", 1)
    with pg_store.database.engine.begin() as connection:
        command.downgrade(pg_store.database.migration_config(connection), "0004_search_settings")
    pg_store.database.migrate()
    other = personal_store(pg_store.database, "after-upgrade")
    other.upsert_jobs([job(description="Changed after migration.")])
    restored = pg_store.list_jobs("active", 1, snap["snapshot_id"])
    assert restored["items"][0]["description"] == raw.description
    assert restored["items"][0]["score"] == 72


def test_invalid_completion_batches_and_wrong_task_inputs_are_rejected(pg_store):
    configure(pg_store)
    task = started(pg_store)
    for batches in [[], [batch(task), batch(task)], [SourceBatch({"id": "foreign"}, [], FETCHED)]]:
        with pytest.raises(StoreError):
            pg_store.complete_search(task, "worker", batches)
    wrong = copy.deepcopy(task)
    wrong["payload"]["search_inputs"]["profile"]["role_keywords"] = ["changed"]
    with pytest.raises(StoreError):
        pg_store.complete_search(wrong, "worker", [batch(wrong)])


async def test_worker_fetches_pinned_inputs_and_completes_atomically(pg_store, tmp_path):
    configure(pg_store)
    queued = pg_store.enqueue_search()
    inputs = pg_store.task(queued["id"], internal=True)["payload"]["search_inputs"]
    pipeline = type(
        "Pipeline",
        (),
        {"fetch": AsyncMock(return_value=[SourceBatch(inputs["sources"][0], [job()], FETCHED)])},
    )()
    worker = Worker(pg_store, WebSettings(tmp_path), gateway=object(), search_pipeline=pipeline)
    assert await worker.once()
    pipeline.fetch.assert_awaited_once_with(inputs)
    assert pg_store.task(queued["id"])["status"] == "ready"
    assert pg_store.get_setting("current_search_run") == queued["id"]


async def test_public_fetch_factory_drops_private_labels_and_tags_and_safely_reports_errors():
    source = {
        "id": "mine",
        "kind": "lever",
        "name": "Private nickname",
        "version": 1,
        "enabled": True,
        "config": {
            "token": "acme",
            "region": "eu",
            "name": "Private nickname",
            "tags": ["private-tag"],
        },
    }
    seen = []

    def response(request):
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "id": "1",
                    "text": "Backend Engineer",
                    "categories": {"location": "Worldwide"},
                    "descriptionPlain": "Python",
                    "workplaceType": "remote",
                }
            ],
        )

    def client():
        return SourceClient(transport=httpx.MockTransport(response))

    inputs = {
        "profile": SearchSpec(role_keywords=["backend engineer"]).model_dump(),
        "sources": [source],
    }
    batches = await PersonalSearchPipeline(client).fetch(inputs)
    assert batches[0].jobs[0].company == "acme" and batches[0].jobs[0].tags == ()
    assert source["config"]["name"] == "Private nickname"
    assert batches[0].fetched_at and not batches[0].error
    assert source_key(source) == source_key(
        {**source, "config": {**source["config"], "name": "Other"}}
    )
    assert source_key(source, ["backend"]) != source_key(source, ["designer"])

    def fail(request):
        raise httpx.ReadTimeout("private provider details")

    result = await PersonalSearchPipeline(
        lambda: SourceClient(transport=httpx.MockTransport(fail))
    ).fetch(inputs)
    assert result[0].error == "ReadTimeout" and "private provider" not in str(result[0].error)
    assert load_job(
        {
            "source": "s",
            "source_id": "1",
            "title": "t",
            "company": "c",
            "location": "l",
            "url": "u",
            "published_at": FETCHED,
        }
    ).published_at == datetime.fromisoformat(FETCHED)


def test_first_superseded_run_stays_in_history_and_does_not_become_current(pg_store):
    configure(pg_store)
    task = started(pg_store)
    pg_store.save_search_profile(SearchSpec(role_keywords=["designer"]), 1)
    assert pg_store.complete_search(task, "worker", [batch(task, [job()])])
    assert pg_store.list_jobs("active", 1)["items"] == []
    assert pg_store.get_setting("current_search_run") is None
    assert pg_store.list_jobs("active", 1, run_id=task["id"])["total"] == 1


def test_canonical_board_ingestion_preserves_legacy_identity_and_hides_private_labels(pg_store):
    old = job(source="Lever · Original private label", company="Old Company")
    pg_store.upsert_jobs([old])
    pg_store.save_search_profile(SearchSpec(role_keywords=["backend engineer"]), 0)
    pg_store.add_source(
        SourceSpec(
            kind="lever",
            config={"token": "company", "name": "Private label", "tags": ["private-tag"]},
        )
    )
    task = started(pg_store)
    canonical = replace(old, company="company", source="Lever · company", resolved_fingerprint=None)
    assert pg_store.complete_search(
        task, "worker", [batch(task, [canonical], fetched_at=utcnow().isoformat())]
    )
    assert pg_store.list_jobs("active", 1)["items"][0]["id"] == old.fingerprint
    with pg_store.read() as connection:
        public = connection.execute(select(s.jobs)).mappings().one()
    assert "private" not in str(public["payload"]).casefold()


def test_duplicate_public_jobs_and_local_tags_produce_one_personal_result(pg_store):
    pg_store.save_search_profile(
        SearchSpec(role_keywords=["backend engineer"], skill_keywords=["local-priority"]), 0
    )
    pg_store.add_source(
        SourceSpec(kind="lever", config={"token": "company", "tags": ["local-priority"]})
    )
    task = started(pg_store)
    assert pg_store.complete_search(task, "worker", [batch(task, [job(), job()])])
    result = pg_store.list_jobs("active", 1)
    assert result["total"] == 1 and result["items"][0]["score"] == 63
    with pg_store.read() as connection:
        assert len(connection.execute(select(s.source_fetch_jobs)).all()) == 1
        assert connection.execute(select(s.jobs.c.payload)).scalar()["tags"] == []


def test_history_pagination_and_snapshot_run_mismatch(pg_store):
    configure(pg_store)
    first = complete(pg_store, [job()])
    snapshot = pg_store.list_jobs("active", 1)
    with pg_store.write() as connection:
        for index in range(21):
            connection.execute(
                s.search_runs.insert().values(
                    id=f"old-{index}",
                    user_id=pg_store.user_id,
                    payload={"errors": []},
                    completed_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
    assert len(pg_store.search_runs()["items"]) == 20 and pg_store.search_runs()["has_more"]
    assert len(pg_store.search_runs(2)["items"]) == 2 and not pg_store.search_runs(2)["has_more"]
    with pytest.raises(StoreError) as error:
        pg_store.list_jobs("active", 1, snapshot["snapshot_id"], run_id="old-1")
    assert error.value.status == 409
    assert pg_store.search_run(first)["count"] == 1


def test_lease_expiring_during_completion_rolls_back_all_results(pg_store, monkeypatch):
    configure(pg_store)
    task = started(pg_store)
    original = pg_store.put_setting

    def expire(connection, key, value):
        original(connection, key, value)
        if key == "last_search_at":
            connection.execute(
                update(s.tasks).where(s.tasks.c.id == task["id"]).values(lease_until=0)
            )

    monkeypatch.setattr(pg_store, "put_setting", expire)
    with pytest.raises(StoreError):
        pg_store.complete_search(task, "worker", [batch(task, [job()])])
    assert pg_store.get_setting("current_search_run") is None
    with pg_store.read() as connection:
        assert connection.execute(select(func.count()).select_from(s.jobs)).scalar() == 0


async def test_default_pipeline_bounds_http_and_telegram_does_not_depend_on_profile(
    monkeypatch,
):
    requests = []
    html = (
        '<div class="tgme_widget_message_wrap"><div data-post="public_jobs/1">'
        '<div class="tgme_widget_message_text">Backend Engineer<br>Remote Worldwide</div>'
        '</div></div><div class="tgme_channel_info"></div></body>'
    )

    def response(request):
        requests.append(request)
        return httpx.Response(200, text=html)

    monkeypatch.setattr(
        "qa_job_bot.search_pipeline.SourceClient",
        lambda **kwargs: SourceClient(transport=httpx.MockTransport(response), **kwargs),
    )
    inputs = {
        "profile": SearchSpec(role_keywords=["qa"]).model_dump(),
        "sources": [{"kind": "telegram", "enabled": True, "config": {"handle": "public_jobs"}}],
    }
    result = await PersonalSearchPipeline().fetch(inputs)
    assert result[0].jobs[0].title == "Backend Engineer"
    assert not result[0].error and len(requests) == 2


def test_catalog_uses_real_fetch_time_and_older_fetch_cannot_overwrite_newer_content(pg_store):
    other = personal_store(pg_store.database, "newer-fetch")
    configure(pg_store)
    configure(other)
    complete(pg_store, [job()], fetched_at=FETCHED)
    complete(
        other, [job(description="Newest public contents")], fetched_at="2026-11-04T00:00:00+00:00"
    )
    complete(
        pg_store,
        [job(description="Older returned contents")],
        fetched_at="2026-11-02T00:00:00+00:00",
    )
    with pg_store.read() as connection:
        public = connection.execute(select(s.jobs)).mappings().one()
    assert public["payload"]["description"] == "Newest public contents"
    assert public["last_seen_at"].isoformat() == "2026-11-04T00:00:00+00:00"
    assert pg_store.get_job(job().fingerprint)["description"] == "Older returned contents"
    assert other.get_job(job().fingerprint)["description"] == "Newest public contents"


async def test_unconfigured_selected_worker_can_run_and_old_unpinned_tasks_fail_safely(
    pg_store, tmp_path, monkeypatch
):
    import asyncio

    worker = Worker(pg_store, WebSettings(tmp_path), gateway=object())
    old = pg_store.enqueue("search", "search", {}, "old-unpinned")
    assert await worker.once()
    assert pg_store.task(old["id"])["status"] == "failed"
    assert "не сохранены настройки" in pg_store.task(old["id"])["error"]
    stop = asyncio.Event()

    async def once():
        stop.set()
        return True

    monkeypatch.setattr(worker, "once", once)
    await worker.run(stop, 1)
    assert pg_store.get_setting("scheduled_search_at")
    assert len(pg_store.tasks()) == 1


def test_ranking_does_not_store_personal_fields_on_shared_raw_jobs():
    from qa_job_bot.matching import rank_jobs

    raw = job()
    result = rank_jobs([raw], SearchSpec(role_keywords=["backend engineer"]).runtime())
    assert result[0] is not raw and result[0].score == 60
    assert raw.score == 0 and raw.match_reasons == [] and raw.location_status == "unknown"


def test_import_after_upgrade_freezes_owner_cards_before_other_users_fetch(pg_store, tmp_path):
    from legacy_fixtures import seed_legacy

    from qa_job_bot.legacy_import import restore_snapshot

    legacy = seed_legacy(tmp_path / "legacy")
    restore_snapshot(
        legacy["snapshot"]["root"],
        pg_store.database,
        pg_store.user_id,
        tmp_path / "restored",
        telegram_chats=[101],
    )
    before = pg_store.list_jobs("active", 1)
    original = before["items"][0]
    other = personal_store(pg_store.database, "after-new-import")
    other.upsert_jobs([load_job({**original, "description": "Changed by different user"})])
    assert pg_store.get_job(original["id"])["description"] == original["description"]
    assert (
        pg_store.list_jobs("active", 1, before["snapshot_id"])["items"][0]["description"]
        == original["description"]
    )


@pytest.mark.parametrize("bad_title", ["Bad\0title", "\ud800"])
async def test_malformed_provider_text_is_a_source_failure_before_the_publish_transaction(
    bad_title,
):
    def response(request):
        if request.url.host == "remotive.com":
            return httpx.Response(
                200, content=json.dumps({"jobs": [{"id": 1, "title": bad_title}]})
            )
        return httpx.Response(200, json={"jobs": [{"guid": "good", "title": "Backend Engineer"}]})

    sources = [{"kind": kind, "enabled": True, "config": {}} for kind in ["remotive", "himalayas"]]
    pipeline = PersonalSearchPipeline(lambda: SourceClient(transport=httpx.MockTransport(response)))
    batches = await pipeline.fetch(
        {"profile": SearchSpec(role_keywords=["backend engineer"]).model_dump(), "sources": sources}
    )
    assert batches[0].error in {"ValueError", "UnicodeEncodeError"} and not batches[0].jobs
    assert not batches[1].error and batches[1].jobs[0].title == "Backend Engineer"


def test_partial_source_publication_keeps_new_jobs_old_jobs_and_channel_identity(pg_store):
    pg_store.save_search_profile(SearchSpec(role_keywords=["backend engineer"]), 0)
    source = pg_store.add_source(SourceSpec(kind="telegram", config={"handle": "public_jobs"}))
    first = complete(pg_store, [job()])
    task = started(pg_store)
    assert pg_store.complete_search(task, "worker", [batch(task, [job(1)], error="HTTP_429")])
    result = pg_store.list_jobs("active", 1)
    assert result["total"] == 2
    assert {row["freshness"] for row in result["items"]} == {"fresh", "retained"}
    run = pg_store.search_run(task["id"])
    assert run["sources"][0]["name"] == "Telegram · @public_jobs"
    assert run["sources"][0]["count"] == 1
    assert run["errors"] == ["Telegram · @public_jobs: HTTP_429"]
    assert pg_store.preferences()["sources"]["items"][0]["name"] == "Telegram · @public_jobs"
    with pg_store.write() as connection:
        payload = pg_store.search_run_data(connection, first)
        payload["sources"][0]["name"] = "Публичный Telegram"
        payload["sources"][0]["error"] = "SourceNetworkError"
        connection.execute(
            update(s.search_runs).where(s.search_runs.c.id == first).values(payload=payload)
        )
    assert pg_store.search_run(first)["errors"] == ["Telegram · @public_jobs: SourceNetworkError"]
    assert source["config"]["handle"] == "public_jobs"


def test_enqueue_rejects_excess_sources_before_creating_task(pg_store, monkeypatch):
    configure(pg_store)
    pg_store.add_source(SourceSpec(kind="himalayas"))
    monkeypatch.setattr("qa_job_bot.pg_search_runs.MAX_RUN_SOURCES", 1)
    with pytest.raises(StoreError) as error:
        pg_store.enqueue_search()
    assert error.value.status == 409
    assert pg_store.tasks() == []
