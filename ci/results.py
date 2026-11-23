import argparse
import hashlib
import json
import re
from datetime import date
from pathlib import Path

REVIEWED_BANDIT = Path(__file__).with_name("bandit-reviewed.json")
REVIEWED_CODEQL = Path(__file__).with_name("codeql-reviewed.json")


def verify_pytest(path):
    from defusedxml import ElementTree as ET

    suites = ET.parse(path).getroot().iter("testsuite")
    counts = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
    for suite in suites:
        for key in counts:
            counts[key] += int(suite.get(key, "0"))
    if counts["tests"] < 1 or any(counts[key] for key in ("failures", "errors", "skipped")):
        raise ValueError(f"All backend tests must pass without skips: {counts}")
    return counts


def verify_playwright(path):
    stats = json.loads(Path(path).read_text())["stats"]
    if stats["expected"] < 1 or any(stats[key] for key in ("unexpected", "flaky", "skipped")):
        raise ValueError(f"All browser tests must pass without retries or skips: {stats}")
    return stats


def verify_vitest(path):
    stats = json.loads(Path(path).read_text())
    if stats["numPassedTests"] < 1 or any(
        stats.get(key, 0) for key in ("numFailedTests", "numPendingTests", "numTodoTests")
    ):
        raise ValueError("All component tests must pass without skips")
    return {
        key: stats.get(key, 0) for key in ("numPassedTests", "numFailedTests", "numPendingTests")
    }


def reviewed_codeql(finding, rule_id, reviews):
    locations = finding.get("locations", [])
    if len(locations) != 1:
        return False
    location = locations[0]["physicalLocation"]
    return any(
        review["rule_id"] == rule_id
        and review["filename"] == location["artifactLocation"]["uri"]
        and review["line"] == location["region"]["startLine"]
        and date.fromisoformat(review["expires"]) >= date.today()
        and all(
            hashlib.sha256(Path(name).read_bytes()).hexdigest() == digest
            for name, digest in review["files"].items()
        )
        for review in reviews
    )


def verify_sarif(directory, reviewed_path=REVIEWED_CODEQL):
    reviews = json.loads(Path(reviewed_path).read_text())
    files = list(Path(directory).rglob("*.sarif"))
    if not files:
        raise ValueError("CodeQL reports missing")
    findings = []
    for path in files:
        for run in json.loads(path.read_text())["runs"]:
            rules = {
                rule["id"]: rule
                for component in [run["tool"]["driver"], *run["tool"].get("extensions", [])]
                for rule in component.get("rules", [])
            }
            for finding in run.get("results", []):
                rule_id = finding.get("ruleId") or finding["rule"]["id"]
                if rule_id not in rules:
                    raise ValueError("CodeQL result has no rule metadata: " + rule_id)
                severity = float(rules[rule_id].get("properties", {}).get("security-severity", 0))
                if severity >= 7 and not reviewed_codeql(finding, rule_id, reviews):
                    findings.append(rule_id)
    if findings:
        raise ValueError(f"High/critical CodeQL findings: {findings}")
    return {"reports": len(files), "high_or_critical": 0}


def verify_bandit(path, reviewed_path=REVIEWED_BANDIT):
    reviews = json.loads(Path(reviewed_path).read_text())
    unreviewed = []
    for item in json.loads(Path(path).read_text())["results"]:
        code = re.sub(r"^\d+\s", "", item["code"], flags=re.MULTILINE)
        digest = hashlib.sha256(code.encode()).hexdigest()
        allowed = any(
            review["test_id"] == item["test_id"]
            and review["filename"] == item["filename"]
            and review["code_sha256"] == digest
            and date.fromisoformat(review["expires"]) >= date.today()
            for review in reviews
        )
        if not allowed:
            unreviewed.append(item["filename"] + ":" + item["test_id"])
    if unreviewed:
        raise ValueError(f"Unreviewed high-confidence Bandit findings: {unreviewed}")
    return {"reviewed_findings": len(reviews), "unreviewed_findings": 0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path, nargs="?")
    parser.add_argument("--sarif", type=Path)
    parser.add_argument("--bandit", type=Path)
    args = parser.parse_args()
    if args.sarif:
        print(json.dumps(verify_sarif(args.sarif)))
        return
    if args.bandit:
        print(json.dumps(verify_bandit(args.bandit)))
        return
    if args.directory is None:
        parser.error("directory or --sarif is required")
    print(
        json.dumps(
            {
                "backend": verify_pytest(args.directory / "test-results/backend.xml"),
                "component": verify_vitest(
                    args.directory / "frontend/coverage/vitest-results.json"
                ),
                "browser": verify_playwright(args.directory / "frontend/test-results/results.json"),
            }
        )
    )


if __name__ == "__main__":
    main()
