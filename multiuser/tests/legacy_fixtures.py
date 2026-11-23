import asyncio
import shutil
import time
from pathlib import Path

from e2e_server import FixtureGateway, FixtureSearch

from qa_job_bot.document_files import parse_template
from qa_job_bot.legacy_snapshot import create_snapshot
from qa_job_bot.models import Job
from qa_job_bot.web import job_context
from qa_job_bot.web_security import WebSettings, password_hash, token_hash
from qa_job_bot.web_store import WebStore, encode
from qa_job_bot.worker import Worker

PASSWORD = "legacy-synthetic-password"


def seed_legacy(root):
    root = root.resolve()
    data, config = root / "old-data", root / "old-config"
    config.mkdir(parents=True)
    (config / "search_profile.toml").write_text('[profile]\nrole_keywords = ["qa engineer"]\n')
    (config / "job_boards.toml").write_text(
        '[[greenhouse]]\nname = "Synthetic Company"\ntoken = "synthetic"\n'
    )
    (config / "telegram_channels.toml").write_text('channels = ["synthetic_jobs"]\n')
    settings = WebSettings(data)
    store = WebStore(data / "jobs.sqlite3")
    store.initialize()
    jobs = [
        Job(
            "Synthetic legacy",
            str(i),
            title,
            "Legacy Company",
            "Europe",
            f"https://example.com/legacy/{i}",
            score=90 - i,
            description="We are looking for a quality engineer to test software applications "
            "and APIs. Build reliable software and improve test coverage.",
            match_reasons=["Historical personal score"],
        )
        for i, title in enumerate(
            ("Legacy Active QA", "Legacy Applied QA", "Legacy Archived QA", "Legacy Incomplete QA")
        )
    ]
    store.upsert_jobs(jobs)
    store.publish_search(jobs, [])
    store.change_state(jobs[1].fingerprint, {"applied": True})
    store.change_state(jobs[2].fingerprint, {"archived": True})
    store.change_state(jobs[3].fingerprint, {"applied": True})
    source = Path(__file__).parent / "fixtures" / "candidate.pdf"
    path = data / "templates" / "candidate.pdf"
    path.parent.mkdir()
    shutil.copyfile(source, path)
    template = store.add_template(
        {
            **parse_template(source.read_bytes(), "candidate.pdf"),
            "path": str(path),
            "filename": "candidate.pdf",
        }
    )
    template = store.activate_template(template["id"], None)
    conversation = store.conversation(job_id=jobs[0].fingerprint)
    store.add_message(conversation["id"], "user", "Confirmed historical instruction", "old-chat")
    store.add_message(conversation["id"], "assistant", "Preserved assistant reply", "old-chat")
    worker = Worker(store, settings, FixtureGateway(), FixtureSearch(store))
    documents = []
    for i, kind in enumerate(("cv", "cover_letter")):
        job = store.get_job(jobs[i].fingerprint)
        request = {
            "operation": "document",
            "job_id": job["id"],
            "kind": kind,
            "language": "en" if kind == "cv" else "auto",
        }
        task = store.enqueue(
            kind,
            f"legacy-{kind}",
            {
                "job_id": job["id"],
                "context": {"owner_id": "owner", "job": job_context(job), "template": template},
            },
            f"legacy-{kind}-request",
            request=request,
        )
        asyncio.run(worker.once())
        ready = store.task(task["id"], internal=True)
        assert ready["status"] == "ready", ready["error"]
        documents.append(ready)
    doubtful = store.enqueue("cv", "legacy-doubtful", documents[0]["payload"], "doubtful")
    original_artifact = store.artifact(documents[0]["artifacts"][0]["id"])
    doubtful_path = data / "artifacts" / doubtful["id"] / original_artifact["filename"]
    doubtful_path.parent.mkdir(parents=True)
    shutil.copyfile(original_artifact["path"], doubtful_path)
    doubtful_artifact = store.add_artifact(
        doubtful["id"], {**original_artifact, "path": str(doubtful_path)}
    )
    running = store.enqueue("cv", "legacy-running", documents[0]["payload"], "running")
    queued = store.enqueue("search", "search", {}, "legacy-search")
    store.set_setting("password", password_hash(PASSWORD))
    store.save_session(
        token_hash("legacy-valid-token"), "old-csrf", time.time(), time.time() + 3600
    )
    store.record_login_attempt("synthetic-old-ip", time.time())
    store.set_subscription(101, True)
    store.set_subscription(202, False)
    store.mark_delivered(101, jobs[:2])
    store.mark_delivered(202, jobs[2:])
    snapshot_id = store.list_jobs("active", 1)["snapshot_id"]
    with store._connect() as connection:
        connection.execute(
            "UPDATE jobs SET payload=NULL WHERE fingerprint=?", (jobs[3].fingerprint,)
        )
        connection.execute(
            "UPDATE web_tasks SET status='ready',result=? WHERE id=?",
            (encode({"verified": True}), doubtful["id"]),
        )
        connection.execute(
            "UPDATE web_tasks SET status='running',worker_id='old-worker',"
            "lease_until=?,attempts=1 WHERE id=?",
            (time.time() + 3600, running["id"]),
        )
        connection.execute(
            "INSERT INTO deliveries VALUES (?,?,?)",
            (303, "missing-legacy-job", "2026-01-01T10:00:00+00:00"),
        )
    (data / "setup-token").write_text("do-not-copy-setup-token")
    (data / ".env").write_text("SYNTHETIC_SECRET=do-not-copy")
    snapshot = create_snapshot(store.path, data, config, root / "snapshot")
    return {
        "store": store,
        "settings": settings,
        "config": config,
        "jobs": jobs,
        "template": template,
        "conversation": conversation,
        "documents": documents,
        "doubtful": doubtful,
        "doubtful_artifact": doubtful_artifact,
        "running": running,
        "queued": queued,
        "snapshot_id": snapshot_id,
        "snapshot": snapshot,
    }
