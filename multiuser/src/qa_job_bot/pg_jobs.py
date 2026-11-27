import json
import math
import time
from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert

from qa_job_bot import pg_schema as s
from qa_job_bot.company_origin import is_russian_company
from qa_job_bot.job_details import extract_details
from qa_job_bot.job_language import language_display
from qa_job_bot.models import canonical_text, utcnow
from qa_job_bot.pg_access import OwnedRepository, upsert
from qa_job_bot.posting_details import (
    last_confirmed_posting,
    posting_observation_key,
    verified_description,
)
from qa_job_bot.posting_summary import posting_summary
from qa_job_bot.repository import JobRepository
from qa_job_bot.web_store import StoreError, digest
from qa_job_bot.work_time import display_work_time


def public_payload(job):
    data = asdict(job)
    for key in ("score", "match_reasons", "location_status", "resolved_fingerprint"):
        data.pop(key)
    return json.loads(json.dumps(data, default=str))


class PublicJobRepository:
    def __init__(self, database):
        self.database = database

    def save(self, connection, jobs, canonical_source=None, fetched_at=None):
        if not jobs:
            return set()
        connection.execute(text("SELECT pg_advisory_xact_lock(1853392742)"))
        inserted = set()
        for job in jobs:
            source_key = JobRepository._source_key(job.source, job.source_id, job.url)
            if canonical_source:
                from qa_job_bot.search_pipeline import source_key as public_source_key

                source_key = public_source_key(canonical_source) + ":" + (job.source_id or job.url)
            job_id = connection.execute(
                select(s.identities.c.job_id).where(s.identities.c.source_key == source_key)
            ).scalar_one_or_none()
            if job_id is None and canonical_source and job.url:
                job_id = connection.execute(
                    select(s.jobs.c.id)
                    .where(
                        s.jobs.c.url == job.url,
                        s.jobs.c.source_id == job.source_id,
                        s.jobs.c.source.ilike(canonical_source["kind"].replace("_", ".") + "%"),
                    )
                    .order_by(s.jobs.c.created_at, s.jobs.c.id)
                    .limit(1)
                ).scalar_one_or_none()
            if job_id is None:
                candidates = connection.execute(
                    select(s.jobs).where(s.jobs.c.url == job.url)
                ).mappings()
                job_id = next(
                    (
                        row["id"]
                        for row in candidates
                        if job.url
                        and sha256(
                            "|".join(
                                canonical_text(row[key]) for key in ("company", "title", "location")
                            ).encode()
                        ).hexdigest()
                        == job.legacy_fingerprint
                    ),
                    None,
                )
                if job_id is None:
                    job_id, suffix = job.legacy_fingerprint, 0
                    while connection.execute(
                        select(s.jobs.c.id).where(s.jobs.c.id == job_id)
                    ).first():
                        job_id = sha256(
                            f"{job.legacy_fingerprint}|{source_key}|{suffix}".encode()
                        ).hexdigest()
                        suffix += 1
            exists = connection.execute(
                select(s.jobs.c.last_seen_at).where(s.jobs.c.id == job_id)
            ).first()
            if not exists:
                inserted.add(job_id)
            data = public_payload(job)
            values = {
                key: data[key]
                for key in ("source", "source_id", "title", "company", "location", "url")
            }
            values.update(
                id=job_id,
                payload=data,
                content_hash=digest(data),
                published_at=job.published_at.isoformat() if job.published_at else None,
                last_seen_at=datetime.fromisoformat(fetched_at) if fetched_at else utcnow(),
            )
            statement = insert(s.jobs).values(**values)
            changes = {key: statement.excluded[key] for key in values if key != "id"}
            if job.published_at is None and exists:
                changes.pop("published_at")
            if not exists or not fetched_at or values["last_seen_at"] >= exists[0]:
                connection.execute(
                    statement.on_conflict_do_update(index_elements=["id"], set_=changes)
                )
            connection.execute(
                insert(s.identities)
                .values(source_key=source_key, job_id=job_id)
                .on_conflict_do_nothing(index_elements=["source_key"])
            )
            job.resolved_fingerprint = job_id
        return inserted

    def upsert_jobs(self, jobs):
        with self.database.transaction() as connection:
            return self.save(connection, jobs)

    def resolve_identities(self, jobs):
        self.upsert_jobs(jobs)


class PersonalJobs(OwnedRepository):
    def resolve_identities(self, jobs):
        PublicJobRepository(self.database).resolve_identities(jobs)

    def upsert_jobs(self, jobs):
        with self.write() as connection:
            inserted = PublicJobRepository(self.database).save(connection, jobs)
            for job in jobs:
                upsert(
                    connection,
                    s.matches,
                    {
                        "user_id": self.user_id,
                        "job_id": job.fingerprint,
                        "score": job.score,
                        "reasons": job.match_reasons,
                        "location_status": job.location_status,
                        "payload": {
                            "job": public_payload(job),
                            "fetched_at": utcnow().isoformat(),
                            "location_status": job.location_status,
                            "freshness": "legacy",
                        },
                    },
                    ["user_id", "job_id"],
                )
        return inserted

    def job_row(self, connection, job_id):
        row = (
            connection.execute(
                select(
                    s.jobs,
                    s.matches.c.score,
                    s.matches.c.reasons,
                    s.matches.c.location_status,
                    s.matches.c.payload.label("personal_payload"),
                    s.states.c.applied_at,
                    s.states.c.archived_at,
                )
                .join(s.matches, s.matches.c.job_id == s.jobs.c.id)
                .outerjoin(
                    s.states,
                    (s.states.c.job_id == s.jobs.c.id) & (s.states.c.user_id == self.user_id),
                )
                .where(s.jobs.c.id == job_id, self.owned(s.matches))
            )
            .mappings()
            .first()
        )
        if row is None:
            raise StoreError("Вакансия недоступна. Обновите поиск.", 404)
        data = dict(row)
        data["payload"] = data["personal_payload"].get("job", data["payload"])
        return data

    def card_data(self, connection, row, card=None, search_run=None):
        card = card or row.get("personal_payload", {})
        saved_job = card.get("job", row["payload"])
        observation = posting_observation_key(row["id"], digest(saved_job))
        check = self.setting(connection, "posting_checks", {}).get(observation, {})
        confirmed = last_confirmed_posting(check)
        job = (
            saved_job
            if card.get("manual") or card.get("historical")
            else verified_description(saved_job, check)
        )
        description = job.get("description", "")
        details = extract_details(description, job["title"])
        work_time = card.get("work_time")
        if job is not saved_job or "work_time" not in card:
            run = search_run or {}
            availability = run.get("inputs", {}).get("profile", {}).get("availability")
            checked_on = datetime.fromisoformat(run["created_at"]) if run else row["last_seen_at"]
            work_time = display_work_time(description, availability, checked_on.date())
        return {
            **job,
            "id": row["id"],
            "score": card.get("score", row["score"]),
            "match_reasons": card.get("reasons", row["reasons"]),
            "location_status": card.get("location_status", row["location_status"]),
            "location_code": card.get("location_code"),
            "work_time": work_time,
            "manual_version": card.get("manual_version"),
            "posting": confirmed if confirmed.get("status") == "closed" else check.get("status"),
            "description_quality": (job.get("description_quality") or "fragment")
            if description
            else "missing",
            "freshness": card.get("freshness", "legacy"),
            "fetched_at": card.get("fetched_at", row["last_seen_at"].isoformat()),
            "last_seen_at": card.get("fetched_at", row["last_seen_at"].isoformat()),
            "source_status": card.get("source"),
            "applied_at": row["applied_at"],
            "archived_at": row["archived_at"],
            "russian_company": is_russian_company(job.get("description", ""), job["company"]),
            "details": details,
            "language_evidence": language_display(description, job["company"]),
            "summary": posting_summary(description, details, job["url"]),
            "documents": self.task_list(connection, row["id"]),
        }

    def get_job(self, job_id):
        with self.read() as connection:
            row = self.job_row(connection, job_id)
            return self.card_data(connection, row)

    def publish_search(self, jobs, errors):
        ids = list(dict.fromkeys(job.fingerprint for job in jobs))
        failed = {error.rsplit(":", 1)[0] for error in errors}
        with self.write() as connection:
            for job_id in self.setting(connection, "current_jobs", []):
                row = self.job_row(connection, job_id)
                if row["source"] in failed and job_id not in ids:
                    ids.append(job_id)
            run_id, now = str(uuid4()), utcnow()
            connection.execute(
                s.search_runs.insert().values(
                    id=run_id, user_id=self.user_id, payload={"errors": errors}, completed_at=now
                )
            )
            for position, job_id in enumerate(ids):
                row = self.job_row(connection, job_id)
                connection.execute(
                    s.search_run_jobs.insert().values(
                        user_id=self.user_id,
                        run_id=run_id,
                        job_id=job_id,
                        position=position,
                        score=row["score"],
                        reasons=row["reasons"],
                        payload={
                            **row["personal_payload"],
                            "job": row["payload"],
                            "score": row["score"],
                            "reasons": row["reasons"],
                        },
                    )
                )
            for key, value in (
                ("current_jobs", ids),
                ("search_errors", errors),
                ("last_search_at", now.isoformat()),
                ("current_search_run", run_id),
            ):
                self.put_setting(connection, key, value)
            self.touch(connection)

    def list_jobs(self, tab, page, snapshot_id=None, run_id=None):
        if tab not in {"active", "applied", "archive", "manual"} or page < 1:
            raise StoreError("Некорректная страница.", 422)
        with self.write() as connection:
            if snapshot_id:
                snapshot = (
                    connection.execute(
                        select(s.snapshots).where(
                            self.owned(s.snapshots), s.snapshots.c.id == snapshot_id
                        )
                    )
                    .mappings()
                    .first()
                )
                if snapshot is None:
                    raise StoreError("Выдача недоступна. Обновите страницу.", 404)
                if run_id and snapshot["payload"].get("run_id") != run_id:
                    raise StoreError("Снимок относится к другому поиску.", 409)
                ids, snapshot_at = snapshot["job_ids"], snapshot["snapshot_at"]
                frozen = snapshot["payload"]
            else:
                selected_run = run_id or self.setting(connection, "current_search_run")
                run_metadata = None
                cards = {}
                if selected_run:
                    run_metadata = self.search_run_data(connection, selected_run)
                    entries = (
                        connection.execute(
                            select(s.search_run_jobs)
                            .where(
                                self.owned(s.search_run_jobs),
                                s.search_run_jobs.c.run_id == selected_run,
                            )
                            .order_by(s.search_run_jobs.c.position)
                        )
                        .mappings()
                        .all()
                    )
                    cards = {
                        row["job_id"]: {
                            **row["payload"],
                            "score": row["score"],
                            "reasons": row["reasons"],
                        }
                        for row in entries
                    }
                    if run_id:
                        for card in cards.values():
                            card["historical"] = True
                    ids = list(cards)
                else:
                    ids = list(
                        connection.execute(
                            select(s.matches.c.job_id)
                            .where(self.owned(s.matches))
                            .order_by(s.matches.c.score.desc(), s.matches.c.job_id)
                        )
                        .scalars()
                        .all()
                    )
                if not selected_run and self.setting(connection, "latest_search_requested"):
                    ids = []
                if not run_id:
                    extras = set(self.setting(connection, "imported_jobs", []))
                    if tab == "manual":
                        extras.update(
                            connection.execute(
                                select(s.matches.c.job_id).where(
                                    self.owned(s.matches),
                                    s.matches.c.payload["manual"].as_boolean().is_(True),
                                )
                            ).scalars()
                        )
                    extras.update(
                        connection.execute(
                            select(s.states.c.job_id).where(self.owned(s.states))
                        ).scalars()
                    )
                    for key in sorted(extras):
                        if key not in ids:
                            ids.append(key)
                for key in ids:
                    if not cards.get(key, {}).get("job"):
                        row = self.job_row(connection, key)
                        cards[key] = {
                            **row["personal_payload"],
                            "job": row["payload"],
                            "score": row["score"],
                            "reasons": row["reasons"],
                        }
                frozen = {"cards": cards, "run_id": selected_run, "search_run": run_metadata}
                snapshot_id, snapshot_at = str(uuid4()), time.time()
                connection.execute(
                    s.snapshots.insert().values(
                        id=snapshot_id,
                        user_id=self.user_id,
                        job_ids=ids,
                        snapshot_at=snapshot_at,
                        payload=frozen,
                    )
                )
                connection.execute(
                    delete(s.snapshots).where(
                        self.owned(s.snapshots), s.snapshots.c.snapshot_at < time.time() - 86400
                    )
                )
            states = {
                row.job_id: (row.applied_at, row.archived_at)
                for row in connection.execute(select(s.states).where(self.owned(s.states)))
            }
            last_search = self.setting(connection, "last_search_at")
            schedule = (
                connection.execute(
                    select(s.search_schedules.c.enabled, s.search_schedules.c.next_run_at).where(
                        self.owned(s.search_schedules)
                    )
                )
                .mappings()
                .first()
                if tab == "active" and not run_id
                else None
            )
            checks = self.setting(connection, "posting_checks", {})
            inputs = (frozen.get("search_run") or {}).get("inputs", {})
            profile = self.profile_data(connection)
            criteria_changed = bool(inputs) and profile["generation"] != inputs.get(
                "profile_generation", inputs.get("profile_version")
            )
        selected = []
        for key in ids:
            card = frozen.get("cards", {}).get(key, {})
            manual = card.get("manual", False)
            if (tab == "manual" and not manual) or (tab == "active" and manual):
                continue
            cached_job = card.get("job", {})
            check = checks.get(posting_observation_key(key, digest(cached_job)), {})
            if tab == "active" and not run_id and check:
                if last_confirmed_posting(check).get("status") == "closed":
                    continue
                if (
                    inputs.get("profile")
                    and check.get("original")
                    and self.invalid_after_enrichment(
                        cached_job, check, inputs["profile"], frozen["search_run"]["created_at"]
                    )
                ):
                    continue
            applied, archived = states.get(key, (None, None))
            belongs = (
                bool(archived)
                if tab == "archive"
                else (
                    bool(applied) and not archived
                    if tab == "applied"
                    else not applied and not archived
                )
            )
            if belongs:
                selected.append(key)
        pages = max(1, math.ceil(len(selected) / 10))
        page = min(page, pages)
        with self.read() as connection:
            items = [
                self.card_data(
                    connection,
                    self.job_row(connection, key),
                    frozen.get("cards", {}).get(key),
                    frozen.get("search_run"),
                )
                for key in selected[(page - 1) * 10 : page * 10]
            ]
        return {
            "items": items,
            "search_run": frozen.get("search_run"),
            "latest_run_id": self.get_setting("current_search_run"),
            "criteria_changed": criteria_changed,
            "total": len(selected),
            "page": page,
            "pages": pages,
            "snapshot_id": snapshot_id,
            "snapshot_at": datetime.fromtimestamp(snapshot_at, UTC).isoformat(),
            "last_search_at": last_search,
            "schedule": dict(schedule) if schedule else None,
        }

    @staticmethod
    def invalid_after_enrichment(job, check, criteria, created_at):
        from qa_job_bot.search_models import SearchSpec
        from qa_job_bot.search_pipeline import load_job
        from qa_job_bot.universal_matching import location_decision

        enriched = load_job(verified_description(job, check))
        profile = SearchSpec.model_validate(criteria).runtime()
        decision = location_decision(enriched, profile, datetime.fromisoformat(created_at).date())
        return decision.status in {"blocked", "not_remote"} or (
            decision.status == "unknown" and not profile.allow_unknown_remote_location
        )

    def change_state(self, job_id, values):
        if not values or not set(values) <= {"applied", "archived"}:
            raise StoreError("Укажите статус вакансии.", 422)
        with self.write() as connection:
            self.job_row(connection, job_id)
            changes = {
                f"{field}_at": utcnow().isoformat() if value else None
                for field, value in values.items()
            }
            upsert(
                connection,
                s.states,
                {"user_id": self.user_id, "job_id": job_id, **changes},
                ["user_id", "job_id"],
            )
            self.touch(connection)
        return self.get_job(job_id)
