import json
import sys

import pytest

from qa_job_bot import cutover as module
from qa_job_bot import pg_schema as s
from qa_job_bot.maintenance import set_mode
from qa_job_bot.pilot import Pilot
from qa_job_bot.web_store import StoreError


@pytest.fixture
def baseline(pg_store, tmp_path):
    with pg_store.write() as connection:
        connection.execute(
            s.users.update().where(s.users.c.id == pg_store.user_id).values(role="admin")
        )
        connection.execute(
            s.import_runs.insert().values(
                id="import-baseline",
                user_id=pg_store.user_id,
                source_hash="a" * 64,
                payload={"target_dir": str(tmp_path)},
            )
        )
    Pilot(pg_store.database).begin(pg_store.user_id)
    set_mode(tmp_path, True)
    (tmp_path / "synthetic.txt").write_text("Synthetic imported file")
    return module.Cutover(pg_store.database, tmp_path)


def test_import_boundary_is_idempotent_and_controls_rollback_without_touching_original_data(
    baseline,
):
    assert baseline.record(services_stopped=True)["recorded"]
    assert baseline.record(services_stopped=True)["recorded"]
    assert baseline.rollback_status() == {
        "database_changed": False,
        "files_changed": False,
        "maintenance": True,
        "matches_import_baseline": True,
        "legacy_restart_allowed": False,
        "preserve_postgres_and_files": False,
    }
    assert baseline.rollback_status(services_stopped=True)["legacy_restart_allowed"]
    set_mode(baseline.data_dir, False)
    assert not baseline.rollback_status(services_stopped=True)["legacy_restart_allowed"]


@pytest.mark.parametrize("change", ["database", "file", "both"])
def test_new_user_progress_blocks_legacy_restart_and_cannot_reset_the_original_boundary(
    baseline, pg_store, change
):
    baseline.record(services_stopped=True)
    if change in {"database", "both"}:
        pg_store.set_setting("new-user-progress", {"PRIVATE": "Do not discard this value"})
    if change in {"file", "both"}:
        (baseline.data_dir / "new-file.pdf").write_bytes(b"New synthetic user document")
    status = baseline.rollback_status(services_stopped=True)
    assert not status["legacy_restart_allowed"] and status["preserve_postgres_and_files"]
    assert status["database_changed"] is (change in {"database", "both"})
    assert status["files_changed"] is (change in {"file", "both"})
    with pytest.raises(StoreError, match="не перезаписывается"):
        baseline.record(services_stopped=True)
    assert "PRIVATE" not in json.dumps(status)


def test_operational_heartbeat_and_pilot_control_changes_do_not_erase_the_boundary(baseline):
    baseline.record(services_stopped=True)
    with baseline.database.transaction() as connection:
        connection.execute(
            s.runtime_control.insert().values(key="service:heartbeat", payload={"at": 123})
        )
    assert baseline.rollback_status(services_stopped=True)["legacy_restart_allowed"]


def test_boundary_requires_maintenance_and_stopped_services(baseline):
    with pytest.raises(StoreError):
        baseline.record()
    set_mode(baseline.data_dir, False)
    with pytest.raises(StoreError):
        baseline.record(services_stopped=True)


def test_boundary_requires_current_schema_matching_import_and_no_running_tasks(
    baseline, pg_store, monkeypatch
):
    with pg_store.write() as connection:
        connection.execute(
            s.tasks.insert().values(
                id="running",
                user_id=pg_store.user_id,
                kind="search",
                scope="active",
                request_id="active",
                status="running",
                payload={},
            )
        )
    with pytest.raises(StoreError, match="незавершённые"):
        baseline.record(services_stopped=True)
    with pg_store.write() as connection:
        connection.execute(s.tasks.delete())
        connection.execute(s.import_runs.delete())
    with pytest.raises(StoreError, match="импорт"):
        baseline.record(services_stopped=True)
    original = module.fingerprint
    monkeypatch.setattr(module, "fingerprint", lambda *args: {**original(*args), "revision": "old"})
    with pytest.raises(StoreError, match="миграция"):
        baseline.record(services_stopped=True)


def test_missing_or_unrelated_data_root_never_gets_a_green_rollback_status(baseline, tmp_path):
    with pytest.raises(StoreError, match="не зафиксирована"):
        baseline.rollback_status()
    baseline.record(services_stopped=True)
    with pytest.raises(StoreError, match="тот же каталог"):
        module.Cutover(baseline.database, tmp_path / "other").rollback_status(services_stopped=True)


def test_boundary_fingerprint_rejects_files_that_change_during_observation(baseline, monkeypatch):
    inventories = [{"file": {"sha256": "before"}}, {"file": {"sha256": "after"}}]
    monkeypatch.setattr(module, "inventory", lambda path: inventories.pop(0))
    with pytest.raises(StoreError, match="изменились"):
        module.fingerprint(baseline.database, baseline.data_dir)


@pytest.mark.parametrize("command", ["record", "rollback-status", "missing"])
def test_cutover_cli_reports_only_counts_flags_and_safe_errors(
    baseline, monkeypatch, capsys, command
):
    monkeypatch.setattr(module.Database, "from_env", lambda: baseline.database)
    if command != "record" and command != "missing":
        baseline.record(services_stopped=True)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cutover",
            "rollback-status" if command == "missing" else command,
            "--data-dir",
            str(baseline.data_dir),
            "--services-stopped",
        ],
    )
    if command == "missing":
        with pytest.raises(SystemExit) as result:
            module.main()
        assert result.value.code == 1
    else:
        module.main()
    value = json.loads(capsys.readouterr().out)
    assert "data_root" not in value and "database_hash" not in value
