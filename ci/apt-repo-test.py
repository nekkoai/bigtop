#!/usr/bin/env python3
"""Exercise real apt metadata, dependency resolution and downloads in private state.

No packages are installed. Host source definitions/indexes are copied for runtime
dependency resolution; host dpkg status and explicitly configured public signing
keys are read-only. Host apt configuration hooks are never loaded. APT_KEY_DIR and
APT_REQUIRE_SIGNATURE have the same meanings as in make-apt-repo.sh.
"""
import argparse
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("apt_validation", Path(__file__).with_name("validate-apt-repo.py"))
metadata = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metadata)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


@contextmanager
def repository_server(dist):
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(dist)))
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, name="apt-repo-test-http", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def copy_regular_files(source, destination):
    destination.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        for path in source.iterdir():
            if path.is_file() and path.name != "lock":
                # Copy, never hardlink: apt must not rewrite a host index/cache.
                shutil.copyfile(path, destination / path.name)


def apt_config(work, system_root):
    """Seed private sources/indexes without running any host apt hooks or update."""
    etc = work / "etc"
    for directory in ("etc/apt.conf.d", "etc/sources.list.d", "etc/preferences.d",
                      "etc/trusted.gpg.d", "state/lists/partial", "cache/archives/partial", "log"):
        (work / directory).mkdir(parents=True, exist_ok=True)
    host_etc = system_root / "etc/apt"
    copy_regular_files(host_etc / "sources.list.d", etc / "sources.list.d")
    copy_regular_files(host_etc / "preferences.d", etc / "preferences.d")
    copy_regular_files(host_etc / "trusted.gpg.d", etc / "trusted.gpg.d")
    copy_regular_files(system_root / "var/lib/apt/lists", work / "state/lists")
    for name in ("sources.list", "preferences", "trusted.gpg"):
        if (host_etc / name).is_file():
            shutil.copyfile(host_etc / name, etc / name)
    (etc / "sources.list").touch(exist_ok=True)
    status = system_root / "var/lib/dpkg/status"
    if not status.is_file():
        status = work / "state/status"
        status.touch()
    settings = {
        "Dir": work, "Dir::Etc": etc, "Dir::Etc::main": "-",
        "Dir::Etc::parts": etc / "apt.conf.d", "Dir::Etc::sourcelist": etc / "sources.list",
        "Dir::Etc::sourceparts": etc / "sources.list.d", "Dir::Etc::preferences": etc / "preferences",
        "Dir::Etc::preferencesparts": etc / "preferences.d", "Dir::Etc::trusted": etc / "trusted.gpg",
        "Dir::Etc::trustedparts": etc / "trusted.gpg.d", "Dir::Etc::netrc": etc / "auth.conf",
        "Dir::Etc::netrcparts": etc / "auth.conf.d", "Dir::State": work / "state",
        "Dir::State::lists": work / "state/lists", "Dir::State::status": status,
        "Dir::State::extended_states": work / "state/extended_states", "Dir::Cache": work / "cache",
        "Dir::Cache::archives": work / "cache/archives", "Dir::Cache::pkgcache": work / "cache/pkgcache.bin",
        "Dir::Cache::srcpkgcache": work / "cache/srcpkgcache.bin", "Dir::Log": work / "log",
        "APT::Sandbox::User": pwd.getpwuid(os.geteuid()).pw_name,
        "APT::Get::List-Cleanup": "false", "Acquire::Languages": "none", "Debug::NoLocking": "true",
    }
    config = work / "apt.conf"
    config.write_text("".join(f"{name} {json.dumps(str(value))};\n" for name, value in settings.items()))
    return config


def indexed_packages(dist):
    records = metadata.paragraphs((dist / "Packages").read_text())
    metadata.require(bool(records), "empty package index")
    seen = set()
    for entry in records:
        name, version, architecture = (entry.get(field, "") for field in ("package", "version", "architecture"))
        metadata.require(bool(re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", name)), "invalid indexed package name")
        metadata.require(name not in seen, f"duplicate indexed package: {name}")
        seen.add(name)
        metadata.require(bool(re.fullmatch(r"[0-9][A-Za-z0-9.+:~\-]*", version)), f"invalid indexed version: {name}")
        metadata.require(bool(re.fullmatch(r"[a-z0-9][a-z0-9-]*", architecture)), f"invalid indexed architecture: {name}")
        filename = entry.get("filename", "")
        metadata.require(metadata.flat_filename(filename) and filename.endswith(".deb"), "unsafe indexed filename")
        metadata.require(bool(re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256", ""))), "missing/invalid indexed SHA256")
        metadata.require(entry.get("size", "").isdigit(), "missing/invalid indexed size")
    return records


def run_apt(arguments, config, work):
    result = subprocess.run(["apt-get", *arguments], env=os.environ | {"APT_CONFIG": str(config), "DEBIAN_FRONTEND": "noninteractive"},
                            cwd=work, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout, end="", flush=True)
    metadata.require(result.returncode == 0, "isolated apt-get failed")


def test_repository(dist, key_dir, require_signature=False, system_root=Path("/"), temp_parent=None):
    dist, key_dir, system_root = Path(dist).resolve(), Path(key_dir).resolve(), Path(system_root).resolve()
    records = indexed_packages(dist)
    # Generic repositories are supported; do not require the full release set.
    metadata.validate_release(dist)
    metadata.validate_signatures(dist, key_dir, require_signature)
    signed = (dist / "InRelease").is_file()
    if not signed:
        print("WARNING: testing unsigned local repository with trusted=yes", file=sys.stderr)
    with tempfile.TemporaryDirectory(prefix="bigtop-apt-client-", dir=temp_parent) as temporary:
        work = Path(temporary)
        config = apt_config(work, system_root)
        with repository_server(dist) as url:
            source = work / "etc/local.list"
            trust = f"signed-by={key_dir / 'bigtop-archive-keyring.gpg'}" if signed else "trusted=yes"
            source.write_text(f"deb [{trust}] {url} ./\n")
            # Update only the test repository: host source indexes are seeded
            # copies and retained for resolution, never refreshed over the network.
            run_apt(["update", "-o", f"Dir::Etc::sourcelist={source}", "-o", "Dir::Etc::sourceparts=-",
                     "-o", "APT::Update::Error-Mode=any"], config, work)
            sources = work / "etc/sources.list"
            sources.write_text(source.read_text() + sources.read_text())
            exact = [entry["package"] + "=" + entry["version"] for entry in records]
            run_apt(["install", "--yes", "--download-only", "--reinstall", "--no-install-recommends", "--no-remove", *exact], config, work)
            archives = work / "cache/archives"
            for entry in records:
                matches = []
                for payload in archives.glob(entry["package"] + "_*.deb"):
                    control = metadata.one_paragraph(metadata.run("dpkg-deb", "--field", str(payload)).decode())
                    if all(control.get(field) == entry[field] for field in ("package", "version", "architecture")):
                        matches.append(payload)
                metadata.require(len(matches) == 1, f"exact requested package was not downloaded: {entry['package']}")
                payload = matches[0]
                metadata.require(payload.stat().st_size == int(entry["size"]), f"downloaded size mismatch: {entry['package']}")
                metadata.require(metadata.digest(payload) == entry["sha256"], f"downloaded SHA256 mismatch: {entry['package']}")
    print(f"apt repository test passed: downloaded and verified all {len(records)} exact package versions")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", nargs="?", type=Path, default=ROOT / "build/dist")
    args = parser.parse_args()
    try:
        required = os.environ.get("APT_REQUIRE_SIGNATURE", "0")
        metadata.require(required in {"0", "1"}, "APT_REQUIRE_SIGNATURE must be 0 or 1")
        test_repository(args.dist, Path(os.environ.get("APT_KEY_DIR", ROOT / "keys")), required == "1")
    except (ValueError, OSError, EOFError, UnicodeError) as error:
        print(f"apt repository test failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
