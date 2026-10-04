from pathlib import Path

import pytest

from qa_job_bot.config import (
    SearchProfile,
    Settings,
    load_boards,
    load_profile,
    load_telegram_channels,
)


def test_missing_configuration_uses_defaults(tmp_path: Path) -> None:
    missing = tmp_path / "missing.toml"
    assert load_profile(missing) == SearchProfile()
    assert load_boards(missing) == []
    assert load_telegram_channels(missing) == ()


def test_environment_defaults_and_token_requirement(monkeypatch: pytest.MonkeyPatch) -> None:
    names = (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_USER_IDS",
        "DATABASE_PATH",
        "SEARCH_PROFILE_PATH",
        "JOB_BOARDS_PATH",
        "TELEGRAM_CHANNELS_PATH",
        "SEARCH_INTERVAL_MINUTES",
        "HTTP_TIMEOUT_SECONDS",
        "MAX_SEARCH_RESULTS",
        "MAX_REVIEW_RESULTS",
        "MAX_ALERTS_PER_RUN",
        "LOG_LEVEL",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN is required"):
        Settings.from_env()
    settings = Settings.from_env(require_token=False)
    assert settings.telegram_bot_token == ""
    assert settings.allowed_user_ids == frozenset()
    assert settings.database_path == Path("data/jobs.sqlite3")
    assert settings.search_interval_minutes == 180
    assert settings.http_timeout_seconds == 25


def test_environment_limits_and_user_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "TELEGRAM_BOT_TOKEN": " token ",
        "TELEGRAM_ALLOWED_USER_IDS": " 1, ,2,1 ",
        "DATABASE_PATH": "/tmp/test.sqlite3",
        "SEARCH_PROFILE_PATH": "profile.toml",
        "JOB_BOARDS_PATH": "boards.toml",
        "TELEGRAM_CHANNELS_PATH": "channels.toml",
        "SEARCH_INTERVAL_MINUTES": "0",
        "HTTP_TIMEOUT_SECONDS": "1.5",
        "MAX_SEARCH_RESULTS": "0",
        "MAX_REVIEW_RESULTS": "-1",
        "MAX_ALERTS_PER_RUN": "-8",
        "LOG_LEVEL": "debug",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    assert settings.telegram_bot_token == "token"
    assert settings.allowed_user_ids == frozenset({1, 2})
    assert settings.search_interval_minutes == 5
    assert settings.http_timeout_seconds == 1.5
    assert (
        settings.max_search_results
        == settings.max_review_results
        == settings.max_alerts_per_run
        == 1
    )
    assert settings.log_level == "DEBUG"
    assert settings.telegram_channels_path == Path("channels.toml")
    assert settings.search_profile_path == Path("profile.toml")
    assert settings.job_boards_path == Path("boards.toml")
    assert settings.database_path == Path("/tmp/test.sqlite3")


@pytest.mark.parametrize("value", ["alice", "1,2,no", "1.2"])
def test_invalid_user_ids_are_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", value)
    with pytest.raises(ValueError, match="numeric IDs"):
        Settings.from_env(require_token=False)


@pytest.mark.parametrize("value", ['"qa"', '["qa", 1]', "1", "true"])
def test_invalid_profile_lists_are_rejected(tmp_path: Path, value: str) -> None:
    path = tmp_path / "profile.toml"
    path.write_text(f"[profile]\nrole_keywords = {value}\n")
    with pytest.raises(ValueError, match="role_keywords must be a list of strings"):
        load_profile(path)


def test_profile_normalizes_terms_and_preserves_explicit_flags(tmp_path: Path) -> None:
    path = tmp_path / "profile.toml"
    path.write_text(
        '[profile]\nrole_keywords = [" QA ", "", "  ", "ТЕСТ"]\n'
        "require_remote = false\nallow_unknown_remote_location = true\nmin_score = 99\n"
    )
    profile = load_profile(path)
    assert profile.role_keywords == ("qa", "тест")
    assert not profile.require_remote
    assert profile.allow_unknown_remote_location
    assert profile.min_score == 99


def test_board_defaults_and_european_metadata(tmp_path: Path) -> None:
    path = tmp_path / "boards.toml"
    path.write_text(
        '[[lever]]\nname="Acme"\ntoken="acme"\nregion="eu"\ntags=["AI", "QA"]\n'
        '[[ashby]]\nname="Other"\ntoken="other"\n'
    )
    lever, ashby = load_boards(path)
    assert (lever.name, lever.region, lever.tags) == ("Acme", "eu", ("AI", "QA"))
    assert (ashby.region, ashby.tags) == ("global", ())


@pytest.mark.parametrize("value", ['"qa_jobs"', "[1]", '["short"]', '["qa-jobs"]', '["a"]'])
def test_invalid_channels_are_rejected(tmp_path: Path, value: str) -> None:
    path = tmp_path / "channels.toml"
    if value == '["short"]':
        value = '["' + ("a" * 33) + '"]'
    path.write_text(f"channels = {value}\n")
    with pytest.raises(ValueError, match="channels|username"):
        load_telegram_channels(path)


def test_channels_deduplicate_case_and_accept_boundary_lengths(tmp_path: Path) -> None:
    path = tmp_path / "channels.toml"
    longest = "a" * 32
    path.write_text(f'channels = ["@QAjob", "qajob", "{longest}"]\n')
    assert load_telegram_channels(path) == ("QAjob", longest)


def test_empty_configuration_sections_are_accepted(tmp_path: Path) -> None:
    path = tmp_path / "empty.toml"
    path.write_text("")
    assert load_profile(path) == SearchProfile()
    assert load_boards(path) == []
    assert load_telegram_channels(path) == ()
