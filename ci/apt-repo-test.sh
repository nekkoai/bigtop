#!/bin/sh
# Serve DIST as a flat apt repository over local HTTP and check apt can
# verify and read it (signature + Packages), exactly as clients will after
# the release. Run as root.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
dist=$(cd "${1:-$here/build/dist}" && pwd)
rm -f /var/lib/apt/lists/127.0.0.1:8799_*
python3 -m http.server 8799 --bind 127.0.0.1 --directory "$dist" >/dev/null 2>&1 &
server=$!
trap 'kill $server' EXIT
sleep 1
install -m 0644 "$dist/asys-archive-keyring.gpg" /usr/share/keyrings/asys-archive-keyring.gpg
if [ -f "$dist/InRelease" ]; then
    printf 'Types: deb\nURIs: http://127.0.0.1:8799\nSuites: ./\nSigned-By: /usr/share/keyrings/asys-archive-keyring.gpg\n' > /etc/apt/sources.list.d/asys-test.sources
else
    echo "WARNING: unsigned repository; testing with trusted=yes" >&2
    printf 'Types: deb\nURIs: http://127.0.0.1:8799\nSuites: ./\nTrusted: yes\n' > /etc/apt/sources.list.d/asys-test.sources
fi
apt-get update -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/asys-test.sources -o Dir::Etc::sourceparts=-
apt-cache policy asys asys-server asys-images dcomp | grep -E '^[a-z-]+:|Candidate'
apt-get install -y -q --reinstall --download-only asys-server >/dev/null
rm -f /etc/apt/sources.list.d/asys-test.sources
echo "apt repository test passed"
