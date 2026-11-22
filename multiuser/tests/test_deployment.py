import plistlib
from pathlib import Path

import pytest

from qa_job_bot import deployment


def test_launchd_private_loopback_worker_and_constant_domain(tmp_path, monkeypatch):
    binary = tmp_path / ".venv/bin/python"
    binary.parent.mkdir(parents=True)
    binary.write_text("binary fixture")
    monkeypatch.setattr(deployment.shutil, "which", lambda name: "/bin/ngrok")
    files = deployment.prepare_launchd(tmp_path, tmp_path / "output", "fixture.ngrok-free.app")
    assert len(files) == 3
    for file in files:
        assert file.stat().st_mode & 0o777 == 0o600
        data = plistlib.loads(file.read_bytes())
        assert data["KeepAlive"] and data["Umask"] == 0o077
        assert data["EnvironmentVariables"]["WEB_PORT"] == "8033"
        assert "https://fixture.ngrok-free.app" in data["EnvironmentVariables"]["WEB_ORIGINS"]
    tunnel = plistlib.loads(files[2].read_bytes())
    assert "--inspect=false" in tunnel["ProgramArguments"]
    assert "--url=https://fixture.ngrok-free.app" in tunnel["ProgramArguments"]


@pytest.mark.parametrize(
    "domain,port", [("bad/host", 8033), ("localhost", 8033), ("ok.test", 80), ("ok.test", 65536)]
)
def test_reject_invalid_deployment(tmp_path, domain, port):
    with pytest.raises(ValueError, match="hostname"):
        deployment.prepare_launchd(tmp_path, tmp_path / "output", domain, port)


def test_dependency_checks_and_cli(tmp_path, monkeypatch, capsys):
    with pytest.raises(ValueError, match="Install"):
        deployment.prepare_launchd(tmp_path, tmp_path / "output", "fixture.test")
    monkeypatch.setattr(deployment, "prepare_launchd", lambda *args: [Path("fixture.plist")])
    monkeypatch.setattr("sys.argv", ["deployment", "--domain", "fixture.test"])
    deployment.main()
    assert capsys.readouterr().out.strip() == "fixture.plist"
