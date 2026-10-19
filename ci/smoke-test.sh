#!/bin/sh
# Install the freshly built packages and exercise the team workflow with a
# second Unix user: asys-server setup, asys-adduser, deterministic hello
# workflow from the shared /srv/asys instance (no inference, no tokens).
# Run as root (sudo) on a disposable noble host with Docker.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
dist=$(cd "${1:-$here/build/dist}" && pwd)
apt-get install -y -q "$dist"/dcomp_*.deb "$dist"/asys-images_*.deb "$dist"/asys_*.deb "$dist"/asys-flow-view_*.deb "$dist"/asys-server_*.deb
asys-flow-view -help >/dev/null 2>&1 || test -x /usr/bin/asys-flow-view
systemctl is-active asys-dashboard
curl -fsS -o /dev/null http://127.0.0.1:8766/
id smoke >/dev/null 2>&1 || adduser --quiet --disabled-password --gecos smoke smoke
asys-adduser smoke
cat > /tmp/smoke.sh <<'INNER'
set -eu
. /srv/asys/asys-env
cd /srv/asys/workspaces
# release record and loaded images agree, no service warnings
asys version | tee /tmp/version.out
! grep -q '^warning' /tmp/version.out
# environment image built from a bare "FROM asys-workers" Dockerfile
asys-run /usr/share/doc/asys/examples/hello/env/dummy /usr/share/doc/asys/examples/hello/workflow.bpmn | tee /tmp/smoke.out
grep -q 'Hello from asys.' /tmp/smoke.out
asys ps
asys status latest
asys-skills --codex && test -f "$HOME/.codex/skills/asys/SKILL.md"
INNER
runuser -u smoke -- sh /tmp/smoke.sh
# The dashboard (user asys) must see the member's run.
curl -fsS http://127.0.0.1:8766/api/runs | grep -q '"name": "Hello"'
echo "smoke test passed"
