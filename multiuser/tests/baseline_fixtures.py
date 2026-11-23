import json
from pathlib import Path

from qa_job_bot.config import SearchProfile
from qa_job_bot.matching import rank_jobs
from qa_job_bot.models import Job

BASELINE = json.loads((Path(__file__).parent / "fixtures" / "search-baseline.json").read_text())


def baseline_profile(name: str) -> SearchProfile:
    return SearchProfile(
        **{
            key: tuple(value) if isinstance(value, list) else value
            for key, value in BASELINE["profiles"][name].items()
        }
    )


def baseline_job(case: dict) -> Job:
    return Job(
        source="Synthetic search baseline",
        source_id=case["id"],
        company=f"Synthetic {case['id']}",
        url=f"https://example.com/baseline/{case['id']}",
        **case["job"],
    )


def seed_baseline_jobs(store) -> list[Job]:
    jobs = [
        job
        for case in BASELINE["cases"]
        for job in rank_jobs([baseline_job(case)], baseline_profile(case["profile"]))
    ]
    store.upsert_jobs(jobs)
    store.publish_search(jobs, [])
    return jobs
