from __future__ import annotations

import json
import math
import sqlite3
import time
from hashlib import sha256
from typing import Any
from uuid import uuid4

from qa_job_bot.job_details import extract_details
from qa_job_bot.models import utcnow
from qa_job_bot.repository import JobRepository

ACTIVE_STATUSES = ("queued", "running", "verifying", "rendering")


class StoreError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return sha256(encode(value).encode()).hexdigest()


class WebStore(JobRepository):
    def initialize(self) -> None:
        super().initialize()
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS web_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS web_state (
                    job_id TEXT PRIMARY KEY, applied_at TEXT, archived_at TEXT);
                CREATE TABLE IF NOT EXISTS web_snapshots (
                    id TEXT PRIMARY KEY, job_ids TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS web_templates (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS web_conversations (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL UNIQUE, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS web_messages (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL,
                    text TEXT NOT NULL, request_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(conversation_id, request_id, role));
                CREATE TABLE IF NOT EXISTS web_tasks (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL,
                    request_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
                    status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
                    error TEXT, created_at TEXT NOT NULL, lease_until REAL,
                    worker_id TEXT, attempts INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS web_tasks_pending ON web_tasks(status, created_at);
                CREATE TABLE IF NOT EXISTS web_task_requests (
                    request_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS web_artifacts (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS web_sessions (
                    token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS web_login_attempts (
                    ip TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
                INSERT OR IGNORE INTO schema_migrations VALUES (1, CURRENT_TIMESTAMP);
            """)
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM schema_migrations WHERE version=2").fetchone():
                for row in db.execute("SELECT * FROM web_tasks"):
                    payload = json.loads(row["payload"])
                    request = {"kind": row["kind"], "scope": row["scope"], "payload": payload}
                    if row["kind"] in {"cv", "cover_letter"} and payload.get("job_id"):
                        request = {
                            "operation": "document",
                            "job_id": payload["job_id"],
                            "kind": row["kind"],
                            "language": "en" if row["kind"] == "cv" else "auto",
                        }
                    elif row["kind"] == "chat" and payload.get("messages"):
                        request = {
                            "operation": "chat",
                            "conversation_id": payload["conversation_id"],
                            "text": payload["messages"][-1]["text"],
                        }
                    db.execute(
                        "INSERT OR IGNORE INTO web_task_requests VALUES (?,?,?)",
                        (row["request_id"], row["id"], encode(request)),
                    )
                db.execute("INSERT INTO schema_migrations VALUES (2,CURRENT_TIMESTAMP)")

    @staticmethod
    def _touch(db: sqlite3.Connection) -> None:
        db.execute("""INSERT INTO web_settings VALUES ('revision','1')
            ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1""")

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._connect() as db:
            row = db.execute("SELECT value FROM web_settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO web_settings VALUES (?,?)", (key, encode(value)))
            self._touch(db)

    def publish_search(self, jobs: list, errors: list[str]) -> None:
        ids = [job.fingerprint for job in jobs]
        failed_sources = {error.rsplit(":", 1)[0] for error in errors}
        previous = self.get_setting("current_jobs", [])
        with self._connect() as db:
            for job_id in previous:
                row = db.execute(
                    "SELECT source FROM jobs WHERE fingerprint=?", (job_id,)
                ).fetchone()
                if row and row[0] in failed_sources and job_id not in ids:
                    ids.append(job_id)
            for key, value in (
                ("current_jobs", ids),
                ("search_errors", errors),
                ("last_search_at", utcnow().isoformat()),
            ):
                db.execute("INSERT OR REPLACE INTO web_settings VALUES (?,?)", (key, encode(value)))
            self._touch(db)

    def get_job(self, job_id: str) -> dict:
        with self._connect() as db:
            row = db.execute(
                """SELECT j.*, s.applied_at, s.archived_at FROM jobs j
                LEFT JOIN web_state s ON s.job_id=j.fingerprint WHERE j.fingerprint=?""",
                (job_id,),
            ).fetchone()
        if not row or not row["payload"]:
            raise StoreError("Вакансия недоступна. Обновите поиск.", 404)
        job = json.loads(row["payload"])
        return {
            **job,
            "id": job_id,
            "applied_at": row["applied_at"],
            "archived_at": row["archived_at"],
            "details": extract_details(job.get("description", ""), job["title"]),
            "documents": self.tasks(job_id),
        }

    def list_jobs(self, tab: str, page: int, snapshot_id: str | None = None) -> dict:
        if tab not in ("active", "archive") or page < 1:
            raise StoreError("Некорректная страница.", 422)
        with self._connect() as db:
            if snapshot_id:
                snapshot = db.execute(
                    "SELECT * FROM web_snapshots WHERE id=?", (snapshot_id,)
                ).fetchone()
                if not snapshot:
                    raise StoreError("Выдача устарела. Обновите страницу.", 409)
                ids = json.loads(snapshot["job_ids"])
            else:
                rows = db.execute("""SELECT fingerprint FROM jobs WHERE payload IS NOT NULL
                    ORDER BY score DESC,published_at DESC,fingerprint""").fetchall()
                current = self.get_setting("current_jobs")
                allowed = set(current) if current is not None else {row[0] for row in rows}
                allowed.update(self.get_setting("imported_jobs", []))
                retained = {row[0] for row in db.execute("SELECT job_id FROM web_state")}
                ids = [row[0] for row in rows if row[0] in allowed | retained]
                snapshot_id = str(uuid4())
                db.execute(
                    "INSERT INTO web_snapshots VALUES (?,?,?)",
                    (snapshot_id, encode(ids), time.time()),
                )
                db.execute("DELETE FROM web_snapshots WHERE created_at<?", (time.time() - 86400,))
            states = {
                row["job_id"]: row["archived_at"]
                for row in db.execute("SELECT job_id,archived_at FROM web_state")
            }
        selected = [key for key in ids if bool(states.get(key)) == (tab == "archive")]
        total = len(selected)
        pages = max(1, math.ceil(total / 10))
        page = min(page, pages)
        return {
            "items": [self.get_job(key) for key in selected[(page - 1) * 10 : page * 10]],
            "total": total,
            "page": page,
            "pages": pages,
            "snapshot_id": snapshot_id,
        }

    def change_state(self, job_id: str, values: dict[str, bool]) -> dict:
        self.get_job(job_id)
        if not values or not set(values) <= {"applied", "archived"}:
            raise StoreError("Укажите статус вакансии.", 422)
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO web_state(job_id) VALUES (?)", (job_id,))
            for field, value in values.items():
                column = {"applied": "applied_at", "archived": "archived_at"}[field]
                db.execute(
                    f"UPDATE web_state SET {column}=? WHERE job_id=?",
                    (utcnow().isoformat() if value else None, job_id),
                )
            self._touch(db)
        return self.get_job(job_id)

    def add_template(self, data: dict) -> dict:
        template = {
            **data,
            "id": str(uuid4()),
            "status": "draft",
            "created_at": utcnow().isoformat(),
            "instructions": "",
        }
        template["version"] = template["id"]
        with self._connect() as db:
            db.execute(
                "INSERT INTO web_templates VALUES (?,?,?)",
                (template["id"], "draft", encode(template)),
            )
            self._touch(db)
        conversation = self.conversation(template_id=template["id"])
        template["conversation_id"] = conversation["id"]
        return self.update_template(template["id"], template)

    def template(self, template_id: str) -> dict:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM web_templates WHERE id=?", (template_id,)
            ).fetchone()
        if not row:
            raise StoreError("Шаблон не найден.", 404)
        return json.loads(row[0])

    def profile(self) -> dict:
        with self._connect() as db:
            templates = [
                json.loads(row[0])
                for row in db.execute("SELECT payload FROM web_templates ORDER BY rowid DESC")
            ]
        return {
            "active": next((t for t in templates if t["status"] == "active"), None),
            "drafts": [t for t in templates if t["status"] == "draft"],
        }

    def update_template(self, template_id: str, changes: dict) -> dict:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM web_templates WHERE id=?", (template_id,)).fetchone()
            if not row:
                raise StoreError("Шаблон не найден.", 404)
            if row["status"] != "draft":
                raise StoreError("Создайте новую версию эталона.", 409)
            template = {**json.loads(row["payload"]), **changes}
            db.execute(
                "UPDATE web_templates SET payload=? WHERE id=?", (encode(template), template_id)
            )
            self._touch(db)
        return template

    def activate_template(self, template_id: str, expected_version: str | None) -> dict:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM web_templates WHERE id=?", (template_id,)).fetchone()
            if not row:
                raise StoreError("Шаблон не найден.", 404)
            active = db.execute("SELECT * FROM web_templates WHERE status='active'").fetchone()
            if (active["id"] if active else None) != expected_version:
                raise StoreError("Эталон изменён в другой вкладке. Обновите профиль.", 409)
            template = json.loads(row["payload"])
            if row["status"] != "draft" or not template["facts"]:
                raise StoreError("Подтвердите непустой черновик профиля.", 409)
            if active:
                retired = {**json.loads(active["payload"]), "status": "retired"}
                db.execute(
                    "UPDATE web_templates SET status='retired',payload=? WHERE id=?",
                    (encode(retired), active["id"]),
                )
            template.update(status="active", confirmed_at=utcnow().isoformat())
            db.execute(
                "UPDATE web_templates SET status='active',payload=? WHERE id=?",
                (encode(template), template_id),
            )
            self._touch(db)
        return template

    def conversation(self, template_id: str | None = None, job_id: str | None = None) -> dict:
        if bool(template_id) == bool(job_id):
            raise StoreError("Выберите шаблон или вакансию.", 422)
        if template_id:
            self.template(template_id)
        else:
            self.get_job(job_id)
        scope = f"template:{template_id}" if template_id else f"job:{job_id}"
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM web_conversations WHERE scope=?", (scope,)
            ).fetchone()
            if row:
                return json.loads(row[0])
            data = {"id": str(uuid4()), "template_id": template_id, "job_id": job_id}
            db.execute(
                "INSERT INTO web_conversations VALUES (?,?,?)", (data["id"], scope, encode(data))
            )
            return data

    def get_conversation(self, conversation_id: str) -> dict:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM web_conversations WHERE id=?", (conversation_id,)
            ).fetchone()
        if not row:
            raise StoreError("Диалог не найден.", 404)
        return json.loads(row[0])

    def messages(self, conversation_id: str) -> list[dict]:
        self.get_conversation(conversation_id)
        with self._connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    """SELECT * FROM web_messages
                WHERE conversation_id=? ORDER BY rowid""",
                    (conversation_id,),
                )
            ]

    def owner_messages(self, job_id: str) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT m.text FROM web_messages m
                JOIN web_conversations c ON c.id=m.conversation_id
                WHERE c.scope=? AND m.role='user' ORDER BY m.rowid DESC LIMIT 8""",
                (f"job:{job_id}",),
            ).fetchall()
        return [row[0] for row in reversed(rows)]

    def add_message(self, conversation_id: str, role: str, text: str, request_id: str) -> dict:
        self.get_conversation(conversation_id)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT * FROM web_messages
                WHERE conversation_id=? AND request_id=? AND role=?""",
                (conversation_id, request_id, role),
            ).fetchone()
            if row:
                if row["text"] != text:
                    raise StoreError("Повторный запрос содержит другой текст.", 409)
                return dict(row)
            message = {
                "id": str(uuid4()),
                "conversation_id": conversation_id,
                "role": role,
                "text": text,
                "request_id": request_id,
                "created_at": utcnow().isoformat(),
            }
            db.execute("INSERT INTO web_messages VALUES (?,?,?,?,?,?)", tuple(message.values()))
            self._touch(db)
        return message

    @staticmethod
    def _request_task_id(db: sqlite3.Connection, request_id: str, request: dict) -> str | None:
        row = db.execute(
            "SELECT * FROM web_task_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if row and json.loads(row["payload"]) != request:
            raise StoreError("Ключ запроса уже использован с другими данными.", 409)
        return row["task_id"] if row else None

    def request_task(self, request_id: str, request: dict) -> dict | None:
        with self._connect() as db:
            task_id = self._request_task_id(db, request_id, request)
        return self.task(task_id) if task_id else None

    def enqueue(
        self, kind: str, scope: str, payload: dict, request_id: str, request: dict | None = None
    ) -> dict:
        request = (
            request if request is not None else {"kind": kind, "scope": scope, "payload": payload}
        )
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task_id = self._request_task_id(db, request_id, request)
            if task_id is None:
                existing = db.execute(
                    """SELECT * FROM web_tasks WHERE scope=?
                    AND status IN ('queued','running','verifying','rendering')""",
                    (scope,),
                ).fetchone()
                if existing:
                    task_id = existing["id"]
                else:
                    task_id = str(uuid4())
                    db.execute(
                        """INSERT INTO web_tasks
                        (id,kind,scope,request_id,payload,status,created_at)
                        VALUES (?,?,?,?,?,'queued',?)""",
                        (task_id, kind, scope, request_id, encode(payload), utcnow().isoformat()),
                    )
                    self._touch(db)
                db.execute(
                    "INSERT INTO web_task_requests VALUES (?,?,?)",
                    (request_id, task_id, encode(request)),
                )
        return self.task(task_id)

    def enqueue_message(
        self, conversation_id: str, context: dict, text: str, request_id: str
    ) -> dict:
        self.get_conversation(conversation_id)
        request = {"operation": "chat", "conversation_id": conversation_id, "text": text}
        scope = f"chat:{conversation_id}"
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task_id = self._request_task_id(db, request_id, request)
            if task_id is None:
                pending = db.execute(
                    """SELECT 1 FROM web_tasks WHERE scope=?
                    AND status IN ('queued','running','verifying','rendering')""",
                    (scope,),
                ).fetchone()
                if pending:
                    raise StoreError("Дождитесь ответа на предыдущее сообщение.", 409)
                history = db.execute(
                    """SELECT role,text FROM web_messages WHERE conversation_id=?
                    ORDER BY rowid DESC LIMIT 8""",
                    (conversation_id,),
                ).fetchall()
                messages = [dict(row) for row in reversed(history)]
                messages.append({"role": "user", "text": text})
                payload = {
                    "conversation_id": conversation_id,
                    "context": context,
                    "messages": messages,
                }
                task_id, now = str(uuid4()), utcnow().isoformat()
                db.execute(
                    "INSERT INTO web_messages VALUES (?,?,?,?,?,?)",
                    (str(uuid4()), conversation_id, "user", text, request_id, now),
                )
                db.execute(
                    """INSERT INTO web_tasks
                    (id,kind,scope,request_id,payload,status,created_at)
                    VALUES (?,'chat',?,?,?,'queued',?)""",
                    (task_id, scope, request_id, encode(payload), now),
                )
                db.execute(
                    "INSERT INTO web_task_requests VALUES (?,?,?)",
                    (request_id, task_id, encode(request)),
                )
                self._touch(db)
        return self.task(task_id)

    def finish_chat(self, task_id: str, worker_id: str, text: str) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT * FROM web_tasks WHERE id=? AND worker_id=?
                AND kind='chat' AND status='running' AND lease_until>=?""",
                (task_id, worker_id, time.time()),
            ).fetchone()
            if not row:
                return False
            payload = json.loads(row["payload"])
            db.execute(
                "INSERT INTO web_messages VALUES (?,?,?,?,?,?)",
                (
                    str(uuid4()),
                    payload["conversation_id"],
                    "assistant",
                    text,
                    row["request_id"],
                    utcnow().isoformat(),
                ),
            )
            db.execute("UPDATE web_tasks SET status='ready' WHERE id=?", (task_id,))
            self._touch(db)
        return True

    def task(self, task_id: str, internal: bool = False) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT * FROM web_tasks WHERE id=?", (task_id,)).fetchone()
            artifacts = [
                json.loads(a[0])
                for a in db.execute("SELECT payload FROM web_artifacts WHERE task_id=?", (task_id,))
            ]
        if not row:
            raise StoreError("Задание не найдено.", 404)
        data = dict(row)
        payload = json.loads(data.pop("payload"))
        data["result"] = json.loads(data["result"])
        data.update(
            job_id=payload.get("job_id"),
            conversation_id=payload.get("conversation_id"),
            artifacts=[{key: a[key] for key in ("id", "filename", "format")} for a in artifacts],
        )
        if internal:
            data["payload"] = payload
        return data

    def tasks(self, job_id: str | None = None) -> list[dict]:
        with self._connect() as db:
            if job_id is None:
                rows = db.execute("SELECT id FROM web_tasks ORDER BY created_at DESC LIMIT 200")
            else:
                rows = db.execute(
                    """SELECT id FROM web_tasks WHERE json_extract(payload,'$.job_id')=?
                    ORDER BY created_at DESC""",
                    (job_id,),
                )
            ids = [row[0] for row in rows]
        return [self.task(task_id) for task_id in ids]

    def claim(self, worker_id: str, lease_seconds: int = 900) -> dict | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            expired = db.execute(
                """UPDATE web_tasks SET status='failed',
                error='Исполнение прервано. Повторите задание.', worker_id=NULL, lease_until=NULL
                WHERE status IN ('running','verifying','rendering') AND lease_until<?""",
                (time.time(),),
            )
            row = db.execute("""SELECT id FROM web_tasks WHERE status='queued'
                ORDER BY CASE kind WHEN 'search' THEN 0 ELSE 1 END,created_at LIMIT 1""").fetchone()
            if expired.rowcount:
                self._touch(db)
            if not row:
                return None
            task_id = row[0]
            db.execute(
                """UPDATE web_tasks SET status='running',worker_id=?,lease_until=?,
                attempts=attempts+1 WHERE id=?""",
                (worker_id, time.time() + lease_seconds, task_id),
            )
            self._touch(db)
        return self.task(task_id, internal=True)

    def finish_task(
        self,
        task_id: str,
        worker_id: str,
        status: str,
        result: dict | None = None,
        error: str | None = None,
    ) -> bool:
        with self._connect() as db:
            changed = db.execute(
                """UPDATE web_tasks SET status=?, result=?, error=?
                WHERE id=? AND worker_id=? AND status IN ('running','verifying','rendering')
                AND lease_until>=?""",
                (status, encode(result or {}), error, task_id, worker_id, time.time()),
            )
            if changed.rowcount:
                self._touch(db)
        return bool(changed.rowcount)

    def cancel_task(self, task_id: str) -> dict:
        self.task(task_id)
        with self._connect() as db:
            db.execute(
                """UPDATE web_tasks SET status='cancelled' WHERE id=?
                AND status IN ('queued','running','verifying','rendering')""",
                (task_id,),
            )
            self._touch(db)
        return self.task(task_id)

    def add_artifact(self, task_id: str, data: dict) -> dict:
        artifact = {**data, "id": str(uuid4()), "task_id": task_id}
        with self._connect() as db:
            db.execute(
                "INSERT INTO web_artifacts VALUES (?,?,?)",
                (artifact["id"], task_id, encode(artifact)),
            )
        return artifact

    def artifact(self, artifact_id: str) -> dict:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM web_artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
        if not row:
            raise StoreError("Файл не найден.", 404)
        artifact = json.loads(row[0])
        task = self.task(artifact["task_id"])
        if task["status"] != "ready" or not task["result"].get("verified"):
            raise StoreError("Файл не прошёл проверку.", 409)
        return artifact
