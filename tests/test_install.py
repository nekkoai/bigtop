"""Installer fixtures: real public-key inspection, no host apt or filesystem changes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("gpg"), "GnuPG required")
class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.bin = self.work / "bin"
        self.bin.mkdir()
        self.os_release = self.work / "os-release"
        self.os_release.write_text("ID=ubuntu\nVERSION_ID=24.04\n")
        # Redirect only the host OS probe; all commands run through offline stubs.
        script = (ROOT / "install.sh").read_text()
        self.assertEqual(script.count(". /etc/os-release"), 1)
        self.script = self.work / "install.sh"
        self.script.write_text(script.replace(". /etc/os-release", '. "$TEST_OS_RELEASE"'))
        self.env = os.environ | {
            "PATH": f"{self.bin}:/usr/bin:/bin", "TEST_ROOT": str(self.work),
            "TEST_OS_RELEASE": str(self.os_release),
            "TEST_KEY": str(ROOT / "keys/bigtop-archive-keyring.gpg"),
            "TEST_GPG": shutil.which("gpg"), "TMPDIR": str(self.work),
        }
        stub = self.bin / "stub"
        stub.write_text(f"#!{sys.executable}\n" + r'''
import json, os, pathlib, shutil, subprocess, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path(os.environ["TEST_ROOT"])
with (root / "calls").open("a") as log:
    log.write(json.dumps([name, *args]) + "\n")
if name == "dpkg":
    print(os.environ.get("TEST_ARCH", "amd64"))
elif name == "id":
    print(os.environ.get("TEST_UID", "1000"))
elif name == "sudo":
    sys.exit(0 if args == ["-v"] else subprocess.call(args))
elif name == "curl":
    if os.environ.get("TEST_CURL_FAIL"):
        sys.exit(22)
    assert "https://github.com/nekkoai/bigtop/releases/latest/download/bigtop-archive-keyring.gpg" in args
    shutil.copyfile(os.environ["TEST_KEY"], args[args.index("-o") + 1])
elif name == "gpg":
    mode = os.environ.get("TEST_GPG_MODE", "")
    if mode == "fail":
        sys.exit(2)
    if mode in ("wrong", "multiple"):
        print("pub:::::::::")
        print("fpr:::::::::" + ("0" * 40))
        if mode == "multiple":
            print("pub:::::::::")
    else:
        sys.exit(subprocess.call([os.environ["TEST_GPG"], *args]))
elif name == "install":
    dest = root / "system" / args[-1].lstrip("/")
    if args[0] == "-d":
        for directory in args[3:]:
            (root / "system" / directory.lstrip("/")).mkdir(parents=True, exist_ok=True)
    else:
        shutil.copyfile(args[-2], dest)
        dest.chmod(int(args[1], 8))
elif name == "apt-get":
    assert args == ["update", "-o", "APT::Update::Error-Mode=any"]
    sys.exit(1 if os.environ.get("TEST_APT_FAIL") else 0)
else:
    raise AssertionError(name)
''')
        stub.chmod(0o755)
        for name in ("dpkg", "id", "sudo", "curl", "gpg", "install", "apt-get"):
            (self.bin / name).symlink_to(stub)

    def run_installer(self, **env):
        return subprocess.run(["bash", "-x", str(self.script)], env=self.env | env,
                              text=True, capture_output=True)

    def calls(self):
        path = self.work / "calls"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_configures_repository_and_is_repeatable(self):
        for _ in range(2):
            result = self.run_installer()
            self.assertEqual(result.returncode, 0, result.stderr)
        system = self.work / "system"
        source = system / "etc/apt/sources.list.d/bigtop.sources"
        key = system / "usr/share/keyrings/bigtop-archive-keyring.gpg"
        self.assertEqual(source.read_bytes(), (ROOT / "ci/bigtop.sources").read_bytes())
        self.assertEqual(key.read_bytes(), Path(self.env["TEST_KEY"]).read_bytes())
        self.assertEqual(source.stat().st_mode & 0o777, 0o644)
        self.assertEqual(key.stat().st_mode & 0o777, 0o644)
        self.assertEqual([c for c in self.calls() if c[0] == "apt-get"],
                         [["apt-get", "update", "-o", "APT::Update::Error-Mode=any"]] * 2)
        self.assertIn(["sudo", "-v"], self.calls())
        self.assertFalse(list(self.work.glob("tmp.*")), "download scratch must be removed")

    def test_root_does_not_use_sudo(self):
        result = self.run_installer(TEST_UID="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c[0] == "sudo" for c in self.calls()))

    def test_unsupported_platforms_do_not_download_or_install(self):
        for os_release, arch in (("ID=debian\nVERSION_ID=12\n", "amd64"),
                                 ("ID=ubuntu\nVERSION_ID=22.04\n", "amd64"),
                                 ("ID=ubuntu\nVERSION_ID=24.04\n", "arm64")):
            with self.subTest(os_release=os_release, arch=arch):
                self.os_release.write_text(os_release)
                result = self.run_installer(TEST_ARCH=arch)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("requires Ubuntu 24.04 (amd64)", result.stderr)
        self.assertFalse(any(c[0] in ("curl", "install", "apt-get") for c in self.calls()))

    def test_download_and_key_errors_never_modify_system(self):
        for env in ({"TEST_CURL_FAIL": "1"}, {"TEST_GPG_MODE": "fail"},
                    {"TEST_GPG_MODE": "wrong"}, {"TEST_GPG_MODE": "multiple"}):
            with self.subTest(env=env):
                result = self.run_installer(**env)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.work / "system").exists())
                self.assertFalse(list(self.work.glob("tmp.*")))
        self.assertFalse(any(c[0] in ("install", "apt-get") for c in self.calls()))

    def test_update_failure_is_not_reported_as_success(self):
        result = self.run_installer(TEST_APT_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Bigtop repository configured.", result.stdout)

    def test_installer_fingerprint_matches_tracked_pin(self):
        fingerprint = (ROOT / "keys/FINGERPRINT").read_text().strip()
        self.assertIn(f"expected={fingerprint}\n", (ROOT / "install.sh").read_text())
        self.assertTrue(os.access(ROOT / "install.sh", os.X_OK))
