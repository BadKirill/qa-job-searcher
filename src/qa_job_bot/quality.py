import json
import os
import subprocess
import sys
from pathlib import Path


def check_coverage(report: Path) -> dict:
    totals = json.loads(report.read_text())["totals"]
    pairs = (
        ("lines", "covered_lines", "num_statements"),
        ("branches", "covered_branches", "num_branches"),
    )
    result = {}
    for name, covered, total in pairs:
        if totals[total] == 0 or totals[covered] * 100 < totals[total] * 90:
            raise ValueError(f"Backend {name} coverage is below 90%")
        result[name] = totals[covered] / totals[total] * 100
    return result


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    environment = {**os.environ, "PYTHONPATH": str(root / "src")}
    commands = (
        ([sys.executable, "-m", "ruff", "check", "."], root),
        (
            [
                sys.executable,
                "-m",
                "pytest",
                "--cov=qa_job_bot",
                "--cov-branch",
                "--cov-report=json:coverage.json",
                "--cov-report=xml:coverage.xml",
                "--cov-report=term-missing",
            ],
            root,
        ),
    )
    for command, directory in commands:
        subprocess.run(command, cwd=directory, env=environment, check=True)
    print(json.dumps(check_coverage(root / "coverage.json")))
    for script in ("build", "test:coverage", "test:e2e"):
        subprocess.run(["npm", "run", script], cwd=root / "frontend", env=environment, check=True)


if __name__ == "__main__":
    main()
