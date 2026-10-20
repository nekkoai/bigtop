#!/usr/bin/env python3
"""Validate the complete, flat bigtop release without installing any packages.

APT_KEY_DIR selects trusted public material (default: repository keys/).
Signatures, when present, are always verified; --require-signature forbids unsigned
metadata. This checks packaging consistency, not upstream application behavior.
"""
import argparse
import gzip
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MAX_ASSET_SIZE = 2 * 1024**3  # GitHub requires each release asset to be under 2 GiB.
EXPECTED_PACKAGES = {
    "dcomp": "amd64", "asys": "amd64", "asys-images": "amd64",
    "asys-flow-view": "amd64", "asys-server": "all", "capsa-core": "amd64",
    "capsa-python-runtime": "amd64", "capsa-federation": "all",
    "capsa-llama-cpu": "amd64", "capsa-embedding-assets-nomic": "all",
    "capsa-indexers": "all", "capsa-services": "all", "capsa-server": "all",
}
METADATA = {
    "Packages", "Packages.gz", "Release", "Release.gpg", "InRelease",
    "asys-archive-keyring.gpg", "asys-archive-keyring.asc", "asys.sources",
    "release.json", "SHA256SUMS",
}
INDEX_FIELDS = {"filename", "size", "md5sum", "sha1", "sha256", "sha512"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def run(*args):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # Never echo command output: input files may contain private key material.
    require(result.returncode == 0, f"{args[0]} failed during repository validation")
    return result.stdout


def paragraphs(text):
    """Strict RFC822 control parsing, with case-insensitive duplicate detection."""
    result, fields, last = [], {}, None
    for line in text.splitlines() + [""]:
        if not line:
            if fields:
                result.append(fields)
            fields, last = {}, None
        elif line[0].isspace():
            require(last is not None, "orphan control continuation")
            fields[last] += "\n" + line[1:]
        else:
            match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9-]*):[ \t]*(.*)", line)
            require(match is not None, "malformed control field")
            last, value = match.group(1).lower(), match.group(2)
            require(last not in fields, f"duplicate control field: {last}")
            fields[last] = value
    return result


def one_paragraph(text):
    records = paragraphs(text)
    require(len(records) == 1, "expected exactly one control paragraph")
    return records[0]


def digest(path, algorithm="sha256"):
    value = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def flat_filename(name):
    # In particular reject tilde (rewritten by GitHub release uploads), path
    # separators, URL escapes, whitespace, and leading command-line options.
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", name)) and name not in {".", ".."}


def validate_key_material(key_dir):
    """Check both public exports against the pinned primary fingerprint in isolation."""
    key_dir = Path(key_dir)
    fingerprint = (key_dir / "FINGERPRINT").read_text().strip().upper()
    require(bool(re.fullmatch(r"[0-9A-F]{40}|[0-9A-F]{64}", fingerprint)), "invalid key fingerprint")
    with tempfile.TemporaryDirectory(prefix="bigtop-key-") as home:
        os.chmod(home, 0o700)
        for suffix in ("gpg", "asc"):
            path = key_dir / f"asys-archive-keyring.{suffix}"
            require(path.is_file() and not path.is_symlink(), "missing/unsafe trusted public key")
            listing = run("gpg", "--batch", "--no-options", "--homedir", home,
                          "--with-colons", "--show-keys", str(path)).decode()
            records = [line.split(":") for line in listing.splitlines()]
            require(not any(row[0] in {"sec", "ssb"} for row in records), "private key material is forbidden")
            require(sum(row[0] == "pub" for row in records) == 1, "expected one public primary key")
            fingerprints = [row[9] for row in records if row[0] == "fpr"]
            require(fingerprints and fingerprints[0] == fingerprint, "public key does not match pinned fingerprint")
    return fingerprint


def validate_signatures(dist, key_dir, required):
    fingerprint = validate_key_material(key_dir)
    for suffix in ("gpg", "asc"):
        name = f"asys-archive-keyring.{suffix}"
        require((dist / name).read_bytes() == (key_dir / name).read_bytes(),
                f"published public key differs from trusted key: {name}")
    signed = [(dist / name).exists() for name in ("InRelease", "Release.gpg")]
    require(not required or all(signed), "signed repository requires InRelease and Release.gpg")
    require(not any(signed) or all(signed), "incomplete signature metadata")
    if not any(signed):
        return
    with tempfile.TemporaryDirectory(prefix="bigtop-gpgv-") as tmp:
        home = Path(tmp)
        os.chmod(home, 0o700)
        keyring = home / "trusted.gpg"
        keyring.write_bytes((key_dir / "asys-archive-keyring.gpg").read_bytes())
        for name in ("InRelease", "Release.gpg"):
            args = ["gpgv", "--homedir", str(home), "--keyring", str(keyring), "--status-fd", "1"]
            if name == "InRelease":
                args += ["--output", str(home / "Release"), str(dist / name)]
            else:
                args += [str(dist / name), str(dist / "Release")]
            status = run(*args).decode()
            valid = [line.split() for line in status.splitlines() if line.startswith("[GNUPG:] VALIDSIG ")]
            require(valid and all(fingerprint in (row[2], row[-1]) for row in valid), "signature uses wrong key")
        require((home / "Release").read_bytes() == (dist / "Release").read_bytes(),
                "InRelease content differs from Release")


def validate_release(dist):
    release = one_paragraph((dist / "Release").read_text())
    for name, value in {"origin": "asys", "label": "asys", "suite": "stable", "codename": "noble"}.items():
        require(release.get(name) == value, f"incorrect Release {name}")
    require(set(release.get("architectures", "").split()) == {"amd64", "all"}, "incorrect Release architectures")
    for field, algorithm in (("md5sum", "md5"), ("sha1", "sha1"), ("sha256", "sha256"), ("sha512", "sha512")):
        require(field != "sha256" or field in release, "Release lacks SHA256")
        if field not in release:
            continue
        seen = set()
        for line in release[field].splitlines():
            if not line.strip():
                continue
            parts = line.split()
            require(len(parts) == 3, f"malformed Release {field} entry")
            checksum, size, name = parts
            require(name in {"Packages", "Packages.gz"} and name not in seen,
                    f"unexpected/duplicate Release checksum filename: {name}")
            seen.add(name)
            require(size.isdigit() and int(size) == (dist / name).stat().st_size, f"Release size mismatch: {name}")
            require(checksum == digest(dist / name, algorithm), f"Release {field} mismatch: {name}")
        require(seen == {"Packages", "Packages.gz"}, f"Release {field} does not cover both indexes")
    with gzip.open(dist / "Packages.gz", "rb") as compressed:
        require(compressed.read() == (dist / "Packages").read_bytes(), "Packages.gz differs from Packages")


def validate_packages(dist):
    packages = paragraphs((dist / "Packages").read_text())
    seen, filenames, versions = set(), set(), {}
    for entry in packages:
        name = entry.get("package", "")
        require(name in EXPECTED_PACKAGES, f"unexpected package: {name}")
        require(name not in seen, f"duplicate package: {name}")
        seen.add(name)
        require(entry.get("architecture") == EXPECTED_PACKAGES[name], f"incorrect architecture: {name}")
        version = entry.get("version", "")
        require(bool(version), f"missing version: {name}")
        family = "capsa" if name.startswith("capsa-") else ("asys" if name.startswith("asys") else "dcomp")
        require(family not in versions or versions[family] == version, f"mismatched {family} family versions")
        versions[family] = version
        filename = entry.get("filename", "")
        require(flat_filename(filename) and filename.endswith(".deb"), f"unsafe/non-flat package filename: {filename}")
        require(filename not in filenames, "multiple packages use one payload")
        filenames.add(filename)
        payload = dist / filename
        require(payload.is_file() and not payload.is_symlink(), f"missing/unsafe payload: {filename}")
        size = entry.get("size", "")
        require(size.isdigit() and int(size) == payload.stat().st_size, f"Packages size mismatch: {name}")
        require(entry.get("sha256") == digest(payload), f"Packages SHA256 mismatch: {name}")
        for field, algorithm in (("md5sum", "md5"), ("sha1", "sha1"), ("sha512", "sha512")):
            if field in entry:
                require(entry[field] == digest(payload, algorithm), f"Packages {field} mismatch: {name}")
        control = one_paragraph(run("dpkg-deb", "--field", str(payload)).decode())
        # Compare every control field, including all dependency relationships in
        # both directions (missing and invented metadata are equally invalid).
        normalize = lambda fields: {k: " ".join(v.split()) for k, v in fields.items()}
        require(normalize({k: v for k, v in entry.items() if k not in INDEX_FIELDS}) == normalize(control),
                f"Packages control/dependency metadata differs from payload: {name}")
    require(seen == set(EXPECTED_PACKAGES), f"incomplete package set; missing: {', '.join(sorted(set(EXPECTED_PACKAGES) - seen))}")
    require(filenames == {p.name for p in dist.glob("*.deb")}, "unindexed or duplicate .deb payloads")


def validate_sums(dist):
    sums = dist / "SHA256SUMS"
    if not sums.exists():
        return
    seen = set()
    for line in sums.read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line)
        require(match is not None, "malformed SHA256SUMS entry")
        checksum, name = match.groups()
        require(flat_filename(name) and name != "SHA256SUMS" and name not in seen, "unsafe/duplicate SHA256SUMS filename")
        seen.add(name)
        require((dist / name).is_file() and checksum == digest(dist / name), f"SHA256SUMS mismatch: {name}")
    require(seen == {p.name for p in dist.iterdir()} - {"SHA256SUMS"}, "SHA256SUMS does not cover release assets")


def validate(dist, key_dir, require_signature=False):
    dist, key_dir = Path(dist).resolve(), Path(key_dir).resolve()
    require(dist.is_dir(), "repository directory does not exist")
    for path in dist.iterdir():
        require(path.is_file() and not path.is_symlink(), f"non-regular release asset: {path.name}")
        require(path.name in METADATA or (flat_filename(path.name) and path.suffix == ".deb"),
                f"unrecognized release asset: {path.name}")
        require(path.stat().st_size < MAX_ASSET_SIZE, f"release asset reaches GitHub 2 GiB limit: {path.name}")
    for name in ("Packages", "Packages.gz", "Release", "asys.sources", "asys-archive-keyring.gpg", "asys-archive-keyring.asc"):
        require((dist / name).is_file(), f"missing repository metadata: {name}")
    validate_packages(dist)
    validate_release(dist)
    validate_signatures(dist, key_dir, require_signature)
    validate_sums(dist)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", nargs="?", type=Path, default=ROOT / "build/dist")
    parser.add_argument("--require-signature", action="store_true")
    args = parser.parse_args()
    try:
        validate(args.dist, Path(os.environ.get("APT_KEY_DIR", ROOT / "keys")), args.require_signature)
    except (ValueError, OSError, EOFError, UnicodeError) as error:
        print(f"apt repository validation failed: {error}", file=sys.stderr)
        return 1
    print(f"Validated all {len(EXPECTED_PACKAGES)} packages" + (" (signatures required)" if args.require_signature else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
