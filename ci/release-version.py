#!/usr/bin/env python3
"""CI-only repository release identity, Debian revisions and provenance."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOGS = {
    "asys": "pkg/asys/debian/changelog",
    "dcomp": "pkg/dcomp/debian/changelog",
    "capsa": "pkg/capsa/debian/changelog",
}
FAMILIES = {
    "asys": ("asys", "asys-images", "asys-flow-view", "asys-server"),
    "dcomp": ("dcomp",),
    "capsa": ("capsa-core", "capsa-python-runtime", "capsa-federation",
              "capsa-llama-cpu", "capsa-embedding-assets-nomic",
              "capsa-indexers", "capsa-services", "capsa-server"),
}
BUILD_UNITS = {
    "dcomp": FAMILIES["dcomp"],
    "asys": ("asys", "asys-flow-view", "asys-server"),
    "asys-images": ("asys-images",),
    "capsa-core": ("capsa-core", "capsa-federation", "capsa-indexers",
                   "capsa-services", "capsa-server"),
    "capsa-runtime": ("capsa-python-runtime",),
    "capsa-llama": ("capsa-llama-cpu",),
    "capsa-assets": ("capsa-embedding-assets-nomic",),
}
NATIVE_PACKAGES = {
    "asys", "asys-images", "asys-flow-view", "dcomp", "capsa-core",
    "capsa-python-runtime", "capsa-llama-cpu",
}
CI_SUFFIX = re.compile(r"\.bigtop\.\d+\.g[0-9a-f]{12}$")


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def identity(root, expected_sha=None):
    if git(root, "rev-parse", "--is-shallow-repository") != "false":
        raise ValueError("release numbering requires checkout fetch-depth: 0")
    sha = git(root, "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("expected a full GitHub commit SHA")
    if expected_sha and sha != expected_sha:
        raise ValueError("checkout HEAD does not match the workflow commit")
    count = int(git(root, "rev-list", "--count", "HEAD"))
    short = sha[:12]
    return {
        "sha": sha,
        "commit_count": count,
        # Shelley-style repository version; not an upstream application version.
        "tag": f"v0.{count}.9{int(short, 16):o}",
        "revision": f"bigtop.{count}.g{short}",
    }


def versions(root):
    return {
        family: subprocess.check_output(
            ["dpkg-parsechangelog", "-l", str(root / path), "-S", "Version"],
            text=True,
        ).strip()
        for family, path in CHANGELOGS.items()
    }


def stamp(root, release):
    """Change only CI workspace Debian revisions, not pins/upstream versions."""
    changes = []
    for family, version in versions(root).items():
        base = CI_SUFFIX.sub("", version)
        if "-" not in base or "~" in base or ".bigtop." in base:
            raise ValueError(f"unsupported release package version: {version}")
        updated = f"{base}.{release['revision']}"
        path = root / CHANGELOGS[family]
        text, count = re.subn(
            r"^(\S+ \()[^)]+(\))",
            lambda match: match[1] + updated + match[2],
            path.read_text(), count=1,
        )
        if count != 1:
            raise ValueError(f"cannot stamp {path}")
        changes.append((path, text))
    for path, text in changes:
        path.write_text(text)
    return versions(root)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def package_records(root, files, units=None):
    expected = {
        name: version for family, version in versions(root).items()
        for name in FAMILIES[family]
    }
    if units is not None:
        names = {name for unit in units for name in BUILD_UNITS[unit]}
        expected = {name: version for name, version in expected.items() if name in names}
    records = {}
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"not a regular package file: {path}")
        fields = subprocess.check_output(
            ["dpkg-deb", "-f", str(path), "Package", "Version", "Architecture"],
            text=True,
        )
        metadata = dict(line.split(": ", 1) for line in fields.splitlines())
        name, version, arch = (metadata[key] for key in ("Package", "Version", "Architecture"))
        if name not in expected or name in records:
            raise ValueError(f"unexpected or duplicate package: {name}")
        if version != expected[name]:
            raise ValueError(f"{name}: expected {expected[name]}, got {version}")
        if arch != ("amd64" if name in NATIVE_PACKAGES else "all"):
            raise ValueError(f"wrong architecture for {name}: {arch}")
        if path.name != f"{name}_{version}_{arch}.deb":
            raise ValueError(f"noncanonical package filename: {path.name}")
        records[name] = {
            "package": name, "version": version, "architecture": arch,
            "filename": path.name, "size": path.stat().st_size, "sha256": digest(path),
        }
    if set(records) != set(expected):
        raise ValueError(f"incomplete release, missing: {sorted(set(expected) - set(records))}")
    return sorted(records.values(), key=lambda row: row["package"])


def source_pins(root):
    pins = {}
    for line in git(root, "ls-tree", "-r", "HEAD", "upstream").splitlines():
        header, path = line.split("\t", 1)
        mode, kind, sha = header.split()
        if mode == "160000" and kind == "commit":
            pins[path] = sha
    if set(pins) != {"upstream/asys", "upstream/dcomp", "upstream/capsa", "upstream/llama.cpp"}:
        raise ValueError("release requires all four source pins")
    return pins


def unit_record(root, source, component, release):
    record = {
        **release, "component": component, "source_pins": source_pins(root),
        "packages": package_records(root, sorted(source.glob("*.deb")), [component]),
    }
    if component in {"asys", "asys-images"}:
        record["asys_build"] = subprocess.check_output(
            [str(root / "upstream/asys/tools/build-id")], text=True,
        ).strip()
        if "-dirty." in record["asys_build"]:
            raise ValueError("asys source was modified during the build")
    return record


def collect(root, source, dist, units=None, require_build_records=False):
    units = list(BUILD_UNITS) if units is None else units
    files = sorted(source.rglob("*.deb"))
    records = package_records(root, files, units)
    if require_build_records:
        release = identity(root)
        pins = source_pins(root)
        inputs = {}
        for path in sorted(source.rglob("*.build.json")):
            if path.is_symlink() or not path.is_file():
                raise ValueError("build provenance must be a regular file")
            record = json.loads(path.read_text())
            unit = record.get("component")
            if unit not in units or unit in inputs:
                raise ValueError("unexpected/duplicate build provenance")
            if any(record.get(key) != value for key, value in release.items()):
                raise ValueError("builds came from different repository commits")
            if record.get("source_pins") != pins:
                raise ValueError("builds came from different upstream pins")
            expected = [row for row in records if row["package"] in BUILD_UNITS[unit]]
            if record.get("packages") != expected:
                raise ValueError("build output does not match its provenance")
            inputs[unit] = record
        if set(inputs) != set(units):
            raise ValueError("missing build provenance")
        if {"asys", "asys-images"} <= set(units):
            build = inputs["asys"].get("asys_build")
            if not build or build != inputs["asys-images"].get("asys_build"):
                raise ValueError("asys host/image build IDs differ")
    if dist.exists() and any(dist.iterdir()):
        raise ValueError("collection destination must be empty (no stale release assets)")
    dist.mkdir(parents=True, exist_ok=True)
    for path in files:
        shutil.copyfile(path, dist / path.name)
    return records


def manifest(root, dist, repository, release):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("expected OWNER/REPO")
    records = package_records(root, sorted(dist.glob("*.deb")))

    provenance = {
        "format": 1, "repository": repository, "tag": release["tag"],
        "sha": release["sha"], "commit_count": release["commit_count"],
        "source_pins": source_pins(root), "packages": records,
    }
    (dist / "release.json").write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    lines = []
    for path in sorted(dist.iterdir()):
        if path.name == "SHA256SUMS":
            continue
        if not path.is_file() or path.is_symlink() or not re.fullmatch(r"[A-Za-z0-9_.+-]+", path.name):
            raise ValueError(f"unsafe release asset: {path}")
        lines.append(f"{digest(path)}  {path.name}\n")
    (dist / "SHA256SUMS").write_text("".join(lines))
    return provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--sha", help="require this exact checkout SHA")
    commands = parser.add_subparsers(dest="command", required=True)
    identify = commands.add_parser("identity")
    identify.add_argument("--github-output", type=Path)
    commands.add_parser("stamp")
    unit = commands.add_parser("record-unit")
    unit.add_argument("--component", choices=BUILD_UNITS, required=True)
    unit.add_argument("--input", type=Path, required=True)
    unit.add_argument("--output", type=Path, required=True)
    gather = commands.add_parser("collect")
    gather.add_argument("--input", type=Path, required=True)
    gather.add_argument("--dist", type=Path, required=True)
    gather.add_argument("--units", choices=BUILD_UNITS, nargs="+")
    gather.add_argument("--require-build-records", action="store_true")
    record = commands.add_parser("manifest")
    record.add_argument("--dist", type=Path, required=True)
    record.add_argument("--repository", required=True)
    args = parser.parse_args()
    release = identity(args.root, args.sha)
    if args.command == "identity":
        result = release
        if args.github_output:
            with args.github_output.open("a") as output:
                for key, value in release.items():
                    output.write(f"{key}={value}\n")
    elif args.command == "stamp":
        result = stamp(args.root, release)
    elif args.command == "collect":
        result = collect(args.root, args.input, args.dist, args.units, args.require_build_records)
    elif args.command == "record-unit":
        result = unit_record(args.root, args.input, args.component, release)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    else:
        result = manifest(args.root, args.dist, args.repository, release)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"release-version: {error}")
