import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from qa_job_bot.pg_store import PostgresStore
from qa_job_bot.web_store import StoreError, WebStore


@dataclass(frozen=True)
class WebSettings:
    data_dir: Path = Path("data")
    origins: tuple[str, ...] = ("http://127.0.0.1:8000", "http://localhost:8000")
    frontend_dir: Path = Path("frontend/dist")

    @classmethod
    def from_env(cls):
        return cls(
            data_dir=Path(os.getenv("WEB_DATA_DIR", "data")).resolve(),
            origins=tuple(
                os.getenv("WEB_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000").split(",")
            ),
            frontend_dir=Path(os.getenv("WEB_FRONTEND_DIR", "frontend/dist")).resolve(),
        )

    @property
    def hosts(self):
        return {urlsplit(origin).netloc for origin in self.origins}


def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    key = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return f"{salt}:{key.hex()}"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Security:
    def __init__(self, store: WebStore | PostgresStore, settings: WebSettings):
        self.store = store
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        settings.data_dir.chmod(0o700)
        self.setup_file = settings.data_dir / "setup-token"
        if not store.get_setting("password") and not self.setup_file.exists():
            descriptor = os.open(self.setup_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(secrets.token_urlsafe(32))

    def setup(self, token: str, password: str) -> None:
        expected = self.setup_file.read_text().strip() if self.setup_file.exists() else ""
        if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
            raise StoreError("Неверный код первоначальной настройки.", 403)
        self.store.set_initial_password(password_hash(password))
        self.setup_file.unlink(missing_ok=True)

    def login(self, password: str, ip: str) -> None:
        self.store.record_login_attempt(ip, time.time())
        saved = self.store.get_setting("password")
        if not saved or not hmac.compare_digest(
            password_hash(password, saved.split(":")[0]), saved
        ):
            raise StoreError("Неверный пароль.", 401)
        self.store.clear_login_attempts(ip)

    def new_session(self) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = time.time()
        self.store.save_session(token_hash(token), csrf, now, now + 86400)
        return token, csrf

    def session(self, token: str) -> dict | None:
        return self.store.find_session(token_hash(token), time.time())

    def logout(self, token: str) -> None:
        self.store.delete_session(token_hash(token))

    def authorize(self, token: str, csrf: str | None = None) -> dict:
        session = self.session(token)
        if not session:
            raise StoreError("Войдите в приложение.", 401)
        if csrf is not None and not hmac.compare_digest(csrf.encode(), session["csrf"].encode()):
            raise StoreError("Проверка безопасности не пройдена. Обновите страницу.", 403)
        return session
