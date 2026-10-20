"""Check release gates and the parallel build DAG, without GitHub or Docker."""
from pathlib import Path
import re
import unittest

try:
    import yaml
except ImportError:
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
UNITS = {"dcomp", "asys", "asys-images", "capsa-core", "capsa-runtime",
         "capsa-llama", "capsa-assets"}


@unittest.skipIf(yaml is None, "workflow graph tests require python3-yaml")
class ReleaseWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.workflow = self.load("build.yml")
        self.jobs = self.workflow["jobs"]

    def load(self, name):
        # BaseLoader keeps YAML 1.1 from changing the Actions 'on' key to True.
        return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)

    def needs(self, name):
        value = self.jobs[name].get("needs", [])
        return {value} if isinstance(value, str) else set(value)

    def ancestors(self, name):
        result = self.needs(name)
        for parent in tuple(result):
            result |= self.ancestors(parent)
        return result

    def condition(self, name, states=None, event="push", ref="refs/heads/main", canceled=False):
        text = self.jobs[name]["if"].removeprefix("${{").removesuffix("}}").strip()
        text = re.sub(r"needs\.([\w-]+)\.result",
                      lambda match: repr((states or {}).get(match[1], "success")), text)
        text = text.replace("github.event_name", repr(event)).replace("github.ref", repr(ref))
        text = text.replace("!cancelled()", repr(not canceled))
        text = text.replace("&&", " and ").replace("||", " or ")
        return eval(text, {"__builtins__": {}}, {})

    def test_every_push_main_builds_no_path_or_tag_filter(self):
        trigger = self.workflow["on"]
        self.assertEqual(trigger["push"], {"branches": ["main"]})
        self.assertIn("pull_request", trigger)
        self.assertIn("workflow_dispatch", trigger)
        self.assertNotIn("pull_request_target", trigger)
        self.assertNotIn("workflow_run", trigger)
        self.assertNotIn("concurrency", self.workflow)

    def test_seven_parallel_independent_build_units(self):
        self.assertEqual({job["with"]["component"] for job in self.jobs.values()
                          if job.get("uses", "").endswith("build-package.yml")}, UNITS)
        for unit in UNITS:
            self.assertEqual(self.needs(unit), {"identity", "tag"})
            self.assertEqual(self.jobs[unit]["with"]["component"], unit)
            self.assertFalse(self.needs(unit) & UNITS)
            self.assertTrue(self.condition(unit, {"tag": "skipped"}, event="pull_request"))
            self.assertTrue(self.condition(unit))
            for failure in ("failure", "cancelled"):
                self.assertFalse(self.condition(unit, {"tag": failure}))
                self.assertFalse(self.condition(unit, {"identity": failure}))
            self.assertFalse(self.condition(unit, canceled=True))
        builder = self.load("build-package.yml")["jobs"]["package"]
        self.assertEqual(builder["runs-on"], "ubuntu-24.04")
        self.assertEqual(builder["steps"][0]["with"]["fetch-depth"], "0")
        commands = "\n".join(step.get("run", "") for step in builder["steps"])
        self.assertIn("record-unit", commands)
        self.assertIn("ci/release-version.py", commands)
        for document in (self.workflow, self.load("build-package.yml")):
            for job in document["jobs"].values():
                for step in job.get("steps", []):
                    if step.get("uses", "").startswith("docker/setup-buildx-action"):
                        self.assertEqual(step["with"]["driver"], "docker")
                    if step.get("uses", "").startswith("actions/upload-artifact"):
                        self.assertEqual(step["with"]["overwrite"], "true")

    def test_integration_and_repository_join_all_builds(self):
        self.assertEqual(self.needs("asys-test"), {"asys", "asys-images", "dcomp"})
        self.assertEqual(self.needs("capsa-test"), UNITS - self.needs("asys-test"))
        self.assertTrue(UNITS <= self.ancestors("apt-repo"))
        self.assertTrue({"checks", "asys-test", "capsa-test"} <= self.needs("apt-repo"))
        self.assertTrue(UNITS <= self.ancestors("publish"))
        for test in ("asys-test", "capsa-test", "apt-repo", "publish"):
            for parent in self.needs(test):
                self.assertFalse(self.condition(test, {parent: "failure"}), (test, parent))
                self.assertFalse(self.condition(test, {parent: "skipped"}), (test, parent))

    def test_remote_writes_and_signing_are_main_only(self):
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        writable = {name for name, job in self.jobs.items()
                    if job.get("permissions", {}).get("contents") == "write"}
        self.assertEqual(writable, {"tag", "publish"})
        for name in writable:
            self.assertFalse(self.condition(name, event="pull_request"))
            self.assertFalse(self.condition(name, event="workflow_dispatch"))
            self.assertFalse(self.condition(name, ref="refs/heads/other"))
            self.assertTrue(self.condition(name))
        for name, job in self.jobs.items():
            self.assertNotIn("secrets", job)
            for step in job.get("steps", []):
                if "APT_GPG_PRIVATE_KEY" in step.get("env", {}):
                    self.assertEqual(name, "publish")
                if step.get("uses", "").startswith("actions/checkout"):
                    self.assertEqual(step["with"]["persist-credentials"], "false")

    def test_publication_queue_and_validation_precede_upload(self):
        concurrency = self.jobs["publish"]["concurrency"]
        self.assertEqual(concurrency["queue"], "max")
        self.assertNotEqual(concurrency.get("cancel-in-progress"), "true")
        self.assertEqual(self.jobs["tag"]["concurrency"]["queue"], "max")
        steps = self.jobs["publish"]["steps"]
        validation = next(i for i, step in enumerate(steps)
                          if "--require-signature" in step.get("run", ""))
        apt = next(i for i, step in enumerate(steps) if "ci/apt-repo-test.sh" in step.get("run", ""))
        publication = next(i for i, step in enumerate(steps) if "github-release.py publish" in step.get("run", ""))
        self.assertLess(validation, apt)
        self.assertLess(apt, publication)
        self.assertEqual(self.jobs["publish"]["env"]["APT_REQUIRE_SIGNATURE"], "1")
        self.assertEqual(steps[-1]["if"], "always()")
