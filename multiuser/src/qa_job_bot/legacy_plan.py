import copy
import re
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import NAMESPACE_URL, uuid5

from qa_job_bot.config import load_boards, load_profile, load_telegram_channels
from qa_job_bot.document_policy import (
    PolicyViolation,
    content_hash,
    template_visual_reference,
    validate_review,
    validate_visual_review,
)
from qa_job_bot.legacy_snapshot import ImportFailure, decode_json, relative_path, write_private_file
from qa_job_bot.models import Job
from qa_job_bot.web_store import digest


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError):
        raise ImportFailure("invalid_timestamp", "Повреждена дата исходной записи.") from None


def entity_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ImportFailure("invalid_identifier", "Некорректный идентификатор исходной записи.")
    return value


def json_object(value):
    result = decode_json(value)
    if not isinstance(result, dict):
        raise ImportFailure("invalid_json", "Ожидался JSON-объект исходной записи.")
    return result


def stable_id(source_hash, name):
    return str(uuid5(NAMESPACE_URL, f"qa-job-import:{source_hash}:{name}"))


class Paths:
    def __init__(self, snapshot, target):
        self.root = PurePosixPath(snapshot["manifest"]["legacy_data_root"])
        if not self.root.is_absolute():
            raise ImportFailure("unsafe_path", "Нужен абсолютный исходный корень файлов.")
        self.target = Path(target).resolve()
        self.files = {entry["path"]: entry for entry in snapshot["manifest"]["files"]}

    def relative(self, value):
        if not isinstance(value, str):
            raise ImportFailure("unsafe_path", "Некорректный путь исходного файла.")
        path = PurePosixPath(value)
        try:
            if path.is_absolute():
                path = path.relative_to(self.root)
            elif path.parts and path.parts[0] == self.root.name:
                path = PurePosixPath(*path.parts[1:])
        except ValueError:
            raise ImportFailure(
                "unsafe_path", "Файл находится вне исходной приватной директории."
            ) from None
        return relative_path(path.as_posix()).as_posix()

    def relocate(self, data, group, task_id=None):
        result = copy.deepcopy(data)
        if "path" not in result:
            return result
        relative = self.relative(result["path"])
        required = f"artifacts/{task_id}/" if task_id else group + "/"
        if not relative.startswith(required) or relative not in self.files:
            raise ImportFailure(
                "missing_reference_file", "Связанный приватный файл не входит в снимок."
            )
        expected = (
            (result.get("source_sha256") or (result.get("reference") or {}).get("sha256"))
            if group == "templates"
            else result.get("sha256")
        )
        if expected and expected != self.files[relative]["sha256"]:
            raise ImportFailure(
                "reference_integrity", "Хеш зарегистрированного файла не совпадает."
            )
        result["path"] = str(self.target.joinpath(*PurePosixPath(relative).parts))
        return result


def configuration(snapshot):
    try:
        with tempfile.TemporaryDirectory(prefix="qa-import-config-") as temporary:
            root = Path(temporary)
            for name, content in snapshot["configuration"].items():
                write_private_file(root, name, content)
            profile = asdict(load_profile(root / "search_profile.toml"))
            boards = load_boards(root / "job_boards.toml")
            channels = load_telegram_channels(root / "telegram_channels.toml")
        sources = [
            {"kind": kind, "payload": {"origin": "legacy"}}
            for kind in ("remotive", "himalayas", "weworkremotely", "remote_com", "hirify")
        ]
        sources += [{"kind": board.kind, "payload": asdict(board)} for board in boards]
        sources += [{"kind": "telegram", "payload": {"handle": channel}} for channel in channels]
        return profile, sources
    except (ValueError, KeyError, TypeError):
        raise ImportFailure(
            "invalid_configuration", "Исходная конфигурация не поддерживается."
        ) from None


def legacy_request(task):
    payload = task["payload"]
    if task["kind"] in {"cv", "cover_letter"} and payload.get("job_id"):
        return {
            "operation": "document",
            "job_id": payload["job_id"],
            "kind": task["kind"],
            "language": "en" if task["kind"] == "cv" else "auto",
        }
    if task["kind"] == "chat" and payload.get("messages"):
        return {
            "operation": "chat",
            "conversation_id": payload["conversation_id"],
            "text": payload["messages"][-1]["text"],
        }
    return {"kind": task["kind"], "scope": task["scope"], "payload": payload}


def checked_document(task, artifacts, paths):
    try:
        result = task["result"]
        context = {**task["payload"]["context"], "kind": task["kind"]}
        draft = result["approved_draft"]
        context["language"] = draft["language"]
        if (
            result.get("verified") is not True
            or content_hash(draft) != result["draft_hash"]
            or result["template_version"] != context["template"]["version"]
        ):
            return False
        review = validate_review(result["review"], draft, context)
        if review["verdict"] != "pass":
            return False
        registered = {entry["id"] for entry in artifacts}
        if not registered or set(result["artifact_ids"]) != registered:
            return False
        pdfs = [entry for entry in artifacts if entry.get("format") == "pdf"]
        if len(pdfs) != 1 or any(
            entry.get("draft_hash") != result["draft_hash"] for entry in artifacts
        ):
            return False
        pdf_relative = Path(pdfs[0]["path"]).relative_to(paths.target).as_posix()
        validate_visual_review(
            result["visual_review"],
            draft,
            template_visual_reference(context["template"]),
            {"sha256": paths.files[pdf_relative]["sha256"]},
        )
        return True
    except (KeyError, TypeError, ValueError, PolicyViolation):
        return False


def job_source_key(row):
    return f"{row['source'].casefold()}\x1f{row['source_id'] or row['url']}"


def legacy_identities(source, issue):
    jobs = {row["fingerprint"]: row for row in source.get("jobs", [])}
    identities = {row["source_key"]: row["fingerprint"] for row in source.get("job_identities", [])}
    declared = set(identities)
    for row in source.get("jobs", []):
        key = job_source_key(row)
        if key in identities and identities[key] != row["fingerprint"]:
            selected = jobs.get(identities[key])
            if (
                key not in declared
                or selected is None
                or job_source_key(selected) != key
                or selected["url"] != row["url"]
            ):
                raise ImportFailure("identity_conflict", "Противоречивая идентичность вакансии.")
            issue("historical_identity_alias", row["fingerprint"])
        else:
            identities[key] = row["fingerprint"]
    return identities


def build_plan(snapshot, owner_id, target, telegram_chats=()):
    entity_id(owner_id)
    source = snapshot["tables"]
    source_hash = snapshot["manifest"]["source_hash"]
    paths = Paths(snapshot, target)
    rows = {
        key: []
        for key in (
            "jobs",
            "job_matches",
            "job_identities",
            "user_jobs",
            "candidate_templates",
            "conversations",
            "messages",
            "tasks",
            "task_requests",
            "artifacts",
            "job_snapshots",
            "user_settings",
            "search_profiles",
            "user_sources",
            "telegram_bindings",
            "deliveries",
        )
    }
    archive, issues, incomplete = [], [], []

    def issue(code, identifier):
        issues.append({"code": code, "id": str(identifier)})

    def require(value, known):
        if value not in known:
            raise ImportFailure("broken_reference", "В снимке обнаружена нарушенная связь.")

    def original(kind, key, value, linked=True):
        archive.append(
            {
                "id": stable_id(source_hash, f"{kind}:{key}"),
                "user_id": owner_id,
                "kind": kind,
                "legacy_key": str(key),
                "linked": linked,
                "payload": value,
            }
        )

    for row in source.get("jobs", []):
        job_id = entity_id(row["fingerprint"])
        data = json_object(row["payload"]) if row.get("payload") else {}
        if set(data) - set(Job.__dataclass_fields__):
            raise ImportFailure("unsupported_job", "Неизвестные поля исходной вакансии.")
        raw = {
            key: value
            for key, value in data.items()
            if key not in {"score", "match_reasons", "location_status", "resolved_fingerprint"}
        }
        for key in ("source", "source_id", "title", "company", "location", "url", "published_at"):
            raw[key] = row[key]
        if not data.get("description"):
            raw.update(description="", needs_refresh=True)
            incomplete.append(job_id)
        score = row["score"]
        if type(score) is not int or not 0 <= score <= 100:
            raise ImportFailure(
                "invalid_score", "Историческая оценка вне поддерживаемого диапазона."
            )
        rows["jobs"].append(
            {
                "id": job_id,
                **{
                    key: row[key]
                    for key in (
                        "source",
                        "source_id",
                        "title",
                        "company",
                        "location",
                        "url",
                        "published_at",
                    )
                },
                "payload": raw,
                "content_hash": digest(raw),
                "created_at": timestamp(row["first_seen_at"]),
                "last_seen_at": timestamp(row["last_seen_at"]),
            }
        )
        rows["job_matches"].append(
            {
                "user_id": owner_id,
                "job_id": job_id,
                "score": score,
                "reasons": data.get("match_reasons", []),
                "location_status": data.get("location_status", "unknown"),
                "payload": {
                    "job": raw,
                    "freshness": "legacy",
                    "fetched_at": timestamp(row["last_seen_at"]).isoformat(),
                    "location_status": data.get("location_status", "unknown"),
                },
            }
        )
        original("job", job_id, row)
    job_ids = {row["id"] for row in rows["jobs"]}
    if len(job_ids) != len(rows["jobs"]):
        raise ImportFailure("duplicate_identity", "Повторные идентификаторы вакансий.")
    identities = legacy_identities(source, issue)
    for key, job_id in identities.items():
        require(job_id, job_ids)
        rows["job_identities"].append({"source_key": key, "job_id": job_id})
    for row in source.get("web_state", []):
        require(row["job_id"], job_ids)
        rows["user_jobs"].append(
            {
                "user_id": owner_id,
                **{key: row[key] for key in ("job_id", "applied_at", "archived_at")},
            }
        )
    for row in source.get("web_templates", []):
        template_id = entity_id(row["id"])
        data = json_object(row["payload"])
        if data.get("id") != template_id or data.get("status") != row["status"]:
            raise ImportFailure(
                "template_conflict", "Версия эталона не согласована со своей записью."
            )
        if row["status"] == "active" and not data.get("facts"):
            raise ImportFailure(
                "invalid_profile", "Активный эталон не содержит подтверждённых фактов."
            )
        original("template", template_id, row)
        data = paths.relocate(data, "templates")
        rows["candidate_templates"].append(
            {
                "id": template_id,
                "user_id": owner_id,
                "status": row["status"],
                "payload": data,
                "created_at": timestamp(data["created_at"]),
            }
        )
    template_ids = {row["id"] for row in rows["candidate_templates"]}
    for row in source.get("web_conversations", []):
        data = json_object(row["payload"])
        conversation_id = entity_id(row["id"])
        if data.get("id") != conversation_id or bool(data.get("template_id")) == bool(
            data.get("job_id")
        ):
            raise ImportFailure("broken_reference", "Некорректная область диалога.")
        require(
            data.get("template_id") or data.get("job_id"),
            template_ids if data.get("template_id") else job_ids,
        )
        rows["conversations"].append(
            {
                "id": conversation_id,
                "user_id": owner_id,
                "scope": row["scope"],
                "template_id": data.get("template_id"),
                "job_id": data.get("job_id"),
            }
        )
    conversation_ids = {row["id"] for row in rows["conversations"]}
    for row in source.get("web_messages", []):
        require(row["conversation_id"], conversation_ids)
        rows["messages"].append(
            {
                "id": entity_id(row["id"]),
                "user_id": owner_id,
                **{key: row[key] for key in ("conversation_id", "role", "text", "request_id")},
                "created_at": timestamp(row["created_at"]),
            }
        )
    original_tasks = {}
    active_scopes = set()
    for row in source.get("web_tasks", []):
        task_id = entity_id(row["id"])
        payload = json_object(row["payload"])
        result = json_object(row["result"])
        original_tasks[task_id] = {**row, "payload": copy.deepcopy(payload)}
        original("task", task_id, row)
        context = payload.get("context")
        if context is not None:
            if context.get("owner_id") not in (None, "owner", owner_id):
                raise ImportFailure(
                    "owner_mismatch", "Исходный контекст принадлежит другому владельцу."
                )
            context["owner_id"] = owner_id
            if context.get("template"):
                context["template"] = paths.relocate(context["template"], "templates")
        context = context or {}
        template_id = (context.get("template") or {}).get("id")
        job_id = payload.get("job_id") or (context.get("job") or {}).get("id")
        conversation_id = payload.get("conversation_id")
        for value, known in (
            (template_id, template_ids),
            (job_id, job_ids),
            (conversation_id, conversation_ids),
        ):
            if value:
                require(value, known)
        status, error = row["status"], row["error"]
        if status in {"running", "verifying", "rendering"}:
            status, error = (
                "failed",
                "Перенос: исход выполнения неизвестен. Нужна сверка перед повтором.",
            )
            issue("provider_outcome_unknown", task_id)
        if status == "queued":
            valid_context = row["kind"] == "search" or (
                template_id and (conversation_id if row["kind"] == "chat" else job_id)
            )
            if row["scope"] in active_scopes or not valid_context:
                status, error = "blocked", "Перенос: требуется проверка контекста перед запуском."
                issue("queued_context_requires_review", task_id)
            else:
                active_scopes.add(row["scope"])
        rows["tasks"].append(
            {
                "id": task_id,
                "user_id": owner_id,
                "kind": row["kind"],
                "scope": row["scope"],
                "request_id": row["request_id"],
                "payload": payload,
                "result": result,
                "status": status,
                "error": error,
                "created_at": timestamp(row["created_at"]),
                "attempts": row["attempts"],
                "worker_id": None,
                "lease_until": None,
                "job_id": job_id,
                "template_id": template_id,
                "conversation_id": conversation_id,
            }
        )
    task_ids = {row["id"] for row in rows["tasks"]}
    for row in source.get("web_artifacts", []):
        require(row["task_id"], task_ids)
        data = json_object(row["payload"])
        if data.get("id") != row["id"] or data.get("task_id") != row["task_id"]:
            raise ImportFailure("broken_reference", "Файл связан с неверным заданием.")
        if not data.get("path") or not data.get("sha256"):
            raise ImportFailure("missing_reference_file", "Нет обязательного пути или хеша файла.")
        data = paths.relocate(data, "artifacts", row["task_id"])
        rows["artifacts"].append(
            {
                "id": entity_id(row["id"]),
                "user_id": owner_id,
                "task_id": row["task_id"],
                "payload": data,
            }
        )
    for task in rows["tasks"]:
        artifacts = [row["payload"] for row in rows["artifacts"] if row["task_id"] == task["id"]]
        if (
            task["status"] == "ready"
            and task["kind"] in {"cv", "cover_letter"}
            and not checked_document(task, artifacts, paths)
        ):
            task.update(status="blocked", error="Перенос: нужна повторная проверка документа.")
            issue("document_verification_required", task["id"])
    requests = {row["request_id"]: row for row in source.get("web_task_requests", [])}
    for task_id, task in original_tasks.items():
        requests.setdefault(
            task["request_id"],
            {"request_id": task["request_id"], "task_id": task_id, "payload": legacy_request(task)},
        )
    for request in requests.values():
        require(request["task_id"], task_ids)
        data = (
            json_object(request["payload"])
            if isinstance(request["payload"], str)
            else request["payload"]
        )
        rows["task_requests"].append(
            {
                "user_id": owner_id,
                "request_id": request["request_id"],
                "task_id": request["task_id"],
                "payload": data,
            }
        )
    settings = {row["key"]: decode_json(row["value"]) for row in source.get("web_settings", [])}
    password = settings.pop("password", None)
    if password is not None and (
        not isinstance(password, str) or not re.fullmatch(r"[a-f0-9]{32}:[a-f0-9]{128}", password)
    ):
        raise ImportFailure(
            "invalid_password_hash", "Формат сохранённого хеша пароля не поддерживается."
        )
    revision = settings.pop("revision", 0)
    if type(revision) is not int or revision < 0:
        raise ImportFailure("invalid_revision", "Недопустимый номер изменений.")
    for key in ("current_jobs", "imported_jobs"):
        ids = settings.get(key, [] if key == "imported_jobs" else sorted(job_ids - set(incomplete)))
        if not isinstance(ids, list) or any(item not in job_ids for item in ids):
            raise ImportFailure("broken_reference", "Нарушена связь сохранённой выдачи.")
        settings[key] = [item for item in ids if item not in incomplete]
    for key, value in settings.items():
        rows["user_settings"].append({"user_id": owner_id, "key": key, "value": value})
    for row in source.get("web_snapshots", []):
        ids = decode_json(row["job_ids"])
        if not isinstance(ids, list) or any(item not in job_ids for item in ids):
            raise ImportFailure("broken_reference", "Нарушена связь снимка выдачи.")
        rows["job_snapshots"].append(
            {
                "id": entity_id(row["id"]),
                "user_id": owner_id,
                "job_ids": ids,
                "snapshot_at": row["created_at"],
                "payload": {
                    "cards": {
                        match["job_id"]: {
                            **match["payload"],
                            "score": match["score"],
                            "reasons": match["reasons"],
                        }
                        for match in rows["job_matches"]
                        if match["job_id"] in ids
                    }
                },
                "created_at": datetime.fromtimestamp(row["created_at"], UTC),
            }
        )
    profile, sources = configuration(snapshot)
    rows["search_profiles"].append(
        {
            "id": stable_id(source_hash, "profile"),
            "user_id": owner_id,
            "version": 1,
            "active": True,
            "payload": profile,
        }
    )
    for index, source_config in enumerate(sources):
        rows["user_sources"].append(
            {
                "id": stable_id(source_hash, f"source:{index}"),
                "user_id": owner_id,
                "enabled": True,
                "version": 1,
                **source_config,
            }
        )
    subscriptions = {row["chat_id"]: row for row in source.get("subscriptions", [])}
    known_chats = set(subscriptions) | {row["chat_id"] for row in source.get("deliveries", [])}
    if not set(telegram_chats) <= known_chats:
        raise ImportFailure(
            "unknown_telegram_chat", "Явная привязка содержит неизвестный Telegram chat ID."
        )
    for chat_id in sorted(set(telegram_chats)):
        subscription = subscriptions.get(chat_id)
        rows["telegram_bindings"].append(
            {
                "chat_id": chat_id,
                "user_id": owner_id,
                "enabled": bool(subscription and subscription["enabled"]),
                **({"created_at": timestamp(subscription["created_at"])} if subscription else {}),
            }
        )
    for row in source.get("subscriptions", []):
        original("telegram_subscription", row["chat_id"], row, row["chat_id"] in telegram_chats)
    for row in source.get("deliveries", []):
        linked = row["chat_id"] in telegram_chats and row["fingerprint"] in job_ids
        original("telegram_delivery", f"{row['chat_id']}:{row['fingerprint']}", row, linked)
        if linked:
            rows["deliveries"].append(
                {
                    "user_id": owner_id,
                    "chat_id": row["chat_id"],
                    "job_id": row["fingerprint"],
                    "created_at": timestamp(row["delivered_at"]),
                }
            )
    return {
        "rows": rows,
        "archive": archive,
        "password_hash": password,
        "revision": revision,
        "report": {
            "source_hash": source_hash,
            "source_counts": {key: len(value) for key, value in snapshot["tables"].items()},
            "target_counts": {key: len(value) for key, value in rows.items()},
            "files": len(paths.files),
            "file_hashes": [
                {"sha256": entry["sha256"], "size": entry["size"]}
                for entry in snapshot["manifest"]["files"]
            ],
            "issues": issues,
            "incomplete_jobs": incomplete,
            "unlinked_telegram": sum(
                not row["linked"] for row in archive if row["kind"].startswith("telegram_")
            ),
            "source_sessions_revoked": len(snapshot["tables"].get("web_sessions", [])),
        },
    }
