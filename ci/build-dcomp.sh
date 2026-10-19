#!/bin/sh
# Build the dcomp .deb from the pinned upstream submodule (or another checkout).
# Usage: ci/build-dcomp.sh [UPSTREAM_DIR] [REV]
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
upstream=${1:-$here/upstream/dcomp}
rev=${2:-HEAD}
version=$(dpkg-parsechangelog -l "$here/dcomp/debian/changelog" -S Version | sed 's/-[^-]*$//')
work=$here/build/dcomp
rm -rf "$work"; mkdir -p "$work"
src=$work/dcomp-$version
git -C "$upstream" archive --format=tar --prefix="dcomp-$version/" "$rev" | tar -C "$work" -xf -
# Vendor Go modules so the package builds offline (GOFLAGS=-mod=vendor).
(cd "$src" && go mod vendor)
tar -C "$work" -cJf "$work/dcomp_$version.orig.tar.xz" "dcomp-$version"
cp -a "$here/dcomp/debian" "$src/"
(cd "$src" && dpkg-buildpackage -us -uc -b ${DPKG_BUILDPACKAGE_OPTS:--d})
ls -l "$work"/*.deb
