from uuid import uuid4

from sqlalchemy import select, update

from qa_job_bot import pg_schema as s
from qa_job_bot.models import Job, utcnow
from qa_job_bot.pg_access import OwnedRepository
from qa_job_bot.pg_jobs import public_payload
from qa_job_bot.posting_details import posting_observation_key
from qa_job_bot.web_store import StoreError, digest


class ManualJobs(OwnedRepository):
    def save_manual_job(self, data, request_id):
        fingerprint = digest(data)
        with self.write() as connection:
            requests = self.setting(connection, "manual_job_requests", {})
            if request_id in requests:
                previous = requests[request_id]
                if previous["fingerprint"] != fingerprint:
                    raise StoreError("Этот запрос уже использован для другой вакансии.", 409)
                return {"id": previous["id"], "existing": True}
            existing = self.manual_job_by_url(connection, data["url"])
            job_id = existing or str(uuid4())
            if not existing:
                content = public_payload(Job("manual", job_id, "", "", "", ""))
                connection.execute(
                    s.jobs.insert().values(
                        id=job_id,
                        source="manual",
                        source_id=job_id,
                        title="",
                        company="",
                        location="",
                        url="",
                        payload=content,
                        content_hash=digest(content),
                    )
                )
                connection.execute(
                    s.matches.insert().values(
                        user_id=self.user_id,
                        job_id=job_id,
                        score=0,
                        reasons=[],
                        location_status="unknown",
                        payload=self.manual_payload(data, job_id, 1),
                    )
                )
            requests[request_id] = {"id": job_id, "fingerprint": fingerprint}
            self.put_setting(connection, "manual_job_requests", requests)
            self.touch(connection)
            return {"id": job_id, "existing": bool(existing)}

    def manual_job_by_url(self, connection, url, excluding=None):
        if not url:
            return None
        query = select(s.matches.c.job_id).where(
            self.owned(s.matches),
            s.matches.c.payload["manual"].as_boolean().is_(True),
            s.matches.c.payload["job"]["url"].as_string() == url,
        )
        if excluding:
            query = query.where(s.matches.c.job_id != excluding)
        return connection.execute(query).scalar_one_or_none()

    @staticmethod
    def manual_payload(data, job_id, version):
        return {
            "manual": True,
            "manual_version": version,
            "fetched_at": utcnow().isoformat(),
            "job": {
                **public_payload(Job("Добавлена вручную", job_id, "", "", "", "")),
                **data,
            },
        }

    def revise_manual_job(self, job_id, data, expected_version):
        with self.write() as connection:
            row = self.job_row(connection, job_id)
            personal = row["personal_payload"]
            if not personal.get("manual"):
                raise StoreError("Вакансия недоступна для редактирования.", 404)
            self.check_version(personal["manual_version"], expected_version)
            if self.manual_job_by_url(connection, data["url"], job_id):
                raise StoreError("Вакансия с такой ссылкой уже добавлена.", 409)
            connection.execute(
                update(s.matches)
                .where(
                    self.owned(s.matches),
                    s.matches.c.job_id == job_id,
                )
                .values(payload=self.manual_payload(data, job_id, expected_version + 1))
            )
            self.touch(connection)
        return self.get_job(job_id)

    def reserve_posting_preview(self):
        now = utcnow().timestamp()
        with self.write() as connection:
            self.reserve_posting_request(connection, now)

    def reserve_posting_request(self, connection, now):
        recent = [
            value for value in self.setting(connection, "posting_requests", []) if value > now - 60
        ]
        if len(recent) >= 5:
            raise StoreError("Подождите минуту перед следующей проверкой объявления.", 429)
        self.put_setting(connection, "posting_requests", [*recent, now])

    def enqueue_posting_check(self, job_id, request_id):
        with self.write() as connection:
            request = {"operation": "posting_check", "job_id": job_id}
            if prior := self.requested_id(connection, request_id, request):
                return {"task": self.task_data(connection, prior)}
            row = self.job_row(connection, job_id)
            job = row["payload"]
            key = digest(job)
            now = utcnow().timestamp()
            observation = posting_observation_key(job_id, key)
            previous = self.setting(connection, "posting_checks", {}).get(observation)
            if previous and previous["next_at"] > now:
                return {"posting": previous["status"]}
            scope = f"posting_check:{job_id}:{key}"
            if not self.pending_scope(connection, scope):
                self.reserve_posting_request(connection, now)
            payload = {
                "job_id": job_id,
                "posting_hash": key,
                "posting_job": job,
                "manual": bool(row["personal_payload"].get("manual")),
            }
            task_id = self.enqueue_in(
                connection, "posting_check", scope, payload, request_id, request
            )
            return {"task": self.task_data(connection, task_id)}

    def complete_posting_check(self, task, worker_id, result):
        with self.write() as connection:
            payload = task["payload"]
            job_id, key = payload["job_id"], payload["posting_hash"]
            row = self.job_row(connection, job_id)
            if digest(row["payload"]) != key:
                raise StoreError(
                    "Объявление изменилось во время проверки. Проверьте его снова.", 409
                )
            checked = connection.execute(
                update(s.tasks)
                .where(*self.live_lease(task["id"], worker_id))
                .values(status="ready", result={"posting": result.public_status()}, error=None)
            ).rowcount
            if not checked:
                return False
            cache = self.setting(connection, "posting_checks", {})
            cache[posting_observation_key(job_id, key)] = {
                "next_at": utcnow().timestamp() + 300,
                "status": result.public_status(),
                "original": result.job,
            }
            self.put_setting(connection, "posting_checks", cache)
            self.touch(connection)
        return True
