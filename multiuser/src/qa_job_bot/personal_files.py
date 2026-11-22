import hashlib
from pathlib import Path

from qa_job_bot.document_files import DocumentFileError


def checked_path(root, path):
    root, path = Path(root).absolute(), Path(path).absolute()
    if not path.is_relative_to(root) or ".." in path.relative_to(root).parts:
        raise DocumentFileError("Путь вне личного хранилища.")
    current = root
    if current.is_symlink():
        raise DocumentFileError("Ссылка вместо каталога хранилища.")
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise DocumentFileError("Ссылки в личном хранилище запрещены.")
    return path


def owner_root(settings, user_id=None):
    root = settings.data_dir.absolute()
    if user_id is None:
        return root
    if not isinstance(user_id, str) or not user_id.strip():
        raise DocumentFileError("Владелец хранилища не задан.")
    return checked_path(root, root / "users" / hashlib.sha256(user_id.encode()).hexdigest())


def private_directory(settings, kind, user_id=None, task_id=None):
    root = settings.data_dir.absolute()
    path = owner_root(settings, user_id) / kind
    if task_id is not None:
        if not task_id or Path(task_id).name != task_id or task_id in {".", ".."}:
            raise DocumentFileError("Некорректный идентификатор каталога.")
        path /= task_id
    checked_path(root, path)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        current.mkdir(exist_ok=True, mode=0o700)
    return path


def allowed_file(settings, path, kind, user_id=None, task_id=None):
    path = Path(path).absolute()
    roots = [owner_root(settings, user_id) / kind]
    if user_id is not None:
        roots.append(settings.data_dir.absolute() / kind)
    for root in roots:
        if task_id is not None:
            root /= task_id
        if path.is_relative_to(root):
            checked_path(settings.data_dir, path)
            if not path.is_file():
                raise DocumentFileError("Личный файл недоступен.")
            return path
    raise DocumentFileError("Путь файла не принадлежит владельцу.")
