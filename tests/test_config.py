from pathlib import Path

from qa_job_bot.config import load_boards, load_profile, load_telegram_channels


def test_load_project_config() -> None:
    profile = load_profile(Path("config/search_profile.toml"))
    boards = load_boards(Path("config/job_boards.toml"))
    channels = load_telegram_channels(Path("config/telegram_channels.toml"))
    assert "qa lead" in profile.role_keywords
    assert profile.require_remote
    assert {board.kind for board in boards} == {"greenhouse", "ashby", "lever"}
    assert any(board.token == "Lendable" for board in boards)
    assert "canada" not in profile.allowed_locations
    assert "poland" not in profile.allowed_locations
    assert "kazakhstan" not in profile.allowed_locations
    assert "europe" in profile.allowed_locations
    assert "emea" in profile.allowed_locations
    assert "serbia" in profile.allowed_locations
    assert "junior" in profile.excluded_title_keywords
    assert "middle" in profile.preferred_seniority
    assert not profile.allow_unknown_remote_location
    assert len(channels) == 13
    assert "opento_dev" in channels
    assert "holder_job_devs" in channels
