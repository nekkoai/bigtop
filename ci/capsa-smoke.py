#!/usr/lib/capsa/python/bin/python3.12 -I
"""Task-owned live acceptance path for the disposable Capsa package VM."""
import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


NAMES = ("drive", "slides", "confluence", "jira")
UNITS = tuple(f"capsa-snapshot@{name}.service" for name in NAMES)
PRIVATE_PYTHON = Path("/usr/lib/capsa/python")
STAGING = Path("/var/lib/capsa/test-staging")
FIXTURE_DB = STAGING / "fixture.db"
SNAPSHOTS = Path("/srv/capsa/snapshots")
FIXTURE_TOKEN = "CAPSA_DISPOSABLE_FIXTURE_NEEDLE_9b31f6"


class SmokeError(RuntimeError):
    """A check failed; keep the error short enough for CI's systemd logs."""


def run(*args, check=True, **kwargs):
    print("+", " ".join(map(str, args)), flush=True)
    return subprocess.run(args, check=check, text=True, **kwargs)


def unit_active(unit):
    return subprocess.run(
        ("systemctl", "is-active", "--quiet", unit), check=False
    ).returncode == 0


def wait_until(label, predicate, timeout=300, interval=1):
    deadline = time.monotonic() + timeout
    error = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as exc:  # readiness is expected to race startup
            error = exc
        time.sleep(interval)
    suffix = f": {error}" if error else ""
    raise SmokeError(f"timed out waiting for {label}{suffix}")


def service_logs(unit):
    result = run("journalctl", "-u", unit, "--no-pager", "-n", "80", check=False,
                 capture_output=True)
    return result.stdout + result.stderr


def wait_unit(unit, timeout=300):
    try:
        wait_until(f"{unit} active", lambda: unit_active(unit), timeout)
    except SmokeError as exc:
        raise SmokeError(f"{exc}\n{service_logs(unit)}") from exc


def http_json(url, payload=None, timeout=20):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json", "Accept": "application/json"}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode())


def wait_embeddings():
    wait_until(
        "embedding health endpoint",
        lambda: http_json("http://127.0.0.1:9001/health", timeout=3)[0] == 200,
        timeout=300,
    )


def check_private_runtime():
    if Path(sys.prefix) != PRIVATE_PYTHON or Path(sys.exec_prefix) != PRIVATE_PYTHON:
        raise SmokeError(f"smoke did not use private Python: {sys.prefix!r}")
    distributions = {
        distribution.metadata["Name"].lower()
        for distribution in importlib.metadata.distributions()
        if distribution.metadata.get("Name")
    }
    if len(distributions) != 29:
        raise SmokeError(
            f"private runtime has {len(distributions)} distributions, expected 29: "
            + ", ".join(sorted(distributions))
        )
    required = {"mcp", "httpx", "pydantic", "uvicorn"}
    if not required <= distributions:
        raise SmokeError(f"private runtime misses SDK dependencies: {sorted(required - distributions)}")


def assert_embeddings():
    status, response = http_json(
        "http://127.0.0.1:9001/v1/embeddings",
        {"model": "nomic-ai/nomic-embed-text-v1.5", "input": [FIXTURE_TOKEN]},
        timeout=90,
    )
    try:
        vector = response["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SmokeError(f"invalid embedding response: {response!r}") from exc
    if status != 200 or len(vector) != 768 or not all(math.isfinite(float(value)) for value in vector):
        raise SmokeError("embedding endpoint did not return 768 finite values")


def start_writer():
    if unit_active("capsa-test-writer.service"):
        raise SmokeError("refusing to reuse an existing capsa-test-writer.service")
    STAGING.mkdir(mode=0o750, parents=True, exist_ok=True)
    shutil.chown(STAGING, user="capsa", group="capsa")
    os.chmod(STAGING, 0o750)
    for candidate in STAGING.glob("fixture.db*"):
        if candidate.is_file():
            raise SmokeError(f"refusing to remove an existing fixture: {candidate}")
    run(
        "systemd-run", "--unit=capsa-test-writer", "--collect",
        "--property=User=capsa", "--property=Group=capsa",
        "--property=UMask=0077", "--property=KillSignal=SIGINT",
        "--property=Restart=no", "--property=Environment=HF_HOME=/usr/share/capsa/tokenizers",
        "--property=Environment=HF_HUB_OFFLINE=1",
        "/usr/bin/capsa", "--base-url", "http://127.0.0.1:9001/v1",
        "--model", "nomic-ai/nomic-embed-text-v1.5", "--db-path", str(FIXTURE_DB),
        "serve", "--bind", "127.0.0.1:10180", "--rw",
    )
    wait_unit("capsa-test-writer.service")


def tool_body(result):
    body = result.structuredContent
    if body is None:
        if not result.content:
            raise SmokeError("MCP tool returned neither structured nor text content")
        body = json.loads(result.content[0].text)
    if result.isError:
        raise SmokeError(f"MCP tool error: {json.dumps(body, sort_keys=True)}")
    return body


async def writer_fixture():
    """Ingest all documents into the one test-only RW staging database."""
    url = "http://127.0.0.1:10180/mcp"
    async with streamablehttp_client(url, timeout=timedelta(seconds=30)) as (reader, writer, _):
        async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
            await client.initialize()
            names = {tool.name for tool in (await client.list_tools()).tools}
            if names != {"search", "fetch_document", "ingest"}:
                raise SmokeError(f"writer exposed unexpected tools: {sorted(names)}")
            for source in NAMES:
                text = (
                    f"{FIXTURE_TOKEN} source={source}. "
                    f"Synthetic disposable fixture for {source}; it is not production data."
                )
                result = await client.call_tool(
                    "ingest", {"text": text, "metadata": {"title": f"smoke-{source}", "source": source}}
                )
                tool_body(result)
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                body = tool_body(await client.call_tool(
                    "search", {"query": FIXTURE_TOKEN, "top_k": 10}
                ))
                complete = set()
                for hit in body.get("results", []):
                    if FIXTURE_TOKEN in (hit.get("chunk") or ""):
                        fetched = tool_body(await client.call_tool(
                            "fetch_document", {"doc_id": hit["doc_id"]}
                        ))
                        if FIXTURE_TOKEN in fetched.get("content", ""):
                            complete.add(hit.get("metadata", {}).get("source"))
                if complete == set(NAMES):
                    return
                await asyncio.sleep(1)
    raise SmokeError("writer never indexed the synthetic fixture")


def sdk_retry(label, unit, operation, timeout=300):
    """Retry complete SDK sessions; retain the task unit's logs on timeout."""
    deadline = time.monotonic() + timeout
    error = None
    while time.monotonic() < deadline:
        try:
            return asyncio.run(operation())
        except Exception as exc:  # a server can be active before MCP is ready
            error = exc
            time.sleep(1)
    raise SmokeError(f"timed out waiting for {label}: {error}\n{service_logs(unit)}")


def stop_writer_checkpointed():
    run("systemctl", "stop", "capsa-test-writer.service")
    wait_until("test writer stop", lambda: not unit_active("capsa-test-writer.service"), timeout=60)
    if not FIXTURE_DB.is_file() or FIXTURE_DB.stat().st_size == 0:
        raise SmokeError("writer did not leave a nonempty staging fixture.db")
    # Do not use sqlite3/integrity_check here: Capsa owns the libSQL format.
    # A sidecar means copying this main file could silently omit committed work.
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(FIXTURE_DB) + suffix)
        if sidecar.exists():
            detail = f" ({sidecar.stat().st_size} bytes)" if sidecar.is_file() else ""
            raise SmokeError(f"writer did not close a checkpointed DB; found {sidecar}{detail}")


def copy_and_register_snapshots():
    SNAPSHOTS.mkdir(mode=0o755, parents=True, exist_ok=True)
    os.chown(SNAPSHOTS, 0, 0)
    os.chmod(SNAPSHOTS, 0o755)
    for name in NAMES:
        destination = SNAPSHOTS / f"{name}.db"
        if destination.exists():
            raise SmokeError(f"refusing to replace existing snapshot fixture: {destination}")
        shutil.copyfile(FIXTURE_DB, destination)
        os.chown(destination, 0, 0)
        os.chmod(destination, 0o444)
        run("capsa-register-snapshot", name, str(destination), "--checkpointed")


def hashes():
    result = {}
    for name in NAMES:
        path = SNAPSHOTS / f"{name}.db"
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


async def gateway_fixture():
    url = "http://127.0.0.1:10084/mcp"
    async with streamablehttp_client(url, timeout=timedelta(seconds=90)) as (reader, writer, _):
        async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=90)) as client:
            await client.initialize()
            names = {tool.name for tool in (await client.list_tools()).tools}
            if names != {"search", "fetch_document", "list_sources"}:
                raise SmokeError(f"gateway exposed unexpected tools: {sorted(names)}")
            search = tool_body(await client.call_tool(
                "search", {"query": FIXTURE_TOKEN, "limit": 50}
            ))
            if search.get("partial"):
                raise SmokeError(f"gateway reported partial fixture search: {search.get('errors')}")
            hits = {row["source"]: row for row in search.get("results", [])}
            if set(hits) != set(NAMES):
                raise SmokeError(f"fixture search did not hit all sources: {sorted(hits)}")
            for name, hit in hits.items():
                document = tool_body(await client.call_tool(
                    "fetch_document", {"document_ref": hit["document_ref"]}
                ))
                if document.get("source") != name or FIXTURE_TOKEN not in document.get("content", ""):
                    raise SmokeError(f"fixture fetch was not verified for {name}")


def recover_and_probe(owned):
    owned.update(UNITS)
    owned.add("capsa-gateway.service")
    run("capsa-managed-recover")
    for unit in UNITS + ("capsa-gateway.service",):
        wait_unit(unit)
    sdk_retry("gateway MCP SDK readiness", "capsa-gateway.service", gateway_fixture)


def debs_in(dist):
    expected = {
        "capsa-core", "capsa-python-runtime", "capsa-federation", "capsa-indexers",
        "capsa-llama-cpu", "capsa-embedding-assets-nomic", "capsa-services", "capsa-server",
    }
    found = {}
    for deb in Path(dist).glob("*.deb"):
        package = run("dpkg-deb", "-f", str(deb), "Package", capture_output=True).stdout.strip()
        if package in found:
            raise SmokeError(f"duplicate .deb for {package}")
        found[package] = deb
    if set(found) != expected:
        raise SmokeError(f"unexpected dist package set: {sorted(found)}")
    return found


def assert_inactive():
    for unit in ("capsa-embeddings.service", "capsa-gateway.service") + UNITS:
        if unit_active(unit):
            raise SmokeError(f"upgrade automatically started {unit}")


def upgrade_probe(dist, before, owned):
    packages = debs_in(dist)
    # dpkg invokes the package preinst as an upgrade during --reinstall. The
    # guard must refuse while the managed gateway is admitting requests.
    active = run(
        "apt-get", "install", "-y", "-q", "--reinstall", str(packages["capsa-python-runtime"]),
        check=False, capture_output=True,
    )
    if active.returncode == 0:
        raise SmokeError("active-stack reinstall unexpectedly bypassed the upgrade guard")
    print(active.stdout + active.stderr, end="", flush=True)

    run("systemctl", "stop", "capsa-gateway.service", *UNITS, "capsa-embeddings.service")
    assert_inactive()
    run("apt-get", "install", "-y", "-q", "--reinstall", *(str(path) for path in packages.values()))
    assert_inactive()
    if hashes() != before:
        raise SmokeError("upgrade changed immutable snapshot bytes")
    if Path("/etc/capsa/embeddings.env").read_text() != "CAPSA_THREADS=2\n":
        raise SmokeError("upgrade did not preserve the intentional thread override")

    run("systemctl", "start", "capsa-embeddings.service")
    owned.add("capsa-embeddings.service")
    wait_unit("capsa-embeddings.service")
    wait_embeddings()
    recover_and_probe(owned)
    if hashes() != before:
        raise SmokeError("recovery/search changed immutable snapshot bytes after upgrade")


def cleanup(owned):
    # The initial shell guard established that no pre-existing Capsa service was
    # active.  Still, only units this driver started are stopped here.
    order = ["capsa-gateway.service", *UNITS, "capsa-test-writer.service", "capsa-embeddings.service"]
    for unit in order:
        if unit in owned:
            run("systemctl", "stop", unit, check=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", required=True, help="directory containing the eight installed .debs")
    args = parser.parse_args()
    if os.geteuid() != 0 or os.environ.get("CAPSA_DISPOSABLE_TEST_HOST") != "1":
        raise SmokeError("driver requires root and CAPSA_DISPOSABLE_TEST_HOST=1")
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
    check_private_runtime()
    owned = set()
    try:
        owned.add("capsa-embeddings.service")
        run("systemctl", "start", "capsa-embeddings.service")
        wait_unit("capsa-embeddings.service")
        wait_embeddings()
        assert_embeddings()

        owned.add("capsa-test-writer.service")
        start_writer()
        sdk_retry("test writer MCP SDK readiness", "capsa-test-writer.service", writer_fixture)
        stop_writer_checkpointed()

        copy_and_register_snapshots()
        before = hashes()
        recover_and_probe(owned)
        if hashes() != before:
            raise SmokeError("recovery/search changed immutable snapshot bytes")
        upgrade_probe(args.dist, before, owned)
        print("Capsa disposable VM smoke test passed.", flush=True)
    finally:
        cleanup(owned)


if __name__ == "__main__":
    try:
        main()
    except (SmokeError, subprocess.CalledProcessError, urllib.error.URLError) as exc:
        print(f"capsa smoke: {exc}", file=sys.stderr)
        raise SystemExit(1)
