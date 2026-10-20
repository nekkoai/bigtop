#!/bin/sh
# Install and exercise the complete Capsa package set on a disposable Noble VM.
# This is deliberately not a host-side test: CAPSA_DISPOSABLE_TEST_HOST is the
# explicit operator/CI acknowledgement that this machine may be modified.
set -eu

fail() {
    echo "smoke-test-capsa: $*" >&2
    exit 1
}

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
driver=$repo/ci/capsa-smoke.py
dist=${1:-"$repo/build/capsa-dist"}

[ "$(id -u)" = 0 ] || fail "must run as root in a disposable Ubuntu 24.04 VM"
[ "${CAPSA_DISPOSABLE_TEST_HOST:-}" = 1 ] ||
    fail "set CAPSA_DISPOSABLE_TEST_HOST=1 only on the disposable test VM"
[ -d /run/systemd/system ] || fail "requires a systemd test VM"
[ -r /etc/os-release ] || fail "missing /etc/os-release"
. /etc/os-release
[ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = 24.04 ] ||
    fail "requires an Ubuntu 24.04 test VM"
[ "$(dpkg --print-architecture)" = amd64 ] || fail "requires amd64"
[ -f "$driver" ] || fail "missing smoke driver: $driver"
dist=$(CDPATH= cd -- "$dist" && pwd)

# Never turn an installation check into an operation on an operator stack.
if systemctl list-units --all --plain --no-legend 'capsa*.service' |
    awk '$3 != "inactive" && $3 != "failed" { found=1 } END { exit !found }'; then
    fail "refusing to run while a Capsa unit is active"
fi
if [ -e /etc/capsa/federation.json ]; then
    fail "refusing to replace existing operator federation.json"
fi
if find /etc/capsa/backends -mindepth 1 -maxdepth 1 -name '*.json' -print -quit 2>/dev/null |
    grep -q .; then
    fail "refusing to replace existing Capsa backend configuration"
fi
if find /etc/capsa -type f -print -quit 2>/dev/null | grep -q .; then
    fail "requires a fresh test host without existing Capsa operator configuration"
fi
if find /srv/capsa/snapshots /var/lib/capsa -type f -name '*.db*' -print -quit 2>/dev/null |
    grep -q .; then
    fail "requires a fresh test host without existing Capsa databases"
fi

set -- "$dist"/*.deb
[ -e "$1" ] || fail "no .deb files in $dist"
expected='capsa-core capsa-python-runtime capsa-federation capsa-indexers capsa-llama-cpu capsa-embedding-assets-nomic capsa-services capsa-server'
seen=
count=0
for deb do
    package=$(dpkg-deb -f "$deb" Package) || fail "cannot read package metadata: $deb"
    case " $expected " in *" $package "*) ;; *) fail "unexpected package in dist: $package" ;; esac
    case " $seen " in *" $package "*) fail "duplicate package in dist: $package" ;; esac
    seen="$seen $package"
    count=$((count + 1))
done
[ "$count" -eq 8 ] || fail "expected exactly eight Capsa .debs, found $count"
for package in $expected; do
    case " $seen " in *" $package "*) ;; *) fail "missing package from dist: $package" ;; esac
done

# Local package paths force apt to exercise the actual eight freshly built debs.
apt-get install -y -q "$@"
for package in $expected; do
    dpkg-query -W -f='${db:Status-Abbrev} ${Version}\n' "$package" |
        grep -q '^ii ' || fail "$package was not installed"
done

# Installation may create empty directories and the capsa account, but it must
# remain inert and must not manufacture operator configuration or snapshots.
for unit in capsa-embeddings.service capsa-gateway.service \
    capsa-snapshot@drive.service capsa-snapshot@slides.service \
    capsa-snapshot@confluence.service capsa-snapshot@jira.service; do
    systemctl is-active --quiet "$unit" && fail "package installation started $unit"
    case "$(systemctl is-enabled "$unit" 2>/dev/null || true)" in
        enabled|enabled-runtime|linked|linked-runtime) fail "package installation enabled $unit" ;;
    esac
done
[ ! -e /etc/capsa/federation.json ] || fail "installation created federation configuration"
if find /etc/capsa -type f -print -quit 2>/dev/null |
    grep -q .; then
    fail "installation created operator configuration"
fi
if find /srv/capsa/snapshots /var/lib/capsa -type f \( -name '*.db' -o -name '*.db-wal' -o -name '*.db-shm' \) \
    -print -quit 2>/dev/null | grep -q .; then
    fail "installation created a snapshot or staging database"
fi

capsa --help >/dev/null
capsa-federation --help >/dev/null
capsa-index-jira --help >/dev/null
capsa-index-confluence --help >/dev/null
for package in capsa-core capsa-federation capsa-indexers; do
    dpkg-query -W -f="$package "'${Version}\n' "$package"
done

# This is an intentional, test-owned operator configuration.  Newer service
# recipes consume it via EnvironmentFile= and retain the production default of
# 24 threads when it is absent.  Two threads keeps this functional VM smoke
# practical on a two-vCPU runner without changing the model batch settings.
install -d -m 0750 -o root -g capsa /etc/capsa
printf '%s\n' 'CAPSA_THREADS=2' > /etc/capsa/embeddings.env
chown root:capsa /etc/capsa/embeddings.env
chmod 0640 /etc/capsa/embeddings.env

exec /usr/lib/capsa/python/bin/python3.12 -I -B "$driver" --dist "$dist"
