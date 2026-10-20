"""Check packaging's ingestion guard without running ingestion or services."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "capsa_indexer_launch", ROOT / "pkg/capsa/launchers/indexer.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class IndexerGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "config"
        self.config.mkdir()
        self.snapshots = self.root / "snapshots"
        self.snapshots.mkdir()
        self.db = self.snapshots / "drive.db"
        self.db.write_bytes(b"fixture, not a database")
        (self.config / "drive.json").write_text(json.dumps({"database": str(self.db)}))

    def test_accepts_separate_staging(self):
        module.guard_paths(["--vector-db", str(self.root / "staging/db")], self.config)

    def test_rejects_registered_snapshot_directory(self):
        with self.assertRaises(ValueError):
            module.guard_paths(["--work-dir", str(self.snapshots)], self.config)

    def test_rejects_snapshot_hardlink_alias(self):
        alias = self.root / "alias.db"
        os.link(self.db, alias)
        with self.assertRaises(ValueError):
            module.guard_paths(["--queue-db", str(alias)], self.config)

    def test_bad_registration_fails_closed(self):
        (self.config / "drive.json").write_text("{broken")
        with self.assertRaises(ValueError):
            module.guard_paths(["--vector-db", str(self.root / "staging/db")], self.config)
