import copy
import runpy
import sys
from pathlib import Path

import pytest

from ci.image_identity import FIELDS, fingerprint


def image():
    return {
        "Id": "sha256:" + "a" * 64,
        "Architecture": "arm64",
        "Os": "linux",
        "RootFS": {"Layers": ["sha256:" + "b" * 64, "sha256:" + "c" * 64]},
        "Config": {"User": "10001:10001", "Labels": {"revision": "tested"}},
    }


def test_identity_survives_docker_serialization_without_losing_configuration():
    original = image()
    loaded = copy.deepcopy(original)
    loaded["Id"] = "sha256:" + "d" * 64
    loaded["Created"] = "engine-normalized-metadata"
    for key, default in FIELDS.items():
        loaded["Config"].setdefault(key, default)
    assert fingerprint(original) == fingerprint(loaded)


@pytest.mark.parametrize("field", list(FIELDS))
def test_every_runtime_configuration_change_rejects_image(field):
    original = image()
    changed = copy.deepcopy(original)
    changed["Config"][field] = "changed-runtime-value"
    assert fingerprint(original) != fingerprint(changed)


@pytest.mark.parametrize("field", ["Architecture", "Os", "layers"])
def test_platform_and_ordered_layer_content_are_bound(field):
    original = image()
    changed = copy.deepcopy(original)
    if field == "layers":
        changed["RootFS"]["Layers"].reverse()
    else:
        changed[field] = "different-platform"
    assert fingerprint(original) != fingerprint(changed)


@pytest.mark.parametrize("layers", [[], ["unverified-layer"], ["sha256:" + "g" * 64]])
def test_invalid_layer_identity_fails_closed(layers):
    invalid = image()
    invalid["RootFS"]["Layers"] = layers
    with pytest.raises(ValueError):
        fingerprint(invalid)


@pytest.mark.parametrize("script", ["deploy.py", "package_release.py"])
def test_installed_scripts_load_the_adjacent_identity_module(monkeypatch, script):
    directory = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(sys, "path", [str(directory), *sys.path])
    loaded = runpy.run_path(str(directory / script), run_name="installed_controller")
    assert loaded["fingerprint"](image()) == fingerprint(image())
