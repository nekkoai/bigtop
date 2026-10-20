"""Exercise Capsa source preparation with fake compilers, not a native build."""
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CapsaCoreBuildTest(unittest.TestCase):
    def setUp(self):
        os_release = Path("/etc/os-release").read_text()
        if 'ID=ubuntu\n' not in os_release or 'VERSION_ID="24.04"' not in os_release:
            self.skipTest("the core build driver deliberately requires Ubuntu 24.04")
        if subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip() != "amd64":
            self.skipTest("the core build driver deliberately requires amd64")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / "ci").mkdir(parents=True)
        shutil.copy2(ROOT / "ci/build-capsa-core.sh", self.repo / "ci/")
        shutil.copytree(ROOT / "pkg/capsa", self.repo / "pkg/capsa")
        self.toolchain = tomllib.loads(
            (self.repo / "pkg/capsa/rust-toolchain.toml").read_text()
        )["toolchain"]["channel"]
        self.upstream = self.repo / "upstream/capsa"
        (self.upstream / "bin").mkdir(parents=True)
        (self.upstream / "Cargo.toml").write_text(
            '[package]\nname = "capsa"\nversion = "0.1.0"\nedition = "2024"\n'
        )
        (self.upstream / "Cargo.lock").write_text("# pinned fixture\nversion = 4\n")
        (self.upstream / "bin/capsa.rs").write_text("// pinned source\n")
        self.run_cmd("git", "init", "-q", str(self.repo))
        self.run_cmd("git", "init", "-q", str(self.upstream))
        self.run_cmd("git", "-C", str(self.upstream), "add", ".")
        self.commit_upstream()
        self.pin = self.run_cmd(
            "git", "-C", str(self.upstream), "rev-parse", "HEAD"
        ).stdout.strip()
        self.run_cmd(
            "git", "update-index", "--add", "--cacheinfo",
            f"160000,{self.pin},upstream/capsa"
        )
        self.bin = self.repo / "fake-bin"
        self.bin.mkdir()
        self.env = os.environ | {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "FIXTURE_LOG": str(self.repo / "tool.log"),
            "FIXTURE_TOOLCHAIN": self.toolchain,
        }
        self.write_tool("rustup", """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$FIXTURE_LOG"
if [ "$*" = 'toolchain list' ]; then
    printf '%s-x86_64-unknown-linux-gnu (default)\\n' "$FIXTURE_TOOLCHAIN"
elif [ "$*" = "run $FIXTURE_TOOLCHAIN rustc --version" ]; then
    printf 'rustc %s (fixture)\\n' "${FIXTURE_RUST_VERSION:-$FIXTURE_TOOLCHAIN}"
elif [ "$*" = "run $FIXTURE_TOOLCHAIN cargo --version" ]; then
    echo 'cargo 1.99.0 (fixture; patch version need not match rustc)'
elif [ "$*" = "run $FIXTURE_TOOLCHAIN cargo vendor --locked vendor" ]; then
    mkdir -p vendor/fixture
    echo 'fixture license' > vendor/fixture/LICENSE
    [ "${FIXTURE_CHANGE_LOCK:-0}" = 0 ] || echo '# changed' >> Cargo.lock
    printf '[source.crates-io]\\nreplace-with = "vendored-sources"\\n[source.vendored-sources]\\ndirectory = "vendor"\\n'
else
    echo "Unexpected fake rustup call: $*" >&2
    exit 2
fi
""")
        self.write_tool("dpkg-buildpackage", """#!/bin/sh
set -eu
test "$*" = '-us -uc -b'
test -x debian/rules
test -f rust-toolchain.toml
test -f .cargo/config.toml
test -f vendor/fixture/LICENSE
grep -q 'Package: capsa-core' debian/control
echo dpkg-buildpackage >> "$FIXTURE_LOG"
touch ../capsa-core_fixture_amd64.deb ../capsa_fixture_amd64.buildinfo
""")

    def run_cmd(self, *args, check=True, env=None):
        return subprocess.run(args, cwd=self.repo, env=env, text=True,
                              capture_output=True, check=check)

    def commit_upstream(self):
        self.run_cmd("git", "-C", str(self.upstream), "-c", "user.name=Fixture",
                     "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")

    def write_tool(self, name, contents):
        path = self.bin / name
        path.write_text(contents)
        path.chmod(0o755)

    def build(self, **overrides):
        # Ignore a caller's optional dpkg override: this test checks the default.
        env = self.env | overrides
        env.pop("DPKG_BUILDPACKAGE_OPTS", None)
        return self.run_cmd(str(self.repo / "ci/build-capsa-core.sh"),
                            check=False, env=env)

    def test_archives_index_pin_not_checkout_and_vendors_locked_sources(self):
        (self.upstream / "bin/capsa.rs").write_text("// different HEAD\n")
        self.run_cmd("git", "-C", str(self.upstream), "add", ".")
        self.commit_upstream()
        (self.upstream / "bin/capsa.rs").write_text("// dirty working tree\n")
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        work = self.repo / "build/capsa-core"
        archive = next(work.glob("*.orig.tar.xz"))
        with tarfile.open(archive) as tar:
            name = next(n for n in tar.getnames() if n.endswith("/bin/capsa.rs"))
            self.assertEqual(tar.extractfile(name).read(), b"// pinned source\n")
            names = tar.getnames()
            self.assertTrue(any(n.endswith("/vendor/fixture/LICENSE") for n in names))
            self.assertTrue(any(n.endswith("/.cargo/config.toml") for n in names))
        self.assertIn("cargo vendor --locked vendor", (self.repo / "tool.log").read_text())
        source = next(p for p in work.iterdir() if p.is_dir() and (p / "debian").exists())
        self.assertEqual((source / "Cargo.lock").read_text(), "# pinned fixture\nversion = 4\n")
        self.assertEqual((source / "debian/control").read_bytes(),
                         (ROOT / "pkg/capsa/debian/control").read_bytes())

    def test_wrong_rust_version_fails_before_source_preparation(self):
        result = self.build(FIXTURE_RUST_VERSION="1.75.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.repo / "build/capsa-core").exists())
        self.assertNotIn("vendor", (self.repo / "tool.log").read_text())

    def test_changed_cargo_lock_is_rejected_before_package_build(self):
        result = self.build(FIXTURE_CHANGE_LOCK="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cargo.lock", result.stderr)
        self.assertNotIn("dpkg-buildpackage", (self.repo / "tool.log").read_text())

    def test_package_revision_is_read_from_changelog(self):
        path = self.repo / "pkg/capsa/debian/changelog"
        text = path.read_text()
        old = text.split("(", 1)[1].split(")", 1)[0]
        path.write_text(text.replace(old, "0.1.0+git20261020.abcdef0-2", 1))
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.repo / "build/capsa-core" /
                         "capsa_0.1.0+git20261020.abcdef0.orig.tar.xz").is_file())


class CapsaCorePolicyTest(unittest.TestCase):
    def test_source_packages_have_generated_core_dependencies_and_inert_services(self):
        debian = ROOT / "pkg/capsa/debian"
        control = (debian / "control").read_text()
        self.assertEqual(control.count("\nPackage:"), 5)
        self.assertIn("Package: capsa-core", control)
        self.assertIn("Depends: ${shlibs:Depends}, ${misc:Depends}", control)
        self.assertFalse((debian / "capsa-core.postinst").exists())
        rules = (debian / "rules").read_text()
        self.assertIn("--locked", rules)
        self.assertIn("--offline", rules)
        self.assertIn("target-cpu=x86-64", rules)
        self.assertNotIn("target-cpu=native", rules)
        self.assertIn("dh_installsystemd --no-start --no-enable", rules)
        server = control.split("Package: capsa-server\n", 1)[1]
        self.assertNotIn("capsa-indexers", server.split("Suggests:", 1)[0])


if __name__ == "__main__":
    unittest.main()
