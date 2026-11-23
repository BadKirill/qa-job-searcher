import time
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from qa_job_bot import pg_schema as s
from qa_job_bot.pg_access import OwnedRepository, serialize
from qa_job_bot.search_models import (
    AGGREGATORS,
    SearchSpec,
    SourceSpec,
    search_presets,
    source_identity,
    universal_profile,
)
from qa_job_bot.source_catalog import LABELS, LIMITATIONS
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
                in ({"handle"} if kind == "telegram" else {"name", "token", "region", "tags"})
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
        if row is None:
            return {"version": 0, "configured": False, "profile": None}
        return {
            "version": row["version"],
            "configured": True,
            "profile": universal_profile(row["payload"]),
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

    def update_source(self, source_id, expected_version, enabled=None, config=None):
        with self.write() as connection:
            row = self.active_source(connection, source_id)
            self.check_version(row["version"], expected_version)
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

    def save_profile_in(self, connection, spec, expected_version):
        current = self.profile_data(connection)
        self.check_version(current["version"], expected_version)
        connection.execute(
            update(s.search_profiles).where(self.owned(s.search_profiles)).values(active=False)
        )
        connection.execute(
            s.search_profiles.insert().values(
                id=str(uuid4()),
                user_id=self.user_id,
                version=expected_version + 1,
                active=True,
                payload=spec.model_dump(),
            )
        )
        self.touch(connection)
        return self.profile_data(connection)

    def save_search_profile(self, spec, expected_version):
        with self.write() as connection:
            return self.save_profile_in(connection, spec, expected_version)

    def search_profile_history(self):
        with self.read() as connection:
            return [
                {"version": row["version"], "active": row["active"], "profile": row["payload"]}
                for row in connection.execute(
                    select(s.search_profiles)
                    .where(self.owned(s.search_profiles))
                    .order_by(s.search_profiles.c.version)
                ).mappings()
            ]

    def apply_search_preset(self, name, expected_version, expected_sources_version):
        presets = search_presets()
        if name not in presets:
            raise StoreError("Пресет не найден.", 404)
        with self.write() as connection:
            self.check_version(
                self.setting(connection, "sources_version", 0), expected_sources_version
            )
            self.save_profile_in(
                connection, SearchSpec.model_validate(presets[name]), expected_version
            )
            kinds = {row["kind"] for row in self.source_rows(connection)}
            for kind in AGGREGATORS:
                if kind not in kinds:
                    self.add_source_in(connection, SourceSpec(kind=kind))
            if set(AGGREGATORS) - kinds:
                self.bump_sources(connection)
            return self.preferences_in(connection)

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
            raise StoreError("Сначала настройте профиль поиска в разделе «Поиск и источники».", 409)
        rows = state["sources"]["items"]
        enabled = [row for row in rows if row["enabled"]]
        if not enabled or any(not row["valid"] for row in enabled):
            raise StoreError("Настройте и включите источники в разделе «Поиск и источники».", 409)
        return SearchSpec.model_validate(state["search_profile"]["profile"]).runtime(), enabled
