#!/usr/bin/env python3
"""Static consistency checks for the private runtime's curated inputs."""
import hashlib
import json
import re
from pathlib import Path

root = Path(__file__).resolve().parents[1]
excluded = {"packaging", "setuptools", "wheel", "pytest", "pytest-asyncio", "pluggy", "iniconfig", "pygments"}


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()

def locked(path):
    result = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        requirement, digest = line.split(" --hash=sha256:")
        name, version = requirement.split("==")
        assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
        result[canonical(name)] = (version, digest)
    return result

runtime = locked(root / "runtime-production.hashed.lock")
bootstrap = locked(root / "bootstrap.hashed.lock")
assert len(runtime) == 29
assert not (set(runtime) & excluded)
assert set(bootstrap) == {"pip"}

wheels = json.loads((root / "wheel-inputs.json").read_text())
wheel_by_name = {canonical(item["name"]): item for item in wheels}
assert len(wheels) == 30
assert set(wheel_by_name) == set(runtime) | {"pip"}
for name, (version, digest) in runtime.items() | bootstrap.items():
    item = wheel_by_name[name]
    assert item["version"] == version
    assert item["sha256"] == digest

metadata = json.loads((root / "wheel-metadata.json").read_text())
assert len(metadata) == 29
assert {canonical(item["name"]) for item in metadata} == set(runtime)
assert all(item["wheel_sha256"] == runtime[canonical(item["name"])][1] for item in metadata)

inputs = json.loads((root / "runtime-inputs.json").read_text())
assert inputs["python_version"] == "3.12.15"
assert inputs["target"] == "CPython 3.12 / Linux x86_64 GNU libc"
archive = inputs["python_archive"]
assert archive["filename"].startswith("cpython-3.12.15+")
assert len(archive["sha256"]) == 64
assert inputs["production_wheel_count"] == 29
assert inputs["bootstrap_wheel"]["sha256"] == bootstrap["pip"][1]

# Keep recipe material text deterministic enough to make accidental binary input
# additions visible to reviewers.
for path in root.glob("*.json"):
    json.loads(path.read_text())
    assert hashlib.sha256(path.read_bytes()).digest()
