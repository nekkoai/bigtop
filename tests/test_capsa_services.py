"""Fixture coverage for Capsa's inert service/operator helpers; no systemd is used."""
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import json
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "pkg/capsa/services/lib/capsa-service-helper"
NAMES = ("drive", "slides", "confluence", "jira")
loader = importlib.machinery.SourceFileLoader("capsa_service_helper", str(HELPER))
spec = importlib.util.spec_from_loader(loader.name, loader)
SERVICE = importlib.util.module_from_spec(spec)
loader.exec_module(SERVICE)


class CapsaServicesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.config = base / "etc/capsa"
        self.snapshots = base / "snapshots"
        self.assets = base / "assets"
        self.config.mkdir(parents=True)
        self.snapshots.mkdir()
        self.paths = self.config / "paths.json"
        self.paths.write_text(json.dumps({"snapshot_root": str(self.snapshots)}))
        self.paths.chmod(0o444)
        self.make_assets()
        self.databases = {}
        for name in NAMES:
            database = self.snapshots / f"{name}.db"
            db = sqlite3.connect(database)
            db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, text TEXT)")
            db.execute("INSERT INTO documents(text) VALUES (?)", (name,))
            db.commit()
            db.close()
            database.chmod(0o444)
            self.databases[name] = database
        self.env = os.environ | {
            "CAPSA_CONFIG_DIR": str(self.config),
        }

    def make_assets(self):
        model = self.assets / "model.gguf"
        model.parent.mkdir(parents=True)
        model.write_bytes(b"fixture model")
        model.chmod(0o444)
        repo = self.assets / "tokenizers/hub/models--nomic-ai--nomic-embed-text-v1.5"
        blob = repo / "blobs/688882a79f44442ddc1f60d70334a7ff5df0fb47"
        blob.parent.mkdir(parents=True)
        blob.write_bytes(b"fixture tokenizer")
        blob.chmod(0o444)
        snapshot = repo / "snapshots/e9b6763023c676ca8431644204f50c2b100d9aab"
        snapshot.mkdir(parents=True)
        (snapshot / "tokenizer.json").symlink_to("../../blobs/688882a79f44442ddc1f60d70334a7ff5df0fb47")
        ref = repo / "refs"
        ref.mkdir()
        (ref / "main").write_text("e9b6763023c676ca8431644204f50c2b100d9aab")
        (ref / "main").chmod(0o444)
        manifest = self.assets / "embedding-assets-nomic/manifest.json"
        manifest.parent.mkdir()
        manifest.write_text(json.dumps({
            "model": {"sha256": hashlib.sha256(b"fixture model").hexdigest(),
                      "bytes": len(b"fixture model")},
            "tokenizer": {
                "sha256": hashlib.sha256(b"fixture tokenizer").hexdigest(),
                "bytes": len(b"fixture tokenizer"),
                "revision": "e9b6763023c676ca8431644204f50c2b100d9aab",
                "hf_home_layout": {
                    "blob": "hub/models--nomic-ai--nomic-embed-text-v1.5/blobs/688882a79f44442ddc1f60d70334a7ff5df0fb47",
                    "snapshot": "hub/models--nomic-ai--nomic-embed-text-v1.5/snapshots/e9b6763023c676ca8431644204f50c2b100d9aab/tokenizer.json",
                    "snapshot_symlink_target": "../../blobs/688882a79f44442ddc1f60d70334a7ff5df0fb47",
                    "ref_file": "hub/models--nomic-ai--nomic-embed-text-v1.5/refs/main",
                    "ref_contents": "e9b6763023c676ca8431644204f50c2b100d9aab",
                },
            },
        }))
        manifest.chmod(0o444)

    def managed_helper(self, env=None):
        # Unit tests mock privilege directly; production has no test-only bypass.
        return _HelperContext(self.env if env is None else env, self.assets)

    def register_all(self):
        with self.managed_helper(), contextlib.redirect_stdout(io.StringIO()):
            for name in NAMES:
                SERVICE.register_snapshot(name, str(self.databases[name]), checkpointed=True)

    def test_registration_only_reads_checkpointed_snapshots_and_generates_sha_generations(self):
        before = {name: self.databases[name].stat() for name in NAMES}
        self.register_all()
        for name in NAMES:
            after = self.databases[name].stat()
            self.assertEqual((after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                             (before[name].st_size, before[name].st_mtime_ns, before[name].st_ctime_ns))
            backend = json.loads((self.config / "backends" / f"{name}.json").read_text())
            digest = hashlib.sha256(self.databases[name].read_bytes()).hexdigest()
            self.assertEqual(backend["sha256"], digest)
            self.assertEqual(backend["generation"], "sha256:" + digest)
            self.assertTrue(backend["immutable_ready"])
            self.assertTrue(backend["checkpointed"])
        federation = json.loads((self.config / "federation.json").read_text())
        self.assertEqual([source["name"] for source in federation["sources"]], list(NAMES))
        self.assertTrue(all(source["immutable"] for source in federation["sources"]))

    def test_registration_requires_actual_root_and_explicit_checkpoint_confirmation(self):
        with patch.object(SERVICE.os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(RuntimeError, "run as root"):
                SERVICE.register_snapshot("drive", str(self.databases["drive"]), checkpointed=True)
        with self.managed_helper():
            with self.assertRaisesRegex(RuntimeError, "--checkpointed"):
                SERVICE.register_snapshot("drive", str(self.databases["drive"]), checkpointed=False)
        self.assertFalse((self.config / "backends/drive.json").exists())

    def test_paths_json_only_allows_a_nonwritable_absolute_snapshot_root(self):
        self.paths.chmod(0o644)
        self.paths.write_text(json.dumps({"snapshot_root": "/var/lib/capsa/not-a-snapshot"}))
        self.paths.chmod(0o444)
        with self.managed_helper():
            with self.assertRaisesRegex(RuntimeError, "writable Capsa service area"):
                SERVICE.paths()

    def test_embedding_readiness_uses_loopback_health_without_a_listener(self):
        observed = []

        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): return None

        class Opener:
            def open(self, request, timeout):
                observed.append((request.full_url, timeout))
                return Response()

        with patch.object(SERVICE.urllib.request, "build_opener", return_value=Opener()):
            SERVICE.wait_embeddings()
        self.assertEqual(observed, [("http://127.0.0.1:9001/health", 3)])

    def test_recovery_stops_gateway_and_all_backends_before_restart_and_leaves_gateway_stopped_on_failure(self):
        self.register_all()
        fake = Path(self.tmp.name) / "systemctl"
        log = Path(self.tmp.name) / "systemctl.log"
        fake.write_text("""#!/bin/sh
printf '%s\\n' "$*" >> "$SYSTEMCTL_LOG"
case "$1" in
  is-active) exit 3 ;;
  show) printf '0\\n' ;;
  restart) [ "${FAIL_RESTART:-0}" = 0 ] || exit 1 ;;
esac
""")
        fake.chmod(0o755)
        env = self.env | {"SYSTEMCTL_LOG": str(log)}
        with self.managed_helper(env), patch.object(SERVICE, "SYSTEMCTL", str(fake)), patch.object(SERVICE, "wait_backend", return_value=None), contextlib.redirect_stdout(io.StringIO()):
            SERVICE.recover()
        calls = log.read_text().splitlines()
        self.assertEqual(calls[0], "stop capsa-gateway.service")
        self.assertEqual(calls[1], "stop capsa-snapshot@drive.service capsa-snapshot@slides.service capsa-snapshot@confluence.service capsa-snapshot@jira.service")
        restart = "restart capsa-snapshot@drive.service capsa-snapshot@slides.service capsa-snapshot@confluence.service capsa-snapshot@jira.service"
        self.assertIn(restart, calls)
        self.assertEqual(calls[-1], "start capsa-gateway.service")
        log.unlink()
        with self.managed_helper(env | {"FAIL_RESTART": "1"}), patch.object(SERVICE, "SYSTEMCTL", str(fake)), patch.object(SERVICE, "wait_backend", return_value=None):
            with self.assertRaises(subprocess.CalledProcessError):
                SERVICE.recover()
        self.assertEqual(log.read_text().splitlines()[-1], "stop capsa-gateway.service")


class _HelperContext:
    """Root/config mocks are scoped to this fixture; no runtime bypass exists."""
    def __init__(self, env, assets):
        self.env = env
        self.assets_dir = assets

    def __enter__(self):
        self.environ = patch.dict(SERVICE.os.environ, self.env, clear=True)
        self.root = patch.object(SERVICE.os, "geteuid", return_value=0)
        self.chown = patch.object(SERVICE.os, "chown", return_value=None)
        self.group = patch.object(SERVICE.grp, "getgrnam", return_value=SimpleNamespace(gr_gid=os.getgid()))
        # The temporary fixture cannot manufacture root-owned files. Production
        # still executes safe_config_file and requires root-owned config.
        self.config = patch.object(SERVICE, "safe_config_file", return_value=None)
        # The same limitation applies to root-owned fixture snapshot directories;
        # production validate_snapshot_file enforces that policy and reads the header.
        self.snapshot = patch.object(
            SERVICE, "validate_snapshot_file",
            side_effect=lambda path, _root: SERVICE.snapshot_stamp(Path(path)),
        )
        self.assets = patch.multiple(
            SERVICE,
            MODEL=self.assets_dir / "model.gguf",
            TOKENIZER_ROOT=self.assets_dir / "tokenizers",
            ASSET_MANIFEST=self.assets_dir / "embedding-assets-nomic/manifest.json",
            MODEL_SHA256=hashlib.sha256(b"fixture model").hexdigest(),
            MODEL_BYTES=len(b"fixture model"),
            TOKENIZER_SHA256=hashlib.sha256(b"fixture tokenizer").hexdigest(),
            TOKENIZER_BYTES=len(b"fixture tokenizer"),
        )
        for item in (self.environ, self.root, self.chown, self.group, self.config, self.snapshot, self.assets):
            item.__enter__()
        return self

    def __exit__(self, *args):
        for item in (self.assets, self.snapshot, self.config, self.group, self.chown, self.root, self.environ):
            item.__exit__(*args)


class CapsaUnitPolicyTest(unittest.TestCase):
    def test_units_are_loopback_hardened_and_inert_by_default(self):
        units = ROOT / "pkg/capsa/services/systemd"
        embedding = (units / "capsa-embeddings.service").read_text()
        snapshot = (units / "capsa-snapshot@.service").read_text()
        gateway = (units / "capsa-gateway.service").read_text()
        self.assertIn("User=capsa", embedding)
        self.assertIn("--host 127.0.0.1 --port 9001", embedding)
        self.assertIn("Environment=CAPSA_THREADS=24", embedding)
        self.assertIn("EnvironmentFile=-/etc/capsa/embeddings.env", embedding)
        self.assertIn("--threads ${CAPSA_THREADS} --batch-size 4096 --ubatch-size 4096", embedding)
        self.assertIn("CAPSA_EMBEDDING_PROFILE=nomic-embed-text-v1.5.Q4_K_M", embedding)
        self.assertIn("ExecStartPost=/usr/lib/capsa/python/bin/python3.12 -I -B", embedding)
        self.assertIn("wait-embeddings", embedding)
        self.assertIn("/usr/share/capsa/models/nomic-embed-text-v1.5.Q4_K_M.gguf", embedding)
        self.assertIn("HF_HOME=/usr/share/capsa/tokenizers", snapshot)
        self.assertIn("serve-gateway", gateway)
        self.assertIn("/usr/lib/capsa/python/bin/python3.12 -I -B", gateway)
        self.assertIn("--immutable", HELPER.read_text())
        self.assertIn("embedding-assets-nomic/manifest.json", HELPER.read_text())
        self.assertIn("688882a79f44442ddc1f60d70334a7ff5df0fb47", HELPER.read_text())
        self.assertNotIn("integrity_check", HELPER.read_text())
        self.assertIn("Requires=capsa-embeddings.service", snapshot)
        self.assertIn("Restart=no", gateway)
        self.assertIn("capsa-snapshot@drive.service", gateway)
        for unit in (embedding, snapshot, gateway):
            self.assertIn("ProtectSystem=strict", unit)
            self.assertIn("ReadWritePaths=/var/lib/capsa /var/cache/capsa", unit)
            self.assertIn("IPAddressAllow=localhost", unit)
            self.assertNotIn("WantedBy=default.target", unit)


if __name__ == "__main__":
    unittest.main()
