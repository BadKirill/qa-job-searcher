import json
import subprocess

import pytest

from qa_job_bot import quality


@pytest.mark.parametrize(
    "lines,branches,valid", [(90, 90, True), (89, 100, False), (100, 89, False)]
)
def test_coverage_independent_thresholds(tmp_path, lines, branches, valid):
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "totals": {
                    "covered_lines": lines,
                    "num_statements": 100,
                    "covered_branches": branches,
                    "num_branches": 100,
                }
            }
        )
    )
    if valid:
        assert quality.check_coverage(report) == {"lines": lines, "branches": branches}
    else:
        with pytest.raises(ValueError, match="below 90"):
            quality.check_coverage(report)


def test_unrounded_and_empty_coverage_cannot_pass(tmp_path):
    report = tmp_path / "coverage.json"
    for covered, total in ((8999, 10000), (0, 0)):
        report.write_text(
            json.dumps({"totals": {"covered_lines": covered, "num_statements": total}})
        )
        with pytest.raises(ValueError):
            quality.check_coverage(report)


def test_gate_executes_every_level_and_stops_on_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(quality.subprocess, "run", lambda command, **kwargs: calls.append(command))
    monkeypatch.setattr(quality, "check_coverage", lambda path: {"lines": 99, "branches": 98})
    quality.main()
    assert len(calls) == 5
    assert calls[-1] == ["npm", "run", "test:e2e"]
    assert "--cov-branch" in calls[1]

    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(quality.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        quality.main()
