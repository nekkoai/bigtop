"""Offline GitHub release protocol tests; no gh invocation or remote writes."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit


SPEC = importlib.util.spec_from_file_location(
    "github_release", Path(__file__).resolve().parents[1] / "ci/github-release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)

REPOSITORY = "example/bigtop"
BASE = f"repos/{REPOSITORY}"
SHA = "0123456789abcdef" * 2 + "01234567"
TAG = f"v0.123.9{int(SHA[:12], 16):o}"
EXPECTED = release.identity(REPOSITORY, TAG, SHA)


def digest(body):
    return hashlib.sha256(body).hexdigest()


class FakeGitHub:
    """Stateful mock API, including starter uploads and post-upload corruption."""

    def __init__(self):
        self.ref = None
        self.releases = []
        self.assets = {}
        self.contents = {}
        self.calls = []
        self.latest = None
        self.next_id = 100
        self.fail_upload = None
        self.corrupt_upload = None
        self.create_race = None
        self.errors = {}
        self.annotated = {}

    def asset(self, name, body, *, state="uploaded", sha256=None):
        self.next_id += 1
        asset = {"id": self.next_id, "name": name, "state": state,
                 "size": len(body), "digest": f"sha256:{sha256 or digest(body)}"}
        self.assets[name] = asset
        self.contents[asset["id"]] = body
        return asset

    def request(self, method, endpoint, *, data=None, upload=None):
        self.calls.append((method, endpoint, data, upload))
        if (method, endpoint) in self.errors:
            raise self.errors[method, endpoint]
        if method == "GET" and endpoint == BASE:
            return {"full_name": REPOSITORY}
        if method == "GET" and endpoint.startswith(f"{BASE}/git/ref/tags/"):
            if self.ref is None:
                raise release.APIError("missing tag", 404)
            return {"ref": f"refs/tags/{TAG}", "object": dict(self.ref)}
        if method == "GET" and endpoint.startswith(f"{BASE}/git/tags/"):
            return {"object": self.annotated[endpoint.rsplit("/", 1)[1]]}
        if method == "POST" and endpoint == f"{BASE}/git/refs":
            self.ref = {"type": "commit", "sha": self.create_race or data["sha"]}
            if self.create_race:
                raise release.APIError("already exists", 422)
            return {"ref": data["ref"], "object": self.ref}
        if method == "GET" and endpoint == f"{BASE}/releases/latest":
            if self.latest is None:
                raise release.APIError("no latest", 404)
            return {"tag_name": self.latest}
        if method == "GET" and endpoint.startswith(f"{BASE}/releases?"):
            page = int(parse_qs(urlsplit(endpoint).query)["page"][0])
            return self.releases[(page - 1) * 100:page * 100]
        if method == "POST" and endpoint == f"{BASE}/releases":
            result = {"id": 1, **data}
            self.releases.append(result)
            return dict(result)
        if method == "GET" and endpoint.startswith(f"{BASE}/releases/1/assets?"):
            page = int(parse_qs(urlsplit(endpoint).query)["page"][0])
            return list(self.assets.values())[(page - 1) * 100:page * 100]
        if method == "GET" and endpoint == f"{BASE}/releases/1":
            return dict(next(item for item in self.releases if item["id"] == 1))
        if method == "DELETE" and endpoint.startswith(f"{BASE}/releases/assets/"):
            asset_id = int(endpoint.rsplit("/", 1)[1])
            for name, asset in list(self.assets.items()):
                if asset["id"] == asset_id:
                    del self.assets[name]
                    del self.contents[asset_id]
                    return None
            raise AssertionError("attempt to delete unknown asset")
        if method == "POST" and endpoint.startswith("https://uploads.github.com/"):
            name = unquote(parse_qs(urlsplit(endpoint).query)["name"][0])
            body = Path(upload).read_bytes()
            if name == self.fail_upload:
                self.asset(name, b"", state="starter")
                raise release.APIError("upload failed", 502)
            asset = self.asset(name, body)
            if name == self.corrupt_upload:
                asset["digest"] = f"sha256:{'0' * 64}"
            return dict(asset)
        if method == "PATCH" and endpoint == f"{BASE}/releases/1":
            result = next(item for item in self.releases if item["id"] == 1)
            result.update(data)
            if data.get("make_latest") == "true":
                self.latest = result["tag_name"]
            return dict(result)
        raise AssertionError(f"unexpected API call: {method} {endpoint}")

    def download(self, endpoint):
        self.calls.append(("GET", endpoint, None, None))
        return self.contents[int(endpoint.rsplit("/", 1)[1])]


class GitHubReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dist = Path(self.tmp.name) / "dist"
        self.dist.mkdir()
        self.api = FakeGitHub()
        self.make_dist()

    def make_dist(self, *, generation="first"):
        for name in release.REQUIRED - {"SHA256SUMS", "release.json"}:
            (self.dist / name).write_bytes(f"{name}: {generation}\n".encode())
        packages = []
        for package in sorted(release.PACKAGES):
            name = f"{package}_0.123-1_amd64.deb"
            body = f"{name}: {generation}\n".encode()
            (self.dist / name).write_bytes(body)
            packages.append({"package": package, "version": "0.123-1",
                             "architecture": "amd64", "filename": name,
                             "sha256": digest(body), "size": len(body)})
        record = {**EXPECTED, "source_pins": {"upstream/asys": "a" * 40,
                  "upstream/dcomp": "b" * 40, "upstream/capsa": "c" * 40,
                  "upstream/llama.cpp": "d" * 40}, "packages": packages}
        (self.dist / "release.json").write_text(json.dumps(record))
        self.rehash()

    def rehash(self):
        sums = "".join(f"{digest(p.read_bytes())}  {p.name}\n"
                       for p in sorted(self.dist.iterdir())
                       if p.name != "SHA256SUMS" and p.is_file())
        (self.dist / "SHA256SUMS").write_text(sums)

    def edit_record(self, change):
        path = self.dist / "release.json"
        record = json.loads(path.read_text())
        change(record)
        path.write_text(json.dumps(record))
        self.rehash()

    def run_command(self, command="publish", *, sha=SHA, tag=TAG):
        args = [command, "--repository", REPOSITORY, "--tag", tag, "--sha", sha]
        if command == "publish":
            args += ["--dist", str(self.dist)]
        self.output = io.StringIO()
        self.error = io.StringIO()
        with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.error):
            return release.main(args, api=self.api)

    def write_calls(self):
        return [call for call in self.api.calls if call[0] != "GET"]

    def seed_published(self):
        self.assertEqual(self.run_command(), 0, self.error.getvalue())
        self.api.calls.clear()

    def test_tag_create_and_existing_same_are_idempotent(self):
        self.assertEqual(self.run_command("tag"), 0)
        self.assertEqual(self.api.ref, {"type": "commit", "sha": SHA})
        self.assertEqual([c[0] for c in self.write_calls()], ["POST"])
        self.api.calls.clear()
        self.assertEqual(self.run_command("tag"), 0)
        self.assertEqual(self.write_calls(), [])

    def test_wrong_tag_never_force_updated(self):
        self.api.ref = {"type": "commit", "sha": "f" * 40}
        self.assertEqual(self.run_command(), 1)
        self.assertIn("refusing to move/force", self.error.getvalue())
        self.assertEqual(self.write_calls(), [])

    def test_annotated_tag_must_peel_to_exact_commit(self):
        self.api.ref = {"type": "tag", "sha": "a" * 40}
        self.api.annotated["a" * 40] = {"type": "commit", "sha": SHA}
        self.assertEqual(self.run_command("tag"), 0)
        self.assertEqual(self.write_calls(), [])
        self.api.annotated["a" * 40] = {"type": "commit", "sha": "b" * 40}
        self.assertEqual(self.run_command("tag"), 1)

    def test_create_tag_race_rechecks_exact_ref(self):
        self.api.create_race = SHA
        self.assertEqual(self.run_command("tag"), 0)
        self.api.ref = None
        self.api.create_race = "f" * 40
        self.assertEqual(self.run_command("tag"), 1)

    def test_auth_network_server_errors_are_not_missing(self):
        for status in (401, 403, 429, 500, None):
            with self.subTest(status=status):
                self.api.errors = {("GET", f"{BASE}/git/ref/tags/{TAG}"):
                                   release.APIError("auth/network failure", status)}
                self.api.calls.clear()
                self.assertEqual(self.run_command("tag"), 1)
                self.assertEqual(self.write_calls(), [])
        self.api.errors = {("GET", BASE): release.APIError("repo hidden", 404)}
        self.api.calls.clear()
        self.assertEqual(self.run_command("tag"), 1)
        self.assertEqual(self.write_calls(), [])

    def test_identity_invalid_before_network(self):
        for sha, tag in (("1234", TAG), (SHA.upper(), TAG),
                         (SHA, "v0.2.2-1"), (SHA, TAG + "0")):
            with self.subTest(sha=sha, tag=tag):
                self.assertEqual(self.run_command("tag", sha=sha, tag=tag), 1)
                self.assertEqual(self.api.calls, [])

    def test_draft_upload_verify_then_publish(self):
        self.assertEqual(self.run_command(), 0, self.error.getvalue())
        self.assertFalse(self.api.releases[0]["draft"])
        self.assertEqual(self.api.latest, TAG)
        self.assertEqual(set(self.api.assets), {p.name for p in self.dist.iterdir()})
        create = next(c for c in self.api.calls if c[0] == "POST"
                      and c[1] == f"{BASE}/releases")
        self.assertIs(create[2]["draft"], True)
        self.assertEqual(create[2]["make_latest"], "false")
        self.assertIn(f"github.com/{REPOSITORY}/releases/download/{TAG}", create[2]["body"])
        self.assertIn("experimental", create[2]["body"])
        body = create[2]["body"]
        base = f"https://github.com/{REPOSITORY}/releases/download/{TAG}"
        self.assertIn(f"{base}/bigtop-archive-keyring.gpg -o /usr/share/keyrings/bigtop-archive-keyring.gpg", body)
        self.assertIn(f"{base}/bigtop.sources -o /etc/apt/sources.list.d/bigtop.sources", body)
        self.assertNotIn(f"{base}/asys", body)
        self.assertTrue({"bigtop.sources", "bigtop-archive-keyring.gpg",
                         "bigtop-archive-keyring.asc"} <= self.api.assets.keys())
        publish = self.api.calls[-1]
        self.assertEqual(publish[:2], ("PATCH", f"{BASE}/releases/1"))
        self.assertEqual(publish[2], {"draft": False, "make_latest": "true"})

    def test_partial_upload_stays_draft_and_retry_repairs_starter(self):
        self.api.fail_upload = "Release.gpg"
        self.assertEqual(self.run_command(), 1)
        self.assertTrue(self.api.releases[0]["draft"])
        self.assertEqual(self.api.assets["Release.gpg"]["state"], "starter")
        self.assertFalse(any(c[0] == "PATCH" for c in self.api.calls))
        self.api.calls.clear()
        self.api.fail_upload = None
        self.assertEqual(self.run_command(), 0, self.error.getvalue())
        self.assertTrue(any(c[0] == "DELETE" for c in self.api.calls))
        self.assertFalse(self.api.releases[0]["draft"])
        self.assertEqual(len(self.api.releases), 1)

    def test_uploaded_digest_mismatch_stays_draft(self):
        self.api.corrupt_upload = "InRelease"
        self.assertEqual(self.run_command(), 1)
        self.assertIn("checksum mismatch", self.error.getvalue())
        self.assertTrue(self.api.releases[0]["draft"])
        self.assertFalse(any(c[0] == "PATCH" for c in self.api.calls))

    def test_published_retry_is_read_only_even_when_rebuild_differs(self):
        self.seed_published()
        old = {name: dict(asset) for name, asset in self.api.assets.items()}
        self.make_dist(generation="different rebuild bytes and signatures")
        self.assertEqual(self.run_command(), 0, self.error.getvalue())
        self.assertEqual(self.write_calls(), [])
        self.assertEqual(old, self.api.assets)
        self.assertIn("no changes", self.output.getvalue())
        downloads = [c for c in self.api.calls if c[1].startswith(f"{BASE}/releases/assets/")]
        self.assertEqual(len(downloads), 2)

    def test_published_missing_or_unknown_asset_fails_read_only(self):
        self.seed_published()
        self.api.assets.pop("Release.gpg")
        self.assertEqual(self.run_command(), 1)
        self.assertEqual(self.write_calls(), [])

    def test_published_remote_manifest_digest_checked(self):
        self.seed_published()
        asset = self.api.assets["SHA256SUMS"]
        self.api.contents[asset["id"]] = b"modified manifest"
        self.assertEqual(self.run_command(), 1)
        self.assertIn("does not match GitHub digest", self.error.getvalue())
        self.assertEqual(self.write_calls(), [])

    def test_published_manifest_must_cover_entire_set(self):
        self.seed_published()
        asset = self.api.assets["SHA256SUMS"]
        body = self.api.contents[asset["id"]].split(b"\n", 1)[1]
        self.api.contents[asset["id"]] = body
        asset.update(size=len(body), digest=f"sha256:{digest(body)}")
        self.assertEqual(self.run_command(), 1)
        self.assertIn("complete release", self.error.getvalue())
        self.assertEqual(self.write_calls(), [])

    def test_published_asset_digest_must_match_remote_manifest(self):
        self.seed_published()
        self.api.assets["Release.gpg"]["digest"] = "sha256:" + "0" * 64
        self.assertEqual(self.run_command(), 1)
        self.assertIn("asset digests", self.error.getvalue())
        self.assertEqual(self.write_calls(), [])

    def test_published_package_size_must_match_remote_provenance(self):
        self.seed_published()
        self.api.assets["asys_0.123-1_amd64.deb"]["size"] += 1
        self.assertEqual(self.run_command(), 1)
        self.assertIn("package checksum/size mismatch", self.error.getvalue())
        self.assertEqual(self.write_calls(), [])

    def test_published_provenance_identity_must_match_even_if_hashes_agree(self):
        self.seed_published()
        asset = self.api.assets["release.json"]
        record = json.loads(self.api.contents[asset["id"]])
        record["sha"] = "f" * 40
        self.api.asset("release.json", json.dumps(record).encode())
        sums = "".join(f"{asset['digest'][7:]}  {name}\n"
                       for name, asset in self.api.assets.items() if name != "SHA256SUMS")
        self.api.asset("SHA256SUMS", sums.encode())
        self.assertEqual(self.run_command(), 1)
        self.assertIn("expected release identity", self.error.getvalue())
        self.assertEqual(self.write_calls(), [])

    def test_published_source_pins_and_versions_must_match(self):
        self.seed_published()
        self.edit_record(lambda r: r["source_pins"].update({"upstream/asys": "f" * 40}))
        self.assertEqual(self.run_command(), 1)
        self.assertIn("source pins", self.error.getvalue())
        self.assertEqual(self.write_calls(), [])

    def test_published_tag_mismatch_rejected_before_downloads(self):
        self.seed_published()
        self.api.ref["sha"] = "f" * 40
        self.assertEqual(self.run_command(), 1)
        self.assertEqual(self.write_calls(), [])

    def test_published_missing_tag_is_not_silently_recreated(self):
        self.seed_published()
        self.api.ref = None
        self.assertEqual(self.run_command(), 1)
        self.assertEqual(self.write_calls(), [])

    def test_published_missing_digest_or_incomplete_state_fails(self):
        self.seed_published()
        for key, value in (("digest", None), ("digest", "md5:bad"),
                           ("state", "starter"), ("size", 0)):
            with self.subTest(key=key, value=value):
                asset = self.api.assets["Packages"]
                old = asset[key]
                asset[key] = value
                self.assertEqual(self.run_command(), 1)
                asset[key] = old
                self.assertEqual(self.write_calls(), [])

    def test_latest_older_newer_unknown_and_legacy(self):
        cases = [
            (None, "true"), ("v0.2.2-1", "true"), ("v0.2.2-3", "true"),
            ("v12.34.56-789", "true"), ("v0.122.9123", "true"),
            ("v0.124.9123", "false"), ("v0.123.9123", "false"),
            ("v9.0-manual", "false"),
        ]
        for latest, expected in cases:
            with self.subTest(latest=latest):
                self.api = FakeGitHub()
                self.api.latest = latest
                self.assertEqual(self.run_command(), 0, self.error.getvalue())
                patch_call = next(c for c in self.api.calls if c[0] == "PATCH")
                self.assertEqual(patch_call[2]["make_latest"], expected)
                self.assertEqual(self.api.latest, TAG if expected == "true" else latest)

    def test_latest_api_failure_never_publishes(self):
        self.api.errors["GET", f"{BASE}/releases/latest"] = release.APIError("denied", 403)
        self.assertEqual(self.run_command(), 1)
        self.assertTrue(self.api.releases[0]["draft"])
        self.assertFalse(any(c[0] == "PATCH" for c in self.api.calls))

    def test_assets_are_paginated_beyond_default_and_one_hundred(self):
        for number in range(123):
            self.api.asset(f"file{number}", b"fixture")
        remote = release.release_assets(self.api, BASE, 1)
        self.assertEqual(len(remote), 123)
        self.assertTrue(any("page=2" in c[1] for c in self.api.calls))

    def test_drafts_are_found_on_second_releases_page(self):
        self.api.ref = {"type": "commit", "sha": SHA}
        self.api.releases = [{"id": i + 2, "tag_name": f"other{i}"} for i in range(100)]
        self.api.releases.append({"id": 1, "tag_name": TAG, "draft": True,
                                  "prerelease": False})
        self.api.asset("failed-old-file", b"", state="starter")
        self.assertEqual(self.run_command(), 0, self.error.getvalue())
        self.assertEqual(len(self.api.releases), 101)
        self.assertTrue(any(c[0] == "DELETE" for c in self.api.calls))
        self.assertTrue(any(c[1] == f"{BASE}/releases?per_page=100&page=2"
                            for c in self.api.calls))

    def test_no_clobber_if_draft_changed_to_published(self):
        self.api.ref = {"type": "commit", "sha": SHA}
        self.api.releases = [{"id": 1, "tag_name": TAG, "draft": True,
                              "prerelease": False}]
        original = self.api.request
        def changed(method, endpoint, **kwargs):
            if endpoint == f"{BASE}/releases/1" and method == "GET":
                return {"id": 1, "tag_name": TAG, "draft": False}
            return original(method, endpoint, **kwargs)
        self.api.request = changed
        self.assertEqual(self.run_command(), 1)
        self.assertEqual(self.write_calls(), [])

    def test_unsafe_directory_symlink_or_name_rejected_before_network(self):
        for name, kind in (("subdir", "dir"), ("linked", "link"), ("bad name", "file"),
                           (".hidden", "file"), ("bad..name", "file")):
            with self.subTest(name=name):
                path = self.dist / name
                if kind == "dir":
                    path.mkdir()
                elif kind == "link":
                    path.symlink_to("Packages")
                else:
                    path.write_bytes(b"unsafe")
                self.assertEqual(self.run_command(), 1)
                self.assertEqual(self.api.calls, [])
                if kind == "dir":
                    path.rmdir()
                else:
                    path.unlink()

    def test_at_or_over_two_gibibytes_rejected_before_hashing(self):
        file = self.dist / "asys_0.123-1_amd64.deb"
        for size in (release.MAX_ASSET_SIZE, release.MAX_ASSET_SIZE + 1):
            with self.subTest(size=size):
                with file.open("wb") as stream:
                    stream.truncate(size)
                self.assertEqual(self.run_command(), 1)
                self.assertIn("2 GiB limit", self.error.getvalue())
                self.assertEqual(self.api.calls, [])

    def test_remote_asset_at_two_gibibyte_limit_rejected(self):
        self.api.asset("Packages", b"fixture")["size"] = release.MAX_ASSET_SIZE
        with self.assertRaisesRegex(release.ReleaseError, "invalid size"):
            release.release_assets(self.api, BASE, 1)

    def test_missing_metadata_and_corrupt_manifest_rejected(self):
        path = self.dist / "InRelease"
        path.unlink()
        self.assertEqual(self.run_command(), 1)
        self.make_dist()
        (self.dist / "Packages").write_text("changed after manifest")
        self.assertEqual(self.run_command(), 1)
        self.assertIn("checksum mismatch", self.error.getvalue())
        self.assertEqual(self.api.calls, [])

    def test_provenance_identity_package_size_and_duplicate_json_rejected(self):
        mutations = [
            lambda r: r.update(sha="f" * 40),
            lambda r: r.update(repository="attacker/repository"),
            lambda r: r.update(commit_count=True),
            lambda r: r["packages"][0].update(size=999),
            lambda r: r["packages"][0].update(architecture=[]),
            lambda r: r["packages"].pop(),
        ]
        for change in mutations:
            with self.subTest(change=change):
                self.make_dist()
                self.edit_record(change)
                self.assertEqual(self.run_command(), 1)
                self.assertEqual(self.api.calls, [])
        self.make_dist()
        path = self.dist / "release.json"
        path.write_text(path.read_text().replace('"format": 1', '"format": 1, "format": 1'))
        self.rehash()
        self.assertEqual(self.run_command(), 1)
        self.assertIn("duplicate JSON", self.error.getvalue())

    def test_manifest_rejects_duplicate_unsafe_and_self_names(self):
        for name in ("../bad", "/bad", "SHA256SUMS", "bad name"):
            with self.subTest(name=name):
                with self.assertRaises(release.ReleaseError):
                    release.checksums(f"{'a' * 64}  {name}\n".encode())
        with self.assertRaises(release.ReleaseError):
            release.checksums((f"{'a' * 64}  Release\n" * 2).encode())


class GhTransportTests(unittest.TestCase):
    def result(self, stdout, returncode=0):
        return subprocess.CompletedProcess(["mock-gh"], returncode, stdout, b"fixture stderr")

    def test_requires_gh_token(self):
        with patch.dict(os.environ, {"GH_TOKEN": ""}):
            with self.assertRaisesRegex(release.ReleaseError, "GH_TOKEN"):
                release.GitHub()

    def test_json_request_and_upload_use_gh_without_shell(self):
        with patch.dict(os.environ, {"GH_TOKEN": "fixture-not-real"}):
            api = release.GitHub()
        response = self.result(b'HTTP/2.0 201 Created\r\nContent-Type: application/json\r\n\r\n{"id":1}')
        with patch.object(release.subprocess, "run", return_value=response) as run:
            self.assertEqual(api.request("POST", f"{BASE}/git/refs", data={"sha": SHA}), {"id": 1})
            args, kwargs = run.call_args
            self.assertEqual(args[0][0:2], ["gh", "api"])
            self.assertIn("github.com", args[0])
            self.assertIn(f"X-GitHub-Api-Version: {release.API_VERSION}", args[0])
            self.assertIn("Content-Type: application/json", args[0])
            self.assertEqual(json.loads(kwargs["input"]), {"sha": SHA})
            self.assertNotIn("shell", kwargs)
            api.request("POST", "https://uploads.github.com/fixture", upload=Path("/tmp/asset"))
            self.assertIn("/tmp/asset", run.call_args.args[0])
            self.assertIsNone(run.call_args.kwargs["input"])
            self.assertIn("Content-Type: application/octet-stream", run.call_args.args[0])

    def test_only_http_404_is_optional_not_auth_or_network(self):
        with patch.dict(os.environ, {"GH_TOKEN": "fixture"}):
            api = release.GitHub()
        for code in (401, 403, 404, 500):
            response = self.result(f"HTTP/2.0 {code} Error\n\n{{}}".encode(), 1)
            with self.subTest(code=code), patch.object(
                    release.subprocess, "run", return_value=response):
                if code == 404:
                    self.assertIsNone(release.optional_get(api, "fixture"))
                else:
                    with self.assertRaises(release.APIError) as raised:
                        release.optional_get(api, "fixture")
                    self.assertEqual(raised.exception.status, code)
        with patch.object(release.subprocess, "run",
                          return_value=self.result(b"", 1)):
            with self.assertRaises(release.APIError) as raised:
                release.optional_get(api, "fixture")
            self.assertIsNone(raised.exception.status)

    def test_binary_download_is_not_json_decoded_or_newline_normalized(self):
        with patch.dict(os.environ, {"GH_TOKEN": "fixture"}):
            api = release.GitHub()
        body = b"sha256 manifest\r\n\r\n\x00\xff"
        headers = (
            b"HTTP/2.0 200 OK\r\nHeader: value\r\n\r\n",
            b"HTTP/2.0 200 OK\nHeader: value\n\n",
            # Actual gh processResponse uses LF status + CRLF header lines.
            b"HTTP/2.0 200 OK\nHeader: value\r\n\r\n",
        )
        for header in headers:
            with self.subTest(header=header), patch.object(
                    release.subprocess, "run", return_value=self.result(header + body)) as run:
                self.assertEqual(api.download("fixture"), body)
                self.assertIn("Accept: application/octet-stream", run.call_args.args[0])

    def test_gh_empty_204_mixed_newlines_are_accepted(self):
        with patch.dict(os.environ, {"GH_TOKEN": "fixture"}):
            api = release.GitHub()
        with patch.object(release.subprocess, "run", return_value=self.result(
                b"HTTP/2.0 204 No Content\n\r\n")):
            self.assertIsNone(api.request("DELETE", "fixture"))


if __name__ == "__main__":
    unittest.main()
