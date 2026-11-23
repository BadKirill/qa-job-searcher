import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

if __package__:
    from .image_identity import fingerprint
else:
    from image_identity import fingerprint


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def package(source, destination, commit):
    destination.mkdir(parents=True, exist_ok=True)
    tag = "job-searcher-runtime:" + commit
    image = json.loads(subprocess.check_output(["docker", "image", "inspect", tag], text=True))[0]
    if (
        image["Architecture"] != "arm64"
        or image["Config"]["Labels"].get("org.opencontainers.image.revision") != commit
    ):
        raise ValueError("Release image architecture or revision mismatch")
    shutil.copyfile(source / "multiuser/compose.runtime.yaml", destination / "compose.runtime.yaml")
    shutil.copyfile(source / "infra/aws/Caddyfile", destination / "Caddyfile")
    override = json.loads((source / "infra/aws/compose.override.json").read_text())
    for service in override["services"].values():
        if service["image"] == "job-searcher-runtime:aws":
            service["image"] = tag
    (destination / "compose.aws.json").write_text(json.dumps(override, indent=2) + "\n")
    manifest = {
        "version": 2,
        "commit": commit,
        "image_id": image["Id"],
        "image_fingerprint": fingerprint(image),
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
