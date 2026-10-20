#!/usr/bin/env python3
"""Offline release tests: disposable Git repositories and tiny, never-installed debs."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_version", ROOT / "ci/release-version.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "release_apt_validator", ROOT / "ci/validate-apt-repo.py")
validator = importlib.util.module_from_spec(VALIDATOR_SPEC)
VALIDATOR_SPEC.loader.exec_module(validator)
# Keep all/amd64 distinctions identical to the publication validator.
ARCHITECTURES = validator.EXPECTED_PACKAGES
PINS = {f"upstream/{name}": digit * 40 for name, digit in
        (("asys", "a"), ("dcomp", "b"), ("capsa", "c"), ("llama.cpp", "d"))}
BUILD_ID = "v0.17.91234567"
UNITS = {
    "dcomp": ("dcomp",),
    "asys": ("asys", "asys-flow-view", "asys-server"),
    "asys-images": ("asys-images",),
    "capsa-core": ("capsa-core", "capsa-federation", "capsa-indexers",
                   "capsa-services", "capsa-server"),
    "capsa-runtime": ("capsa-python-runtime",),
    "capsa-llama": ("capsa-llama-cpu",),
    "capsa-assets": ("capsa-embedding-assets-nomic",),
}
TOOLS = ("git", "dpkg", "dpkg-deb", "dpkg-parsechangelog")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@unittest.skipUnless(all(shutil.which(tool) for tool in TOOLS), "Git and dpkg tools required")
class ReleaseVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bigtop-release-tests-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        # Do not inherit signing, hooks, identity, alternate indexes or Git dirs.
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("GIT_")}
        self.env.update({
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_AUTHOR_DATE": "2026-10-19T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-10-19T00:00:00+00:00",
            "SOURCE_DATE_EPOCH": "1792368000", "LC_ALL": "C",
        })
        self.git("init", "--quiet", "--initial-branch=main")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", str(self.base / "no-hooks"))
        for path in release.CHANGELOGS.values():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / path, target)
        (self.root / ".gitmodules").write_text("".join(
            f'[submodule "{path}"]\n\tpath = {path}\n'
            f"\turl = https://example.invalid/{path}\n" for path in PINS))
        self.git("add", "pkg", ".gitmodules")
        # Index-only fake gitlinks: no submodule cloning or network traffic.
        for path, pin in PINS.items():
            self.git("update-index", "--add", "--cacheinfo", f"160000,{pin},{path}")
        self.git("commit", "--quiet", "-m", "fixture source pins and primary changelogs")
        self.commit("second repository commit")
        self.release = release.identity(self.root)
        self.original_versions = release.versions(self.root)
        self.build_id_script = self.root / "upstream/asys/tools/build-id"
        self.build_id_script.parent.mkdir(parents=True)
        self.set_build_id(BUILD_ID)
        self.data_root = self.base / "dataRoot"
        (self.data_root / "runs/active").mkdir(parents=True)
        (self.data_root / "runs/active/events.jsonl").write_bytes(b'{"preserve":"run"}\n')
        (self.data_root / "config.json").write_bytes(b'{"preserve":"config"}\n')
        self.data_snapshot = self.snapshot(self.data_root)
        self.addCleanup(self.assert_data_root_preserved)

    def assert_data_root_preserved(self):
        self.assertEqual(self.snapshot(self.data_root), self.data_snapshot)

    def run_tool(self, *args, check=True):
        return subprocess.run(
            [str(arg) for arg in args], env=self.env, check=check,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def git(self, *args):
        return self.run_tool("git", "-C", self.root, *args).stdout.strip()

    def commit(self, message):
        path = self.root / "history.txt"
        with path.open("a") as output:
            output.write(message + "\n")
        self.git("add", "history.txt")
        self.git("commit", "--quiet", "-m", message)

    @staticmethod
    def snapshot(root):
        return {str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*") if path.is_file()}

    def set_build_id(self, build_id):
        self.build_id_script.write_text(f"#!/bin/sh\nprintf '%s\\n' '{build_id}'\n")
        self.build_id_script.chmod(0o755)

    def cli(self, *args, root=None, sha=None, check=True):
        command = [sys.executable, ROOT / "ci/release-version.py", "--root", root or self.root]
        if sha is not None:
            command += ["--sha", sha]
        return self.run_tool(*command, *args, check=check)

    def make_deb(self, output, name, version=None, architecture=None, payload=b"fixture\n"):
        output.mkdir(parents=True, exist_ok=True)
        if version is None:
            family = next(key for key, names in release.FAMILIES.items() if name in names)
            version = release.versions(self.root)[family]
        architecture = architecture or ARCHITECTURES[name]
        with tempfile.TemporaryDirectory(dir=self.base, prefix="deb-") as directory:
            package = Path(directory)
            (package / "DEBIAN").mkdir()
            (package / "DEBIAN/control").write_text(
                f"Package: {name}\nVersion: {version}\nArchitecture: {architecture}\n"
                "Maintainer: Fixture <fixture@example.invalid>\n"
                "Description: tiny release test fixture\n")
            target = package / "usr/share" / name / "fixture"
            target.parent.mkdir(parents=True)
            target.write_bytes(payload)
            path = output / f"{name}_{version}_{architecture}.deb"
            self.run_tool("dpkg-deb", "--build", "--root-owner-group", "-Znone", package, path)
            return path

    def packages(self):
        release.stamp(self.root, self.release)
        source = self.base / "downloads"
        paths = {}
        current = release.versions(self.root)
        for unit, names in UNITS.items():
            for name in names:
                family = next(key for key, members in release.FAMILIES.items() if name in members)
                paths[name] = self.make_deb(source / unit, name, current[family])
        return source, paths

    def unit_inputs(self):
        source, paths = self.packages()
        records = {}
        for unit in UNITS:
            record = release.unit_record(self.root, source / unit, unit, self.release)
            path = source / unit / f"{unit}.build.json"
            path.write_text(json.dumps(record))
            records[unit] = path
        return source, paths, records

    def assert_collect_rejected(self, source, pattern, **kwargs):
        dist = self.base / "rejected-dist"
        with self.assertRaisesRegex(ValueError, pattern):
            release.collect(self.root, source, dist, **kwargs)
        self.assertFalse(dist.exists(), "invalid input must not publish any assets")

    def test_full_history_identity_is_deterministic_sha12_octal_not_first_parent(self):
        self.git("checkout", "--quiet", "-b", "side")
        (self.root / "side.txt").write_text("side\n")
        self.git("add", "side.txt")
        self.git("commit", "--quiet", "-m", "side")
        self.git("checkout", "--quiet", "main")
        self.commit("main")
        self.git("merge", "--quiet", "--no-ff", "side", "-m", "merge side")
        head = self.git("rev-parse", "HEAD")
        expected = {"sha": head, "commit_count": 5,
                    "tag": f"v0.5.9{int(head[:12], 16):o}",
                    "revision": f"bigtop.5.g{head[:12]}"}
        self.assertEqual(self.git("rev-list", "--first-parent", "--count", "HEAD"), "4")
        self.assertEqual(release.identity(self.root, head), expected)
        (self.root / "untracked").write_text("does not affect release identity\n")
        self.assertEqual(release.identity(self.root), expected)
        clone = self.base / "full-clone"
        self.run_tool("git", "clone", "--quiet", "--no-checkout", self.root.as_uri(), clone)
        self.assertEqual(release.identity(clone), expected)
        self.assertEqual(json.loads(self.cli("identity", sha=head).stdout), expected)

    def test_identity_github_outputs_append_and_sha_mismatch_is_rejected(self):
        output = self.base / "github-output"
        output.write_text("preserve=previous-step\n")
        self.cli("identity", "--github-output", output)
        self.assertEqual(output.read_text().splitlines(), ["preserve=previous-step"] + [
            f"{key}={value}" for key, value in self.release.items()])
        with self.assertRaisesRegex(ValueError, "workflow commit"):
            release.identity(self.root, "f" * 40)
        before = self.snapshot(self.root / "pkg")
        result = self.cli("stamp", sha="f" * 40, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("workflow commit", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.snapshot(self.root / "pkg"), before)

    def test_real_shallow_checkout_is_rejected(self):
        clone = self.base / "shallow"
        self.run_tool("git", "clone", "--quiet", "--depth=1", "--no-checkout",
                      self.root.as_uri(), clone)
        self.assertEqual(self.run_tool("git", "-C", clone, "rev-parse",
                                      "--is-shallow-repository").stdout.strip(), "true")
        with self.assertRaisesRegex(ValueError, "fetch-depth: 0"):
            release.identity(clone)
        result = self.cli("identity", root=clone, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fetch-depth: 0", result.stderr)

    def test_stamp_only_workspace_revision_is_idempotent_and_preserves_state(self):
        index = (self.root / ".git/index").read_bytes()
        tree = self.git("ls-tree", "-r", "HEAD")
        modules = (self.root / ".gitmodules").read_bytes()
        before = self.snapshot(self.root / "pkg")
        self.assertEqual(release.source_pins(self.root), PINS)
        expected = {family: f"{version}.{self.release['revision']}"
                    for family, version in self.original_versions.items()}
        self.assertEqual(json.loads(self.cli("stamp", sha=self.release["sha"]).stdout), expected)
        once = self.snapshot(self.root / "pkg")
        self.assertEqual(release.stamp(self.root, self.release), expected)
        self.assertEqual(self.snapshot(self.root / "pkg"), once)
        for family, path in release.CHANGELOGS.items():
            relative = str(Path(path).relative_to("pkg"))
            self.assertEqual(once[relative], before[relative].replace(
                f"({self.original_versions[family]})".encode(),
                f"({expected[family]})".encode(), 1))
            upstream, revision = expected[family].rsplit("-", 1)
            self.assertEqual(upstream, self.original_versions[family].rsplit("-", 1)[0])
            self.assertTrue(revision.endswith(self.release["revision"]))
            self.run_tool("dpkg", "--compare-versions", expected[family], "gt",
                          self.original_versions[family])
        self.assertEqual((self.root / ".git/index").read_bytes(), index)
        self.assertEqual(self.git("ls-tree", "-r", "HEAD"), tree)
        self.assertEqual((self.root / ".gitmodules").read_bytes(), modules)
        self.assertEqual(release.source_pins(self.root), PINS)
        self.assertEqual(self.snapshot(self.data_root), self.data_snapshot)

    def test_restamp_replaces_old_suffix_and_dpkg_orders_numeric_counts(self):
        for count in (9, 10, 99, 100):
            identity = {**self.release, "revision": f"bigtop.{count}.g0123456789ab"}
            current = release.stamp(self.root, identity)
            for family, value in current.items():
                self.assertEqual(value, f"{self.original_versions[family]}.{identity['revision']}")
                if count != 9:
                    self.run_tool("dpkg", "--compare-versions", value, "gt", previous[family])
            previous = current
        for family, version in previous.items():
            upstream, revision = self.original_versions[family].rsplit("-", 1)
            next_revision = f"{upstream}-{int(revision) + 1}"
            self.run_tool("dpkg", "--compare-versions", version, "lt", next_revision)

    def test_unsupported_versions_fail_before_any_changelog_is_written(self):
        path = self.root / release.CHANGELOGS["capsa"]
        original = path.read_text()
        base = self.original_versions["capsa"]
        for bad in ("1.0", "1.0~rc1-1", "1.0-1.bigtop.invalid"):
            with self.subTest(version=bad):
                path.write_text(original.replace(f"({base})", f"({bad})", 1))
                before = self.snapshot(self.root / "pkg")
                with self.assertRaisesRegex(ValueError, "unsupported release package version"):
                    release.stamp(self.root, self.release)
                self.assertEqual(self.snapshot(self.root / "pkg"), before)
        path.write_text(original)

    def test_source_pins_are_exactly_four_committed_gitlinks(self):
        self.assertEqual(release.source_pins(self.root), PINS)
        # A staged pin is not the release pin until committed.
        self.git("update-index", "--cacheinfo", f"160000,{'e' * 40},upstream/asys")
        self.assertEqual(release.source_pins(self.root), PINS)
        self.git("update-index", "--force-remove", "upstream/llama.cpp")
        self.git("commit", "--quiet", "-m", "missing source pin")
        with self.assertRaisesRegex(ValueError, "all four source pins"):
            release.source_pins(self.root)

    def test_build_units_partition_all_thirteen_packages(self):
        self.assertEqual(release.BUILD_UNITS, UNITS)
        names = [name for members in UNITS.values() for name in members]
        self.assertEqual(len(names), 13)
        self.assertEqual(len(set(names)), 13)
        self.assertEqual(set(names), set(ARCHITECTURES))

    def test_all_thirteen_records_exact_versions_architectures_sizes_hashes(self):
        source, paths = self.packages()
        records = release.package_records(self.root, list(reversed(list(paths.values()))))
        self.assertEqual([row["package"] for row in records], sorted(ARCHITECTURES))
        for row in records:
            name = row["package"]
            family = next(key for key, names in release.FAMILIES.items() if name in names)
            version = f"{self.original_versions[family]}.{self.release['revision']}"
            self.assertEqual(row, {
                "package": name, "version": version, "architecture": ARCHITECTURES[name],
                "filename": f"{name}_{version}_{ARCHITECTURES[name]}.deb",
                "size": paths[name].stat().st_size, "sha256": sha256(paths[name]),
            })
        dist = self.base / "dist"
        dist.mkdir()  # An existing empty destination is allowed.
        self.assertEqual(release.collect(self.root, source, dist), records)
        self.assertEqual(set(path.name for path in dist.iterdir()),
                         {path.name for path in paths.values()})
        for path in paths.values():
            self.assertEqual((dist / path.name).read_bytes(), path.read_bytes())

    def test_package_records_accepts_each_independent_unit_subset(self):
        _, paths = self.packages()
        for unit, names in UNITS.items():
            with self.subTest(unit=unit):
                rows = release.package_records(self.root, [paths[name] for name in names], [unit])
                self.assertEqual([row["package"] for row in rows], sorted(names))
        units = ["asys", "asys-images"]
        names = [name for unit in units for name in UNITS[unit]]
        self.assertEqual(len(release.package_records(
            self.root, [paths[name] for name in names], units)), 4)

    def test_collection_rejects_missing_duplicate_unexpected_and_wrong_version(self):
        source, paths = self.packages()
        path = paths["dcomp"]
        saved = path.read_bytes()
        path.unlink()
        self.assert_collect_rejected(source, "missing.*dcomp")
        path.write_bytes(saved)
        duplicate = source / "duplicate" / path.name
        duplicate.parent.mkdir()
        duplicate.write_bytes(saved)
        self.assert_collect_rejected(source, "duplicate package: dcomp")
        duplicate.unlink()
        extra = self.make_deb(source / "extra", "not-a-release-package", "1.0-1", "all")
        self.assert_collect_rejected(source, "unexpected or duplicate package")
        extra.unlink()
        path.unlink()
        for version in (self.original_versions["dcomp"], "9.0-1"):
            with self.subTest(version=version):
                wrong = self.make_deb(source / "dcomp", "dcomp", version)
                self.assert_collect_rejected(source, "dcomp: expected")
                wrong.unlink()

    def test_package_records_rejects_wrong_architecture(self):
        for name in ("dcomp", "asys-server"):
            with self.subTest(package=name):
                unit = next(unit for unit, names in UNITS.items() if name in names)
                paths = [self.make_deb(self.base / name, member, architecture=(
                    "arm64" if member == name else ARCHITECTURES[member]))
                         for member in UNITS[unit]]
                with self.assertRaisesRegex(ValueError, "[Aa]rchitecture"):
                    release.package_records(self.root, paths, [unit])

    def test_package_records_rejects_noncanonical_name_symlink_or_directory(self):
        path = self.make_deb(self.base / "input", "dcomp")
        renamed = path.with_name("renamed.deb")
        path.rename(renamed)
        with self.assertRaisesRegex(ValueError, "noncanonical package filename"):
            release.package_records(self.root, [renamed], ["dcomp"])
        path.symlink_to(renamed)
        with self.assertRaisesRegex(ValueError, "not a regular package file"):
            release.package_records(self.root, [path], ["dcomp"])
        with self.assertRaisesRegex(ValueError, "not a regular package file"):
            release.package_records(self.root, [path.parent], ["dcomp"])

    def test_collection_refuses_stale_destination_without_changing_it(self):
        source, _ = self.packages()
        dist = self.base / "dist"
        dist.mkdir()
        (dist / "old.deb").write_bytes(b"stale package must not be overwritten")
        before = self.snapshot(dist)
        with self.assertRaisesRegex(ValueError, "destination must be empty"):
            release.collect(self.root, source, dist)
        self.assertEqual(self.snapshot(dist), before)

    def test_seven_unit_records_have_exact_identity_sourcepins_packages_and_build_id(self):
        source, paths, records = self.unit_inputs()
        for unit, record_path in records.items():
            with self.subTest(unit=unit):
                record = json.loads(record_path.read_text())
                expected = {**self.release, "component": unit, "source_pins": PINS,
                            "packages": release.package_records(
                                self.root, [paths[name] for name in UNITS[unit]], [unit])}
                if unit in {"asys", "asys-images"}:
                    expected["asys_build"] = BUILD_ID
                self.assertEqual(record, expected)
        dist = self.base / "dist"
        result = self.cli("collect", "--input", source, "--dist", dist,
                          "--require-build-records", sha=self.release["sha"])
        self.assertEqual(len(json.loads(result.stdout)), 13)
        self.assertEqual(len(list(dist.iterdir())), 13)
        self.assertFalse(list(dist.glob("*.json")), "unit metadata is not a release asset")

    def test_record_unit_cli_and_subset_collection(self):
        source, paths = self.packages()
        record_path = source / "capsa-runtime/metadata/runtime.build.json"
        result = self.cli("record-unit", "--component", "capsa-runtime", "--input",
                          source / "capsa-runtime", "--output", record_path,
                          sha=self.release["sha"])
        record = json.loads(record_path.read_text())
        self.assertEqual(json.loads(result.stdout), record)
        self.assertEqual(record["component"], "capsa-runtime")
        self.assertEqual(record["source_pins"], PINS)
        dist = self.base / "runtime-dist"
        result = self.cli("collect", "--input", source / "capsa-runtime", "--dist", dist,
                          "--units", "capsa-runtime", "--require-build-records")
        self.assertEqual(len(json.loads(result.stdout)), 1)
        self.assertEqual([path.name for path in dist.iterdir()],
                         [paths["capsa-python-runtime"].name])
        self.assert_collect_rejected(source, "unexpected or duplicate package",
                                     units=["capsa-runtime"])

    def test_each_unit_rejects_cross_run_identity_even_with_same_versions_and_pins(self):
        source, _, records = self.unit_inputs()
        # A different real repository commit need not change package versions/pins.
        prior = self.git("rev-parse", "HEAD~1")
        foreign = {"sha": prior, "commit_count": 1,
                   "tag": f"v0.1.9{int(prior[:12], 16):o}",
                   "revision": f"bigtop.1.g{prior[:12]}"}
        for unit, path in records.items():
            with self.subTest(unit=unit):
                original = path.read_text()
                record = json.loads(original)
                packages, pins = record["packages"], record["source_pins"]
                record.update(foreign)
                self.assertEqual(record["packages"], packages)
                self.assertEqual(record["source_pins"], pins)
                path.write_text(json.dumps(record))
                self.assert_collect_rejected(source, "different repository commits",
                                             require_build_records=True)
                path.write_text(original)

    def test_all_release_identity_fields_are_checked_not_only_sha(self):
        source, _, paths = self.unit_inputs()
        path = paths["capsa-assets"]
        original = path.read_text()
        for key, value in (("sha", "e" * 40), ("commit_count", 999),
                           ("tag", "v0.999.9123"), ("revision", "bigtop.999.g0123456789ab")):
            with self.subTest(field=key):
                record = json.loads(original)
                record[key] = value
                path.write_text(json.dumps(record))
                self.assert_collect_rejected(source, "different repository commits",
                                             require_build_records=True)
        path.write_text(original)

    def test_each_unit_rejects_wrong_sourcepins_and_package_hash_metadata(self):
        source, _, paths = self.unit_inputs()
        for unit, path in paths.items():
            original = path.read_text()
            for field in ("source_pins", "sha256"):
                with self.subTest(unit=unit, field=field):
                    record = json.loads(original)
                    if field == "source_pins":
                        record[field]["upstream/asys"] = "e" * 40
                        pattern = "different upstream pins"
                    else:
                        record["packages"][0][field] = "0" * 64
                        pattern = "does not match its provenance"
                    path.write_text(json.dumps(record))
                    self.assert_collect_rejected(source, pattern, require_build_records=True)
            path.write_text(original)

    def test_package_provenance_requires_exact_size_filename_arch_and_complete_list(self):
        source, _, paths = self.unit_inputs()
        path = paths["capsa-core"]
        original = path.read_text()
        for field, value in (("size", 1), ("filename", "wrong.deb"),
                             ("architecture", "arm64"), ("version", "1.0-1"),
                             ("package", "wrong-package")):
            with self.subTest(field=field):
                record = json.loads(original)
                record["packages"][0][field] = value
                path.write_text(json.dumps(record))
                self.assert_collect_rejected(source, "does not match its provenance",
                                             require_build_records=True)
        record = json.loads(original)
        record["packages"].pop()
        path.write_text(json.dumps(record))
        self.assert_collect_rejected(source, "does not match its provenance",
                                     require_build_records=True)
        path.write_text(original)

    def test_changed_archive_bytes_reject_even_when_control_version_and_pins_match(self):
        source, paths, _ = self.unit_inputs()
        path = paths["dcomp"]
        original = path.read_bytes()
        self.make_deb(path.parent, "dcomp", payload=b"another run, same control metadata\n")
        self.assertNotEqual(path.read_bytes(), original)
        self.assert_collect_rejected(source, "does not match its provenance",
                                     require_build_records=True)

    def test_missing_duplicate_unknown_and_symlink_build_records_are_rejected(self):
        source, _, records = self.unit_inputs()
        path = records["capsa-runtime"]
        original = path.read_text()
        path.unlink()
        self.assert_collect_rejected(source, "missing build provenance", require_build_records=True)
        path.write_text(original)
        duplicate = source / "duplicate.build.json"
        duplicate.write_text(original)
        self.assert_collect_rejected(source, "duplicate build provenance", require_build_records=True)
        duplicate.unlink()
        record = json.loads(original)
        record["component"] = "unknown-unit"
        path.write_text(json.dumps(record))
        self.assert_collect_rejected(source, "unexpected/duplicate build provenance",
                                     require_build_records=True)
        path.unlink()
        outside = self.base / "outside.json"
        outside.write_text(original)
        path.symlink_to(outside)
        self.assert_collect_rejected(source, "provenance must be a regular file",
                                     require_build_records=True)

    def test_missing_record_is_rejected_for_every_independent_unit(self):
        source, _, paths = self.unit_inputs()
        for unit, path in paths.items():
            with self.subTest(unit=unit):
                original = path.read_bytes()
                path.unlink()
                self.assert_collect_rejected(source, "missing build provenance",
                                             require_build_records=True)
                path.write_bytes(original)

    def test_asys_host_image_build_ids_must_match_and_be_present(self):
        source, _, records = self.unit_inputs()
        path = records["asys-images"]
        original = path.read_text()
        for value in (None, "", "different-build-id"):
            with self.subTest(build_id=value):
                record = json.loads(original)
                if value is None:
                    record.pop("asys_build")
                else:
                    record["asys_build"] = value
                path.write_text(json.dumps(record))
                self.assert_collect_rejected(source, "build IDs differ", require_build_records=True)
        path.write_text(original)
        host = records["asys"]
        record = json.loads(host.read_text())
        record.pop("asys_build")
        host.write_text(json.dumps(record))
        self.assert_collect_rejected(source, "build IDs differ", require_build_records=True)

    def test_unit_record_rejects_dirty_asys_build_for_host_and_images(self):
        source, _ = self.packages()
        self.set_build_id(BUILD_ID + "-dirty.123")
        for unit in ("asys", "asys-images"):
            with self.subTest(unit=unit):
                with self.assertRaisesRegex(ValueError, "asys source was modified"):
                    release.unit_record(self.root, source / unit, unit, self.release)

    def test_manifest_records_all_provenance_and_checksums_every_release_asset(self):
        source, _, _ = self.unit_inputs()
        dist = self.base / "dist"
        records = release.collect(self.root, source, dist, require_build_records=True)
        # Representative apt metadata and trust exports, generated as inert bytes.
        assets = ("Packages", "Packages.gz", "Release", "InRelease", "Release.gpg",
                  "asys.sources", "asys-archive-keyring.gpg", "asys-archive-keyring.asc")
        for name in assets:
            (dist / name).write_bytes(f"fixture {name}\n".encode())
        (dist / "SHA256SUMS").write_text("obsolete manifest must be replaced\n")
        repository = "fixture/bigtop"
        result = self.cli("manifest", "--dist", dist, "--repository", repository,
                          sha=self.release["sha"])
        expected = {
            "format": 1, "repository": repository, "tag": self.release["tag"],
            "sha": self.release["sha"], "commit_count": self.release["commit_count"],
            "source_pins": PINS, "packages": records,
        }
        self.assertEqual(json.loads(result.stdout), expected)
        self.assertEqual(json.loads((dist / "release.json").read_text()), expected)
        sums = (dist / "SHA256SUMS").read_text()
        expected_paths = sorted(path for path in dist.iterdir() if path.name != "SHA256SUMS")
        self.assertEqual(sums, "".join(f"{sha256(path)}  {path.name}\n" for path in expected_paths))
        self.assertEqual(len(sums.splitlines()), 22)  # 13 debs + 8 apt assets + release.json
        self.assertEqual({line.split("  ", 1)[1] for line in sums.splitlines()},
                         {path.name for path in expected_paths})
        self.assertNotIn("SHA256SUMS", {line.split("  ", 1)[1] for line in sums.splitlines()})
        before = self.snapshot(dist)
        self.assertEqual(release.manifest(self.root, dist, repository, self.release), expected)
        self.assertEqual(self.snapshot(dist), before)

    def test_manifest_rejects_missing_package_invalid_repository_and_unsafe_asset(self):
        source, _ = self.packages()
        dist = self.base / "dist"
        release.collect(self.root, source, dist)
        for repository in ("bad", "a/b/c", "../bad/repo", "owner/repo\n"):
            with self.subTest(repository=repository):
                with self.assertRaisesRegex(ValueError, "OWNER/REPO"):
                    release.manifest(self.root, dist, repository, self.release)
        for kind in ("directory", "symlink", "unsafe name"):
            with self.subTest(kind=kind):
                path = dist / kind
                if kind == "directory":
                    path.mkdir()
                elif kind == "symlink":
                    path.symlink_to("Release")
                else:
                    path.write_bytes(b"unsafe")
                with self.assertRaisesRegex(ValueError, "unsafe release asset"):
                    release.manifest(self.root, dist, "fixture/bigtop", self.release)
                if path.is_dir():
                    path.rmdir()
                else:
                    path.unlink()
        next(dist.glob("dcomp_*.deb")).unlink()
        with self.assertRaisesRegex(ValueError, "missing.*dcomp"):
            release.manifest(self.root, dist, "fixture/bigtop", self.release)


if __name__ == "__main__":
    unittest.main()
