import copy
from uuid import uuid4

from sqlalchemy import delete, select, update

from qa_job_bot import pg_schema as s
from qa_job_bot.document_policy import content_hash
from qa_job_bot.pg_access import OwnedRepository, serialize
from qa_job_bot.web_store import ACTIVE_STATUSES, StoreError


class PersonalTasks(OwnedRepository):
    def requested_id(self, connection, request_id, request):
        row = (
            connection.execute(
                select(s.task_requests).where(
                    self.owned(s.task_requests), s.task_requests.c.request_id == request_id
                )
            )
            .mappings()
            .first()
        )
        if row and row["payload"] != request:
            raise StoreError("Ключ запроса уже использован с другими данными.", 409)
        return row["task_id"] if row else None

    def request_task(self, request_id, request):
        with self.read() as connection:
            task_id = self.requested_id(connection, request_id, request)
            return self.task_data(connection, task_id) if task_id else None

    def pending_scope(self, connection, scope):
        return (
            connection.execute(
                select(s.tasks).where(
                    self.owned(s.tasks),
                    s.tasks.c.scope == scope,
                    s.tasks.c.status.in_(ACTIVE_STATUSES),
                )
            )
            .mappings()
            .first()
        )

    def pin_payload(self, connection, payload):
        data = copy.deepcopy(payload)
        context = data.get("context")
        if context is not None:
            if context.get("owner_id", self.user_id) != self.user_id:
                raise StoreError("Контекст задания недоступен.", 404)
            context["owner_id"] = self.user_id
        context = context or {}
        template_id = (context.get("template") or {}).get("id")
        job_id = data.get("job_id") or (context.get("job") or {}).get("id")
        conversation_id = data.get("conversation_id")
        if template_id:
            self.row(connection, s.templates, template_id, "Шаблон не найден.")
        if job_id:
            self.job_row(connection, job_id)
        if conversation_id:
            self.row(connection, s.conversations, conversation_id, "Диалог не найден.")
        return data, {
            "template_id": template_id,
            "job_id": job_id,
            "conversation_id": conversation_id,
        }

    def enqueue_in(self, connection, kind, scope, payload, request_id, request):
        if not request_id or not scope:
            raise StoreError("Укажите ключ и область задания.", 422)
        task_id = self.requested_id(connection, request_id, request)
        if task_id:
            return task_id
        pinned, links = self.pin_payload(connection, payload)
        existing = self.pending_scope(connection, scope)
        if existing:
            if existing["kind"] != kind:
                raise StoreError("Область задания уже занята.", 409)
            task_id = existing["id"]
        else:
            task_id = str(uuid4())
            connection.execute(
                s.tasks.insert().values(
                    id=task_id,
                    user_id=self.user_id,
                    kind=kind,
                    scope=scope,
                    request_id=request_id,
                    payload=pinned,
                    **links,
                )
            )
            self.touch(connection)
        connection.execute(
            s.task_requests.insert().values(
                user_id=self.user_id, request_id=request_id, task_id=task_id, payload=request
            )
        )
        return task_id

    def enqueue(self, kind, scope, payload, request_id, request=None):
        if kind not in {"search", "chat", "cv", "cover_letter"}:
            raise StoreError("Неизвестный вид задания.", 422)
        request = (
            request if request is not None else {"kind": kind, "scope": scope, "payload": payload}
        )
        with self.write() as connection:
            task_id = self.enqueue_in(connection, kind, scope, payload, request_id, request)
            return self.task_data(connection, task_id)

    def enqueue_message(self, conversation_id, context, text, request_id):
        request = {"operation": "chat", "conversation_id": conversation_id, "text": text}
        with self.write() as connection:
            self.row(connection, s.conversations, conversation_id, "Диалог не найден.")
            existing = self.requested_id(connection, request_id, request)
            if existing:
                return self.task_data(connection, existing)
            scope = f"chat:{conversation_id}"
            if self.pending_scope(connection, scope):
                raise StoreError("Дождитесь ответа на предыдущее сообщение.", 409)
            history = self.recent_messages(connection, conversation_id)
            history.append({"role": "user", "text": text})
            task_id = self.enqueue_in(
                connection,
                "chat",
                scope,
                {"conversation_id": conversation_id, "context": context, "messages": history},
                request_id,
                request,
            )
            self.insert_message(connection, conversation_id, "user", text, request_id)
            return self.task_data(connection, task_id)

    def task_data(self, connection, task_id, internal=False):
        row = self.row(connection, s.tasks, task_id, "Задание не найдено.")
        if row["status"] == "deleted" and not internal:
            raise StoreError("Задание не найдено.", 404)
        data = serialize(row)
        payload = data.pop("payload")
        data["preview_available"] = bool(
            data["status"] == "ready"
            and data["result"].get("verified")
            and isinstance(data["result"].get("approved_draft"), dict)
        )
        if not internal:
            data["result"].pop("approved_draft", None)
        data["artifacts"] = [
            {key: entry["payload"][key] for key in ("id", "filename", "format")}
            for entry in connection.execute(
                select(s.artifacts).where(self.owned(s.artifacts), s.artifacts.c.task_id == task_id)
            ).mappings()
        ]
        if data["kind"] in {"cv", "cover_letter"} and payload.get("context"):
            context = payload["context"]
            template = context.get("template") or {}
            job = context.get("job") or {}
            data["input_summary"] = {
                "template_id": template.get("id"),
                "template_version": template.get("version"),
                "template_filename": template.get("filename"),
                "confirmed_at": template.get("confirmed_at"),
                "job_title": job.get("title"),
                "job_version": context.get("job_version"),
            }
        if internal:
            data["payload"] = payload
        return data

    def task(self, task_id, internal=False):
        with self.read() as connection:
            return self.task_data(connection, task_id, internal)

    def task_list(self, connection, job_id=None):
        query = select(s.tasks.c.id).where(self.owned(s.tasks), s.tasks.c.status != "deleted")
        if job_id is not None:
            query = query.where(
                s.tasks.c.job_id == job_id, s.tasks.c.kind.in_(["cv", "cover_letter"])
            )
        query = query.order_by(s.tasks.c.created_at.desc(), s.tasks.c.id).limit(200)
        return [
            self.task_data(connection, task_id)
            for task_id in connection.execute(query).scalars().all()
        ]

    def tasks(self, job_id=None):
        with self.read() as connection:
            if job_id is not None:
                self.job_row(connection, job_id)
            return self.task_list(connection, job_id)

    def claim(self, worker_id, lease_seconds=900):
        from qa_job_bot.pg_queue import QueueRepository

        return QueueRepository(self.database).claim(worker_id, lease_seconds, self.user_id)

    def finish_task(self, task_id, worker_id, status, result=None, error=None):
        if status not in {"ready", "failed", "blocked", "needs_input", "verifying", "rendering"}:
            raise StoreError("Недопустимый статус задания.", 422)
        with self.write() as connection:
            changed = connection.execute(
                update(s.tasks)
                .where(*self.live_lease(task_id, worker_id))
                .values(status=status, result=result or {}, error=error)
            ).rowcount
            if changed:
                self.touch(connection)
        return bool(changed)

    def finish_chat(self, task_id, worker_id, text, metadata=None):
        with self.write() as connection:
            row = (
                connection.execute(
                    select(s.tasks)
                    .where(
                        *self.live_lease(task_id, worker_id),
                        s.tasks.c.kind == "chat",
                        s.tasks.c.status == "running",
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if row is None:
                return False
            self.insert_message(
                connection, row["conversation_id"], "assistant", text, row["request_id"]
            )
            connection.execute(
                update(s.tasks)
                .where(self.owned(s.tasks), s.tasks.c.id == task_id)
                .values(status="ready", result={"call": metadata or {}})
            )
            self.touch(connection)
        return True

    def cancel_task(self, task_id):
        with self.write() as connection:
            self.row(connection, s.tasks, task_id, "Задание не найдено.")
            connection.execute(
                update(s.tasks)
                .where(
                    self.owned(s.tasks),
                    s.tasks.c.id == task_id,
                    s.tasks.c.status.in_(ACTIVE_STATUSES),
                )
                .values(status="cancelled")
            )
            self.touch(connection)
            return self.task_data(connection, task_id)

    def document_task_for_deletion(self, task_id):
        task = self.task(task_id, internal=True)
        if task["status"] == "deleted":
            raise StoreError("Задание не найдено.", 404)
        if task["kind"] not in {"cv", "cover_letter"}:
            raise StoreError("Удалять можно только CV и cover letter.", 422)
        if task["status"] in ACTIVE_STATUSES:
            raise StoreError("Сначала отмените создание документа.", 409)
        return task

    def delete_document_task(self, task_id):
        self.document_task_for_deletion(task_id)
        with self.write() as connection:
            self.row(connection, s.tasks, task_id, "Задание не найдено.")
            connection.execute(
                delete(s.artifacts).where(self.owned(s.artifacts), s.artifacts.c.task_id == task_id)
            )
            connection.execute(
                delete(s.task_requests).where(
                    self.owned(s.task_requests), s.task_requests.c.task_id == task_id
                )
            )
            changed = connection.execute(
                update(s.tasks)
                .where(
                    self.owned(s.tasks),
                    s.tasks.c.id == task_id,
                    s.tasks.c.status.not_in([*ACTIVE_STATUSES, "deleted"]),
                    s.tasks.c.kind.in_(["cv", "cover_letter"]),
                )
                .values(
                    status="deleted",
                    request_id=f"deleted:{task_id}",
                    payload={},
                    result={},
                    error=None,
                    lease_until=None,
                    worker_id=None,
                    job_id=None,
                    template_id=None,
                    conversation_id=None,
                )
            ).rowcount
            if not changed:
                raise StoreError("Документ нельзя удалить в текущем состоянии.", 409)
            self.touch(connection)

    def finish_document(self, task_id, worker_id, result, files):
        with self.write() as connection:
            row = (
                connection.execute(
                    select(s.tasks)
                    .where(
                        *self.live_lease(task_id, worker_id),
                        s.tasks.c.kind.in_(["cv", "cover_letter"]),
                        s.tasks.c.status == "rendering",
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if row is None:
                return False
            context = row["payload"]["context"]
            if (
                result.get("input_hash") != content_hash(context)
                or result.get("template_version") != context["template"].get("version")
                or result.get("job_version") != context.get("job_version")
            ):
                raise StoreError("Результат относится к другому контексту задания.", 409)
            artifacts = [{**data, "id": str(uuid4()), "task_id": task_id} for data in files]
            for artifact in artifacts:
                connection.execute(
                    s.artifacts.insert().values(
                        id=artifact["id"], user_id=self.user_id, task_id=task_id, payload=artifact
                    )
                )
            changed = connection.execute(
                update(s.tasks)
                .where(*self.live_lease(task_id, worker_id))
                .values(
                    status="ready",
                    result={**result, "artifact_ids": [entry["id"] for entry in artifacts]},
                    error=None,
                )
            ).rowcount
            if not changed:
                raise StoreError("Истёк срок исполнения документа.", 409)
            self.touch(connection)
        return True

    def add_artifact(self, task_id, data):
        artifact = {**data, "id": str(uuid4()), "task_id": task_id}
        with self.write() as connection:
            self.row(connection, s.tasks, task_id, "Задание не найдено.")
            connection.execute(
                s.artifacts.insert().values(
                    id=artifact["id"], user_id=self.user_id, task_id=task_id, payload=artifact
                )
            )
        return artifact

    def artifact(self, artifact_id):
        with self.read() as connection:
            row = self.row(connection, s.artifacts, artifact_id, "Файл не найден.")
            task = self.task_data(connection, row["task_id"])
            if task["status"] != "ready" or not task["result"].get("verified"):
                raise StoreError("Файл не прошёл проверку.", 409)
            return row["payload"]
