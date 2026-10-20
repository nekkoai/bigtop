"""Entrypoints for explicit ingestion, never for registered serving snapshots."""
import argparse
import json
from pathlib import Path
import runpy
import sys


def guard_paths(arguments, config_dir=Path("/etc/capsa/backends")):
    parser = argparse.ArgumentParser(add_help=False)
    for argument in ("vector-db", "queue-db", "work-dir"):
        parser.add_argument("--" + argument)
    known, _ = parser.parse_known_args(arguments)
    requested = [Path(value).resolve() for value in vars(known).values() if value]
    if not requested:
        return
    roots = [Path("/srv/capsa/snapshots").resolve(), Path("/index").resolve()]
    snapshots = []
    configs = list(config_dir.iterdir()) if config_dir.exists() else []
    for config in configs:
        if config.suffix != ".json":
            continue
        record = json.loads(config.read_text())
        snapshot = Path(record["database"]).resolve(strict=True)
        snapshots.append(snapshot)
        roots.append(snapshot.parent)
    for path in requested:
        if any(path == root or path.is_relative_to(root) for root in roots):
            raise ValueError(f"ingestion path overlaps published snapshots: {path}")
        if path.exists() and any(path.samefile(snapshot) for snapshot in snapshots):
            raise ValueError(f"ingestion path aliases a registered snapshot: {path}")


def main():
    modules = {"jira": "indexers.jira.run_jira", "confluence": "indexers.confluence.run"}
    name = sys.argv.pop(1)
    if name not in modules:
        raise ValueError("unknown packaged indexer")
    guard_paths(sys.argv[1:])
    sys.path.insert(0, "/usr/lib/capsa/app")
    runpy.run_module(modules[name], run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError) as exc:
        sys.exit(f"capsa-indexer: {exc}")
