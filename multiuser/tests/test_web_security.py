from pathlib import Path

import pytest

from qa_job_bot.web_security import Security, WebSettings, password_hash, token_hash
from qa_job_bot.web_store import StoreError, WebStore


@pytest.fixture
def security(tmp_path: Path) -> Security:
    store = WebStore(tmp_path / "state.sqlite3")
    store.initialize()
    return Security(store, WebSettings(data_dir=tmp_path / "private"))


def configure(security: Security) -> str:
    password = "a sufficiently long private passphrase"
    security.setup(security.setup_file.read_text(), password)
    return password


def test_web_settings_env_and_default_hosts(monkeypatch, tmp_path: Path) -> None:
    for key in ("WEB_DATA_DIR", "WEB_FRONTEND_DIR", "WEB_ORIGINS"):
        monkeypatch.delenv(key, raising=False)
    defaults = WebSettings.from_env()
    assert defaults.data_dir == Path("data").resolve()
    assert defaults.frontend_dir == Path("frontend/dist").resolve()
    assert defaults.hosts == {"127.0.0.1:8000", "localhost:8000"}
    monkeypatch.setenv("WEB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_FRONTEND_DIR", str(tmp_path / "dist"))
    monkeypatch.setenv("WEB_ORIGINS", "https://jobs.example,http://localhost:9000")
    custom = WebSettings.from_env()
    assert custom.data_dir == tmp_path
    assert custom.frontend_dir == tmp_path / "dist"
    assert custom.hosts == {"jobs.example", "localhost:9000"}


def test_password_hash_uses_salt_and_token_hash_is_one_way() -> None:
    encoded = password_hash("private")
    salt, value = encoded.split(":")
    assert len(salt) == 32
    assert len(value) == 128
    assert password_hash("private", salt) == encoded
    assert password_hash("different", salt) != encoded
    assert password_hash("private") != encoded
    assert token_hash("token") != "token"
    assert len(token_hash("token")) == 64
    assert token_hash("token") == token_hash("token")


def test_setup_token_is_private_stable_and_consumed(security: Security) -> None:
    original = security.setup_file.read_text()
    assert len(original) >= 32
    assert security.setup_file.stat().st_mode & 0o777 == 0o600
    restarted = Security(security.store, security.settings)
    assert restarted.setup_file.read_text() == original
    password = configure(restarted)
    assert not restarted.setup_file.exists()
    stored = restarted.store.get_setting("password")
    assert stored != password
    assert stored == password_hash(password, stored.split(":")[0])
    assert not Security(security.store, security.settings).setup_file.exists()
    with pytest.raises(StoreError) as error:
        restarted.setup(original, "replacement")
    assert error.value.status == 403


def test_wrong_setup_token_preserves_initialization_and_existing_password(
    security: Security,
) -> None:
    original = security.setup_file.read_text()
    with pytest.raises(StoreError) as error:
        security.setup("wrong", "password")
    assert error.value.status == 403
    assert security.store.get_setting("password") is None
    security.store.set_setting("password", "already-configured")
    with pytest.raises(StoreError) as error:
        security.setup(original, "replacement")
    assert error.value.status == 409
    assert security.store.get_setting("password") == "already-configured"


def test_login_requires_setup_and_valid_password_then_clears_attempts(security: Security) -> None:
    with pytest.raises(StoreError) as error:
        security.login("password", "client")
    assert error.value.status == 401
    password = configure(security)
    with pytest.raises(StoreError) as error:
        security.login("wrong", "client")
    assert error.value.status == 401
    security.login(password, "client")
    with security.store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM web_login_attempts").fetchone()[0] == 0


def test_tenth_failed_login_is_allowed_then_rate_limited_and_recovers_after_window(
    security: Security,
    monkeypatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr("qa_job_bot.web_security.time.time", lambda: clock[0])
    password = configure(security)
    for _ in range(10):
        with pytest.raises(StoreError) as error:
            security.login("wrong", "limited")
        assert error.value.status == 401
    with pytest.raises(StoreError) as error:
        security.login(password, "limited")
    assert error.value.status == 429
    security.login(password, "other-client")
    clock[0] += 901
    security.login(password, "limited")


def test_sessions_require_token_and_matching_csrf_and_logout_is_idempotent(
    security: Security,
) -> None:
    token, csrf = security.new_session()
    other, other_csrf = security.new_session()
    assert token != other and csrf != other_csrf
    row = security.authorize(token)
    assert row["token_hash"] == token_hash(token)
    assert row["csrf"] == csrf
    assert security.authorize(token, csrf) == row
    for invalid in ("", "wrong", "неверный", other_csrf):
        with pytest.raises(StoreError) as error:
            security.authorize(token, invalid)
        assert error.value.status == 403
    with pytest.raises(StoreError) as error:
        security.authorize("unknown", csrf)
    assert error.value.status == 401
    security.logout(token)
    security.logout(token)
    assert security.session(token) is None
    assert security.session(other) is not None


def test_sessions_expire_exactly_at_boundary_and_cleanup(security: Security, monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr("qa_job_bot.web_security.time.time", lambda: clock[0])
    token, _ = security.new_session()
    clock[0] += 86399
    assert security.session(token) is not None
    clock[0] += 1
    assert security.session(token) is None
    clock[0] += 1
    security.new_session()
    with security.store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0] == 1
