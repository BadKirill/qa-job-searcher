import hashlib
import json
import re

FIELDS = {
    "User": "",
    "Env": [],
    "Cmd": [],
    "Entrypoint": [],
    "WorkingDir": "",
    "ExposedPorts": {},
    "Volumes": {},
    "Labels": {},
    "StopSignal": "",
    "Shell": [],
    "Healthcheck": {},
    "OnBuild": [],
}


def fingerprint(image):
    layers = image["RootFS"]["Layers"]
    if not layers or any(not re.fullmatch("sha256:[0-9a-f]{64}", layer) for layer in layers):
        raise ValueError("Invalid image layer identity")
    configuration = image["Config"]
    identity = {
        "architecture": image["Architecture"],
        "os": image["Os"],
        "layers": layers,
        "configuration": {
            key: configuration.get(key) or default for key, default in FIELDS.items()
        },
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
