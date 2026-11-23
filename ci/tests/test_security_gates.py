import hashlib
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ci import results, verify_public


def test_sarif_gate_runs_without_installed_application_dependencies(tmp_path):
    (tmp_path / "actions.sarif").write_text('{"runs": [{"tool": {"driver": {}}}]}')
    completed = subprocess.run(
        [sys.executable, "-S", results.__file__, "--sarif", str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {"reports": 1, "high_or_critical": 0}


@pytest.mark.parametrize("defect", [None, "test_id", "filename", "code_sha256", "expires"])
def test_bandit_reviews_are_specific_and_expire(tmp_path, defect):
    finding = {"test_id": "B310", "filename": "operations.py", "code": "10 urlopen(request)\n"}
    review = {
        "test_id": "B310",
        "filename": "operations.py",
        "code_sha256": hashlib.sha256(b"urlopen(request)\n").hexdigest(),
        "expires": "2999-01-01",
    }
    if defect:
        review[defect] = "2000-01-01" if defect == "expires" else "different"
    report, reviews = tmp_path / "report.json", tmp_path / "reviews.json"
    report.write_text(json.dumps({"results": [finding]}))
    reviews.write_text(json.dumps([review]))
    if defect:
        with pytest.raises(ValueError):
            results.verify_bandit(report, reviews)
    else:
        assert results.verify_bandit(report, reviews)["unreviewed_findings"] == 0


def test_bandit_cli_uses_explicit_report(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["results", "--bandit", str(tmp_path / "report")])
    monkeypatch.setattr(results, "verify_bandit", lambda path: {"unreviewed_findings": 0})
    results.main()


@pytest.mark.parametrize("status", [200, 503])
def test_public_probe_verifies_status_and_closes_connection(monkeypatch, status):
    calls = []
    connection = SimpleNamespace(
        request=lambda *args: calls.append(args),
        getresponse=lambda: SimpleNamespace(status=status),
        close=lambda: calls.append("closed"),
    )
    monkeypatch.setattr(
        verify_public.http.client, "HTTPSConnection", lambda *args, **kwargs: connection
    )
    if status == 200:
        assert verify_public.check("getcareer.app", "/", 200)["status"] == 200
    else:
        with pytest.raises(RuntimeError):
            verify_public.check("getcareer.app", "/", 200)
    assert calls[-1] == "closed"


def test_public_matrix_includes_private_api_and_admin_routes(monkeypatch):
    calls = []
    monkeypatch.setattr(
        verify_public, "check", lambda host, path, status: calls.append((host, path, status))
    )
    verify_public.main()
    assert ("api.getcareer.app", "/api/login", 404) in calls
    assert ("getcareer.app", "/api/admin/users", 401) in calls
    assert ("getcareer.app", "/api/setup", 404) in calls
    assert ("getcareer.app", "/api/session", 200) in calls


@pytest.mark.parametrize("reference", [{"ruleId": "unsafe"}, {"rule": {"id": "unsafe"}}])
def test_sarif_extension_metadata_blocks_high_security_results(tmp_path, reference):
    report = {
        "runs": [
            {
                "tool": {
                    "driver": {},
                    "extensions": [
                        {"rules": [{"id": "unsafe", "properties": {"security-severity": "7.5"}}]}
                    ],
                },
                "results": [reference],
            }
        ]
    }
    (tmp_path / "python.sarif").write_text(json.dumps(report))
    with pytest.raises(ValueError, match="High/critical"):
        results.verify_sarif(tmp_path)
    report["runs"][0]["tool"]["extensions"] = []
    (tmp_path / "python.sarif").write_text(json.dumps(report))
    with pytest.raises(ValueError, match="no rule metadata"):
        results.verify_sarif(tmp_path)


@pytest.mark.parametrize("defect", [None, "expired", "code", "configuration", "location"])
def test_codeql_review_is_exact_and_expires(tmp_path, defect):

    reviews = json.loads(results.REVIEWED_CODEQL.read_text())
    review = reviews[0]
    finding = {
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": review["filename"]},
                    "region": {"startLine": review["line"]},
                }
            }
        ]
    }
    assert results.reviewed_codeql(finding, review["rule_id"], reviews)
    assert not results.reviewed_codeql({}, review["rule_id"], reviews)
    if defect == "expired":
        review["expires"] = "2020-01-01"
    if defect in {"code", "configuration"}:
        name = "ci/deploy.py" if defect == "code" else "infra/aws/Caddyfile"
        review["files"][name] = "0" * 64
    if defect == "location":
        finding["locations"][0]["physicalLocation"]["region"]["startLine"] += 1
    path = tmp_path / "reviewed.sarif"
    path.write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "tool": {
                            "driver": {
                                "rules": [
                                    {
                                        "id": review["rule_id"],
                                        "properties": {"security-severity": "7.8"},
                                    }
                                ]
                            }
                        },
                        "results": [{**finding, "ruleId": review["rule_id"]}],
                    }
                ]
            }
        )
    )
    review_path = tmp_path / "reviews.json"
    review_path.write_text(json.dumps(reviews))
    if defect:
        with pytest.raises(ValueError, match="High/critical"):
            results.verify_sarif(tmp_path, review_path)
    else:
        assert results.verify_sarif(tmp_path, review_path)["high_or_critical"] == 0
