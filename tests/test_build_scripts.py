#!/usr/bin/env python3
"""Fast regression coverage for the packaging layout used by ci/build-*.sh."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PYTHON = shutil.which("python3") or "/usr/bin/python3"


class BuildScriptsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        shutil.copytree(ROOT / "ci", self.repo / "ci")
        shutil.copytree(ROOT / "pkg", self.repo / "pkg")
        self.bin = self.repo / "bin"
        self.bin.mkdir()
        self.write("go", "#!/bin/sh\nexit 0\n")
        self.write("python3", "#!/bin/sh\nif [ \"${1:-}\" = -m ] && [ \"${2:-}\" = pip ]; then exit 0; fi\nexec '" + PYTHON + "' \"$@\"\n")
        self.write("dpkg-buildpackage", "#!/bin/sh\nset -eu\ntest -f debian/changelog\ntest -f debian/control\ntest -x debian/rules\ntouch ../fixture-built.deb\n")
        self.env = os.environ | {"PATH": f"{self.bin}:/usr/bin:/bin"}

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text, directory=None):
        path = (directory or self.bin) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)
        return path

    def execute(self, *args, env=None):
        return subprocess.run(args, cwd=self.repo, env=env or self.env,
                              text=True, capture_output=True, check=True)

    def upstream(self, name, files):
        path = self.repo / "fixture-upstream" / name
        for relative, content in files.items():
            file = path / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(content)
            if relative == "tools/build-id":
                file.chmod(0o755)
        self.execute("git", "init", "-q", str(path))
        self.execute("git", "-C", str(path), "add", ".")
        self.execute("git", "-C", str(path), "-c", "user.name=test",
                 "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
        return path

    def test_source_builds_copy_new_packaging_directories(self):
        fixtures = {
            "asys": {
                "VERSION": "0.2.2\n", "tools/build-id": "#!/bin/sh\necho 0.2.2-deadbee\n",
                "asys-inference/go.mod": "module fixture/asys\n", "python/requirements-host.txt": "",
                "asys-human-interface/requirements-host.txt": "",
            },
            "dcomp": {"go.mod": "module fixture/dcomp\n"},
        }
        for name, files in fixtures.items():
            with self.subTest(name=name):
                upstream = self.upstream(name, files)
                self.execute(str(self.repo / "ci" / f"build-{name}.sh"), str(upstream))
                version = subprocess.check_output(
                    ["dpkg-parsechangelog", "-l", str(ROOT / "pkg" / name / "debian/changelog"), "-S", "Version"],
                    text=True).strip().rsplit("-", 1)[0]
                debian = self.repo / "build" / name / f"{name}-{version}" / "debian"
                self.assertEqual((debian / "control").read_bytes(),
                                 (ROOT / "pkg" / name / "debian/control").read_bytes())
                self.assertTrue(next((self.repo / "build" / name).glob("*.deb")).exists())

    def test_images_builds_a_valid_deb_from_matching_release(self):
        build = "0.2.2-deadbee"
        release = {"build": build, "images": {
            "runtime": {"ref": "example.invalid/asys-runtime:" + build},
            "worker": {"ref": "example.invalid/asys-worker:" + build},
        }}
        upstream = self.upstream("asys", {
            "tools/build-id": "#!/bin/sh\necho " + build + "\n",
            "release.json": json.dumps(release) + "\n",
        })
        make_log, docker_log = self.repo / "make.log", self.repo / "docker.log"
        self.write("make", "#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$MAKE_LOG\"\n")
        self.write("docker", "#!/bin/sh\n[ \"$1\" = save ]; shift\nprintf '%s\\n' \"$*\" > \"$DOCKER_LOG\"\nprintf 'image archive\\n'\n")
        env = self.env | {"MAKE_LOG": str(make_log), "DOCKER_LOG": str(docker_log)}
        self.execute(str(self.repo / "ci/build-images.sh"), str(upstream), env=env)
        deb = next((self.repo / "build/asys-images").glob("*.deb"))
        version = subprocess.check_output(
            ["dpkg-parsechangelog", "-l", str(ROOT / "pkg/asys/debian/changelog"), "-S", "Version"], text=True).strip()
        self.assertEqual(subprocess.check_output(["dpkg-deb", "-f", deb, "Version"], text=True).strip(), version)
        extract = self.repo / "extracted"
        self.execute("dpkg-deb", "-x", str(deb), str(extract))
        control = self.repo / "control"
        self.execute("dpkg-deb", "-e", str(deb), str(control))
        self.assertEqual(json.loads((extract / "usr/share/asys/images/release.json").read_text()), release)
        self.assertEqual(subprocess.check_output(["zstd", "-dc", extract / "usr/share/asys/images/asys-images.tar.zst"]), b"image archive\n")
        loader = extract / "usr/sbin/asys-images-load"
        self.assertEqual(loader.read_bytes(), (ROOT / "pkg/asys/images/debian/asys-images-load").read_bytes())
        self.assertTrue(loader.stat().st_mode & 0o111)
        for name in ("postinst", "postrm"):
            script = control / name
            self.assertEqual(script.read_bytes(), (ROOT / "pkg/asys/images/debian" / name).read_bytes())
            self.assertTrue(script.stat().st_mode & 0o111)
        self.assertEqual(make_log.read_text().strip(), "build ASYS_BUILD=" + build)
        self.assertEqual(docker_log.read_text().split(), [entry["ref"] for entry in release["images"].values()])


if __name__ == "__main__":
    unittest.main()
