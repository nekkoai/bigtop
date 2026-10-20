#!/usr/bin/env python3
"""Publish a verified flat apt repository without modifying published assets.

The caller must restrict publication to trusted main pushes, after all thirteen
packages pass build/smoke checks and signed apt metadata validation. Serialize
the entire publish job with a repository-wide Actions concurrency group and
``queue: max`` (up to 100 pending jobs); otherwise latest selection can race.

No force pushes, published-asset replacement, or automatic API retries. Retry
the same command after failure: exact tags are reusable, incomplete drafts are
replaceable, and published releases are verified using THEIR manifest, not a
potentially timestamp-different rebuild. Unknown/manual latest tags are left
latest; vX.Y.Z-N apt release tags are the supported legacy migration.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from urllib.parse import quote


MAX_ASSET_SIZE = 2 * 1024**3
MAX_MANIFEST_SIZE = 1024**2
API_VERSION = "2026-03-10"
PACKAGES = frozenset({
    "asys", "asys-flow-view", "asys-server", "asys-images", "dcomp",
    "capsa-core", "capsa-federation", "capsa-indexers", "capsa-services",
    "capsa-server", "capsa-python-runtime", "capsa-llama-cpu",
    "capsa-embedding-assets-nomic",
})
REQUIRED = frozenset({
    "Packages", "Packages.gz", "Release", "Release.gpg", "InRelease",
    "asys-archive-keyring.gpg", "asys-archive-keyring.asc", "asys.sources",
    "SHA256SUMS", "release.json",
})
SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
AUTO_TAG = re.compile(r"v0\.([1-9][0-9]*)\.9([0-7]+)")


class ReleaseError(Exception):
    """A closed failure: never proceed to publication after this."""


class APIError(ReleaseError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def check(condition, message):
    if not condition:
        raise ReleaseError(message)


def safe_name(name):
    return (isinstance(name, str) and len(name) <= 255
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", name)
            and not name.endswith(".") and ".." not in name)


def load_json(data, description):
    # Duplicate keys make provenance ambiguous and must not silently win.
    def pairs(items):
        result = {}
        for key, value in items:
            check(key not in result, f"duplicate JSON key in {description}: {key}")
            result[key] = value
        return result

    try:
        return json.loads(data, object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as exc:
        raise ReleaseError(f"invalid JSON in {description}") from exc


class GitHub:
    """All network I/O goes through gh; injectable in tests, GitHub.com only."""

    def __init__(self):
        check(bool(os.environ.get("GH_TOKEN")), "GH_TOKEN is required")

    def _call(self, method, endpoint, *, data=None, upload=None, raw=False):
        command = [
            "gh", "api", "--hostname", "github.com", "--include",
            "--method", method, "--header", f"X-GitHub-Api-Version: {API_VERSION}",
            "--header", "Accept: application/octet-stream" if raw else
            "Accept: application/vnd.github+json",
        ]
        stdin = None
        if upload is not None:
            # gh stats --input FILE and sets HTTP ContentLength to its byte
            # size, streaming the raw reader. Do not pipe binary uploads via
            # --input -: stdin has unknown length (GitHub requires a length).
            command += ["--header", "Content-Type: application/octet-stream",
                        "--input", str(upload)]
        elif data is not None:
            command += ["--header", "Content-Type: application/json", "--input", "-"]
            stdin = json.dumps(data).encode()
        command.append(endpoint)
        try:
            result = subprocess.run(command, input=stdin, capture_output=True,
                                    check=False)
        except OSError as exc:
            raise APIError(f"cannot execute gh: {exc}") from exc
        # gh --include prints the response status even for API errors. Never
        # infer "not found" from stderr, a failed command, or a network failure.
        # Its HTTP client follows asset-download redirects, then prints only
        # the final response. The status line is LF, headers/terminator CRLF;
        # accept either newline form and preserve body bytes exactly.
        separator = re.search(rb"\r?\n\r?\n", result.stdout)
        header = result.stdout[:separator.start()] if separator else b""
        body = result.stdout[separator.end():] if separator else b""
        status = re.match(rb"HTTP/\S+\s+(\d{3})(?:\s|$)", header)
        code = int(status[1]) if status and separator else None
        if result.returncode or code is None or not 200 <= code < 300:
            raise APIError(
                f"GitHub {method} {endpoint} failed"
                f" (HTTP {code if code is not None else 'unknown/network'}); "
                "check GH_TOKEN permissions/network and retry",
                code,
            )
        if raw:
            return body
        return load_json(body, endpoint) if body.strip() else None

    def request(self, method, endpoint, *, data=None, upload=None):
        return self._call(method, endpoint, data=data, upload=upload)

    def download(self, endpoint):
        return self._call("GET", endpoint, raw=True)


def optional_get(api, endpoint):
    try:
        return api.request("GET", endpoint)
    except APIError as exc:
        if exc.status == 404:
            return None
        raise


def identity(repository, tag, sha):
    check(bool(re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/"
        r"[A-Za-z0-9][A-Za-z0-9_.-]*", repository)),
        "repository must be OWNER/REPO")
    check(repository.split("/")[1] not in {".", ".."}, "invalid repository")
    check(bool(SHA.fullmatch(sha)), "sha must be a full lowercase 40-hex commit SHA")
    match = AUTO_TAG.fullmatch(tag)
    check(match is not None, "tag must use v0.<commit-count>.9<SHA-prefix-octal>")
    check(match[2] == format(int(sha[:12], 16), "o"),
          "tag octal suffix does not match the first twelve SHA hex digits")
    return {"format": 1, "repository": repository, "tag": tag, "sha": sha,
            "commit_count": int(match[1])}


def ref_sha(api, base, tag):
    ref = optional_get(api, f"{base}/git/ref/tags/{quote(tag, safe='')}")
    if ref is None:
        return None
    check(isinstance(ref, dict) and ref.get("ref") == f"refs/tags/{tag}",
          "GitHub returned the wrong tag ref")
    obj = ref.get("object")
    seen = set()
    for _ in range(16):
        check(isinstance(obj, dict) and isinstance(obj.get("sha"), str)
              and SHA.fullmatch(obj["sha"]), "invalid GitHub tag object")
        if obj.get("type") == "commit":
            return obj["sha"]
        check(obj.get("type") == "tag" and obj["sha"] not in seen,
              "tag does not resolve to a commit")
        seen.add(obj["sha"])
        annotated = api.request("GET", f"{base}/git/tags/{obj['sha']}")
        check(isinstance(annotated, dict), "invalid annotated tag response")
        obj = annotated.get("object")
    raise ReleaseError("tag indirection limit exceeded")


def require_tag(api, base, expected):
    actual = ref_sha(api, base, expected["tag"])
    check(actual == expected["sha"],
          f"tag {expected['tag']} targets {actual}, expected {expected['sha']}; "
          "refusing to move/force an existing tag")


def ensure_tag(api, base, expected):
    actual = ref_sha(api, base, expected["tag"])
    if actual is not None:
        check(actual == expected["sha"],
              f"tag {expected['tag']} targets {actual}, expected {expected['sha']}; "
              "refusing to move/force an existing tag")
        return
    try:
        api.request("POST", f"{base}/git/refs", data={
            "ref": f"refs/tags/{expected['tag']}", "sha": expected["sha"],
        })
    except APIError as exc:
        if exc.status not in {409, 422}:
            raise
        # Concurrent creator: success only if the ref now resolves exactly.
    require_tag(api, base, expected)


def paginated(api, endpoint):
    page = 1
    while True:
        items = api.request("GET", f"{endpoint}?per_page=100&page={page}")
        check(isinstance(items, list) and len(items) <= 100,
              "invalid paginated GitHub response")
        yield from items
        if len(items) < 100:
            return
        page += 1


def checksums(data):
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise ReleaseError("SHA256SUMS is not UTF-8") from exc
    sums = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line)
        check(match is not None, "invalid SHA256SUMS line")
        digest, name = match.groups()
        check(safe_name(name) and name != "SHA256SUMS" and name not in sums,
              "unsafe/duplicate/self-referential SHA256SUMS filename")
        sums[name] = digest
    check(bool(sums), "empty SHA256SUMS")
    return sums


def provenance(data, expected, assets):
    result = load_json(data, "release.json")
    check(isinstance(result, dict), "release.json must be an object")
    for key, value in expected.items():
        check(type(result.get(key)) is type(value) and result[key] == value,
              f"release.json {key} does not match expected release identity")
    pins = result.get("source_pins")
    check(isinstance(pins, dict) and bool(pins), "release.json source_pins missing")
    for path, sha in pins.items():
        check(isinstance(path, str) and path.startswith("upstream/")
              and all(part not in {"", ".", ".."} for part in path.split("/"))
              and isinstance(sha, str) and SHA.fullmatch(sha),
              "invalid release.json source pin")
    packages = result.get("packages")
    check(isinstance(packages, list) and len(packages) == len(PACKAGES),
          "release.json must describe all thirteen packages exactly once")
    by_name = {}
    filenames = set()
    for package in packages:
        check(isinstance(package, dict), "invalid release.json package")
        name = package.get("package")
        filename = package.get("filename")
        version = package.get("version")
        architecture = package.get("architecture")
        check(isinstance(name, str) and name in PACKAGES and name not in by_name,
              "unexpected/duplicate package in release.json")
        check(isinstance(version, str) and bool(version)
              and isinstance(architecture, str) and architecture in {"amd64", "all"}
              and safe_name(filename) and filename.endswith(".deb")
              and filename == f"{name}_{version}_{architecture}.deb"
              and filename not in filenames,
              "invalid package filename/version/architecture in release.json")
        asset = assets.get(filename)
        check(asset is not None and type(package.get("size")) is int
              and package["size"] == asset["size"]
              and package.get("sha256") == asset["sha256"],
              f"release.json package checksum/size mismatch: {filename}")
        filenames.add(filename)
        by_name[name] = package
    check({name for name in assets if name.endswith(".deb")} == filenames,
          "unexpected/missing .deb asset")
    return result


def local_distribution(path, expected):
    path = Path(path)
    check(not path.is_symlink() and path.is_dir(), "dist must be a real directory")
    assets = {}
    for file in sorted(path.iterdir()):
        info = file.lstat()
        check(safe_name(file.name) and stat.S_ISREG(info.st_mode),
              f"dist must be flat, regular, safely named files: {file.name}")
        check(0 < info.st_size < MAX_ASSET_SIZE,
              f"asset is empty or reaches the 2 GiB limit: {file.name}")
        check(file.name not in {"SHA256SUMS", "release.json"}
              or info.st_size <= MAX_MANIFEST_SIZE, "manifest exceeds 1 MiB")
        digest = hashlib.sha256()
        with file.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024**2), b""):
                digest.update(chunk)
        assets[file.name] = {"size": info.st_size, "sha256": digest.hexdigest(),
                             "path": file}
    check(REQUIRED <= assets.keys(), "dist is missing signed apt/manifest/key assets")
    # Explicit whitelist avoids uploading unrelated build products or secrets.
    check(all(name in REQUIRED or name.endswith(".deb") for name in assets),
          "dist contains an unexpected release asset")
    sums = checksums(assets["SHA256SUMS"]["path"].read_bytes())
    check(set(sums) == assets.keys() - {"SHA256SUMS"},
          "SHA256SUMS must cover every other asset exactly")
    check(all(sums[name] == assets[name]["sha256"] for name in sums),
          "local SHA256SUMS checksum mismatch")
    record = provenance(assets["release.json"]["path"].read_bytes(), expected, assets)
    return assets, record


def release_assets(api, base, release_id):
    assets = {}
    for asset in paginated(api, f"{base}/releases/{release_id}/assets"):
        check(isinstance(asset, dict), "invalid GitHub asset")
        name = asset.get("name")
        digest = asset.get("digest")
        check(safe_name(name) and name not in assets, "unsafe/duplicate GitHub asset")
        check(type(asset.get("id")) is int and asset["id"] > 0,
              "invalid GitHub asset ID")
        check(asset.get("state") == "uploaded"
              and type(asset.get("size")) is int
              and 0 < asset["size"] < MAX_ASSET_SIZE,
              f"GitHub asset incomplete or invalid size: {name}")
        check(isinstance(digest, str) and digest.startswith("sha256:")
              and SHA256.fullmatch(digest[7:]),
              f"GitHub SHA256 digest required for asset: {name}")
        assets[name] = {**asset, "sha256": digest[7:]}
    return assets


def same_assets(remote, local):
    check(remote.keys() == local.keys(), "GitHub asset set is incomplete/unexpected")
    for name, asset in local.items():
        check(remote[name]["size"] == asset["size"]
              and remote[name]["sha256"] == asset["sha256"],
              f"GitHub uploaded size/checksum mismatch: {name}")


def verify_published(api, base, release, local, expected, local_record):
    remote = release_assets(api, base, release["id"])
    check(remote.keys() == local.keys(), "published asset set is incomplete/unexpected")
    downloaded = {}
    for name in ("SHA256SUMS", "release.json"):
        asset = remote[name]
        check(asset["size"] <= MAX_MANIFEST_SIZE, "remote manifest exceeds 1 MiB")
        body = api.download(f"{base}/releases/assets/{asset['id']}")
        check(len(body) == asset["size"]
              and hashlib.sha256(body).hexdigest() == asset["sha256"],
              f"downloaded published {name} does not match GitHub digest")
        downloaded[name] = body
    sums = checksums(downloaded["SHA256SUMS"])
    check(set(sums) == remote.keys() - {"SHA256SUMS"},
          "published SHA256SUMS does not cover the complete release")
    check(all(sums[name] == remote[name]["sha256"] for name in sums),
          "published SHA256SUMS does not match GitHub asset digests")
    remote_record = provenance(downloaded["release.json"], expected, remote)
    check(remote_record["source_pins"] == local_record["source_pins"],
          "published source pins differ from expected commit")
    # Byte hashes/sizes deliberately differ on nondeterministic rebuilds;
    # package identity and source provenance must never differ.
    def package_identity(record):
        return sorted((p["package"], p["version"], p["architecture"], p["filename"])
                      for p in record["packages"])
    check(package_identity(remote_record) == package_identity(local_record),
          "published package identities differ from expected commit")


def notes(expected):
    base = (f"https://github.com/{expected['repository']}/releases/download/"
            f"{expected['tag']}")
    return f"""Signed flat apt repository for Ubuntu 24.04 amd64.

Verify the archive signing-key fingerprint through a trusted channel first.
```sh
sudo curl -fsSL {base}/asys-archive-keyring.gpg -o /usr/share/keyrings/asys-archive-keyring.gpg
sudo curl -fsSL {base}/asys.sources -o /etc/apt/sources.list.d/asys.sources
sudo apt update
sudo apt install asys-server
```
The supplied asys.sources follows releases/latest; replace latest/download with
download/{expected['tag']} in its URI to pin this release.
Capsa packages are experimental and require explicit configuration/activation;
they are not a production-ready serving or compatibility guarantee.
SHA256SUMS and release.json record this release's complete asset provenance.
"""


def make_latest(api, base, expected):
    latest = optional_get(api, f"{base}/releases/latest")
    if latest is None:
        return "true"
    check(isinstance(latest, dict) and isinstance(latest.get("tag_name"), str),
          "invalid GitHub latest release response")
    tag = latest["tag_name"]
    if re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+-[0-9]+", tag):
        return "true"
    match = AUTO_TAG.fullmatch(tag)
    if match and int(match[1]) < expected["commit_count"]:
        return "true"
    # Equal counts, later counts, or a manual/unknown tag must not be displaced.
    return "false"


def find_release(api, base, tag):
    found = None
    for release in paginated(api, f"{base}/releases"):
        check(isinstance(release, dict), "invalid GitHub release response")
        if release.get("tag_name") == tag:
            check(found is None, "multiple releases exist for this tag")
            check(type(release.get("id")) is int and release["id"] > 0
                  and type(release.get("draft")) is bool
                  and release.get("prerelease") is False, "invalid release identity/state")
            found = release
    return found


def publish(api, base, expected, local, record):
    release = find_release(api, base, expected["tag"])
    if release is not None and not release["draft"]:
        require_tag(api, base, expected)
        verify_published(api, base, release, local, expected, record)
        require_tag(api, base, expected)
        return "published release verified; no changes"
    ensure_tag(api, base, expected)
    if release is None:
        release = api.request("POST", f"{base}/releases", data={
            "tag_name": expected["tag"], "target_commitish": expected["sha"],
            "name": expected["tag"], "body": notes(expected),
            "draft": True, "prerelease": False, "make_latest": "false",
        })
        check(isinstance(release, dict) and type(release.get("id")) is int
              and release["id"] > 0 and release.get("draft") is True
              and release.get("prerelease") is False
              and release.get("tag_name") == expected["tag"],
              "GitHub did not create the expected draft")
    release_id = release["id"]
    # Re-fetch immediately before destructive draft-only operations.
    current = api.request("GET", f"{base}/releases/{release_id}")
    check(isinstance(current, dict) and current.get("id") == release_id
          and current.get("draft") is True and current.get("prerelease") is False
          and current.get("immutable") is not True
          and current.get("tag_name") == expected["tag"],
          "release is no longer a mutable draft; refusing to replace assets")
    # Incomplete uploads may lack a digest or have state=starter. List those
    # without strict verification so a failed draft can actually be repaired.
    for asset in paginated(api, f"{base}/releases/{release_id}/assets"):
        check(isinstance(asset, dict) and type(asset.get("id")) is int
              and asset["id"] > 0, "invalid draft asset ID")
        api.request("DELETE", f"{base}/releases/assets/{asset['id']}")
    for name, asset in local.items():
        endpoint = (f"https://uploads.github.com/{base}/releases/{release_id}/assets"
                    f"?name={quote(name, safe='')}")
        api.request("POST", endpoint, upload=asset["path"])
    same_assets(release_assets(api, base, release_id), local)
    require_tag(api, base, expected)
    latest = make_latest(api, base, expected)
    result = api.request("PATCH", f"{base}/releases/{release_id}", data={
        "draft": False, "make_latest": latest,
    })
    check(isinstance(result, dict) and result.get("id") == release_id
          and result.get("draft") is False and result.get("prerelease") is False
          and result.get("tag_name") == expected["tag"],
          "GitHub did not confirm publication; retry to verify")
    return f"published {expected['tag']} (make_latest={latest})"


def main(argv=None, *, api=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("tag", "publish"):
        sub = commands.add_parser(command)
        sub.add_argument("--repository", required=True)
        sub.add_argument("--tag", required=True)
        sub.add_argument("--sha", required=True)
        if command == "publish":
            sub.add_argument("--dist", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        expected = identity(args.repository, args.tag, args.sha)
        if args.command == "publish":
            local, record = local_distribution(args.dist, expected)
        if api is None:
            api = GitHub()
        base = f"repos/{args.repository}"
        # Check repository access separately: a hidden private repo's 404 must
        # never be mistaken for permission to create a missing tag/release.
        repository = api.request("GET", base)
        check(isinstance(repository, dict)
              and str(repository.get("full_name", "")).lower() == args.repository.lower(),
              "GitHub returned the wrong repository")
        if args.command == "tag":
            ensure_tag(api, base, expected)
            result = f"tag {args.tag} verified at {args.sha}"
        else:
            result = publish(api, base, expected, local, record)
        print(result)
        return 0
    except (ReleaseError, OSError) as exc:
        print(f"github-release: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
