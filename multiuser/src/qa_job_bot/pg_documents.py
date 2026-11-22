from uuid import uuid4

from sqlalchemy import select, update

from qa_job_bot import pg_schema as s
from qa_job_bot.models import utcnow
from qa_job_bot.pg_access import OwnedRepository, serialize
from qa_job_bot.web_store import StoreError


class PersonalDocuments(OwnedRepository):
    def add_template(self, data):
        template_id = str(uuid4())
        template = {
            **data,
            "id": template_id,
            "status": "draft",
            "version": template_id,
            "created_at": utcnow().isoformat(),
            "instructions": data.get("instructions", ""),
        }
        with self.write() as connection:
            connection.execute(
                s.templates.insert().values(
                    id=template_id, user_id=self.user_id, status="draft", payload=template
                )
            )
            conversation = self.make_conversation(connection, template_id=template_id)
            template["conversation_id"] = conversation["id"]
            connection.execute(
                update(s.templates)
                .where(self.owned(s.templates), s.templates.c.id == template_id)
                .values(payload=template)
            )
            self.touch(connection)
        return template

    def template(self, template_id):
        with self.read() as connection:
            return self.row(connection, s.templates, template_id, "Шаблон не найден.")["payload"]

    def profile(self):
        with self.read() as connection:
            rows = (
                connection.execute(
                    select(s.templates)
                    .where(self.owned(s.templates))
                    .order_by(s.templates.c.created_at.desc(), s.templates.c.id.desc())
                )
                .mappings()
                .all()
            )
        return {
            "active": next((row["payload"] for row in rows if row["status"] == "active"), None),
            "drafts": [row["payload"] for row in rows if row["status"] == "draft"],
        }

    def update_template(self, template_id, changes):
        with self.write() as connection:
            row = self.row(connection, s.templates, template_id, "Шаблон не найден.")
            if row["status"] != "draft":
                raise StoreError("Создайте новую версию эталона.", 409)
            protected = {"id", "version", "status", "created_at", "user_id"}
            template = {
                **row["payload"],
                **{k: v for k, v in changes.items() if k not in protected},
            }
            connection.execute(
                update(s.templates)
                .where(self.owned(s.templates), s.templates.c.id == template_id)
                .values(payload=template)
            )
            self.touch(connection)
        return template

    def activate_template(self, template_id, expected_version):
        with self.write() as connection:
            row = self.row(connection, s.templates, template_id, "Шаблон не найден.")
            active = (
                connection.execute(
                    select(s.templates).where(
                        self.owned(s.templates), s.templates.c.status == "active"
                    )
                )
                .mappings()
                .first()
            )
            if (active["id"] if active else None) != expected_version:
                raise StoreError("Эталон изменён в другой вкладке. Обновите профиль.", 409)
            template = dict(row["payload"])
            if row["status"] != "draft" or not template.get("facts"):
                raise StoreError("Подтвердите непустой черновик профиля.", 409)
            if active:
                retired = {**active["payload"], "status": "retired"}
                connection.execute(
                    update(s.templates)
                    .where(self.owned(s.templates), s.templates.c.id == active["id"])
                    .values(status="retired", payload=retired)
                )
            template.update(status="active", confirmed_at=utcnow().isoformat())
            connection.execute(
                update(s.templates)
                .where(self.owned(s.templates), s.templates.c.id == template_id)
                .values(status="active", payload=template)
            )
            self.touch(connection)
        return template

    def make_conversation(
        self, connection, template_id=None, job_id=None, snapshot_id=None, run_id=None
    ):
        if bool(template_id) == bool(job_id):
            raise StoreError("Выберите шаблон или вакансию.", 422)
        if job_id:
            row = self.job_conversation(connection, job_id, snapshot_id, run_id)
            return {key: row[key] for key in ("id", "template_id", "job_id")}
        self.row(connection, s.templates, template_id, "Шаблон не найден.")
        scope = f"template:{template_id}"
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
            values = {
                "id": str(uuid4()),
                "user_id": self.user_id,
                "scope": scope,
                "template_id": template_id,
                "job_id": job_id,
            }
            connection.execute(s.conversations.insert().values(**values))
            row = values
        return {key: row[key] for key in ("id", "template_id", "job_id")}

    def conversation(self, template_id=None, job_id=None, snapshot_id=None, run_id=None):
        with self.write() as connection:
            return self.make_conversation(connection, template_id, job_id, snapshot_id, run_id)

    def get_conversation(self, conversation_id):
        with self.read() as connection:
            row = self.row(connection, s.conversations, conversation_id, "Диалог не найден.")
        return {key: row[key] for key in ("id", "template_id", "job_id")}

    def messages(self, conversation_id):
        with self.read() as connection:
            self.row(connection, s.conversations, conversation_id, "Диалог не найден.")
            return [
                serialize(row)
                for row in connection.execute(
                    select(s.messages)
                    .where(self.owned(s.messages), s.messages.c.conversation_id == conversation_id)
                    .order_by(s.messages.c.sequence)
                ).mappings()
            ]

    def recent_messages(self, connection, conversation_id, limit=8):
        rows = (
            connection.execute(
                select(s.messages.c.role, s.messages.c.text)
                .where(self.owned(s.messages), s.messages.c.conversation_id == conversation_id)
                .order_by(s.messages.c.sequence.desc())
                .limit(limit)
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in reversed(rows)]

    def owner_messages(self, job_id):
        with self.read() as connection:
            self.job_row(connection, job_id)
            rows = (
                connection.execute(
                    select(s.messages.c.text)
                    .join(s.conversations)
                    .where(
                        self.owned(s.messages),
                        self.owned(s.conversations),
                        s.conversations.c.job_id == job_id,
                        s.messages.c.role == "user",
                    )
                    .order_by(s.messages.c.sequence.desc())
                    .limit(8)
                )
                .scalars()
                .all()
            )
        return list(reversed(rows))

    def document_conversation(self, job_id):
        with self.read() as connection:
            self.job_row(connection, job_id)
            rows = (
                connection.execute(
                    select(s.messages.c.role, s.messages.c.text)
                    .join(s.conversations)
                    .where(
                        self.owned(s.messages),
                        self.owned(s.conversations),
                        s.conversations.c.job_id == job_id,
                    )
                    .order_by(s.messages.c.sequence.desc())
                    .limit(16)
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in reversed(rows)]

    def insert_message(self, connection, conversation_id, role, text, request_id):
        if role not in {"user", "assistant"}:
            raise StoreError("Недопустимая роль сообщения.", 422)
        self.row(connection, s.conversations, conversation_id, "Диалог не найден.")
        row = (
            connection.execute(
                select(s.messages).where(
                    self.owned(s.messages),
                    s.messages.c.conversation_id == conversation_id,
                    s.messages.c.request_id == request_id,
                    s.messages.c.role == role,
                )
            )
            .mappings()
            .first()
        )
        if row:
            if row["text"] != text:
                raise StoreError("Повторный запрос содержит другой текст.", 409)
            return serialize(row)
        data = {
            "id": str(uuid4()),
            "user_id": self.user_id,
            "conversation_id": conversation_id,
            "role": role,
            "text": text,
            "request_id": request_id,
        }
        row = (
            connection.execute(s.messages.insert().values(**data).returning(s.messages))
            .mappings()
            .one()
        )
        self.touch(connection)
        return serialize(row)

    def add_message(self, conversation_id, role, text, request_id):
        with self.write() as connection:
            return self.insert_message(connection, conversation_id, role, text, request_id)
