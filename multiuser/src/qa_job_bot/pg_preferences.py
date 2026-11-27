import time
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from qa_job_bot import pg_schema as s
from qa_job_bot.pg_access import OwnedRepository, serialize
from qa_job_bot.search_models import (
    AGGREGATORS,
    SearchSpec,
    SourceSpec,
    source_identity,
    universal_profile,
)
from qa_job_bot.source_catalog import DESCRIPTIONS, LABELS, LIMITATIONS
from qa_job_bot.web_store import StoreError


class PersonalPreferences(OwnedRepository):
    def source_data(self, row):
        raw = row["payload"]
        kind = row["kind"]
        config = (
            {}
            if kind in AGGREGATORS
            else {
                key: value
                for key, value in raw.items()
                if key
                in (
                    {"handle", "tags"}
                    if kind == "telegram"
                    else {"name", "token", "region", "tags"}
                )
            }
        )
        try:
            config = SourceSpec(kind=kind, config=config).normalized()
            valid = True
        except (ValueError, TypeError, KeyError):
            valid = False
        return {
            "id": row["id"],
            "kind": kind,
            "builtin": kind in AGGREGATORS,
            "description": DESCRIPTIONS.get(kind),
            "actions": ["enable", "check"]
            if kind in AGGREGATORS
            else ["enable", "check", "edit", "delete"],
            "enabled": row["enabled"],
            "version": row["version"],
            "config": config,
            "valid": valid,
            "name": f"Telegram · @{config['handle']}"
            if kind == "telegram" and valid
            else config.get("name") or LABELS.get(kind, kind),
            "limitations": LIMITATIONS.get(kind, "Неподдерживаемый тип источника"),
        }

    def source_rows(self, connection):
        return (
            connection.execute(
                select(s.user_sources)
                .where(self.owned(s.user_sources))
                .order_by(s.user_sources.c.created_at, s.user_sources.c.id)
            )
            .mappings()
            .all()
        )

    def profile_metadata(self, connection):
        return self.setting(
            connection,
            "search_profile_metadata",
            {
                "variants": {},
                "revisions": {},
                "generation": None,
                "editor_version": 0,
            },
        )

    @staticmethod
    def named_revision(row, metadata):
        version = row["version"]
        variant = metadata["revisions"].get(str(version), str(version))
        named = metadata["variants"].get(variant, {})
        return {
            "id": row["id"],
            "version": version,
            "active": row["active"],
            "variant_id": variant,
            "name": named.get("name", f"Версия {variant}"),
            "deleted": named.get("deleted", False),
            "created_at": row["created_at"].isoformat(),
            "profile": row["payload"],
            "editable_profile": universal_profile(row["payload"]),
        }

    def profile_data(self, connection):
        row = (
            connection.execute(
                select(s.search_profiles).where(
                    self.owned(s.search_profiles), s.search_profiles.c.active.is_(True)
                )
            )
            .mappings()
            .first()
        )
        metadata = self.profile_metadata(connection)
        version = row["version"] if row else 0
        return {
            "version": version,
            "generation": version if metadata["generation"] is None else metadata["generation"],
            "editor_version": metadata["editor_version"],
            "configured": row is not None,
            **(self.named_revision(row, metadata) if row else {}),
            "profile": universal_profile(row["payload"]) if row else None,
        }

    def preferences_in(self, connection):
        return {
            "search_profile": self.profile_data(connection),
            "sources": {
                "version": self.setting(connection, "sources_version", 0),
                "items": [self.source_data(row) for row in self.source_rows(connection)],
            },
        }

    def preferences(self):
        with self.write() as connection:
            return self.preferences_in(connection)

    def active_source(self, connection, source_id):
        return self.row(connection, s.user_sources, source_id, "Источник не найден.")

    def record_source(self, connection, row, deleted=False):
        connection.execute(
            insert(s.source_versions)
            .values(
                id=str(uuid4()),
                user_id=self.user_id,
                source_id=row["id"],
                version=row["version"],
                deleted=deleted,
                payload={"kind": row["kind"], "enabled": row["enabled"], "config": row["payload"]},
            )
            .on_conflict_do_nothing(index_elements=["user_id", "source_id", "version"])
        )

    def bump_sources(self, connection):
        self.put_setting(
            connection, "sources_version", self.setting(connection, "sources_version", 0) + 1
        )
        self.touch(connection)

    def ensure_unique_source(self, connection, kind, config, excluding=None):
        wanted = source_identity(kind, config)
        for row in self.source_rows(connection):
            data = self.source_data(row)
            if (
                data["id"] != excluding
                and data["valid"]
                and source_identity(data["kind"], data["config"]) == wanted
            ):
                raise StoreError("Такой источник уже добавлен.", 409)

    def add_source_in(self, connection, spec):
        try:
            config = spec.normalized()
        except (ValueError, KeyError, TypeError) as error:
            raise StoreError(str(error), 422) from None
        self.ensure_unique_source(connection, spec.kind, config)
        connection.execute(
            insert(s.source_definitions)
            .values(kind=spec.kind, payload={})
            .on_conflict_do_nothing(index_elements=["kind"])
        )
        row = {
            "id": str(uuid4()),
            "user_id": self.user_id,
            "kind": spec.kind,
            "enabled": spec.enabled,
            "version": 1,
            "payload": config,
        }
        connection.execute(s.user_sources.insert().values(**row))
        self.record_source(connection, row)
        return row

    def add_source(self, spec):
        with self.write() as connection:
            row = self.add_source_in(connection, spec)
            self.bump_sources(connection)
            return self.source_data(row)

    def source_identities(self, connection):
        return {
            source_identity(data["kind"], data["config"])
            for row in self.source_rows(connection)
            if (data := self.source_data(row))["valid"]
        }

    def source_import_identities(self):
        with self.read() as connection:
            return self.source_identities(connection)

    def add_source_set(self, specs):
        with self.write() as connection:
            identities = self.source_identities(connection)
            added = []
            for spec in specs:
                try:
                    identity = source_identity(spec.kind, spec.normalized())
                except (ValueError, KeyError, TypeError) as error:
                    raise StoreError(str(error), 422) from None
                if identity in identities:
                    continue
                added.append(self.source_data(self.add_source_in(connection, spec)))
                identities.add(identity)
            if added:
                self.bump_sources(connection)
            return {"items": added}

    def update_source(self, source_id, expected_version, enabled=None, config=None):
        with self.write() as connection:
            row = self.active_source(connection, source_id)
            self.check_version(row["version"], expected_version)
            if row["kind"] in AGGREGATORS and config is not None:
                raise StoreError("Параметры встроенного источника изменять нельзя.", 422)
            data = self.source_data(row)
            spec = SourceSpec(
                kind=row["kind"],
                enabled=row["enabled"] if enabled is None else enabled,
                config=data["config"] if config is None else config,
            )
            try:
                normalized = spec.normalized()
            except (ValueError, KeyError, TypeError) as error:
                raise StoreError(str(error), 422) from None
            self.ensure_unique_source(connection, row["kind"], normalized, source_id)
            self.record_source(connection, row)
            revised = {
                **row,
                "version": row["version"] + 1,
                "enabled": spec.enabled,
                "payload": normalized,
            }
            connection.execute(
                update(s.user_sources)
                .where(self.owned(s.user_sources), s.user_sources.c.id == source_id)
                .values(version=revised["version"], enabled=spec.enabled, payload=normalized)
            )
            self.record_source(connection, revised)
            self.bump_sources(connection)
            return self.source_data(revised)

    def delete_source(self, source_id, expected_version):
        with self.write() as connection:
            row = self.active_source(connection, source_id)
            self.check_version(row["version"], expected_version)
            if row["kind"] in AGGREGATORS:
                raise StoreError("Встроенный источник можно выключить.", 409)
            self.record_source(connection, row)
            self.record_source(connection, {**row, "version": row["version"] + 1}, deleted=True)
            connection.execute(
                delete(s.user_sources).where(
                    self.owned(s.user_sources), s.user_sources.c.id == source_id
                )
            )
            self.bump_sources(connection)

    def source_history(self, source_id):
        with self.read() as connection:
            history = [
                serialize(row)
                for row in connection.execute(
                    select(s.source_versions)
                    .where(
                        self.owned(s.source_versions), s.source_versions.c.source_id == source_id
                    )
                    .order_by(s.source_versions.c.version)
                ).mappings()
            ]
            current = (
                connection.execute(
                    select(s.user_sources).where(
                        self.owned(s.user_sources), s.user_sources.c.id == source_id
                    )
                )
                .mappings()
                .first()
            )
            if current and not any(row["version"] == current["version"] for row in history):
                history.append(
                    {
                        "version": current["version"],
                        "deleted": False,
                        "payload": {
                            "kind": current["kind"],
                            "enabled": current["enabled"],
                            "config": current["payload"],
                        },
                    }
                )
            if not history:
                raise StoreError("Источник не найден.", 404)
            return [{key: row[key] for key in ("version", "deleted", "payload")} for row in history]

    @staticmethod
    def check_version(actual, expected):
        if actual != expected:
            raise StoreError(
                "Настройки изменились в другой вкладке. Загрузите актуальную версию.", 409
            )

    def profile_revisions(self, connection, metadata):
        return [
            self.named_revision(row, metadata)
            for row in connection.execute(
                select(s.search_profiles)
                .where(self.owned(s.search_profiles))
                .order_by(s.search_profiles.c.version)
            ).mappings()
        ]

    def commit_profile_metadata(self, connection, metadata, current, active_changed):
        metadata["editor_version"] += 1
        if active_changed:
            metadata["generation"] = current["generation"] + 1
        self.put_setting(connection, "search_profile_metadata", metadata)
        self.touch(connection)

    def set_active_profile(self, connection, revision_id):
        connection.execute(
            update(s.search_profiles)
            .where(self.owned(s.search_profiles), s.search_profiles.c.active.is_(True))
            .values(active=False)
        )
        connection.execute(
            update(s.search_profiles)
            .where(self.owned(s.search_profiles), s.search_profiles.c.id == revision_id)
            .values(active=True)
        )

    @staticmethod
    def selectable_revision(revisions, version):
        found = next((row for row in revisions if row["version"] == version), None)
        if found is None or found["deleted"]:
            raise StoreError("Версия профиля недоступна.", 404)
        return found

    def save_search_profile(
        self,
        spec,
        expected_version,
        *,
        expected_editor_version=None,
        variant_id=None,
        name=None,
        create=False,
    ):
        with self.write() as connection:
            current = self.profile_data(connection)
            metadata = self.profile_metadata(connection)
            self.check_version(current["version"], expected_version)
            if expected_editor_version is not None:
                self.check_version(metadata["editor_version"], expected_editor_version)
            profiles = self.profile_revisions(connection, metadata)
            selected = next(
                (row for row in reversed(profiles) if row["variant_id"] == variant_id), None
            )
            if variant_id and (selected is None or selected["deleted"]):
                raise StoreError("Версия профиля недоступна.", 404)
            payload = spec.model_dump()
            if not create and current["profile"] == payload and variant_id is None:
                return current
            if not create and selected and selected["editable_profile"] == payload:
                return current
            version = (
                connection.execute(
                    select(func.max(s.search_profiles.c.version)).where(
                        self.owned(s.search_profiles)
                    )
                ).scalar_one()
                or 0
            ) + 1
            new_variant = str(version) if create or selected is None else selected["variant_id"]
            named = metadata["variants"].get(new_variant, {})
            metadata["revisions"][str(version)] = new_variant
            metadata["variants"][new_variant] = {
                "name": name or named.get("name") or f"Версия {new_variant}",
            }
            active = (
                not current["configured"]
                or expected_editor_version is None
                or (not create and new_variant == current.get("variant_id"))
            )
            revision_id = str(uuid4())
            connection.execute(
                s.search_profiles.insert().values(
                    id=revision_id,
                    user_id=self.user_id,
                    version=version,
                    active=False,
                    payload=payload,
                )
            )
            if active:
                self.set_active_profile(connection, revision_id)
            self.commit_profile_metadata(connection, metadata, current, active)
            return {
                **self.profile_data(connection),
                "saved_version": version,
                "saved_variant_id": new_variant,
                "saved_name": metadata["variants"][new_variant]["name"],
            }

    def search_profile_history(self):
        with self.read() as connection:
            return self.profile_revisions(connection, self.profile_metadata(connection))

    def change_profile_variant(self, version, action, expected_editor_version, name=None):
        with self.write() as connection:
            current = self.profile_data(connection)
            metadata = self.profile_metadata(connection)
            self.check_version(metadata["editor_version"], expected_editor_version)
            selected = self.selectable_revision(
                self.profile_revisions(connection, metadata), version
            )
            variant = selected["variant_id"]
            active_changed = False
            if action == "activate":
                if selected["active"]:
                    return current
                self.set_active_profile(connection, selected["id"])
                active_changed = True
            elif action == "rename":
                metadata["variants"][variant] = {"name": name}
            else:
                if current.get("variant_id") == variant:
                    raise StoreError("Сначала выберите другой вариант для поиска.", 409)
                metadata["variants"][variant] = {"name": selected["name"], "deleted": True}
            self.commit_profile_metadata(connection, metadata, current, active_changed)
            return self.profile_data(connection)

    def source_for_check(self, source_id, expected_version):
        with self.write() as connection:
            row = self.active_source(connection, source_id)
            self.check_version(row["version"], expected_version)
            now = time.time()
            checks = [
                value for value in self.setting(connection, "source_checks", []) if value > now - 60
            ]
            if len(checks) >= 5:
                raise StoreError("Не более пяти проверок источников в минуту.", 429)
            self.put_setting(connection, "source_checks", [*checks, now])
            profile = self.profile_data(connection)
            return self.source_data(row), profile

    def search_configuration(self):
        state = self.preferences()
        if not state["search_profile"]["configured"]:
            raise StoreError("Сначала настройте профиль поиска в разделе «Настройки поиска».", 409)
        rows = state["sources"]["items"]
        enabled = [row for row in rows if row["enabled"]]
        if not enabled or any(not row["valid"] for row in enabled):
            raise StoreError("Настройте и включите источники в разделе «Настройки поиска».", 409)
        return SearchSpec.model_validate(state["search_profile"]["profile"]).runtime(), enabled
