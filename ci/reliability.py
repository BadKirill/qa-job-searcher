import json
import os
import subprocess
from pathlib import Path


def summarize(runs):
    completed = [run for run in runs if run["status"] == "completed"][:100]
    successes = sum(
        run["conclusion"] == "success" and run.get("run_attempt", 1) == 1 for run in completed
    )
    return {
        "sample": len(completed),
        "first_attempt_successes": successes,
        "success_percent": successes * 100 / len(completed) if completed else None,
        "target_percent": 99,
    }


def main():
    raw = subprocess.check_output(
        [
            "gh",
            "api",
            "repos/BadKirill/qa-job-searcher/actions/workflows/ci.yml/runs?branch=master&event=push&per_page=100",
        ],
        text=True,
    )
    result = summarize(json.loads(raw)["workflow_runs"])
    print(json.dumps(result))
    Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(
        "## CI first-attempt reliability\n\n"
        + json.dumps(result, indent=2)
        + "\n\nLast 100 completed production-branch CI runs. Reruns count as failures. "
        "A small sample does not establish future reliability.\n"
    )
    if result["success_percent"] is not None and result["success_percent"] < 99:
        raise SystemExit("CI reliability below 99%; investigate first-attempt failures")


if __name__ == "__main__":
    main()
