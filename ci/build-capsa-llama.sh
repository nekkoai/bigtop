#!/bin/sh
# Build capsa-llama-cpu from the exact llama.cpp gitlink in this repository.
set -eu

here=$(cd "$(dirname "$0")/.." && pwd)
upstream=$here/upstream/llama.cpp
recipe=$here/pkg/capsa/llama-cpu/debian
upgrade_guard=$here/pkg/capsa/upgrade-guard
work=$here/build/capsa-llama

fail() {
    echo "build-capsa-llama: $*" >&2
    exit 1
}

[ -r /etc/os-release ] || fail "missing /etc/os-release"
. /etc/os-release
[ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = 24.04 ] ||
    fail "only Ubuntu 24.04 builders are supported"
[ "$(dpkg --print-architecture)" = amd64 ] || fail "only amd64 builders are supported"
[ -f "$upgrade_guard" ] || fail "missing shared Capsa upgrade guard: $upgrade_guard"
for command in git cmake ninja dpkg-buildpackage dpkg-parsechangelog; do
    command -v "$command" >/dev/null 2>&1 || fail "missing build command: $command"
done

pin=$(git -C "$here" ls-files --stage -- upstream/llama.cpp |
    awk '$1 == "160000" { print $2; exit }')
[ -n "$pin" ] || fail "upstream/llama.cpp is not a gitlink in the index"
git -C "$upstream" cat-file -e "$pin^{commit}" ||
    fail "gitlink commit $pin is unavailable in $upstream"
[ -z "$(git -C "$upstream" status --porcelain --untracked-files=all)" ] ||
    fail "refusing to archive a dirty llama.cpp checkout"

# The gitlink is the only source selection.  Do not replace it by a mutable
# release lookup or a checkout HEAD: the repository's main update process
# verifies the requested latest fork tag before changing this pin.
debversion=$(dpkg-parsechangelog -l "$here/pkg/capsa/debian/changelog" -S Version)
version=${debversion%-*}
[ "$version" != "$debversion" ] || fail "changelog version lacks a Debian revision"

rm -rf "$work"
mkdir -p "$work"
src=$work/capsa-llama-cpu-$version
git -C "$upstream" archive --format=tar --prefix="capsa-llama-cpu-$version/" "$pin" |
    tar -C "$work" -xf -
tar -C "$work" -cJf "$work/capsa-llama-cpu_$version.orig.tar.xz" "capsa-llama-cpu-$version"
cp -a "$recipe" "$src/debian"
printf '%s\n' "$pin" > "$src/debian/source-pin"
python3 - "$src/debian/changelog" "$debversion" <<'PY'
import re
import sys

path, version = sys.argv[1:]
text = open(path, encoding="utf-8").read()
updated, count = re.subn(
    r"^([^(]+\()[^)]+(\)\s+\S+\s*;)",
    lambda match: match.group(1) + version + match.group(2),
    text,
    count=1,
)
if count != 1:
    raise SystemExit("cannot replace companion changelog version")
open(path, "w", encoding="utf-8").write(updated)
PY
install -D -m 0755 "$upgrade_guard" "$src/debian/capsa-llama-cpu.preinst"
(cd "$src" && dpkg-buildpackage -us -uc -b ${DPKG_BUILDPACKAGE_OPTS:-})
ls -l "$work"/*.deb "$work"/*.buildinfo
