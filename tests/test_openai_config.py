import json
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from qa_job_bot.openai_config import OpenAIConfigurationError, OpenAISettings, private_text


@pytest.fixture
def configured(tmp_path):
    key = tmp_path / "openai-api-key"
    key.write_text("sk-" + "synthetic" * 4 + "\n")
    key.chmod(0o600)
    config = tmp_path / "openai-agent.json"
    config.write_text(json.dumps({"agent_id": "agent_synthetic", "project_id": "proj_synthetic"}))
    config.chmod(0o600)
    return tmp_path


def test_load_private_key_and_selected_model(configured, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")
    settings = OpenAISettings.load(configured)
    assert settings.api_key == "sk-" + "synthetic" * 4
    assert settings.model == "gpt-5.6-luna"
    assert settings.reasoning_effort == "medium"
    assert settings.project_id == "proj_synthetic"
    assert settings.api_key not in repr(settings)
    assert replace(settings, project_id=None).project_id is None


def test_load_private_files_from_container_mounts(configured, monkeypatch):
    other = configured / "other"
    other.mkdir()
    key = other / "key"
    key.write_text("sk-" + "mounted" * 4)
    key.chmod(0o600)
    config = other / "agent.json"
    config.write_text(json.dumps({"agent_id": "agent_mounted", "project_id": "proj_mounted"}))
    config.chmod(0o600)
    monkeypatch.setenv("OPENAI_API_KEY_FILE", str(key))
    monkeypatch.setenv("OPENAI_AGENT_CONFIG_FILE", str(config))
    settings = OpenAISettings.load(configured)
    assert settings.api_key == key.read_text()
    assert settings.agent_id == "agent_mounted"
    assert settings.project_id == "proj_mounted"


@pytest.mark.parametrize("name", ["OPENAI_API_KEY_FILE", "OPENAI_AGENT_CONFIG_FILE"])
@pytest.mark.parametrize("problem", ["missing", "public", "symlink"])
def test_container_file_override_fails_closed(configured, monkeypatch, name, problem):
    target = configured / f"{name.lower()}.txt"
    if problem == "missing":
        pass
    elif problem == "symlink":
        target.symlink_to(configured / "openai-api-key")
    else:
        target.write_text("secret")
        target.chmod(0o644)
    monkeypatch.setenv(name, str(target))
    with pytest.raises(OpenAIConfigurationError):
        OpenAISettings.load(configured)


@pytest.mark.parametrize(
    "field,value",
    [
        ("api_key", ""),
        ("api_key", "sk-short"),
        ("api_key", "sk-" + "x" * 501),
        ("agent_id", "../secrets"),
        ("model", "https://other.example"),
        ("reasoning_effort", "ultra"),
        ("project_id", "invalid"),
    ],
)
def test_reject_invalid_configuration(configured, field, value):
    settings = OpenAISettings.load(configured)
    with pytest.raises(OpenAIConfigurationError):
        replace(settings, **{field: value})


@pytest.mark.parametrize("content", ["[]", "{", "{}", '{"api_key":"secret"}', '{"agent_id":null}'])
def test_bad_json_and_schema(configured, content):
    (configured / "openai-agent.json").write_text(content)
    with pytest.raises(OpenAIConfigurationError):
        OpenAISettings.load(configured)


@pytest.mark.parametrize("name", ["openai-api-key", "openai-agent.json"])
@pytest.mark.parametrize(
    "problem", ["missing", "public", "symlink", "large", "binary", "directory"]
)
def test_reject_unreadable_or_exposed_config(configured, name, problem):
    path = configured / name
    if problem == "public":
        path.chmod(0o644)
    elif problem == "large":
        path.write_text("x" * 5000)
    elif problem == "binary":
        path.write_bytes(b"\xff")
    else:
        path.unlink()
        if problem == "symlink":
            path.symlink_to(configured / "other")
        elif problem == "directory":
            path.mkdir(mode=0o700)
    with pytest.raises(OpenAIConfigurationError):
        OpenAISettings.load(configured)


def test_owner_and_bounded_read(configured, monkeypatch):
    path = configured / "openai-api-key"
    actual = path.stat()
    monkeypatch.setattr(
        "qa_job_bot.openai_config.os.fstat",
        lambda fd: SimpleNamespace(
            st_mode=actual.st_mode,
            st_uid=os.getuid() + 1,
            st_size=actual.st_size,
        ),
    )
    with pytest.raises(OpenAIConfigurationError):
        private_text(path, 1000)
    monkeypatch.setattr(
        "qa_job_bot.openai_config.os.fstat",
        lambda fd: SimpleNamespace(
            st_mode=actual.st_mode,
            st_uid=os.getuid(),
            st_size=0,
        ),
    )
    with pytest.raises(OpenAIConfigurationError, match="size limit"):
        private_text(path, 1)
