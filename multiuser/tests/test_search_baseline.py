import pytest
from baseline_fixtures import BASELINE, baseline_job, baseline_profile
from fastapi.testclient import TestClient

from qa_job_bot.matching import match_job
from qa_job_bot.service import JobSearchService
from qa_job_bot.web import create_app
from qa_job_bot.web_security import WebSettings
from qa_job_bot.web_store import WebStore


class BaselineSource:
    name = "Synthetic search baseline"

    def __init__(self, case):
        self.case = case

    async def fetch(self, client):
        return [baseline_job(self.case)]


@pytest.mark.parametrize("case", BASELINE["cases"], ids=lambda case: case["id"])
def test_baseline_matching_records_current_behavior(case):
    result = match_job(baseline_job(case), baseline_profile(case["profile"]))
    assert result.accepted == case["baseline"]["accepted"]
    assert result.location_status == case["baseline"]["location_status"]
    assert result.reasons
    if result.accepted:
        assert 35 <= result.score <= 100


@pytest.mark.parametrize("profile_name", BASELINE["profiles"])
async def test_baseline_search_survives_storage_and_api_round_trip(tmp_path, profile_name):
    cases = [case for case in BASELINE["cases"] if case["profile"] == profile_name]
    store = WebStore(tmp_path / "jobs.sqlite3")
    store.initialize()
    service = JobSearchService(
        baseline_profile(profile_name), [BaselineSource(case) for case in cases], store, 1
    )
    report = await service.refresh()
    expected = {case["id"] for case in cases if case["baseline"]["accepted"]}
    assert {job.source_id for job in report.jobs} == expected
    assert not report.errors
    store.publish_search(report.jobs, report.errors)
    reopened = WebStore(store.path)
    app = create_app(WebSettings(tmp_path, ("http://testserver",)), reopened)
    with TestClient(app, headers={"origin": "http://testserver"}) as client:
        assert client.get("/api/jobs").status_code == 401
        response = client.post(
            "/api/setup",
            json={
                "token": app.state.security.setup_file.read_text(),
                "password": "synthetic-baseline-password",
            },
        )
        assert response.status_code == 200
        client.headers["x-csrf-token"] = response.json()["csrf_token"]
        listing = client.get("/api/jobs")
        assert listing.status_code == 200
        payload = listing.json()
        assert {job["source_id"] for job in payload["items"]} == expected
        for saved in payload["items"]:
            original = next(job for job in report.jobs if job.source_id == saved["source_id"])
            assert saved["id"] == original.fingerprint
            assert saved["score"] == original.score
            assert saved["match_reasons"] == original.match_reasons
        selected = payload["items"][0]["id"]
        assert (
            client.patch(f"/api/jobs/{selected}/state", json={"applied": True}).status_code == 200
        )
        assert reopened.get_job(selected)["applied_at"]
        snapshot = client.get(f"/api/jobs?snapshot_id={payload['snapshot_id']}")
        assert snapshot.status_code == 200
        assert snapshot.json()["total"] == len(expected) - 1
