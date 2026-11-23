import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def package(source, destination, commit):
    destination.mkdir(parents=True, exist_ok=True)
    tag = "job-searcher-runtime:" + commit
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", tag, "--format", "{{.Id}}"], text=True
    ).strip()
    shutil.copyfile(source / "multiuser/compose.runtime.yaml", destination / "compose.runtime.yaml")
    shutil.copyfile(source / "infra/aws/Caddyfile", destination / "Caddyfile")
    override = json.loads((source / "infra/aws/compose.override.json").read_text())
    for service in override["services"].values():
        if service["image"] == "job-searcher-runtime:aws":
            service["image"] = tag
    (destination / "compose.aws.json").write_text(json.dumps(override, indent=2) + "\n")
    manifest = {
        "version": 1,
        "commit": commit,
        "image_id": image_id,
        "files": {
            name: digest(destination / name)
            for name in (
                "runtime-image.tar.gz",
                "compose.runtime.yaml",
                "compose.aws.json",
                "Caddyfile",
            )
        },
    }
    (destination / "release.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("commit")
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(package(Path.cwd(), args.destination, args.commit)))


if __name__ == "__main__":
    main()
