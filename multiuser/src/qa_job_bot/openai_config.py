import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path


class OpenAIConfigurationError(ValueError):
    pass


def private_text(path: Path, limit: int) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > limit
            ):
                raise OpenAIConfigurationError("OpenAI configuration requires a private owner file")
            raw = stream.read(limit + 1)
            if len(raw) > limit:
                raise OpenAIConfigurationError("OpenAI configuration exceeds the size limit")
            return raw.decode("utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise OpenAIConfigurationError("OpenAI configuration is missing or unreadable") from error


@dataclass(frozen=True)
class OpenAISettings:
    api_key: str = field(repr=False)
    agent_id: str
    model: str = "gpt-5.6-luna"
    reasoning_effort: str = "medium"
    project_id: str | None = None

    def __post_init__(self):
        if not re.fullmatch(r"sk-[A-Za-z0-9_-]{20,500}", self.api_key):
            raise OpenAIConfigurationError("OpenAI API key is missing or malformed")
        if not re.fullmatch(r"agent_[A-Za-z0-9]{5,128}", self.agent_id):
            raise OpenAIConfigurationError("OpenAI agent ID is malformed")
        if not re.fullmatch(r"gpt-[a-z0-9.-]{3,80}", self.model):
            raise OpenAIConfigurationError("OpenAI model is malformed")
        if self.reasoning_effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise OpenAIConfigurationError("OpenAI reasoning effort is invalid")
        if self.project_id is not None and not re.fullmatch(
            r"proj_[A-Za-z0-9_-]{5,128}", self.project_id
        ):
            raise OpenAIConfigurationError("OpenAI project ID is malformed")

    @classmethod
    def load(cls, data_dir: Path = Path("data")):
        try:
            config = json.loads(private_text(data_dir / "openai-agent.json", 4096))
            if not isinstance(config, dict) or set(config) - {
                "agent_id",
                "model",
                "reasoning_effort",
                "project_id",
            }:
                raise OpenAIConfigurationError("OpenAI agent configuration has unknown fields")
            return cls(api_key=private_text(data_dir / "openai-api-key", 1024), **config)
        except (TypeError, json.JSONDecodeError) as error:
            raise OpenAIConfigurationError("OpenAI agent configuration is invalid") from error
