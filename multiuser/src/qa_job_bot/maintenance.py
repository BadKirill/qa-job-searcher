import argparse
import json
import os
import stat
from contextlib import suppress
from pathlib import Path

MARKER = ".maintenance"


def active(data_dir):
    try:
        (Path(data_dir) / MARKER).lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def set_mode(data_dir, enabled):
    root = Path(data_dir).absolute()
    if root.is_symlink():
        raise ValueError("Maintenance data root cannot be a symlink")
    marker = root / MARKER
    if enabled:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with suppress(FileExistsError):
            marker.mkdir(mode=0o700)
    if marker.exists() or marker.is_symlink():
        info = marker.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Maintenance marker must be a private owner directory")
        if not enabled:
            marker.rmdir()
    return {"maintenance": active(root)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("on", "off", "status"))
    parser.add_argument("--data-dir", type=Path, default=Path(os.getenv("WEB_DATA_DIR", "data")))
    arguments = parser.parse_args()
    result = (
        {"maintenance": active(arguments.data_dir)}
        if arguments.mode == "status"
        else set_mode(arguments.data_dir, arguments.mode == "on")
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
