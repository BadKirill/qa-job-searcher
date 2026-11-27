import copy
from dataclasses import replace
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from qa_job_bot import pg_schema as s
from qa_job_bot.models import utcnow
from qa_job_bot.pg_access import OwnedRepository, upsert
from qa_job_bot.pg_jobs import PublicJobRepository, public_payload
from qa_job_bot.search_models import SearchSpec
from qa_job_bot.search_pipeline import load_job, personal_match, source_key, source_queries
from qa_job_bot.source_http import MAX_RUN_SOURCES
from qa_job_bot.web_store import StoreError
from qa_job_bot.work_time import display_work_time


class PersonalSearchRuns(OwnedRepository):
    def enqueue_search(self, request_id=None):
        with self.write() as connection:
            return self.enqueue_search_in(connection, request_id)

    def enqueue_search_in(self, connection, request_id=None):
        request_id = request_id or str(uuid4())
        request = {"operation": "personal-search"}
        prior = self.requested_id(connection, request_id, request)
        if prior:
            return self.task_data(connection, prior)
        state = self.preferences_in(connection)
        if not state["search_profile"]["configured"]:
            raise StoreError("Сначала настройте профиль поиска в разделе «Настройки поиска».", 409)
        sources = [row for row in state["sources"]["items"] if row["enabled"]]
        if not sources or any(not source["valid"] for source in sources):
            raise StoreError("Настройте и включите источники в разделе «Настройки поиска».", 409)
        if len(sources) > MAX_RUN_SOURCES:
            raise StoreError("В одном поиске можно включить не более 128 источников.", 409)
        inputs = {
            "profile_version": state["search_profile"]["version"],
            "profile_id": state["search_profile"]["id"],
            "profile_generation": state["search_profile"]["generation"],
            "sources_version": state["sources"]["version"],
            "profile": state["search_profile"]["profile"],
            "sources": sources,
        }
        scope = f"search:{inputs['profile_generation']}:{inputs['sources_version']}"
        task_id = self.enqueue_in(
            connection, "search", scope, {"search_inputs": inputs}, request_id, request
        )
        exists = connection.execute(
            select(s.search_runs.c.id).where(
                self.owned(s.search_runs), s.search_runs.c.id == task_id
            )
        ).first()
        if not exists:
            connection.execute(
                s.search_runs.insert().values(
                    id=task_id,
                    user_id=self.user_id,
                    profile_version=str(inputs["profile_version"]),
                    payload={"inputs": inputs, "state": "pending", "published": False},
                )
            )
        self.put_setting(connection, "latest_search_requested", task_id)
        return self.task_data(connection, task_id)

    def search_run_data(self, connection, run_id):
        row = self.row(connection, s.search_runs, run_id, "Поиск не найден.")
        task = (
            connection.execute(
                select(s.tasks.c.status, s.tasks.c.error).where(
                    self.owned(s.tasks), s.tasks.c.id == run_id
                )
            )
            .mappings()
            .first()
        )
        data = copy.deepcopy(row["payload"])
        source_inputs = {
            source["id"]: source for source in data.get("inputs", {}).get("sources", [])
        }
        for source in data.get("sources", []):
            original = source_inputs.get(source["id"], {})
            handle = original.get("config", {}).get("handle")
            if original.get("kind") == "telegram" and handle:
                source["name"] = f"Telegram · @{handle}"
        if data.get("sources"):
            data["errors"] = [
                f"{source['name']}: {source['error']}"
                for source in data["sources"]
                if source.get("error")
            ]
        data.update(
            id=run_id,
            created_at=row["created_at"].isoformat(),
            completed_at=row["completed_at"].isoformat() if row["completed_at"] else None,
            profile_version=row["profile_version"],
            task_status=task["status"] if task else "ready",
            task_error=task["error"] if task else None,
        )
        if "count" not in data:
            data["count"] = connection.execute(
                select(func.count())
                .select_from(s.search_run_jobs)
                .where(self.owned(s.search_run_jobs), s.search_run_jobs.c.run_id == run_id)
            ).scalar_one()
        if row["completed_at"] is None and task:
            data["state"] = task["status"]
        return data

    def search_run(self, run_id):
        with self.read() as connection:
            data = self.search_run_data(connection, run_id)
            data["count"] = len(
                connection.execute(
                    select(s.search_run_jobs.c.job_id).where(
                        self.owned(s.search_run_jobs), s.search_run_jobs.c.run_id == run_id
                    )
                ).all()
            )
            return data

    def search_runs(self, page=1):
        with self.read() as connection:
            ids = (
                connection.execute(
                    select(s.search_runs.c.id)
                    .where(self.owned(s.search_runs))
                    .order_by(s.search_runs.c.created_at.desc(), s.search_runs.c.id.desc())
                    .offset((page - 1) * 20)
                    .limit(21)
                )
                .scalars()
                .all()
            )
            return {
                "items": [self.search_run_data(connection, key) for key in ids[:20]],
                "page": page,
                "has_more": len(ids) > 20,
            }

    def complete_search(self, task, worker_id, batches):
        inputs = task["payload"]["search_inputs"]
        profile = SearchSpec.model_validate(inputs["profile"]).runtime()
        with self.write() as connection:
            live = (
                connection.execute(
                    select(s.tasks)
                    .where(
                        *self.live_lease(task["id"], worker_id),
                        s.tasks.c.kind == "search",
                        s.tasks.c.status == "running",
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if live is None:
                return False
            if live["payload"].get("search_inputs") != inputs:
                raise StoreError("Вход поиска изменился.", 409)
            run = self.row(connection, s.search_runs, task["id"], "Поиск не найден.")
            if run["payload"].get("inputs") != inputs:
                raise StoreError("Вход поиска не соответствует запуску.", 409)
            if run["completed_at"] is not None:
                return False
            state = self.preferences_in(connection)
            published = (
                state["search_profile"]["generation"]
                == inputs.get("profile_generation", inputs["profile_version"])
                and state["sources"]["version"] == inputs["sources_version"]
                and self.setting(connection, "latest_search_requested") == task["id"]
            )
            if sorted(batch.source["id"] for batch in batches) != sorted(
                source["id"] for source in inputs["sources"]
            ) or any(batch.source not in inputs["sources"] for batch in batches):
                raise StoreError("Источники не соответствуют входу поиска.", 409)
            matches, sources, seen = {}, [], set()
            now = utcnow()
            for batch in batches:
                source = batch.source
                fetch_id = str(uuid4())
                info = {
                    "id": source["id"],
                    "version": source["version"],
                    "name": source["name"],
                    "kind": source["kind"],
                    "key": source_key(source),
                    "fetch_id": fetch_id,
                    "fetched_at": batch.fetched_at,
                    "error": batch.error,
                    "limited": source["kind"] not in {"greenhouse", "ashby"},
                    "cached": False,
                    "count": len(batch.jobs),
                }
                sources.append(info)
                connection.execute(
                    s.source_fetches.insert().values(
                        id=fetch_id,
                        cache_key=source_key(source, source_queries(profile)),
                        payload={
                            "kind": source["kind"],
                            "fetched_at": batch.fetched_at,
                            "error": batch.error,
                            "limited": info["limited"],
                            "cached": False,
                        },
                    )
                )
                jobs = [replace(job, match_reasons=list(job.match_reasons)) for job in batch.jobs]
                PublicJobRepository(self.database).save(
                    connection, jobs, canonical_source=source, fetched_at=batch.fetched_at
                )
                for job in jobs:
                    connection.execute(
                        insert(s.source_fetch_jobs)
                        .values(fetch_id=fetch_id, job_id=job.fingerprint)
                        .on_conflict_do_nothing()
                    )
                    seen.add(job.fingerprint)
                    result = personal_match(job, source, profile, day=now.date())
                    if result.accepted:
                        card = {
                            "job": public_payload(job),
                            "source": info,
                            "freshness": "fresh",
                            "fetched_at": batch.fetched_at,
                            "location_status": result.location_status,
                            "location_code": result.location_code,
                            "work_time": display_work_time(
                                job.description, profile.availability, now.date()
                            ),
                            "score": result.score,
                            "reasons": result.reasons,
                        }
                        if (
                            job.fingerprint not in matches
                            or result.score > matches[job.fingerprint]["score"]
                        ):
                            matches[job.fingerprint] = card
            previous = self.setting(connection, "current_jobs", [])
            source_map = {source["id"]: source for source in inputs["sources"]}
            infos = {source["id"]: source for source in sources}
            for job_id in previous:
                if job_id in seen:
                    continue
                row = (
                    connection.execute(
                        select(s.matches).where(self.owned(s.matches), s.matches.c.job_id == job_id)
                    )
                    .mappings()
                    .first()
                )
                old = row["payload"] if row else {}
                provenance = old.get("source", {})
                source = source_map.get(provenance.get("id"))
                if not source or provenance.get("key") != source_key(source):
                    continue
                result = personal_match(load_job(old["job"]), source, profile, day=now.date())
                if result.accepted:
                    matches[job_id] = {
                        **old,
                        "score": result.score,
                        "reasons": result.reasons,
                        "location_status": result.location_status,
                        "location_code": result.location_code,
                        "work_time": display_work_time(
                            old["job"]["description"], profile.availability, now.date()
                        ),
                        "freshness": "retained" if infos[source["id"]]["error"] else "unconfirmed",
                    }
            ids = sorted(
                matches,
                key=lambda key: (
                    -matches[key]["score"],
                    -(
                        load_job(matches[key]["job"]).published_at.timestamp()
                        if load_job(matches[key]["job"]).published_at
                        else 0
                    ),
                    key,
                ),
            )
            for position, job_id in enumerate(ids):
                card = matches[job_id]
                existing = connection.execute(
                    select(s.matches.c.job_id).where(
                        self.owned(s.matches), s.matches.c.job_id == job_id
                    )
                ).first()
                if published or not existing:
                    upsert(
                        connection,
                        s.matches,
                        {
                            "user_id": self.user_id,
                            "job_id": job_id,
                            "score": card["score"],
                            "reasons": card["reasons"],
                            "location_status": card["location_status"],
                            "payload": card,
                        },
                        ["user_id", "job_id"],
                    )
                connection.execute(
                    s.search_run_jobs.insert().values(
                        user_id=self.user_id,
                        run_id=task["id"],
                        job_id=job_id,
                        position=position,
                        score=card["score"],
                        reasons=card["reasons"],
                        payload=card,
                    )
                )
            errors = [
                f"{source['name']}: {source['error']}" for source in sources if source["error"]
            ]
            result = {
                "count": len(ids),
                "errors": errors,
                "published": published,
                "run_id": task["id"],
            }
            payload = {
                **run["payload"],
                **result,
                "state": "published" if published else "superseded",
                "sources": sources,
            }
            connection.execute(
                update(s.search_runs)
                .where(self.owned(s.search_runs), s.search_runs.c.id == task["id"])
                .values(payload=payload, completed_at=now)
            )
            if published:
                for key, value in [
                    ("current_jobs", ids),
                    ("current_search_run", task["id"]),
                    ("last_search_at", now.isoformat()),
                    ("search_errors", errors),
                ]:
                    self.put_setting(connection, key, value)
                self.enqueue_posting_checks_after_search(connection, matches, ids, now.timestamp())
            changed = connection.execute(
                update(s.tasks)
                .where(*self.live_lease(task["id"], worker_id))
                .values(status="ready", result=result, error=None)
            ).rowcount
            if not changed:
                raise StoreError("Истёк срок исполнения поиска.", 409)
            self.touch(connection)
        return True
