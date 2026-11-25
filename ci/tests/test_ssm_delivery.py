import json
import subprocess
from types import SimpleNamespace

import pytest

from ci import ssm_delivery

COMMAND = "13f7e078-e567-400c-8ae0-d3d0323eb0b4"
INSTANCE = "i-0123456789abcdef0"


def test_invocation_queries_only_the_existing_command(monkeypatch):
    response = {"Status": "Pending", "StatusDetails": "Pending", "ResponseCode": -1}

    def run(command, **options):
        assert command == [
            "aws",
            "ssm",
            "get-command-invocation",
            f"--command-id={COMMAND}",
            f"--instance-id={INSTANCE}",
            "--output",
            "json",
            "--no-cli-pager",
        ]
        assert options == {"capture_output": True, "check": True, "text": True, "timeout": 11}
        return SimpleNamespace(stdout=json.dumps(response))

    monkeypatch.setattr(ssm_delivery.subprocess, "run", run)
    assert ssm_delivery.read_invocation(COMMAND, INSTANCE, 11) == response


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        (
            "An error occurred (InvocationDoesNotExist) when calling the operation",
            "InvocationDoesNotExist",
        ),
        (
            "An error occurred (AccessDeniedException) when calling the operation",
            "AccessDeniedException",
        ),
        ("Could not connect to the endpoint URL", "AwsCliExit255"),
    ],
)
def test_poll_cli_reports_safe_error_codes(monkeypatch, stderr, code):
    def run(command, **options):
        raise subprocess.CalledProcessError(255, command, stderr=stderr)

    monkeypatch.setattr(ssm_delivery.subprocess, "run", run)
    assert ssm_delivery.read_invocation(COMMAND, INSTANCE, 8) == {"PollError": code}


def test_poll_cli_timeout_is_separate_from_command_status(monkeypatch):
    def run(command, **options):
        raise subprocess.TimeoutExpired(command, options["timeout"])

    monkeypatch.setattr(ssm_delivery.subprocess, "run", run)
    assert ssm_delivery.read_invocation(COMMAND, INSTANCE, 8) == {"PollError": "AwsCliTimeout"}


@pytest.mark.parametrize("stdout", ["", "[]", "{}", '{"Status":null}'])
def test_poll_cli_rejects_unusable_responses(monkeypatch, stdout):
    monkeypatch.setattr(
        ssm_delivery.subprocess, "run", lambda *args, **options: SimpleNamespace(stdout=stdout)
    )
    assert ssm_delivery.read_invocation(COMMAND, INSTANCE, 8) == {
        "PollError": "InvalidInvocationResponse"
    }


@pytest.fixture
def delivery_clock(monkeypatch):
    clock = SimpleNamespace(current=0, sleeps=[])

    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.current += seconds

    monkeypatch.setattr(
        ssm_delivery, "time", SimpleNamespace(monotonic=lambda: clock.current, sleep=sleep)
    )
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return clock


def events(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_delivery_logs_poll_errors_transitions_and_periodic_progress(
    monkeypatch, delivery_clock, capsys, tmp_path
):
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    pending = {"Status": "Pending", "StatusDetails": "Pending", "ResponseCode": -1}
    responses = iter(
        [
            {"PollError": "InvocationDoesNotExist"},
            *[pending] * 5,
            {"Status": "InProgress", "StatusDetails": "In Progress", "ResponseCode": -1},
            {
                "Status": "Success",
                "StatusDetails": "Success",
                "ResponseCode": 0,
                "ExecutionElapsedTime": "PT9S",
                "StandardOutputContent": "Deployed",
            },
        ]
    )
    monkeypatch.setattr(ssm_delivery, "read_invocation", lambda *args, **kwargs: next(responses))
    assert ssm_delivery.wait(COMMAND, INSTANCE) == 0
    records = events(capsys)
    assert [item["attempts"] for item in records] == [1, 2, 6, 7, 8, 8]
    assert records[0]["PollError"] == "InvocationDoesNotExist"
    assert "Status" not in records[0]
    assert records[-2]["Status"] == "Success"
    assert records[-1]["StandardOutputContent"] == "Deployed"
    assert records[-1]["ExecutionElapsedTime"] == "PT9S"
    assert records[-1]["elapsed_seconds"] == 70
    assert delivery_clock.sleeps == [10] * 7
    report = summary.read_text()
    assert COMMAND in report and INSTANCE in report and "PT9S" in report
    assert "Deployed" not in report


def test_terminal_lock_contention_reports_failure_before_preparation(
    monkeypatch, delivery_clock, capsys, tmp_path
):
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    stderr = "fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)\nBlockingIOError: [Errno 11]"
    response = {
        "Status": "Failed",
        "StatusDetails": "Failed",
        "ResponseCode": 1,
        "StandardErrorContent": stderr,
    }
    monkeypatch.setattr(ssm_delivery, "read_invocation", lambda *args, **kwargs: response)
    assert ssm_delivery.wait(COMMAND, INSTANCE) == 1
    result = events(capsys)[-1]
    assert result["reason"] == "shared_backup_deploy_lock_busy"
    assert "before preparation" in result["message"]
    assert result["StandardErrorContent"] == stderr
    assert result["ResponseCode"] == 1
    assert "Traceback" not in summary.read_text() and "fcntl" not in summary.read_text()


@pytest.mark.parametrize("status", ["Cancelled", "TimedOut", "Failed"])
def test_other_terminal_errors_keep_service_details(monkeypatch, delivery_clock, capsys, status):
    responses = iter(
        [
            {"Status": "Cancelling", "StatusDetails": "Cancelling", "ResponseCode": -1},
            {
                "Status": status,
                "StatusDetails": "Delivery Timed Out",
                "ResponseCode": -1,
                "StandardErrorContent": "Other failure",
            },
        ]
    )
    monkeypatch.setattr(ssm_delivery, "read_invocation", lambda *args, **kwargs: next(responses))
    assert ssm_delivery.wait(COMMAND, INSTANCE) == 1
    records = events(capsys)
    assert records[0]["Status"] == "Cancelling"
    assert records[-1]["reason"] == "ssm_terminal_failure"
    assert records[-1]["StatusDetails"] == "Delivery Timed Out"


def test_wait_timeout_keeps_last_command_state_and_latest_poll_error(
    monkeypatch, delivery_clock, capsys
):
    responses = iter(
        [
            {"Status": "InProgress", "StatusDetails": "In Progress", "ResponseCode": -1},
            {"PollError": "AccessDeniedException"},
        ]
    )
    budgets = []

    def read(command_id, instance_id, timeout):
        budgets.append(timeout)
        return next(responses)

    monkeypatch.setattr(ssm_delivery, "read_invocation", read)
    assert ssm_delivery.wait(COMMAND, INSTANCE, wait_seconds=14) == 1
    assert budgets == [14, 4]
    assert delivery_clock.sleeps == [10, 4]
    report = events(capsys)[-1]
    assert report["elapsed_seconds"] == 14
    assert report["Status"] == "InProgress"
    assert report["PollError"] == "AccessDeniedException"
    assert report["reason"] == "poll_timeout"
    assert "existing SSM command" in report["message"]


def test_main_passes_the_command_and_instance_and_preserves_exit_status(monkeypatch):
    def wait(command_id, instance_id):
        assert (command_id, instance_id) == (COMMAND, INSTANCE)
        return 1

    monkeypatch.setattr(ssm_delivery, "wait", wait)
    monkeypatch.setattr("sys.argv", ["ssm_delivery.py", COMMAND, INSTANCE])
    with pytest.raises(SystemExit) as exit_info:
        ssm_delivery.main()
    assert exit_info.value.code == 1
