import json

import pytest

from ci import package_release, reliability, results


@pytest.mark.parametrize("attribute", ["failures", "errors", "skipped", "tests"])
def test_pytest_rejects_incomplete_or_failed_runs(tmp_path, attribute):
    path = tmp_path / "backend.xml"
    path.write_text('<testsuites><testsuite tests="2"/></testsuites>')
    assert results.verify_pytest(path)["tests"] == 2
    attributes = {"tests": "2", attribute: "0" if attribute == "tests" else "1"}
    fields = " ".join(f'{key}="{value}"' for key, value in attributes.items())
    path.write_text(f"<testsuites><testsuite {fields}/></testsuites>")
    with pytest.raises(ValueError):
        results.verify_pytest(path)


@pytest.mark.parametrize("key", ["unexpected", "flaky", "skipped", "expected"])
def test_browser_results_fail_closed(tmp_path, key):
    path = tmp_path / "results.json"
    counts = {"expected": 25, "unexpected": 0, "flaky": 0, "skipped": 0}
    path.write_text(json.dumps({"stats": counts}))
    assert results.verify_playwright(path)["expected"] == 25
    counts[key] = 0 if key == "expected" else 1
    path.write_text(json.dumps({"stats": counts}))
    with pytest.raises(ValueError):
        results.verify_playwright(path)


@pytest.mark.parametrize(
    "key", ["numPassedTests", "numFailedTests", "numPendingTests", "numTodoTests"]
)
def test_component_results_reject_skips_and_failures(tmp_path, key):
    path = tmp_path / "vitest.json"
    path.write_text('{"numPassedTests":169}')
    assert results.verify_vitest(path)["numPassedTests"] == 169
    counts = {"numPassedTests": 169, key: 0 if key == "numPassedTests" else 1}
    path.write_text(json.dumps(counts))
    with pytest.raises(ValueError):
        results.verify_vitest(path)


def test_sarif_requires_report_and_blocks_high_findings(tmp_path):
    with pytest.raises(ValueError):
        results.verify_sarif(tmp_path)
    path = tmp_path / "python.sarif"
    data = {
        "runs": [
            {
                "tool": {
                    "driver": {
                        "rules": [{"id": "unsafe", "properties": {"security-severity": "6.9"}}]
                    }
                },
                "results": [{"ruleId": "unsafe"}],
            }
        ]
    }
    path.write_text(json.dumps(data))
    assert results.verify_sarif(tmp_path) == {"reports": 1, "high_or_critical": 0}
    data["runs"][0]["tool"]["driver"]["rules"][0]["properties"]["security-severity"] = "7"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        results.verify_sarif(tmp_path)


def test_result_cli_checks_all_levels(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("sys.argv", ["results", str(tmp_path)])
    monkeypatch.setattr(results, "verify_pytest", lambda path: {"tests": 1})
    monkeypatch.setattr(results, "verify_vitest", lambda path: {"numPassedTests": 1})
    monkeypatch.setattr(results, "verify_playwright", lambda path: {"expected": 1})
    results.main()
    assert set(json.loads(capsys.readouterr().out)) == {"backend", "component", "browser"}
    monkeypatch.setattr("sys.argv", ["results", "--sarif", str(tmp_path)])
    monkeypatch.setattr(results, "verify_sarif", lambda path: {"reports": 1})
    results.main()
    assert json.loads(capsys.readouterr().out)["reports"] == 1
    monkeypatch.setattr("sys.argv", ["results"])
    with pytest.raises(SystemExit):
        results.main()


def test_reliability_counts_first_attempts_without_rounding():
    success = {"status": "completed", "conclusion": "success", "run_attempt": 1}
    assert reliability.summarize([])["success_percent"] is None
    assert reliability.summarize([success])["success_percent"] == 100
    assert (
        reliability.summarize([success] * 99 + [dict(success, run_attempt=2)])["success_percent"]
        == 99
    )
    assert (
        reliability.summarize([success] * 98 + [dict(success, conclusion="failure")] * 2)[
            "success_percent"
        ]
        == 98
    )
    assert reliability.summarize([dict(success, status="in_progress")])["sample"] == 0
    assert reliability.summarize([success] * 101)["sample"] == 100


@pytest.mark.parametrize(
    ("status", "conclusion", "current_commit", "fails"),
    [
        ("completed", "success", "current", False),
        ("completed", "failure", "current", True),
        ("completed", "cancelled", "current", True),
        ("completed", "failure", "newer", False),
        ("in_progress", None, "current", False),
    ],
)
def test_reliability_cli_reports_history_and_only_fails_current_ci(
    monkeypatch, tmp_path, capsys, status, conclusion, current_commit, fails
):
    runs = [{"head_sha": "current", "status": status, "conclusion": conclusion}]
    responses = iter([json.dumps({"workflow_runs": runs}), json.dumps({"sha": current_commit})])
    monkeypatch.setattr(
        reliability.subprocess, "check_output", lambda *args, **kwargs: next(responses)
    )
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    if fails:
        with pytest.raises(SystemExit):
            reliability.main()
    else:
        reliability.main()
    assert "target_percent" in (tmp_path / "summary.md").read_text()
    report = json.loads(capsys.readouterr().out.splitlines()[0])
    assert report["current_commit"] == current_commit
    assert report["target_met"] == (conclusion == "success" and status == "completed")


def test_reliability_preserves_failure_history_after_current_ci_succeeds(
    monkeypatch, tmp_path, capsys
):
    runs = [
        {"head_sha": "current", "status": "completed", "conclusion": "success"},
        {"head_sha": "old", "status": "completed", "conclusion": "failure"},
    ]
    responses = iter([{"workflow_runs": runs}, {"sha": "current"}])
    monkeypatch.setattr(reliability, "fetch_json", lambda endpoint: next(responses))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    reliability.main()
    output = capsys.readouterr().out
    report = json.loads(output.splitlines()[0])
    assert report["success_percent"] == 50
    assert report["target_met"] is False
    assert "::warning" in output


def test_reliability_does_not_fabricate_an_empty_history(monkeypatch, tmp_path, capsys):
    responses = iter([{"workflow_runs": []}, {"sha": "current"}])
    monkeypatch.setattr(reliability, "fetch_json", lambda endpoint: next(responses))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    reliability.main()
    report = json.loads(capsys.readouterr().out.splitlines()[0])
    assert report["sample"] == 0
    assert report["success_percent"] is None
    assert report["current_status"] == "not_run"
    assert report["target_met"] is False


def test_release_manifest_pins_every_payload(monkeypatch, tmp_path):
    source, destination = tmp_path / "source", tmp_path / "release"
    (source / "multiuser").mkdir(parents=True)
    (source / "infra/aws").mkdir(parents=True)
    destination.mkdir()
    (source / "multiuser/compose.runtime.yaml").write_text("services: {}")
    (source / "infra/aws/Caddyfile").write_text("getcareer.app {}")
    (source / "infra/aws/compose.override.json").write_text(
        json.dumps(
            {
                "services": {
                    "api": {"image": "job-searcher-runtime:aws"},
                    "postgres": {"image": "postgres@sha256:pinned"},
                }
            }
        )
    )
    (destination / "runtime-image.tar.gz").write_bytes(b"synthetic-image")
    monkeypatch.setattr(
        package_release.subprocess,
        "check_output",
        lambda *args, **kwargs: json.dumps(
            [
                {
                    "Id": "sha256:" + "a" * 64,
                    "Architecture": "arm64",
                    "Os": "linux",
                    "RootFS": {"Layers": ["sha256:" + "c" * 64]},
                    "Config": {"Labels": {"org.opencontainers.image.revision": "b" * 40}},
                }
            ]
        ),
    )
    manifest = package_release.package(source, destination, "b" * 40)
    assert set(manifest["files"]) == {
        "runtime-image.tar.gz",
        "compose.runtime.yaml",
        "compose.aws.json",
        "Caddyfile",
    }
    assert (
        json.loads((destination / "compose.aws.json").read_text())["services"]["api"]["image"]
        == "job-searcher-runtime:" + "b" * 40
    )
    for name, digest in manifest["files"].items():
        assert package_release.digest(destination / name) == digest
    monkeypatch.setattr("sys.argv", ["package", "b" * 40, str(destination)])
    monkeypatch.setattr(package_release, "package", lambda *args: manifest)
    package_release.main()


@pytest.mark.parametrize("defect", ["architecture", "revision"])
def test_release_packaging_rejects_wrong_platform_or_source(tmp_path, monkeypatch, defect):
    metadata = {
        "Id": "sha256:" + "a" * 64,
        "Architecture": "amd64" if defect == "architecture" else "arm64",
        "Config": {"Labels": {"org.opencontainers.image.revision": "wrong"}},
    }
    monkeypatch.setattr(
        package_release.subprocess, "check_output", lambda *args, **kwargs: json.dumps([metadata])
    )
    with pytest.raises(ValueError, match="architecture or revision"):
        package_release.package(tmp_path, tmp_path / "release", "b" * 40)
