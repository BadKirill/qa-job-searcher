import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path

TERMINAL = {"Success", "Failed", "TimedOut", "Cancelled"}
STATUS_FIELDS = ("Status", "StatusDetails", "ResponseCode")
EXECUTION_FIELDS = ("ExecutionStartDateTime", "ExecutionEndDateTime", "ExecutionElapsedTime")


def read_invocation(command_id, instance_id, timeout):
    try:
        result = subprocess.run(
            [
                "aws",
                "ssm",
                "get-command-invocation",
                f"--command-id={command_id}",
                f"--instance-id={instance_id}",
                "--output",
                "json",
                "--no-cli-pager",
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=timeout,
        )
        invocation = json.loads(result.stdout)
    except subprocess.CalledProcessError as failure:
        error = re.search(r"An error occurred \(([A-Za-z0-9_.-]+)\)", failure.stderr)
        return {"PollError": error[1] if error else f"AwsCliExit{failure.returncode}"}
    except subprocess.TimeoutExpired:
        return {"PollError": "AwsCliTimeout"}
    except json.JSONDecodeError:
        return {"PollError": "InvalidInvocationResponse"}
    if not isinstance(invocation, dict) or not isinstance(invocation.get("Status"), str):
        return {"PollError": "InvalidInvocationResponse"}
    return invocation


def log(event, **fields):
    print(json.dumps({"event": event, **fields}), flush=True)


def finish(report, invocation=None):
    outputs = {}
    if invocation is None:
        reason = "poll_timeout"
        message = "Inspect the existing SSM command before any retry. Its outcome is unknown."
    else:
        report.update({field: invocation.get(field) for field in EXECUTION_FIELDS})
        outputs["StandardOutputContent"] = invocation.get("StandardOutputContent")
        reason = "ssm_completed"
        message = "SSM reported a terminal result."
        if invocation["Status"] != "Success":
            reason = "ssm_terminal_failure"
            stderr = invocation.get("StandardErrorContent") or ""
            outputs["StandardErrorContent"] = stderr
            if "fcntl.flock" in stderr and "BlockingIOError: [Errno 11]" in stderr:
                reason = "shared_backup_deploy_lock_busy"
                message = (
                    "Release stopped before preparation because /run/qa-job-searcher-backup.lock "
                    "is busy. Check backup and deployment activity before retrying."
                )
    report.update(reason=reason, message=message)
    log("ssm_delivery_result", **report, **outputs)
    if summary := os.getenv("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as output:
            output.write(f"\n### SSM result\n\n```json\n{json.dumps(report, indent=2)}\n```\n")
    return 0 if report.get("Status") == "Success" else 1


def wait(command_id, instance_id, wait_seconds=1800):
    context = {"CommandId": command_id, "InstanceId": instance_id}
    started = time.monotonic()
    deadline = started + wait_seconds
    previous = {}
    attempts = 0
    report = dict(context, attempts=attempts, elapsed_seconds=0)
    while (remaining := deadline - time.monotonic()) > 0:
        attempts += 1
        invocation = read_invocation(command_id, instance_id, timeout=min(remaining, 20))
        if "PollError" in invocation:
            state = {**previous, "PollError": invocation["PollError"]}
        else:
            state = {field: invocation.get(field) for field in STATUS_FIELDS}
        report = dict(
            context, **state, attempts=attempts, elapsed_seconds=int(time.monotonic() - started)
        )
        if state != previous or attempts % 6 == 0:
            log("ssm_invocation_observed", **report)
        if invocation.get("Status") in TERMINAL:
            return finish(report, invocation)
        previous = state
        time.sleep(max(0, min(10, deadline - time.monotonic())))
    report["elapsed_seconds"] = int(time.monotonic() - started)
    return finish(report)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command_id")
    parser.add_argument("instance_id")
    args = parser.parse_args()
    raise SystemExit(wait(args.command_id, args.instance_id))


if __name__ == "__main__":
    main()
