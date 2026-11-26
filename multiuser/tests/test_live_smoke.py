import json
import sys
from uuid import uuid4

import pytest
from e2e_server import FixtureGateway
from sqlalchemy import text

from qa_job_bot import live_smoke as module
from qa_job_bot.agent_result import AgentResponse
from qa_job_bot.openai_config import OpenAIConfigurationError


class SmokeGateway(FixtureGateway):
    def response(self, value, context, role):
        return AgentResponse(
            value,
            {
                "session_id": "synthetic-" + uuid4().hex,
                "owner_id": context["owner_id"],
                "task_id": context["task_id"],
                "template_version": context["template"]["version"],
                "role": role,
            },
        )

    async def generate(self, context):
        return self.response(await super().generate(context), context, "generator")

    async def review(self, context, draft):
        result = await super().review(context, draft)
        if "nuclear" in json.dumps(draft):
            result.update(verdict="revise", issues=["Unsupported synthetic experience"])
        return self.response(result, context, "reviewer")

    async def visual_review(self, context, draft, rendered_reference):
        result = await super().visual_review(context, draft, rendered_reference)
        return self.response(result, context, "reviewer")


async def test_smoke_driver_checks_two_documents_sessions_downloads_and_a_negative_review(
    pg_database, tmp_path
):
    result = await module.exercise(pg_database, tmp_path, SmokeGateway())
    assert result == {
        "users": 2,
        "positive_documents": 2,
        "downloaded_kinds": ["cv", "cover_letter"],
        "negative_blocked": True,
        "distinct_agent_sessions": 7,
        "mcp": "stdio",
    }
    assert "provider" not in result


def test_smoke_assertions_fail_closed():
    module.require(True, "ok")
    with pytest.raises(RuntimeError, match="controlled failure"):
        module.require(False, "controlled failure")


@pytest.mark.parametrize(
    "error,category",
    [
        ("PRIVATE unsupported numbers", "unsupported_fact"),
        ("Employer name is not supported by PRIVATE", "unsupported_fact"),
        ("CV title repeated PRIVATE", "repeated_title"),
        ("Document title differs PRIVATE", "candidate_identity"),
        ("Invalid document structure PRIVATE", "document_schema"),
        ("Missing evidence PRIVATE", "evidence_references"),
        ("Проверка оформления PRIVATE", "visual_review"),
        (None, "blocked"),
        ("Private review wording", "blocked"),
    ],
)
def test_smoke_failure_categories_never_copy_document_or_reviewer_text(error, category):
    assert module.gate_failure("blocked", error) == category


async def test_live_command_requires_private_configuration_before_database_access(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        module.Database, "from_env", lambda: pytest.fail("Database must not be touched")
    )
    with pytest.raises(OpenAIConfigurationError):
        await module.run(tmp_path)


@pytest.mark.parametrize("failure", [False, True])
async def test_live_run_cleans_its_synthetic_schema_on_success_or_failure(
    pg_database, tmp_path, monkeypatch, failure
):
    monkeypatch.setenv("SMOKE_REPORT_DIR", str(tmp_path / "evidence"))
    monkeypatch.setattr(module.OpenAISettings, "load", lambda path: "synthetic-config")
    monkeypatch.setattr(module.Database, "from_env", lambda: pg_database)
    monkeypatch.setattr(module, "AgentGateway", lambda settings: "controlled-gateway")
    observed = []

    async def exercise(database, directory, gateway):
        observed.append(database.schema)
        assert gateway == "controlled-gateway" and directory.is_dir()
        if failure:
            raise RuntimeError("controlled provider failure")
        return {"users": 2}

    monkeypatch.setattr(module, "exercise", exercise)
    if failure:
        with pytest.raises(RuntimeError, match="provider failure"):
            await module.run(tmp_path)
    else:
        assert await module.run(tmp_path) == {"users": 2, "provider": "live_openai"}
    with pg_database.transaction() as connection:
        assert (
            connection.execute(
                text("SELECT nspname FROM pg_namespace WHERE nspname=:name"), {"name": observed[0]}
            ).first()
            is None
        )

    evidence = tmp_path / "evidence" / (observed[0] + ".json")
    saved = json.loads(evidence.read_text())
    assert saved["passed"] is not failure
    assert evidence.stat().st_mode & 0o777 == 0o600
    assert saved["usage_reservations"] == []


def test_smoke_cli_outputs_only_the_check_summary(tmp_path, monkeypatch, capsys):
    async def run(directory):
        assert directory == tmp_path
        return {"users": 2}

    monkeypatch.setattr(module, "run", run)
    monkeypatch.setattr(sys, "argv", ["smoke", "--credentials-dir", str(tmp_path)])
    module.main()
    assert json.loads(capsys.readouterr().out) == {"users": 2}
