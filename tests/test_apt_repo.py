#!/usr/bin/env python3
"""Real tiny debs and disposable signing keys; never install or touch host apt state."""
import getpass
import gzip
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from unittest import mock
from urllib.parse import urlsplit
import socket

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("validate_apt_repo", ROOT / "ci/validate-apt-repo.py")
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)
APT_SPEC = importlib.util.spec_from_file_location("apt_repo_client", ROOT / "ci/apt-repo-test.py")
APT_CLIENT = importlib.util.module_from_spec(APT_SPEC)
APT_SPEC.loader.exec_module(APT_CLIENT)
TOOLS = ("dpkg-deb", "dpkg-scanpackages", "apt-ftparchive", "gpg", "gpgv", "gzip")


def execute(*args, env=None, cwd=None, check=True):
    return subprocess.run(args, env=env, cwd=cwd, check=check, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


@unittest.skipUnless(all(shutil.which(tool) for tool in TOOLS), "apt metadata/deb/GPG tools required")
class AptRepoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory(prefix="bigtop-apt-tests-")
        cls.base = Path(cls.scratch.name)
        cls.home = cls.base / "signing"
        cls.home.mkdir(mode=0o700)
        cls.sign_env = os.environ | {"GNUPGHOME": str(cls.home)}
        cls.keys = cls.generate_key("fixture", cls.home)
        cls.other_home = cls.base / "other-signing"
        cls.other_home.mkdir(mode=0o700)
        cls.other_keys = cls.generate_key("other-fixture", cls.other_home)
        cls.fixture = cls.base / "fixture"
        cls.fixture.mkdir()
        for name, architecture in VALIDATOR.EXPECTED_PACKAGES.items():
            cls.build_deb(cls.fixture, name, architecture)
        cls.maker(cls.fixture, cls.sign_env | {"APT_KEY_DIR": str(cls.keys), "APT_REQUIRE_SIGNATURE": "1"})

    @classmethod
    def tearDownClass(cls):
        # Stop only task-owned agents before deleting their scratch homes.
        if shutil.which("gpgconf"):
            for home in (cls.home, cls.other_home):
                execute("gpgconf", "--homedir", str(home), "--kill", "gpg-agent", check=False)
        cls.scratch.cleanup()

    @classmethod
    def generate_key(cls, name, home):
        env = os.environ | {"GNUPGHOME": str(home)}
        execute("gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
                "--quick-generate-key", name + " <test@example.invalid>", "ed25519", "sign", "0", env=env)
        listing = execute("gpg", "--batch", "--with-colons", "--list-keys", env=env).stdout
        fingerprint = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
        keys = cls.base / (name + "-keys")
        keys.mkdir()
        (keys / "FINGERPRINT").write_text(fingerprint + "\n")
        for suffix, armor in (("gpg", []), ("asc", ["--armor"])):
            with (keys / ("asys-archive-keyring." + suffix)).open("wb") as output:
                subprocess.run(["gpg", "--batch", *armor, "--export", fingerprint], env=env,
                               stdout=output, stderr=subprocess.PIPE, check=True)
        return keys

    @staticmethod
    def build_deb(dist, name, architecture, version="1.0-1", depends=None):
        with tempfile.TemporaryDirectory(dir=dist.parent) as tmp:
            package = Path(tmp)
            (package / "DEBIAN").mkdir()
            dependencies = {"asys": "dcomp (>= 1.0-1), asys-images (= 1.0-1)",
                            "capsa-server": "capsa-services (= 1.0-1)"}
            control = (f"Package: {name}\nVersion: {version}\nArchitecture: {architecture}\n"
                       "Maintainer: Fixture <test@example.invalid>\nSection: devel\nPriority: optional\n"
                       "Description: disposable fixture\n Tiny apt regression payload.\n")
            depends = dependencies.get(name) if depends is None else depends
            if depends:
                control += "Depends: " + depends + "\n"
            (package / "DEBIAN/control").write_text(control)
            (package / "usr/share" / name).mkdir(parents=True)
            (package / "usr/share" / name / "fixture.txt").write_text(name + "\n")
            output = dist / f"{name}_{version}_{architecture}.deb"
            execute("dpkg-deb", "--build", "--root-owner-group", str(package), str(output))
            return output

    @staticmethod
    def maker(dist, env):
        return execute(str(ROOT / "ci/make-apt-repo.sh"), str(dist), env=env)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=self.base)
        self.path = Path(self.tmp.name)
        self.dist = self.path / "dist"
        shutil.copytree(self.fixture, self.dist)
        self.empty_home = self.path / "empty-home"
        self.empty_home.mkdir(mode=0o700)
        self.env = os.environ | {"GNUPGHOME": str(self.empty_home), "APT_KEY_DIR": str(self.keys),
                                 "APT_REQUIRE_SIGNATURE": "0"}
        self.signed_env = self.env | {"GNUPGHOME": str(self.home), "APT_REQUIRE_SIGNATURE": "1"}
        # Parent process/CI environment must not silently alter fixture source URLs.
        self.env.pop("APT_REPOSITORY_URL", None)
        self.signed_env.pop("APT_REPOSITORY_URL", None)

    def tearDown(self):
        self.tmp.cleanup()

    def validator(self, *args, env=None, success=True):
        result = execute("python3", str(ROOT / "ci/validate-apt-repo.py"), str(self.dist), *args,
                         env=env or self.env, check=False)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def refresh_release(self):
        """Regenerate valid unsigned Release so individual index checks get exercised."""
        for name in ("Release", "InRelease", "Release.gpg"):
            (self.dist / name).unlink(missing_ok=True)
        (self.dist / "Packages.gz").write_bytes(gzip.compress((self.dist / "Packages").read_bytes(), mtime=0))
        result = execute("apt-ftparchive", "-o", "APT::FTPArchive::Release::Origin=asys",
                         "-o", "APT::FTPArchive::Release::Label=asys",
                         "-o", "APT::FTPArchive::Release::Suite=stable",
                         "-o", "APT::FTPArchive::Release::Codename=noble",
                         "-o", "APT::FTPArchive::Release::Architectures=amd64 all", "release", ".", cwd=self.dist)
        (self.dist / "Release").write_text(result.stdout)

    def change_index(self, change):
        path = self.dist / "Packages"
        path.write_text(change(path.read_text()))
        self.refresh_release()

    def test_full_signed_repository(self):
        self.validator("--require-signature")
        self.assertEqual(len(list(self.dist.glob("*.deb"))), 13)
        self.assertNotIn(" Release\n", (self.dist / "Release").read_text())
        self.assertEqual((ROOT / "ci/asys.sources").read_bytes(), (self.dist / "asys.sources").read_bytes())

    def test_unsigned_local_default_and_stale_signature_cleanup(self):
        self.maker(self.dist, self.env)
        self.assertFalse((self.dist / "InRelease").exists())
        self.assertFalse((self.dist / "Release.gpg").exists())
        self.validator()
        self.validator("--require-signature", success=False)

    def test_generic_local_maker_accepts_partial_set(self):
        for path in self.dist.glob("capsa-*.deb"):
            path.unlink()
        self.maker(self.dist, self.env)
        self.assertEqual(len(VALIDATOR.paragraphs((self.dist / "Packages").read_text())), 5)
        self.validator(success=False)

    def test_required_signing_missing_or_wrong_secret_key(self):
        for home in (self.empty_home, self.other_home):
            with self.subTest(home=home):
                for name in ("InRelease", "Release.gpg"):
                    (self.dist / name).write_text("stale")
                result = execute(str(ROOT / "ci/make-apt-repo.sh"), str(self.dist),
                                 env=self.signed_env | {"GNUPGHOME": str(home)}, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.dist / "InRelease").exists())
                self.assertFalse((self.dist / "Release.gpg").exists())

    def test_missing_or_mismatched_public_key(self):
        keys = self.path / "keys"
        shutil.copytree(self.keys, keys)
        for kind in ("missing", "wrong"):
            with self.subTest(kind=kind):
                public = keys / "asys-archive-keyring.gpg"
                if kind == "missing":
                    public.unlink()
                else:
                    shutil.copyfile(self.other_keys / public.name, public)
                env = self.signed_env | {"APT_KEY_DIR": str(keys)}
                result = execute(str(ROOT / "ci/make-apt-repo.sh"), str(self.dist), env=env, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.validator("--require-signature", env=env, success=False)

    def test_missing_key_directory_removes_stale_signatures(self):
        result = execute(str(ROOT / "ci/make-apt-repo.sh"), str(self.dist),
                         env=self.signed_env | {"APT_KEY_DIR": str(self.path / "missing-keys")}, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.dist / "InRelease").exists())
        self.assertFalse((self.dist / "Release.gpg").exists())

    def test_invalid_deb_scan_fails_before_signing(self):
        (self.dist / "broken.deb").write_text("not a deb")
        result = execute(str(ROOT / "ci/make-apt-repo.sh"), str(self.dist),
                         env=self.signed_env, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.dist / "InRelease").exists())
        self.assertFalse((self.dist / "Release.gpg").exists())

    def test_private_key_export_is_never_published(self):
        keys = self.path / "keys"
        shutil.copytree(self.keys, keys)
        fingerprint = (keys / "FINGERPRINT").read_text().strip()
        with (keys / "asys-archive-keyring.gpg").open("wb") as output:
            subprocess.run(["gpg", "--batch", "--export-secret-keys", fingerprint], env=self.sign_env,
                           stdout=output, stderr=subprocess.PIPE, check=True)
        result = execute(str(ROOT / "ci/make-apt-repo.sh"), str(self.dist),
                         env=self.signed_env | {"APT_KEY_DIR": str(keys)}, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.dist / "asys-archive-keyring.gpg").read_bytes(),
                         (self.keys / "asys-archive-keyring.gpg").read_bytes())

    def test_url_override_and_injection_rejection(self):
        self.maker(self.dist, self.env | {"APT_REPOSITORY_URL": "https://github.com/example/repo/releases/latest/download/"})
        source = (self.dist / "asys.sources").read_text()
        self.assertIn("URIs: https://github.com/example/repo/releases/latest/download\n", source)
        self.assertIn("Signed-By: /usr/share/keyrings/asys-archive-keyring.gpg", source)
        for bad in ("https://example.invalid/\nTrusted: yes", "https://example.invalid/a b", "file:///tmp/repo", "https://user:secret@example.invalid/"):
            with self.subTest(url=bad):
                result = execute(str(ROOT / "ci/make-apt-repo.sh"), str(self.dist),
                                 env=self.env | {"APT_REPOSITORY_URL": bad}, check=False)
                self.assertNotEqual(result.returncode, 0)

    def test_incomplete_unexpected_and_duplicate_packages(self):
        original = (self.dist / "Packages").read_text()
        first = original.split("\n\n")[0] + "\n\n"
        for name, changed in (("missing", original[len(first):]),
                              ("unexpected", original.replace("Package: dcomp\n", "Package: surprise\n")),
                              ("duplicate", original + first)):
            with self.subTest(case=name):
                self.change_index(lambda _: changed)
                self.validator(success=False)

    def test_family_version_and_architecture(self):
        original = (self.dist / "Packages").read_text()
        for changed in (original.replace("Version: 1.0-1", "Version: 2.0-1", 1),
                        original.replace("Architecture: amd64", "Architecture: arm64", 1)):
            self.change_index(lambda _: changed)
            self.validator(success=False)

    def test_rebuilt_payload_wrong_family_version_architecture_or_package(self):
        # Validly generated metadata must not legitimize the wrong binary set.
        for kind in ("version", "architecture", "package"):
            with self.subTest(kind=kind):
                for path in self.dist.glob("capsa-core_*.deb"):
                    path.unlink()
                replacement = self.build_deb(self.dist, "intruder" if kind == "package" else "capsa-core",
                                             "arm64" if kind == "architecture" else "amd64",
                                             "2.0-1" if kind == "version" else "1.0-1")
                self.maker(self.dist, self.signed_env)
                self.validator("--require-signature", success=False)
                replacement.unlink()

    def test_unsafe_filenames(self):
        original = (self.dist / "Packages").read_text()
        filename = next(line for line in original.splitlines() if line.startswith("Filename: "))
        for name in ("../outside.deb", "nested/payload.deb", "/tmp/payload.deb", "./payload.deb",
                     "bad name.deb", "payload~1.deb", "%2e%2e%2fpayload.deb", "-option.deb"):
            with self.subTest(filename=name):
                self.change_index(lambda _: original.replace(filename, "Filename: " + name, 1))
                self.validator(success=False)

    def test_index_size_sha256_dependencies_and_duplicate_fields(self):
        original = (self.dist / "Packages").read_text()
        cases = {
            "missing size": "\n".join(line for line in original.split("\n") if not line.startswith("Size:")),
            "wrong size": original.replace("Size: ", "Size: 999", 1),
            "missing sha256": "\n".join(line for line in original.split("\n") if not line.startswith("SHA256:")),
            "wrong sha256": original.replace("SHA256: ", "SHA256: f", 1),
            "missing depends": "\n".join(line for line in original.split("\n") if not line.startswith("Depends:")),
            "wrong depends": original.replace("Depends: dcomp (>= 1.0-1)", "Depends: dcomp (>= 99.0)"),
            "extra depends": original.replace("Package: dcomp\n", "Package: dcomp\nDepends: surprise\n"),
            "duplicate field": original.replace("Package: dcomp\n", "Package: dcomp\npackage: dcomp\n"),
        }
        for name, changed in cases.items():
            with self.subTest(case=name):
                self.change_index(lambda _: changed)
                self.validator(success=False)

    def test_payload_tampering_or_unindexed_deb(self):
        payload = next(self.dist.glob("*.deb"))
        original = payload.read_bytes()
        payload.write_bytes(original + b"tampered")
        self.validator("--require-signature", success=False)
        payload.write_bytes(original)
        shutil.copyfile(payload, self.dist / "unindexed.deb")
        self.validator("--require-signature", success=False)

    def test_release_checksums_and_compressed_index(self):
        release = self.dist / "Release"
        original = release.read_text()
        self.maker(self.dist, self.env)
        for change in (lambda text: text.replace("SHA256:", "NoSHA256:"),
                       lambda text: text.replace(" Packages\n", " ../Packages\n"),
                       lambda text: text.replace("SHA256:\n ", "SHA256:\n f", 1),
                       lambda text: text.replace(" Packages\n", " Packages\n 00 1 Packages\n", 1)):
            with self.subTest(change=change):
                release.write_text(change(original))
                self.validator(success=False)
        (self.dist / "Packages.gz").write_bytes(gzip.compress(b"wrong index"))
        # Refresh only Release, deliberately preserving the wrong gzip data.
        release.unlink()
        output = execute("apt-ftparchive", "-o", "APT::FTPArchive::Release::Origin=asys",
                         "-o", "APT::FTPArchive::Release::Label=asys", "-o", "APT::FTPArchive::Release::Suite=stable",
                         "-o", "APT::FTPArchive::Release::Codename=noble", "-o",
                         "APT::FTPArchive::Release::Architectures=amd64 all", "release", ".", cwd=self.dist).stdout
        release.write_text(output)
        self.validator(success=False)

    def test_signature_tampering_missing_and_wrong_signer(self):
        for name in ("InRelease", "Release.gpg"):
            original = (self.dist / name).read_bytes()
            (self.dist / name).write_bytes(b"not a signature")
            self.validator("--require-signature", success=False)
            (self.dist / name).write_bytes(original)
            (self.dist / name).unlink()
            self.validator(success=False)
            (self.dist / name).write_bytes(original)
        fingerprint = (self.other_keys / "FINGERPRINT").read_text().strip()
        execute("gpg", "--batch", "--yes", "--local-user", fingerprint, "--clearsign", "-o",
                str(self.dist / "InRelease"), str(self.dist / "Release"),
                env=self.env | {"GNUPGHOME": str(self.other_home)})
        self.validator("--require-signature", success=False)

    def test_inrelease_content_must_match_release(self):
        release = self.dist / "Release"
        release.write_text(release.read_text().replace("Suite: stable", "Suite: testing"))
        fingerprint = (self.keys / "FINGERPRINT").read_text().strip()
        execute("gpg", "--batch", "--yes", "--local-user", fingerprint, "-abs", "-o",
                str(self.dist / "Release.gpg"), str(release), env=self.sign_env)
        # Direct helper exercises the independent clearsigned-content consistency
        # check while both signatures themselves remain cryptographically valid.
        with self.assertRaisesRegex(ValueError, "InRelease content differs"):
            VALIDATOR.validate_signatures(self.dist, self.keys, True)

    def test_public_key_tampering(self):
        shutil.copyfile(self.other_keys / "asys-archive-keyring.gpg", self.dist / "asys-archive-keyring.gpg")
        self.validator("--require-signature", success=False)

    def test_private_stray_symlink_and_github_size_limit(self):
        for name in ("private.asc", "private.key", "FINGERPRINT", "notes.txt"):
            path = self.dist / name
            path.write_text("must not publish")
            self.validator(success=False)
            path.unlink()
        path = self.dist / "escape.deb"
        path.symlink_to(next(self.dist.glob("*.deb")))
        self.validator(success=False)
        path.unlink()
        path = self.dist / "release.json"
        with path.open("wb") as output:
            output.truncate(VALIDATOR.MAX_ASSET_SIZE)
        self.validator(success=False)
        path.unlink()

    def test_optional_provenance_and_sha256sums(self):
        (self.dist / "release.json").write_text('{"fixture": true}\n')
        sums = "".join(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name + "\n"
                       for path in sorted(self.dist.iterdir()))
        (self.dist / "SHA256SUMS").write_text(sums)
        self.validator("--require-signature")
        (self.dist / "release.json").write_text('{"fixture": false}\n')
        self.validator("--require-signature", success=False)
        (self.dist / "SHA256SUMS").write_text(sums.replace("  Packages\n", "  ../Packages\n"))
        self.validator(success=False)
        (self.dist / "SHA256SUMS").write_text(sums + sums.splitlines()[0] + "\n")
        self.validator(success=False)

    def client_system_root(self):
        """An empty disposable host, with an apt hook that must never execute."""
        root = self.path / "system-root"
        (root / "etc/apt/apt.conf.d").mkdir(parents=True, exist_ok=True)
        (root / "var/lib/apt/lists").mkdir(parents=True, exist_ok=True)
        (root / "var/lib/dpkg").mkdir(parents=True, exist_ok=True)
        (root / "var/lib/dpkg/status").write_text(
            "Package: dcomp\nStatus: install ok installed\nPriority: optional\nSection: devel\n"
            "Architecture: amd64\nVersion: 1.0-1\nMaintainer: Test <test@example.invalid>\n"
            "Description: Already installed but must still download with --reinstall\n\n")
        (root / "etc/apt/apt.conf.d/99-must-not-run").write_text(
            'APT::Update::Post-Invoke {"touch ' + str(root / "HOOK-RAN") + '";};\n')
        return root

    @staticmethod
    def tree_contents(path):
        return {str(file.relative_to(path)): file.read_bytes()
                for file in path.rglob("*") if file.is_file()}

    @staticmethod
    def host_apt_state():
        # No private config contents are read. Include actual host state as well
        # as the synthetic host used by these tests to catch an escaping path.
        state = {}
        for name in ("/etc/apt", "/var/lib/apt/lists", "/var/cache/apt", "/var/log/apt",
                     "/var/lib/dpkg/status"):
            root = Path(name)
            for path in [root, *root.rglob("*")]:
                try:
                    info = path.lstat()
                except (FileNotFoundError, PermissionError):
                    continue
                state[str(path)] = (info.st_ino, info.st_size, info.st_mtime_ns)
        return state

    def run_isolated_client(self, system_root, required=True):
        temporary = self.path / "client-work"
        temporary.mkdir(exist_ok=True)
        stdout, stderr = io.StringIO(), io.StringIO()
        before = self.tree_contents(system_root)
        host_before = self.host_apt_state()
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                APT_CLIENT.test_repository(self.dist, self.keys, require_signature=required,
                                           system_root=system_root, temp_parent=temporary)
        finally:
            self.assertEqual(self.tree_contents(system_root), before, "client mutated host apt/dpkg state")
            self.assertEqual(self.host_apt_state(), host_before, "client mutated actual host apt/dpkg state")
            self.assertEqual(list(temporary.iterdir()), [], "private apt files leaked after client exit")
            self.client_output = stdout.getvalue() + stderr.getvalue()
        return self.client_output

    @unittest.skipUnless(shutil.which("apt-get"), "apt-get required")
    def test_client_downloads_all_13_signed_exact_versions_and_reinstalls(self):
        root = self.client_system_root()
        output = self.run_isolated_client(root)
        self.assertIn("downloaded and verified all 13 exact package versions", output)
        self.assertIn("Download complete and in download only mode", output)

    @unittest.skipUnless(shutil.which("apt-get"), "apt-get required")
    def test_client_generic_unsigned_local_repository(self):
        for path in self.dist.glob("capsa-*.deb"):
            path.unlink()
        self.maker(self.dist, self.env)
        output = self.run_isolated_client(self.client_system_root(), required=False)
        self.assertIn("testing unsigned local repository", output)
        self.assertIn("downloaded and verified all 5 exact package versions", output)

    @unittest.skipUnless(shutil.which("apt-get"), "apt-get required")
    def test_client_download_tampering_fails_and_cleans_server_and_state(self):
        root = self.client_system_root()
        payload = next(self.dist.glob("dcomp_*.deb"))
        data = payload.read_bytes()
        payload.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))  # Same size, wrong SHA256.
        observed = []
        original_server = APT_CLIENT.repository_server

        @contextmanager
        def recording_server(dist):
            with original_server(dist) as url:
                observed.append(url)
                yield url

        with mock.patch.object(APT_CLIENT, "repository_server", recording_server):
            with self.assertRaisesRegex(ValueError, "isolated apt-get failed"):
                self.run_isolated_client(root)
        self.assertIn("Hash Sum mismatch", self.client_output)
        self.assertEqual(len(observed), 1)
        address = urlsplit(observed[0])
        with self.assertRaises(OSError):
            with socket.create_connection((address.hostname, address.port), timeout=1):
                pass

    @unittest.skipUnless(shutil.which("apt-get"), "apt-get required")
    def test_client_signature_corruption_is_rejected_before_download(self):
        root = self.client_system_root()
        signature = self.dist / "InRelease"
        signature.write_text(signature.read_text().replace("Suite: stable", "Suite: tampered"))
        with self.assertRaisesRegex(ValueError, "gpgv failed"):
            self.run_isolated_client(root)

    @unittest.skipUnless(shutil.which("apt-get"), "apt-get required")
    def test_client_resolves_and_downloads_dependency_from_seeded_system_lists(self):
        root = self.client_system_root()
        dependencies = self.path / "dependency-repo"
        dependencies.mkdir()
        self.build_deb(dependencies, "fixture-extra", "amd64")
        self.maker(dependencies, self.env)
        (root / "etc/apt/sources.list").write_text(
            f"deb [trusted=yes] {dependencies.as_uri()} ./\n")
        seed = self.path / "seed-client"
        config = APT_CLIENT.apt_config(seed, root)
        execute("apt-get", "update", env=self.env | {"APT_CONFIG": str(config)})
        APT_CLIENT.copy_regular_files(seed / "state/lists", root / "var/lib/apt/lists")
        for payload in self.dist.glob("*.deb"):
            payload.unlink()
        self.build_deb(self.dist, "fixture-app", "amd64", depends="fixture-extra (= 1.0-1)")
        self.maker(self.dist, self.signed_env)
        output = self.run_isolated_client(root)
        self.assertIn("fixture-extra", output)
        self.assertIn("downloaded and verified all 1 exact package versions", output)

    @unittest.skipUnless(shutil.which("apt-get"), "apt-get required")
    def test_client_dependency_failure_cleans_state(self):
        root = self.client_system_root()
        for payload in self.dist.glob("*.deb"):
            payload.unlink()
        self.build_deb(self.dist, "fixture-app", "amd64", depends="fixture-missing (= 1.0-1)")
        self.maker(self.dist, self.signed_env)
        with self.assertRaisesRegex(ValueError, "isolated apt-get failed"):
            self.run_isolated_client(root)
        self.assertIn("unmet dependencies", self.client_output)

    @unittest.skipUnless(shutil.which("apt-get"), "apt-get required")
    def test_signed_apt_update_and_download_with_isolated_state(self):
        # APT_CONFIG redirects config loading before apt reads host apt.conf.d.
        # All lists, status, cache, logs, source configuration and downloads live
        # under this task's temporary directory. No install/root/sudo involved.
        apt = self.path / "apt"
        for directory in ("state/lists/partial", "cache/archives/partial", "etc/apt.conf.d", "log", "downloads"):
            (apt / directory).mkdir(parents=True)
        (apt / "state/status").touch()
        source = apt / "etc/sources.list"
        source.write_text(f"deb [signed-by={self.keys / 'asys-archive-keyring.gpg'}] {self.dist.as_uri()} ./\n")
        config = apt / "apt.conf"
        config.write_text(f'''Dir "{apt}";
Dir::State "{apt / 'state'}";
Dir::State::status "{apt / 'state/status'}";
Dir::Cache "{apt / 'cache'}";
Dir::Log "{apt / 'log'}";
Dir::Etc "{apt / 'etc'}";
Dir::Etc::main "-";
Dir::Etc::parts "{apt / 'etc/apt.conf.d'}";
Dir::Etc::sourcelist "{source}";
Dir::Etc::sourceparts "-";
Dir::Etc::trusted "-";
Dir::Etc::trustedparts "-";
APT::Sandbox::User "{getpass.getuser()}";
APT::Get::List-Cleanup "false";
Acquire::Languages "none";
''')
        env = self.env | {"APT_CONFIG": str(config)}
        update = execute("apt-get", "update", "-o", "APT::Update::Error-Mode=any", env=env, cwd=apt / "downloads")
        self.assertNotIn("NO_PUBKEY", update.stdout + update.stderr)
        self.assertTrue(any((apt / "state/lists").glob("*Packages*")))
        execute("apt-get", "download", "dcomp=1.0-1", env=env, cwd=apt / "downloads")
        downloaded = next((apt / "downloads").glob("dcomp*.deb"))
        self.assertEqual(downloaded.read_bytes(), (self.dist / downloaded.name).read_bytes())
        # The same isolated apt client must reject tampered signed metadata.
        (self.dist / "InRelease").write_text((self.dist / "InRelease").read_text().replace("Suite: stable", "Suite: tampered"))
        result = execute("apt-get", "update", "-o", "APT::Update::Error-Mode=any", env=env,
                         cwd=apt / "downloads", check=False)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
