import copy
from uuid import uuid4

from sqlalchemy import select

from qa_job_bot import pg_schema as s
from qa_job_bot.pg_access import OwnedRepository
from qa_job_bot.web_store import StoreError, digest


def document_job(job):
    excluded = {
        "documents",
        "applied_at",
        "archived_at",
        "score",
        "match_reasons",
        "location_status",
        "freshness",
        "fetched_at",
        "last_seen_at",
        "source_status",
    }
    return {key: copy.deepcopy(value) for key, value in job.items() if key not in excluded}


class PersonalDocumentContext(OwnedRepository):
    def selected_document_job(self, connection, job_id, snapshot_id=None, run_id=None):
        row = self.job_row(connection, job_id)
        card = None
        if snapshot_id:
            snap = self.row(connection, s.snapshots, snapshot_id, "Снимок не найден.")
            if job_id not in snap["job_ids"]:
                raise StoreError("Вакансии нет в выбранном снимке.", 404)
            if run_id and snap["payload"].get("run_id") != run_id:
                raise StoreError("Снимок относится к другому поиску.", 409)
            card = snap["payload"].get("cards", {}).get(job_id)
            run_id = snap["payload"].get("run_id")
        elif run_id:
            self.row(connection, s.search_runs, run_id, "Поиск не найден.")
            item = (
                connection.execute(
                    select(s.search_run_jobs).where(
                        self.owned(s.search_run_jobs),
                        s.search_run_jobs.c.run_id == run_id,
                        s.search_run_jobs.c.job_id == job_id,
                    )
                )
                .mappings()
                .first()
            )
            if item is None:
                raise StoreError("Вакансии нет в выбранном поиске.", 404)
            card = item["payload"]
        job = document_job(self.card_data(connection, row, card))
        return job, {"run_id": run_id, "snapshot_id": snapshot_id, "content_hash": digest(job)}

    def job_conversation(self, connection, job_id, snapshot_id=None, run_id=None):
        job, version = self.selected_document_job(connection, job_id, snapshot_id, run_id)
        scope = f"job:{job_id}:{version['content_hash']}"
        row = (
            connection.execute(
                select(s.conversations).where(
                    self.owned(s.conversations), s.conversations.c.scope == scope
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            legacy = (
                connection.execute(
                    select(s.conversations).where(
                        self.owned(s.conversations), s.conversations.c.scope == f"job:{job_id}"
                    )
                )
                .mappings()
                .first()
            )
            current_job, _ = self.selected_document_job(connection, job_id)
            if legacy and not legacy["payload"] and digest(current_job) == version["content_hash"]:
                connection.execute(
                    s.conversations.update()
                    .where(self.owned(s.conversations), s.conversations.c.id == legacy["id"])
                    .values(scope=scope, payload={"job": job, "job_version": version})
                )
                row = {**legacy, "scope": scope, "payload": {"job": job, "job_version": version}}
            else:
                row = {
                    "id": str(uuid4()),
                    "user_id": self.user_id,
                    "scope": scope,
                    "template_id": None,
                    "job_id": job_id,
                    "payload": {"job": job, "job_version": version},
                }
                connection.execute(s.conversations.insert().values(**row))
        return row

    def active_document_template(self, connection):
        row = (
            connection.execute(
                select(s.templates).where(self.owned(s.templates), s.templates.c.status == "active")
            )
            .mappings()
            .first()
        )
        if row is None:
            raise StoreError("Сначала задайте и подтвердите эталонный шаблон.", 409)
        return copy.deepcopy(row["payload"])

    def enqueue_document(
        self,
        job_id,
        kind,
        language,
        request_id,
        snapshot_id=None,
        run_id=None,
        conversation_id=None,
    ):
        request = {"operation": "document", "job_id": job_id, "kind": kind, "language": language}
        request.update(
            {
                key: value
                for key, value in {
                    "snapshot_id": snapshot_id,
                    "run_id": run_id,
                    "conversation_id": conversation_id,
                }.items()
                if value
            }
        )
        with self.write() as connection:
            prior = self.requested_id(connection, request_id, request)
            if prior:
                return self.task_data(connection, prior)
            if conversation_id:
                conversation = self.row(
                    connection, s.conversations, conversation_id, "Диалог не найден."
                )
                if conversation["job_id"] != job_id:
                    raise StoreError("Диалог относится к другой вакансии.", 404)
                if snapshot_id or run_id:
                    job, version = self.selected_document_job(
                        connection, job_id, snapshot_id, run_id
                    )
                    if (
                        conversation["payload"].get("job_version", {}).get("content_hash")
                        != version["content_hash"]
                    ):
                        raise StoreError("Диалог относится к другой версии вакансии.", 409)
            else:
                conversation = self.job_conversation(connection, job_id, snapshot_id, run_id)
            pinned = conversation["payload"]
            if snapshot_id or run_id or not conversation_id or not pinned:
                job, version = self.selected_document_job(connection, job_id, snapshot_id, run_id)
                pinned = {"job": job, "job_version": version}
            if pinned["job"].get("needs_refresh"):
                raise StoreError(
                    "Историческая вакансия без описания. Обновите её перед генерацией.", 409
                )
            messages = self.recent_messages(connection, conversation["id"], limit=16)
            template = self.active_document_template(connection)
            context = {
                "owner_id": self.user_id,
                **copy.deepcopy(pinned),
                "template": template,
                "conversation_messages": messages,
                "owner_messages": [
                    message["text"] for message in messages if message["role"] == "user"
                ][-8:],
            }
            task_id = self.enqueue_in(
                connection,
                kind,
                f"{kind}:{job_id}:{digest(context)}",
                {"job_id": job_id, "conversation_id": conversation["id"], "context": context},
                request_id,
                request,
            )
            return self.task_data(connection, task_id)

    def enqueue_personal_message(self, conversation_id, text, request_id):
        request = {"operation": "chat", "conversation_id": conversation_id, "text": text}
        with self.write() as connection:
            conversation = self.row(
                connection, s.conversations, conversation_id, "Диалог не найден."
            )
            prior = self.requested_id(connection, request_id, request)
            if prior:
                return self.task_data(connection, prior)
            if self.pending_scope(connection, f"chat:{conversation_id}"):
                raise StoreError("Дождитесь ответа на предыдущее сообщение.", 409)
            template = (
                copy.deepcopy(
                    self.row(connection, s.templates, conversation["template_id"])["payload"]
                )
                if conversation["template_id"]
                else self.active_document_template(connection)
            )
            pinned = copy.deepcopy(conversation["payload"])
            if conversation["job_id"] and not pinned:
                job, version = self.selected_document_job(connection, conversation["job_id"])
                pinned = {"job": job, "job_version": version}
            context = {"owner_id": self.user_id, "job": {}, **pinned, "template": template}
            history = self.recent_messages(connection, conversation_id)
            history.append({"role": "user", "text": text})
            task_id = self.enqueue_in(
                connection,
                "chat",
                f"chat:{conversation_id}",
                {"conversation_id": conversation_id, "context": context, "messages": history},
                request_id,
                request,
            )
            self.insert_message(connection, conversation_id, "user", text, request_id)
            return self.task_data(connection, task_id)
