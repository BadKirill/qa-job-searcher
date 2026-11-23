import json
from types import SimpleNamespace

import pytest

from ci import deploy

COMMIT = "a" * 40


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    root = tmp_path / "runtime"
    (root / ".runtime").mkdir(parents=True)
    for name in ("compose.runtime.yaml", "compose.aws.json"):
        (root / name).write_text("old-" + name)
    (root / ".runtime/runtime.env").write_text("WEB_ORIGINS=https://getcareer.app\n")
    monkeypatch.setattr(deploy, "ROOT", root)
    monkeypatch.setattr(deploy, "LOCK", tmp_path / "lock")
    monkeypatch.setattr(deploy, "CADDY", root / "Caddyfile")
    (root / "Caddyfile").write_text("old-Caddyfile")
    return root


@pytest.mark.parametrize(
    "value,length", [("../x", 40), ("A" * 40, 40), ("a" * 39, 40), ("g" * 64, 64)]
)
def test_release_identifiers_reject_shell_and_path_input(value, length):
    with pytest.raises(ValueError):
        deploy.validate_identifier(value, length)
    assert deploy.validate_identifier("a" * length, length) == "a" * length


@pytest.mark.parametrize(
    "defect",
    [
        None,
        "manifest_hash",
        "scope",
        "image_id",
        "file_hash",
        "architecture",
        "revision",
        "loaded_config",
        "old_manifest",
    ],
)
def test_prepare_checks_hashes_scope_architecture_and_source(runtime, monkeypatch, defect):
    image = {
        "Id": "sha256:" + "b" * 64,
        "Architecture": "arm64",
        "Os": "linux",
        "RootFS": {"Layers": ["sha256:" + "e" * 64]},
        "Config": {"Labels": {"org.opencontainers.image.revision": COMMIT}},
    }
    contents = {name: name.encode() for name in deploy.FILES}
    manifest = {
        "version": 2,
        "commit": COMMIT,
        "image_id": image["Id"],
        "image_fingerprint": deploy.fingerprint(image),
        "files": {
            name: __import__("hashlib").sha256(value).hexdigest()
            for name, value in contents.items()
        },
    }
    if defect == "old_manifest":
        manifest["version"] = 1
    if defect == "scope":
        manifest["commit"] = "c" * 40
    if defect == "image_id":
        manifest["image_id"] = "bad"
    if defect == "file_hash":
        manifest["files"]["compose.aws.json"] = "c" * 64
    contents["release.json"] = json.dumps(manifest).encode()
    expected = __import__("hashlib").sha256(contents["release.json"]).hexdigest()
    if defect == "manifest_hash":
        expected = "0" * 64

    def download(commit, name, directory):
        (directory / name).write_bytes(contents[name])

    monkeypatch.setattr(deploy, "download", download)
    if defect == "architecture":
        image["Architecture"] = "amd64"
    if defect == "revision":
        image["Config"]["Labels"] = {}
    image["Id"] = "sha256:" + "d" * 64
    if defect == "loaded_config":
        image["Config"]["User"] = "0"
    if defect in {"architecture", "revision"}:
        manifest["image_fingerprint"] = deploy.fingerprint(image)
        contents["release.json"] = json.dumps(manifest).encode()
        expected = __import__("hashlib").sha256(contents["release.json"]).hexdigest()
    monkeypatch.setattr(
        deploy, "run", lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps([image]))
    )
    if defect:
        with pytest.raises(ValueError):
            deploy.prepare(COMMIT, expected)
    else:
        assert deploy.prepare(COMMIT, expected).name == COMMIT


@pytest.mark.parametrize("failure", [None, "drain", "up", "schema"])
def test_activation_rollback_preserves_database_and_private_files(runtime, monkeypatch, failure):
    release = runtime / ".runtime/candidate"
    release.mkdir()
    for name in ("compose.runtime.yaml", "compose.aws.json", "Caddyfile"):
        (release / name).write_text("new-" + name)
    calls = []
    monkeypatch.setattr(deploy, "run", lambda *args, **kwargs: None)
    failed = False

    def dc(*args, **kwargs):
        nonlocal failed
        calls.append(args)
        if args[0] == "up" and failure in {"up", "schema"} and not failed:
            failed = True
            raise RuntimeError("candidate failed")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(deploy, "dc", dc)
    monkeypatch.setattr(
        deploy, "maintenance", lambda enabled: calls.append(("maintenance", enabled))
    )
    monkeypatch.setattr(
        deploy,
        "drain",
        lambda: (_ for _ in ()).throw(RuntimeError("active task")) if failure == "drain" else None,
    )
    monkeypatch.setattr(deploy, "healthy", lambda: None)
    monkeypatch.setattr(deploy, "ready", lambda: None)
    revisions = iter(["0007", "0008" if failure == "schema" else "0007"])
    monkeypatch.setattr(deploy, "revision", lambda: next(revisions))
    if failure:
        with pytest.raises(RuntimeError):
            deploy.activate(release)
        assert (
            (runtime / "compose.aws.json")
            .read_text()
            .startswith("new-" if failure == "schema" else "old-")
        )
        if failure == "schema":
            assert calls[-1][0] == "stop"
        else:
            assert ("maintenance", False) in calls
    else:
        deploy.activate(release)
        assert (runtime / "compose.aws.json").read_text().startswith("new-")
    assert (runtime / ".runtime/runtime.env").read_text() == "WEB_ORIGINS=https://getcareer.app\n"
    assert all("restore" not in args and "--volumes" not in args for args in calls)


def test_backup_and_activation_sequence_under_shared_lock(runtime, monkeypatch):
    calls = []
    monkeypatch.setattr(deploy, "prepare", lambda *args: runtime)
    monkeypatch.setattr(deploy, "dc", lambda *args: calls.append("validate"))
    monkeypatch.setattr(
        deploy,
        "run",
        lambda command, **kwargs: calls.append(("backup", kwargs["env"]["QA_BACKUP_LOCK_HELD"])),
    )
    monkeypatch.setattr(deploy, "activate", lambda directory: calls.append("activate"))
    deploy.deploy(COMMIT, "b" * 64)
    assert calls == ["validate", ("backup", "1"), "activate"]
    assert (runtime / ".runtime/deployed-commit").read_text().strip() == COMMIT
    monkeypatch.setattr("sys.argv", ["deploy", COMMIT, "b" * 64])
    deploy.main()


def test_command_arguments_and_runtime_health(runtime, monkeypatch):
    calls = []
    monkeypatch.setattr(
        deploy.subprocess,
        "run",
        lambda command, **kwargs: (
            calls.append((command, kwargs)) or SimpleNamespace(stdout="0007\n")
        ),
    )
    assert deploy.revision() == "0007"
    deploy.download(COMMIT, "release.json", runtime)
    deploy.maintenance(True)
    deploy.maintenance(False)
    deploy.drain()
    assert all(kwargs["check"] is True for _, kwargs in calls)
    assert any("s3://" in str(command) for command, _ in calls)
    healthy = "\n".join(
        json.dumps({"Service": service, "Health": "healthy"})
        for service in ["postgres", *deploy.SERVICES]
    )
    monkeypatch.setattr(deploy, "dc", lambda *args, **kwargs: SimpleNamespace(stdout=healthy))
    deploy.healthy()
    monkeypatch.setattr(deploy, "dc", lambda *args, **kwargs: SimpleNamespace(stdout=""))
    with pytest.raises(RuntimeError):
        deploy.healthy()


@pytest.mark.parametrize("status", [200, 503])
def test_internal_readiness_uses_exact_allowed_host(runtime, monkeypatch, status):
    calls = []
    connection = SimpleNamespace(
        request=lambda *args, **kwargs: calls.append((args, kwargs)),
        getresponse=lambda: SimpleNamespace(status=status),
        close=lambda: calls.append("closed"),
    )
    monkeypatch.setattr(deploy.http.client, "HTTPConnection", lambda *args, **kwargs: connection)
    if status == 200:
        deploy.ready()
    else:
        with pytest.raises(RuntimeError):
            deploy.ready()
    assert calls[0][1]["headers"] == {"Host": "getcareer.app"}
    assert calls[-1] == "closed"


def test_configuration_replacement_limits_read_access(runtime, monkeypatch):
    source = runtime / "candidate"
    source.write_text("verified configuration")
    calls = []
    monkeypatch.setattr(deploy, "run", lambda command: calls.append(command))
    deploy.replace(source, deploy.CADDY)
    assert deploy.CADDY.read_text() == "verified configuration"
    assert deploy.CADDY.stat().st_mode & 0o777 == 0o640
    assert calls == [["chown", "root:caddy", str(deploy.CADDY.with_suffix(".deploying"))]]
    destination = runtime / "compose.aws.json"
    deploy.replace(source, destination)
    assert destination.stat().st_mode & 0o777 == 0o600
