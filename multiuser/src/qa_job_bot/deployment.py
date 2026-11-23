import argparse
import os
import plistlib
import re
import shutil
from pathlib import Path


def prepare_launchd(root: Path, output: Path, domain: str, port: int = 8033) -> list[Path]:
    root = root.resolve()
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]+\.[a-z]+", domain) or not 1024 <= port <= 65535:
        raise ValueError("Provide a hostname and an unprivileged TCP port")
    python = root / ".venv" / "bin" / "python"
    ngrok = shutil.which("ngrok")
    if not python.is_file() or not ngrok:
        raise ValueError("Install the project's virtual environment and ngrok first")
    logs = root / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {
        "HOME": str(Path.home()),
        "PATH": os.defpath,
        "PYTHONPATH": str(root / "src"),
        "PYTHONUNBUFFERED": "1",
        "WEB_DATA_DIR": str(root / "data"),
        "WEB_FRONTEND_DIR": str(root / "frontend" / "dist"),
        "WEB_PORT": str(port),
        "WEB_ORIGINS": f"http://127.0.0.1:{port},http://localhost:{port},https://{domain}",
    }
    commands = {
        "web": [str(python), "-m", "qa_job_bot.web"],
        "worker": [str(python), "-m", "qa_job_bot.worker"],
        "tunnel": [
            ngrok,
            "http",
            str(port),
            f"--url=https://{domain}",
            "--inspect=false",
            "--log=stdout",
            "--log-level=warn",
        ],
    }
    files = []
    for name, command in commands.items():
        label = f"com.qajobsearcher.{name}"
        payload = {
            "Label": label,
            "ProgramArguments": command,
            "WorkingDirectory": str(root),
            "EnvironmentVariables": environment,
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "Umask": 0o077,
            "StandardOutPath": str(logs / f"{name}.log"),
            "StandardErrorPath": str(logs / f"{name}.error.log"),
        }
        target = output / f"{label}.plist"
        target.write_bytes(plistlib.dumps(payload))
        target.chmod(0o600)
        files.append(target)
    return files


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", required=True)
    parser.add_argument("--port", type=int, default=8033)
    parser.add_argument("--output", type=Path, default=Path(".runtime/launchd"))
    args = parser.parse_args()
    for path in prepare_launchd(
        Path(__file__).resolve().parents[2], args.output, args.domain, args.port
    ):
        print(path)


if __name__ == "__main__":
    main()
