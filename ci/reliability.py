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


def fetch_json(endpoint):
    return json.loads(subprocess.check_output(["gh", "api", endpoint], text=True))


def main():
    runs = fetch_json(
        "repos/BadKirill/qa-job-searcher/actions/workflows/ci.yml/runs?branch=master&event=push&per_page=100"
    )["workflow_runs"]
    commit = fetch_json("repos/BadKirill/qa-job-searcher/commits/master")["sha"]
    current = next((run for run in runs if run["head_sha"] == commit), {})
    result = summarize(runs)
    result.update(
        current_commit=commit,
        current_status=current.get("status", "not_run"),
        current_conclusion=current.get("conclusion"),
        target_met=result["success_percent"] is not None
        and result["success_percent"] >= result["target_percent"],
    )
    print(json.dumps(result))
    Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(
        "## CI first-attempt reliability\n\n"
        + json.dumps(result, indent=2)
        + "\n\nLast 100 completed production-branch CI runs. Reruns count as failures. "
        "A small sample does not establish future reliability.\n\n"
        "This is a historical CI target, not a production availability check. "
        "Past failures remain in the window after fixes and successful deployments. "
        "Historical target breaches are reported as warnings without repeated failure emails. "
        "Only a failed completed CI run for the current master commit fails this report.\n"
    )
    if result["success_percent"] is not None and result["success_percent"] < 99:
        print(
            "::warning title=Historical CI target below 99%::"
            f"First-attempt reliability is {result['success_percent']:.2f}%; "
            "previous failures remain counted. This is not a new CI failure."
        )
    if current.get("status") == "completed" and current.get("conclusion") != "success":
        raise SystemExit("Latest CI for the current master commit is not successful")


if __name__ == "__main__":
    main()
