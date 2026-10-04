import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from qa_job_bot.web_store import StoreError, WebStore, encode


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
    def __init__(self, store: WebStore, settings: WebSettings):
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
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM web_settings WHERE key='password'").fetchone():
                raise StoreError("Первоначальная настройка уже завершена.", 409)
            db.execute(
                "INSERT INTO web_settings VALUES ('password',?)", (encode(password_hash(password)),)
            )
        self.setup_file.unlink(missing_ok=True)

    def login(self, password: str, ip: str) -> None:
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM web_login_attempts WHERE created_at<?", (time.time() - 900,))
            attempts = db.execute(
                "SELECT COUNT(*) FROM web_login_attempts WHERE ip=?", (ip,)
            ).fetchone()[0]
            if attempts >= 10:
                raise StoreError("Слишком много попыток. Подождите 15 минут.", 429)
            db.execute("INSERT INTO web_login_attempts VALUES (?,?)", (ip, time.time()))
        saved = self.store.get_setting("password")
        if not saved or not hmac.compare_digest(
            password_hash(password, saved.split(":")[0]), saved
        ):
            raise StoreError("Неверный пароль.", 401)
        with self.store._connect() as db:
            db.execute("DELETE FROM web_login_attempts WHERE ip=?", (ip,))

    def new_session(self) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.store._connect() as db:
            db.execute("DELETE FROM web_sessions WHERE expires_at<?", (time.time(),))
            db.execute(
                "INSERT INTO web_sessions VALUES (?,?,?)",
                (token_hash(token), csrf, time.time() + 86400),
            )
        return token, csrf

    def session(self, token: str) -> dict | None:
        with self.store._connect() as db:
            row = db.execute(
                "SELECT * FROM web_sessions WHERE token_hash=? AND expires_at>?",
                (token_hash(token), time.time()),
            ).fetchone()
        return dict(row) if row else None

    def logout(self, token: str) -> None:
        with self.store._connect() as db:
            db.execute("DELETE FROM web_sessions WHERE token_hash=?", (token_hash(token),))

    def authorize(self, token: str, csrf: str | None = None) -> dict:
        session = self.session(token)
        if not session:
            raise StoreError("Войдите в приложение.", 401)
        if csrf is not None and not hmac.compare_digest(csrf.encode(), session["csrf"].encode()):
            raise StoreError("Проверка безопасности не пройдена. Обновите страницу.", 403)
        return session
