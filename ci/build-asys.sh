#!/bin/sh
# Build the asys, asys-server and asys-flow-view .debs from the pinned upstream submodule.
# Usage: ci/build-asys.sh [UPSTREAM_DIR] [REV]
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
upstream=$(cd "${1:-$here/upstream/asys}" && pwd)
rev=${2:-HEAD}
version=$(dpkg-parsechangelog -l "$here/pkg/asys/debian/changelog" -S Version | sed 's/-[^-]*$//')
work=$here/build/asys
rm -rf "$work"; mkdir -p "$work"
src=$work/asys-$version
git -C "$upstream" archive --format=tar --prefix="asys-$version/" "$rev" | tar -C "$work" -xf -
# The build ID (upstream tools/build-id: VERSION-commit) is shared with the
# images built by ci/build-images.sh from the same tree; git archive loses the
# commit, so record it in the source.
[ "$rev" = HEAD ] && "$upstream/tools/build-id" > "$src/BUILD-ID" || \
    git -C "$upstream" show "$rev:VERSION" | sed "s/\$/-$(git -C "$upstream" rev-parse --short=7 "$rev")/" > "$src/BUILD-ID"
# Vendor the Go modules (offline build) and the private host Python dependencies
# (ruamel.yaml for the commands; textual & co for asys-human-prompt — noble's
# python3-textual is far too old). All become part of the orig tarball.
(cd "$src/asys-inference" && go mod vendor)
pipv() { PIP_BREAK_SYSTEM_PACKAGES=1 python3 -m pip install --disable-pip-version-check --no-compile --no-deps --quiet --target "$@"; }
pipv "$src/hostdeps" -r "$src/python/requirements-host.txt"
pipv "$src/hostdeps-human" -r "$src/asys-human-interface/requirements-host.txt"
tar -C "$work" -cJf "$work/asys_$version.orig.tar.xz" "asys-$version"
cp -a "$here/pkg/asys/debian" "$src/"
(cd "$src" && dpkg-buildpackage -us -uc -b ${DPKG_BUILDPACKAGE_OPTS:--d})
ls -l "$work"/*.deb
