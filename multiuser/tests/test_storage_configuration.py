import runpy

import pytest
from fastapi.testclient import TestClient

from qa_job_bot import pg_database as pg_module
from qa_job_bot import storage
from qa_job_bot.legacy_cli import bootstrap_owner
from qa_job_bot.web import create_app
from qa_job_bot.web_security import WebSettings
from qa_job_bot.web_store import StoreError, WebStore


def configure_database(monkeypatch, database):
    monkeypatch.setenv("DATABASE_URL", database.engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("DATABASE_SCHEMA", database.schema)


def test_legacy_configuration_is_explicitly_preserved_until_import(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    store = storage.open_store(WebSettings(tmp_path))
    assert isinstance(store, WebStore)
    assert store.path == tmp_path / "jobs.sqlite3"
    storage.close_store(store)


def test_postgresql_configuration_requires_explicit_existing_owner(
    monkeypatch, pg_database, tmp_path
):
    configure_database(monkeypatch, pg_database)
    monkeypatch.delenv("WEB_OWNER_ID", raising=False)
    with pytest.raises(ValueError, match="WEB_OWNER_ID"):
        storage.open_store(WebSettings(tmp_path))
    monkeypatch.setenv("WEB_OWNER_ID", "missing")
    with pytest.raises(StoreError):
        storage.open_store(WebSettings(tmp_path))


def test_configured_web_uses_postgresql_and_closes_owned_pool(monkeypatch, pg_database, tmp_path):
    configure_database(monkeypatch, pg_database)
    bootstrap_owner(pg_database, "owner", "Owner")
    monkeypatch.delenv("WEB_OWNER_ID", raising=False)
    settings = WebSettings(tmp_path, ("http://testserver",))
    app = create_app(settings)
    assert app.state.store is None and app.state.accounts is not None
    closed = []
    original = app.state.database.close

    def close():
        closed.append(True)
        original()

    monkeypatch.setattr(app.state.database, "close", close)
    with TestClient(app, headers={"origin": "http://testserver"}) as client:
        assert client.get("/api/session").json()["setup_required"]
        response = client.post(
            "/api/setup",
            json={
                "token": app.state.security.setup_file.read_text(),
                "password": "configured-postgres-password",
            },
        )
        assert response.status_code == 200
        assert client.get("/api/jobs").json()["total"] == 0
    assert closed == [True]
    assert not (tmp_path / "jobs.sqlite3").exists()


def test_migration_cli_uses_configured_database_without_printing_credentials(
    monkeypatch, pg_database, capsys
):
    configure_database(monkeypatch, pg_database)
    pg_module.main()
    runpy.run_module("qa_job_bot.pg_database", run_name="__main__")
    output = capsys.readouterr().out
    assert output.count(f"Database revision: {pg_module.REVISION}") == 2
    assert "local-test-only" not in output
