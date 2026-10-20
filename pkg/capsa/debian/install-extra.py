#!/usr/bin/python3
"""Install only the application modules and packaging-owned integration files."""
from pathlib import Path
import shutil


def install(source, package, destination, executable=False):
    target = Path("debian") / package / destination.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(0o755 if executable else 0o644)


for source in Path("federation/capsa_federation").rglob("*.py"):
    install(source, "capsa-federation",
            "/usr/lib/capsa/app/federation/" + str(source.relative_to("federation")))
for name in ("capsa-federation", "federation.py"):
    install(Path("debian/launchers") / name, "capsa-federation",
            ("/usr/lib/capsa/launch/" if name.endswith(".py") else "/usr/bin/") + name,
            executable=not name.endswith(".py"))

modules = [
    "__init__.py", "common/__init__.py", "common/config.py", "common/mcp_client.py",
    "confluence/__init__.py", "confluence/normalize.py", "confluence/run.py",
    "jira/__init__.py", "jira/normalize_jira.py", "jira/run_jira.py",
]
for name in modules:
    install(Path("indexers") / name, "capsa-indexers", "/usr/lib/capsa/app/indexers/" + name)
for name in ("capsa-index-jira", "capsa-index-confluence", "indexer.py"):
    install(Path("debian/launchers") / name, "capsa-indexers",
            ("/usr/lib/capsa/launch/" if name.endswith(".py") else "/usr/bin/") + name,
            executable=not name.endswith(".py"))
for name in ("drive/README.md", "drive/gws_pdf_ingest.py", "slides/README.md"):
    install(Path("indexers") / name, "capsa-indexers",
            "/usr/share/doc/capsa-indexers/reference/" + name)

for directory, destination in (
    ("bin", "/usr/bin"), ("lib", "/usr/lib/capsa/services"),
    ("systemd", "/usr/lib/systemd/system"), ("config", "/etc/capsa"),
):
    for source in (Path("debian/services") / directory).rglob("*"):
        if source.is_file() and "__pycache__" not in source.parts:
            install(source, "capsa-services",
                    destination + "/" + str(source.relative_to(Path("debian/services") / directory)),
                    executable=directory in ("bin", "lib"))
for package in ("capsa-federation", "capsa-indexers", "capsa-services", "capsa-server"):
    install("LICENSE", package, f"/usr/share/doc/{package}/LICENSE")
